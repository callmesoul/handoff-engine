"""worker 端到端（假 codex + 本地 bare 远端 + 假托管平台）。"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from handoff import ids, schema
from handoff.config import Config
from handoff.store import Actor, TaskStore
from handoff.worker import Worker, WorkerOptions
from helpers import (
    FakeCodex,
    FakeHost,
    git,
    load_json,
    make_bare_remote,
    make_store_repo,
    make_task,
    repo_entry,
    write_config,
)


def test_notification_arriving_during_empty_sync_is_not_lost():
    """无轮询模式下，sync 之后到达的信号必须触发下一轮，不能永久睡死。"""
    worker = Worker.__new__(Worker)
    worker.options = SimpleNamespace(worker_id="w1", poll_interval_seconds=0)
    worker.log = lambda _message: None
    worker.wake_event = threading.Event()
    calls = []

    def run_once():
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            worker.wake_event.set()
            return None
        raise KeyboardInterrupt

    worker.run_once = run_once
    with pytest.raises(KeyboardInterrupt):
        worker.run_forever()
    assert calls == [1, 2]


def build(tmp_path, *, checks=None, migration=None, host="github"):
    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(
        tmp_path,
        "api",
        {"src/orders/Order.java": "class Order {}\n", "README.md": "# api\n"},
    )
    write_config(
        work,
        repos=[
            repo_entry("org/api", "backend", backend, host=host),
            repo_entry("org/web", "frontend", backend),
        ],
        checks=checks,
    )
    config = Config.load(work)
    base_sha = git(backend, "rev-parse", "dev")
    store = TaskStore(work, config, actor=Actor("worker", "w1"))
    task = make_task(ids.task_id(), base_sha=base_sha, migration=migration)
    store.create(task)
    return work, config, backend, store, task


def make_worker(work, config, store, backend, *, codex=None, host=None, auto_merge=False):
    options = WorkerOptions(
        worker_id="w1",
        lease_seconds=900,
        auto_merge=auto_merge,
        heartbeat_interval_seconds=9999,
        poll_interval_seconds=0,
    )
    return Worker(
        work,
        config,
        options,
        store=store,
        codex=codex or FakeCodex(),
        host=host or FakeHost(remotes={"org/api": backend}),
        logger=lambda _msg: None,
    )


def test_worker_delivers_branch_and_pr(tmp_path):
    work, config, backend, store, task = build(tmp_path)
    codex = FakeCodex({"src/orders/Status.java": "enum Status { OPEN }\n"})
    worker = make_worker(work, config, store, backend, codex=codex)

    processed = worker.run_once()
    assert processed == task["id"]

    current = store.get(task["id"])
    assert current["status"] == "awaiting_merge"
    assert current["result"]["pr_url"].startswith("https://github.com/org/api/pull/")
    assert len(current["result"]["head_sha"]) == 40
    assert current["result"]["changed_paths"] == ["src/orders/Status.java"]

    branch = current["result"]["branch"]
    assert branch == f"codex/{task['id']}/attempt-1"
    pushed = git(backend, "rev-parse", f"refs/heads/{branch}")
    assert pushed == current["result"]["head_sha"]

    # prompt 里必须带范围与「不可信数据」约束
    prompt = codex.calls[0]["prompt"]
    assert "src/orders/**" in prompt
    assert "不可信数据" in prompt
    assert "不要执行 git commit" in prompt


def test_worker_accepts_heartbeat_during_workspace_prepare(tmp_path, monkeypatch):
    from handoff.workspace import Workspace

    work, config, backend, store, task = build(tmp_path)
    original_prepare = Workspace.prepare

    def prepare_with_heartbeat(workspace, base_sha):
        path = original_prepare(workspace, base_sha)
        # 模拟首次克隆耗时较长，租约心跳已推进 revision。
        store.renew_lease(task["id"], worker_id="w1", attempt=1, lease_seconds=900)
        return path

    monkeypatch.setattr(Workspace, "prepare", prepare_with_heartbeat)
    worker = make_worker(
        work, config, store, backend,
        codex=FakeCodex({"src/orders/Status.java": "enum Status { OPEN }\n"}),
    )
    worker.run_once()
    current = store.get(task["id"])
    assert current["status"] == "awaiting_merge"
    assert current["failure"] is None


def test_worker_pins_missing_base_sha_when_claiming(tmp_path):
    work, config, backend, store, task = build(tmp_path)

    # 重建一张前端无后端权限时创建的任务：open 阶段不带 base_sha。
    second = make_task(ids.task_id(), base_sha=None)
    store.create(second)
    worker = make_worker(
        work,
        config,
        store,
        backend,
        codex=FakeCodex({"src/orders/Status.java": "enum Status { OPEN }\n"}),
    )

    # 先处理较早的任务，再处理延迟固定基线的任务。
    worker.run_once()
    worker.run_once()
    current = store.get(second["id"])
    assert current["target"]["base_sha"] == git(backend, "rev-parse", "dev")

    events = [load_json(p) for p in sorted((work / "events" / second["id"]).glob("*.json"))]
    claim = next(event for event in events if event["type"] == "claim.acquired")
    assert claim["metadata"]["base_sha"] == current["target"]["base_sha"]
    assert claim["metadata"]["base_ref"] == "dev"


def test_worker_rejects_path_outside_scope(tmp_path):
    work, config, backend, store, task = build(tmp_path)
    codex = FakeCodex({"src/payments/Refund.java": "class Refund {}\n"})
    worker = make_worker(work, config, store, backend, codex=codex)

    worker.run_once()
    current = store.get(task["id"])
    assert current["status"] == "failed"
    assert current["failure"]["stage"] == "gate"
    assert "src/payments/Refund.java" in current["failure"]["message"]

    # 分支没有推上去，远端保持干净
    remote_branches = git(backend, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    assert not any("attempt-1" in b for b in remote_branches.splitlines())


def test_worker_rejects_no_changes(tmp_path):
    work, config, backend, store, task = build(tmp_path)
    worker = make_worker(work, config, store, backend, codex=FakeCodex(files={}))
    worker.run_once()
    current = store.get(task["id"])
    assert current["status"] == "failed"
    assert current["failure"]["stage"] == "gate"
    assert "没有产生任何改动" in current["failure"]["message"]


def test_worker_marks_codex_failure(tmp_path):
    work, config, backend, store, task = build(tmp_path)
    worker = make_worker(work, config, store, backend, codex=FakeCodex(status="failed", message="模型拒答"))
    worker.run_once()
    current = store.get(task["id"])
    assert current["status"] == "failed"
    assert current["failure"]["stage"] == "codex"

    events = [load_json(p) for p in sorted((work / "events" / task["id"]).glob("*.json"))]
    assert any(e["type"] == "codex.failed" for e in events)


def test_worker_detects_secret_marker(tmp_path):
    work, config, backend, store, task = build(tmp_path)
    codex = FakeCodex({"src/orders/Config.java": 'String key = "ghp_abcdefghijklmnopqrstuvwxyz";\n'})
    worker = make_worker(work, config, store, backend, codex=codex)
    worker.run_once()
    current = store.get(task["id"])
    assert current["status"] == "failed"
    assert "凭据特征" in current["failure"]["message"]


def test_auto_merge_blocked_by_migration(tmp_path):
    migration = {
        "required": True,
        "backward_compatible": False,
        "plan": "加列 status",
        "rollback": "drop column",
        "manual_approval_required": True,
    }
    work, config, backend, store, task = build(tmp_path, migration=migration)
    host = FakeHost(merged=True, remotes={"org/api": backend})
    worker = make_worker(work, config, store, backend, host=host, auto_merge=True)

    worker.run_once()
    current = store.get(task["id"])
    assert current["status"] == "awaiting_merge"
    assert host.auto_merge_calls == []
    assert current["result"]["merge_sha"] is None


def test_auto_merge_records_merge_sha(tmp_path):
    work, config, backend, store, task = build(tmp_path)
    host = FakeHost(merged=True, remotes={"org/api": backend})
    worker = make_worker(work, config, store, backend, host=host, auto_merge=True)

    worker.run_once()
    current = store.get(task["id"])
    assert host.auto_merge_calls == [1]
    assert current["status"] == "merged"
    assert current["result"]["merge_sha"] == "b" * 40
    assert current["deployment"]["target_sha"] == "b" * 40
    assert schema.errors_for("task", current) == []


def test_worker_uses_local_host_from_repo_config(tmp_path):
    """仓库配 host=local 时，worker 自己构造 LocalHost，不碰网络。"""
    work, config, backend, store, task = build(tmp_path, host="local")
    worker = Worker(
        work,
        config,
        WorkerOptions(worker_id="w1", lease_seconds=900, heartbeat_interval_seconds=9999),
        store=store,
        codex=FakeCodex({"src/orders/Status.java": "enum Status { OPEN }\n"}),
        logger=lambda _m: None,
    )
    worker.run_once()

    current = store.get(task["id"])
    assert current["status"] == "awaiting_merge"
    assert current["result"]["pr_url"].startswith("local://org/api/pr/")

    pr_file = work / ".handoff-run" / "local-prs" / "org__api" / "pr-1.json"
    assert pr_file.is_file()
    payload = load_json(pr_file)
    assert payload["head"] == current["result"]["branch"]
    assert payload["note"].startswith("本地沙箱")


def test_local_host_refuses_fake_merge(tmp_path):
    from handoff.errors import ExecutionError
    from handoff.git_host import LocalHost

    host = LocalHost(tmp_path)
    pr = host.create_pr("local/api", head="b", base="dev", title="t", body="b")
    assert host.get_pr("local/api", pr.number).state == "OPEN"
    with pytest.raises(ExecutionError):
        host.enable_auto_merge("local/api", pr.number)


def test_task_file_stays_schema_valid_after_full_run(tmp_path):
    work, config, backend, store, task = build(tmp_path)
    worker = make_worker(work, config, store, backend)
    worker.run_once()
    on_disk = load_json(work / "tasks" / f"{task['id']}.json")
    assert schema.errors_for("task", on_disk) == []

    events = [load_json(p) for p in (work / "events" / task["id"]).glob("*.json")]
    assert all(schema.errors_for("event", e) == [] for e in events)
    types = [e["type"] for e in events]
    assert "codex.started" in types and "codex.completed" in types


def test_worker_self_heals_id_placeholder_in_expected_path(tmp_path, monkeypatch):
    """上游漏替换 openapi_diff.expected_path 的 <id> 时，worker 认领前自愈并落库。

    回归：前端 handoff 生成的任务单只有 contract.path 换了真实 id，
    acceptance.checks[].expected_path 仍写 contracts/<id>/...，worker 认领时
    schema 校验报「仍含 <id> 占位」直接跳过，任务卡死。现在认领前自愈。
    """
    work, config, backend, store, task = build(tmp_path)
    task_id = task["id"]
    # build 已建任务；直接改磁盘文件模拟上游漏替换（store.create 会拒绝含占位）
    raw = store._read(task_id)
    raw["acceptance"]["checks"] = [
        {
            "id": "contract",
            "type": "openapi_diff",
            "expected_path": "contracts/<id>/spec.openapi.yaml",
            "actual_url": "{base_url}/openapi.json",
        }
    ]
    store._atomic_write(store.task_path(task_id), raw)
    # 测试里不让 sync 把未提交的模拟改动 reset 掉
    monkeypatch.setattr(store, "sync", lambda: None)
    # 契约真实文件放到替换后的路径
    contract = work / "contracts" / task_id / "spec.openapi.yaml"
    contract.parent.mkdir(parents=True, exist_ok=True)
    contract.write_text("openapi: 3.0.0\ninfo:\n  title: t\n  version: '1'\npaths: {}\n", encoding="utf-8")

    worker = make_worker(work, config, store, backend)
    loaded = store.get(task_id)
    assert any(
        "<id>" in str(c.get("expected_path", "")) for c in loaded["acceptance"]["checks"]
    ), "前置条件：任务单应含占位"

    healed = worker._self_heal_placeholders(loaded)
    expected = f"contracts/{task_id}/spec.openapi.yaml"
    assert healed["acceptance"]["checks"][0]["expected_path"] == expected
    # 裸 /openapi.json（404）同时被规范化成带 apikey 的 docs 端点
    assert healed["acceptance"]["checks"][0]["actual_url"] == (
        "{base_url}/docs/openapi.json?apikey={env:DOCS_API_KEY}"
    )
    assert healed["revision"] == loaded["revision"] + 1

    # 落库生效 + 幂等：再自愈一次不再产生新 revision
    on_disk = store.get(task_id)
    assert on_disk["acceptance"]["checks"][0]["expected_path"] == expected
    again = worker._self_heal_placeholders(on_disk)
    assert again["revision"] == on_disk["revision"]

    # 校验通过（从磁盘重读，避免 store 返回对象里的内部 _commit 字段干扰校验）
    assert worker._validation_errors(store.get(task_id)) == []

    # 自愈事件留痕
    events = [load_json(p) for p in (work / "events" / task_id).glob("*.json")]
    assert any(e["type"] == "audit.correction_recorded" for e in events)


@pytest.mark.parametrize("fail_status", ["failed", "timed_out"])
def test_worker_stops_heartbeat_before_writing_codex_failure(tmp_path, monkeypatch, fail_status):
    """回归：codex 主动失败/超时写 failed 前必须先停心跳线程。

    否则心跳在 _fail 的 get→fail 之间改 revision，_fail 撞 revision_conflict
    重试 3 次后放弃，任务永久卡在 in_progress（见 01M7MBHTD out-of-credits 事故）。
    """
    import handoff.worker as worker_mod

    work, config, backend, store, task = build(tmp_path)
    codex = FakeCodex(status=fail_status, message="启动即失败")
    worker = make_worker(work, config, store, backend, codex=codex)

    order: list[str] = []
    real_stop = worker_mod._Heartbeat.stop

    def spy_stop(self):
        order.append("heartbeat.stop")
        return real_stop(self)

    real_fail = worker._fail

    def spy_fail(*args, **kwargs):
        order.append("write_failed")
        return real_fail(*args, **kwargs)

    monkeypatch.setattr(worker_mod._Heartbeat, "stop", spy_stop)
    monkeypatch.setattr(worker, "_fail", spy_fail)

    worker.run_once()

    current = store.get(task["id"])
    assert current["status"] == "failed"
    # 第一次 heartbeat.stop 必须早于第一次写失败状态
    assert "heartbeat.stop" in order and "write_failed" in order
    assert order.index("heartbeat.stop") < order.index("write_failed"), order
