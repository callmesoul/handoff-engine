"""任务存储层：CAS、租约、事件同 commit、并发认领。"""

from __future__ import annotations

import json
import time

import pytest

from handoff import ids, notification, schema
from handoff.config import Config
from handoff.errors import ConflictError, LeaseError, SchemaError, TaskNotFound, TransitionError
from handoff.gitcmd import Result, push_rejected
from handoff.store import Actor, TaskStore
from helpers import git, load_json, make_bare_remote, make_store_repo, make_task, repo_entry, write_config


def build(tmp_path):
    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api", {"src/orders/Order.java": "class Order {}\n"})
    write_config(
        work,
        repos=[repo_entry("org/api", "backend", backend), repo_entry("org/web", "frontend", backend)],
    )
    config = Config.load(work)
    base_sha = git(backend, "rev-parse", "dev")
    return work, config, backend, base_sha


def test_create_writes_task_and_event_in_same_commit(tmp_path):
    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config, actor=Actor("user", "alice"))
    task = make_task(ids.task_id(), base_sha=base_sha)

    created = store.create(task)
    assert created["revision"] == 1
    assert created["status"] == "open"

    on_disk = load_json(work / "tasks" / f"{task['id']}.json")
    assert on_disk["id"] == task["id"]
    assert schema.errors_for("task", on_disk) == []

    files = git(work, "show", "--name-only", "--pretty=format:", "HEAD").splitlines()
    assert f"tasks/{task['id']}.json" in files
    assert any(f.startswith(f"events/{task['id']}/") for f in files)

    events = list((work / "events" / task["id"]).glob("*.json"))
    assert len(events) == 1
    assert load_json(events[0])["type"] == "task.created"


def test_create_runs_required_precondition_before_writing_task(tmp_path):
    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config, actor=Actor("user", "alice"))
    task = make_task(ids.task_id(), base_sha=base_sha)
    called = []

    def reject(prepared):
        called.append(prepared["id"])
        assert not store.task_path(prepared["id"]).exists()
        raise SchemaError("前端会话登记失败")

    with pytest.raises(SchemaError, match="前端会话登记失败"):
        store.create(task, before_write=reject)

    assert called == [task["id"]]
    assert not store.task_path(task["id"]).exists()
    assert not (work / "events" / task["id"]).exists()


def test_reverify_verification_failure_preserves_attempt_and_merge(tmp_path):
    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config, actor=Actor("user", "alice"))
    task = make_task(ids.task_id(), base_sha=base_sha)
    target = "b" * 40
    observed = "c" * 40
    task["attempt"] = 5
    task["status"] = "failed"
    task["result"]["merge_sha"] = target
    task["result"]["checks"] = [
        {
            "id": "smoke",
            "status": "failed",
            "started_at": "2026-11-05T00:00:00Z",
            "output": "bad contract",
            "observed_sha": observed,
        }
    ]
    task["deployment"].update(
        {
            "status": "timeout",
            "target_sha": target,
            "observed_sha": observed,
            "updated_at": "2026-11-05T00:00:00Z",
            "error": "old timeout",
        }
    )
    task["failure"] = {
        "stage": "verification",
        "message": "acceptance typo",
        "check_id": "smoke",
        "observed_sha": observed,
        "at": "2026-11-05T00:00:00Z",
    }
    created = store.create(task)

    updated = store.request_reverify(
        task["id"],
        expected_revision=created["revision"],
        reason="fix acceptance only",
    )

    assert updated["status"] == "deployed"
    assert updated["attempt"] == 5
    assert updated["result"]["merge_sha"] == target
    assert updated["result"]["checks"] == []
    assert updated["failure"] is None
    assert updated["deployment"]["status"] == "deployed"
    assert updated["deployment"]["error"] is None
    assert schema.errors_for("task", load_json(work / "tasks" / f"{task['id']}.json")) == []


def test_claim_increments_attempt_and_sets_lease(tmp_path):
    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config, actor=Actor("worker", "w1"))
    task = make_task(ids.task_id(), base_sha=base_sha)
    store.create(task)

    claimed = store.claim(task["id"], worker_id="w1", lease_seconds=600)
    assert claimed["status"] == "claimed"
    assert claimed["attempt"] == 1
    assert claimed["claim"]["worker_id"] == "w1"

    with pytest.raises(LeaseError):
        store.claim(task["id"], worker_id="w2", lease_seconds=600)


def test_stale_revision_is_rejected(tmp_path):
    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config, actor=Actor("worker", "w1"))
    task = make_task(ids.task_id(), base_sha=base_sha)
    store.create(task)

    store.claim(task["id"], worker_id="w1", lease_seconds=600)
    with pytest.raises(ConflictError):
        store.transition(task["id"], "in_progress", expected_revision=1, summary="基于过期 revision 的写入")


def test_transition_whitelist_enforced(tmp_path):
    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config)
    task = make_task(ids.task_id(), base_sha=base_sha)
    store.create(task)

    with pytest.raises(TransitionError):
        store.transition(task["id"], "merged", expected_revision=1, summary="跳过中间状态")


def test_failed_task_requires_failure_block(tmp_path):
    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config)
    task = make_task(ids.task_id(), base_sha=base_sha)
    store.create(task)

    with pytest.raises(SchemaError):
        store.transition(task["id"], "failed", expected_revision=1, summary="直接失败")

    failed = store.fail(task["id"], stage="codex", message="模型超时")
    assert failed["status"] == "failed"
    assert failed["failure"]["stage"] == "codex"


def test_terminal_transition_emits_completion_with_landed_commit(tmp_path, monkeypatch):
    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config)
    task = make_task(ids.task_id(), base_sha=base_sha)
    store.create(task)
    calls = []

    def emit(root, notify_config, current, commit):
        assert git(work, "rev-parse", "origin/main") == commit
        calls.append((root, notify_config, current["status"], commit))
        return {"status": "sent", "event_id": "event"}

    monkeypatch.setattr(notification, "emit_task_completed", emit)
    failed = store.fail(task["id"], stage="codex", message="模型超时")

    assert failed["_completion_notification"]["status"] == "sent"
    assert calls == [(work.resolve(), config.notifications, "failed", failed["_commit"])]


def test_terminal_transition_emits_completion_even_without_push(tmp_path, monkeypatch):
    """`--no-push` 只关 git push，绝不能顺带关掉回程完成通知。

    09c 事故：push_enabled 被当成发通知的前置条件，CLI 用 push=False 跑完验收，
    任务进了 verified 却一条 task.completed 都没入队，前端一直等不到。
    """
    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config, push=False)
    task = make_task(ids.task_id(), base_sha=base_sha)
    store.create(task)
    calls = []

    def emit(root, notify_config, current, commit):
        calls.append((current["id"], current["status"], commit))
        return {"status": "pending", "event_id": "event"}

    monkeypatch.setattr(notification, "emit_task_completed", emit)
    failed = store.fail(task["id"], stage="codex", message="模型超时")

    assert failed["_completion_notification"]["status"] == "pending"
    assert [c[1] for c in calls] == ["failed"]
    # 没推送也要能定位落盘 commit（从交接仓 git 历史反查），否则补发会拼不出正文
    assert calls[0][2]


def test_terminal_transition_logs_when_notification_cannot_enqueue(tmp_path, monkeypatch, caplog):
    """通知入队失败不能静默：必须留一条 warning 供巡检/人工发现。"""
    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config)
    task = make_task(ids.task_id(), base_sha=base_sha)
    store.create(task)

    def emit(root, notify_config, current, commit):
        return {"status": "disabled", "reason": "no_completion_url"}

    monkeypatch.setattr(notification, "emit_task_completed", emit)
    with caplog.at_level("WARNING", logger="handoff.store"):
        store.fail(task["id"], stage="codex", message="模型超时")

    assert any("完成通知入队失败" in r.getMessage() for r in caplog.records)


def test_accept_marks_frontend_accepted_as_terminal_record(tmp_path, monkeypatch):
    """前端验收成功 → verified 转 frontend_accepted，写审计事件但不重发完成通知。"""
    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config, actor=Actor("user", "alice"))
    task = make_task(ids.task_id(), base_sha=base_sha)
    target = "b" * 40
    task["status"] = "verified"
    task["result"]["merge_sha"] = target
    task["deployment"].update(
        {
            "status": "deployed",
            "target_sha": target,
            "observed_sha": "c" * 40,
            "updated_at": "2026-11-05T00:00:00Z",
        }
    )
    created = store.create(task)

    emitted = []

    def emit(root, notify_config, current, commit):
        emitted.append(current["status"])
        return {"status": "sent", "event_id": "event"}

    monkeypatch.setattr(notification, "emit_task_completed", emit)

    updated = store.transition(
        task["id"],
        "frontend_accepted",
        expected_revision=created["revision"],
        summary="前端验收通过",
        actor=Actor("user", "frontend"),
        event_type="frontend.accepted",
    )

    assert updated["status"] == "frontend_accepted"
    # 只是终态记录，不向后端重发完成通知
    assert emitted == []
    events = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((work / "events" / task["id"]).glob("*.json"))
    ]
    assert "frontend.accepted" in {e["type"] for e in events}
    assert schema.errors_for("task", load_json(work / "tasks" / f"{task['id']}.json")) == []


def test_expired_lease_can_be_taken_over(tmp_path):
    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config, actor=Actor("worker", "w1"))
    task = make_task(ids.task_id(), base_sha=base_sha)
    store.create(task)

    store.claim(task["id"], worker_id="w1", lease_seconds=1)
    time.sleep(1.05)
    assert not store.still_owner(task["id"], worker_id="w1", attempt=1)

    second = store.claim(task["id"], worker_id="w2", lease_seconds=600)
    assert second["claim"]["worker_id"] == "w2"
    assert second["attempt"] == 2
    assert second["status"] == "claimed"


def test_claim_requires_worker_to_pin_missing_base_sha(tmp_path):
    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config, actor=Actor("worker", "w1"))
    task = make_task(ids.task_id(), base_sha=None)
    store.create(task)

    with pytest.raises(SchemaError, match="固定 target.base_sha"):
        store.claim(task["id"], worker_id="w1")

    claimed = store.claim(task["id"], worker_id="w1", base_sha=base_sha)
    assert claimed["target"]["base_sha"] == base_sha


def test_expire_stale_requeues_and_keeps_history(tmp_path):
    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config, actor=Actor("worker", "w1"))
    task = make_task(ids.task_id(), base_sha=base_sha)
    store.create(task)
    store.claim(task["id"], worker_id="w1", lease_seconds=1)
    time.sleep(1.05)

    expired = store.expire_stale()
    assert expired == [task["id"]]
    current = store.get(task["id"])
    assert current["status"] == "open"
    assert current["claim"] is None

    events = [load_json(p) for p in sorted((work / "events" / task["id"]).glob("*.json"))]
    assert any(e["type"] == "claim.expired" for e in events)


def test_release_lease_puts_task_back_to_open(tmp_path):
    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config)
    task = make_task(ids.task_id(), base_sha=base_sha)
    store.create(task)
    store.claim(task["id"], worker_id="w1", lease_seconds=600)

    released = store.release_lease(task["id"], actor=Actor("user", "alice"), reason="换机器")
    assert released["status"] == "open"
    assert released["claim"] is None


def test_events_are_append_only_and_ordered(tmp_path):
    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config)
    task = make_task(ids.task_id(), base_sha=base_sha)
    store.create(task)
    store.claim(task["id"], worker_id="w1", lease_seconds=600)
    store.fail(task["id"], stage="codex", message="失败")

    events = [load_json(p) for p in sorted((work / "events" / task["id"]).glob("*.json"))]
    assert [e["type"] for e in events] == ["task.created", "claim.acquired", "task.status_changed"]
    assert [e["task_revision"] for e in events] == sorted(e["task_revision"] for e in events)
    assert all(schema.errors_for("event", e) == [] for e in events)


def test_semantic_validation_rejects_unregistered_repo(tmp_path):
    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config)
    task = make_task(ids.task_id(), base_sha=base_sha, target_repo="org/unknown")
    with pytest.raises(SchemaError):
        store.create(task)


def test_semantic_validation_rejects_disallowed_base_ref(tmp_path):
    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config)
    task = make_task(ids.task_id(), base_sha=base_sha)
    task["target"]["base_ref"] = "main"
    with pytest.raises(SchemaError):
        store.create(task)


def test_semantic_validation_rejects_unresolved_id_placeholder(tmp_path):
    """expected_path 残留 `<id>` 占位 → 拒绝落库（create 应已替换）。"""
    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config)
    task = make_task(ids.task_id(), base_sha=base_sha)
    task["acceptance"]["checks"] = [
        {
            "id": "contract",
            "type": "openapi_diff",
            "expected_path": "contracts/<id>/openapi.yaml",
            "actual_url": "{base_url}/docs/openapi.json",
        }
    ]
    with pytest.raises(SchemaError) as exc:
        store.create(task)
    assert any("<id>" in e for e in exc.value.errors)


def test_semantic_validation_rejects_missing_expected_contract(tmp_path):
    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config)
    task = make_task(ids.task_id(), base_sha=base_sha)
    task["acceptance"]["checks"] = [
        {
            "id": "contract",
            "type": "openapi_diff",
            "expected_path": f"contracts/{task['id']}/nope.yaml",
            "actual_url": "{base_url}/docs/openapi.json",
        }
    ]
    with pytest.raises(SchemaError) as exc:
        store.create(task)
    assert any("文件不存在" in e for e in exc.value.errors)


def test_state_file_never_leaks_runtime_commit_key(tmp_path):
    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config)
    task = make_task(ids.task_id(), base_sha=base_sha)
    created = store.create(task)
    assert created.get("_commit")
    on_disk = json.loads((work / "tasks" / f"{task['id']}.json").read_text(encoding="utf-8"))
    assert "_commit" not in on_disk


def test_delete_tasks_removes_all_associated_git_data(tmp_path):
    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config)
    first = make_task(ids.task_id(), base_sha=base_sha)
    second = make_task(ids.task_id(), base_sha=base_sha)
    store.create(first)
    store.create(second)

    contract = store.contract_dir(first["id"]) / "contract.md"
    artifact = store.artifact_dir(first["id"]) / "failure.log"
    contract.parent.mkdir(parents=True)
    artifact.parent.mkdir(parents=True)
    contract.write_text("contract", encoding="utf-8")
    artifact.write_text("failure", encoding="utf-8")
    lease = store.lease_path(first["id"])
    lease.parent.mkdir(parents=True, exist_ok=True)
    lease.write_text("{}", encoding="utf-8")
    git(work, "add", "contracts", "artifacts")
    git(work, "commit", "-m", "add task materials")
    git(work, "push", "origin", "main")

    result = store.delete_tasks(delete_all=True)

    assert result["deleted"] == sorted([first["id"], second["id"]])
    assert result["count"] == 2
    assert result["store_commit"] == git(work, "rev-parse", "HEAD")
    assert list((work / "tasks").glob("*.json")) == []
    for task in (first, second):
        assert not store.event_dir(task["id"]).exists()
        assert not store.contract_dir(task["id"]).exists()
        assert not store.artifact_dir(task["id"]).exists()
    assert not lease.exists()
    assert git(work, "rev-parse", "HEAD") == git(work, "rev-parse", "origin/main")


def test_delete_tasks_rejects_missing_and_path_like_ids(tmp_path):
    work, config, _backend, _base_sha = build(tmp_path)
    store = TaskStore(work, config)
    with pytest.raises(SchemaError, match="格式非法"):
        store.delete_tasks(["../../tasks/escape"])
    with pytest.raises(TaskNotFound):
        store.delete_tasks([ids.task_id()])


def test_push_rejection_detection():
    assert push_rejected(
        Result(argv=["git", "push"], returncode=1, stdout="", stderr="! [rejected] main -> main (non-fast-forward)")
    )
    assert not push_rejected(
        Result(argv=["git", "push"], returncode=1, stdout="", stderr="fatal: unable to access remote")
    )


def test_sync_waits_for_pending_commit(tmp_path, monkeypatch):
    import threading

    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config)
    task = make_task(ids.task_id(), base_sha=base_sha)
    store.create(task)
    reader_ready = threading.Event()
    reader_synced = threading.Event()
    errors = []
    original_sync = store.sync
    original_commit = store._commit

    def tracked_sync():
        if threading.current_thread().name == "reader":
            reader_synced.set()
        return original_sync()

    def read_task():
        reader_ready.set()
        try:
            store.get(task["id"])
        except Exception as exc:
            errors.append(exc)

    reader = threading.Thread(target=read_task, name="reader")

    def guarded_commit(message):
        # 模拟状态写盘后、提交前，心跳线程尝试同步同一 checkout。
        reader.start()
        assert reader_ready.wait(2)
        assert not reader_synced.wait(0.2)
        return original_commit(message)

    monkeypatch.setattr(store, "sync", tracked_sync)
    monkeypatch.setattr(store, "_commit", guarded_commit)
    store.update(task["id"], expected_revision=1,
                 mutate=lambda t: t.update(title="提交期间不允许心跳重置"),
                 event_type="audit.correction_recorded", summary="校验同一仓库事务串行")
    reader.join(timeout=30)
    assert not reader.is_alive()
    assert not errors
    assert reader_synced.is_set()
    assert store.get(task["id"])["title"] == "提交期间不允许心跳重置"


def test_scan_skips_residual_atomic_write_temp_files(tmp_path):
    """_atomic_write 被硬杀会残留 .tmp-*.json；它带着旧快照，不能被当成第二个任务。"""
    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config, actor=Actor("user", "alice"))
    task = make_task(ids.task_id(), base_sha=base_sha)
    store.create(task)

    # 模拟进程在 os.replace 之前被硬杀：tasks/ 下留下同 id 的旧快照
    stale = dict(load_json(work / "tasks" / f"{task['id']}.json"))
    stale["status"] = "failed"
    stale["revision"] = 6
    (work / "tasks" / ".tmp-evepz87m.json").write_text(
        json.dumps(stale, ensure_ascii=False), encoding="utf-8"
    )

    tasks = store.peek_tasks()
    assert [t["id"] for t in tasks].count(task["id"]) == 1
    assert len(tasks) == 1


def test_commit_never_stages_atomic_write_temp_files(tmp_path):
    """git add -A 扫目录时不能把 .tmp-* 临时文件带进版本库（曾误提交旧快照）。"""
    work, config, _backend, base_sha = build(tmp_path)
    store = TaskStore(work, config, actor=Actor("user", "alice"))
    task = make_task(ids.task_id(), base_sha=base_sha)
    store.create(task)

    # 模拟 _atomic_write 中途被硬杀，tasks/ 下残留一个临时文件
    (work / "tasks" / ".tmp-deadbeef.json").write_text("{}", encoding="utf-8")

    store._commit("test: 状态变更")
    tracked = git(work, "ls-tree", "-r", "--name-only", "HEAD")
    assert ".tmp-deadbeef.json" not in tracked
    # 临时文件本身还在工作区（不该被提交，也不该被这次 commit 顺手删掉）
    assert (work / "tasks" / ".tmp-deadbeef.json").exists()
