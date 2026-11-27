"""面板 API 端到端：假执行器 + 本地 bare 远端 + 真实 HTTP。"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from handoff import ids
from handoff.config import Config
from handoff.errors import HandoffError
from handoff.deploy import CommitAncestry
from handoff.panel.server import PanelServer
from handoff.panel.service import PanelService
from handoff.store import Actor, TaskStore
from helpers import FakeHost, git, make_bare_remote, make_store_repo, make_task, repo_entry, write_config


class FakeZcode:
    """假 zcode 执行器：按脚本写文件，可阻塞以测取消。"""

    name = "zcode"

    def __init__(self, files=None, *, block_event=None):
        from handoff.codex_runner import CodexOutcome

        self._Outcome = CodexOutcome
        self.files = files if files is not None else {"src/orders/Status.java": "enum Status { OPEN }\n"}
        self.block_event = block_event
        self.calls: list[dict] = []
        self.resume_ids: list = []

    def version(self):
        return "zcode 0.0.0-fake"

    def run(self, worktree, prompt, *, task_id, attempt, cancel_event=None):
        from handoff.codex_runner import CodexOutcome

        self.calls.append({"task_id": task_id, "attempt": attempt, "prompt": prompt})
        if self.block_event is not None:
            # 模拟长任务：等外部取消或放行信号
            while not (cancel_event is not None and cancel_event.is_set()) and not self.block_event.is_set():
                time.sleep(0.02)
            if cancel_event is not None and cancel_event.is_set():
                return CodexOutcome(status="failed", result=None, message="被取消", run_id="run-x")
        for rel, content in self.files.items():
            path = Path(worktree) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        result = {
            "status": "completed",
            "summary": "zcode 完成",
            "changed_paths": list(self.files),
            "tests": [{"name": "unit", "status": "passed", "output": ""}],
            "migration_required": False,
            "risks": [],
            "notes": "",
        }
        return CodexOutcome(
            status="completed",
            result=result,
            message="zcode 完成",
            run_id="sess_fake_zcode",
            thread_id="sess_fake_zcode",
            cli_version=self.version(),
        )


@pytest.fixture()
def env(tmp_path, monkeypatch):
    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(
        tmp_path,
        "api",
        {"src/orders/Order.java": "class Order {}\n", "README.md": "# api\n"},
    )
    # 前端来源项目必须有本地路径，面板发单才能读到真实 HEAD（不能伪造全零 SHA）。
    checks = {
        "test_suites": {},
        "repo_scripts": {},
        "gate": {
            "required_checks": [],
            "high_risk_paths": [".github/**", "infra/prod/**", "**/migrations/**"],
            "deny_auto_merge_if_migration_required": True,
            "max_changed_files": 50,
            "max_file_bytes": 65536,
        },
        "verifier": {
            "output_limit_chars": 2000,
            "max_parallel": 1,
            "repo_paths": {"org/web": str(backend)},
        },
    }
    write_config(
        work,
        repos=[
            repo_entry("org/api", "backend", backend),
            repo_entry("org/web", "frontend", backend),
        ],
        checks=checks,
    )
    config = Config.load(work)
    base_sha = git(backend, "rev-parse", "dev")
    store = TaskStore(work, config, actor=Actor("worker", "w1"))
    task = make_task(ids.task_id(), base_sha=base_sha)
    store.create(task)

    fake_zcode = FakeZcode()
    service = PanelService(
        work,
        config,
        runner_overrides={"zcode": fake_zcode, "codex": _codex_factory()},
        host=FakeHost(remotes={"org/api": backend}),
    )
    # 不碰真实 8788 端口
    monkeypatch.setattr(service, "_start_wake_server", lambda: None)
    server = PanelServer(service, host="127.0.0.1", port=0)
    host, port = server.start()
    base_url = f"http://{host}:{port}"

    yield Simple(host=host, port=port, base_url=base_url, service=service, server=server,
                store=service._shared_store, work=work, config=config, backend=backend, task=task, fake_zcode=fake_zcode)
    service.shutdown()
    server.stop()


@pytest.fixture()
def frontend_env(env):
    """Expose the same fake store through a real frontend-role service and server."""
    service = PanelService(
        env.work,
        env.config,
        runner_overrides=env.service._runner_overrides,
        host=env.service._host,
        role="frontend",
    )
    server = PanelServer(service, host="127.0.0.1", port=0)
    host, port = server.start()
    try:
        yield Simple(base_url=f"http://{host}:{port}", service=service, server=server)
    finally:
        service.shutdown()
        server.stop()


class Simple:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _codex_factory():
    from helpers import FakeCodex

    def factory(resume_session_id=None, permission_mode=None):
        runner = FakeCodex({"src/orders/CodexNote.java": "// by codex\n"})
        runner.name = "codex"
        runner.resume_thread = resume_session_id
        runner.version = lambda: "codex 0.0.0-fake"
        return runner

    return factory


def _get(url):
    with urllib.request.urlopen(url, timeout=30) as res:
        return json.loads(res.read().decode("utf-8"))


def _get_text(url):
    with urllib.request.urlopen(url, timeout=30) as res:
        return res.status, res.read().decode("utf-8")


def _get_error(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as res:
            return res.status, json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _post(url, payload=None):
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _wait_run_done(service, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        active = service.state().get("active")
        if active is None or active["phase"] == "finished":
            return active
        time.sleep(0.1)
    active = service.state().get("active") or {}
    raise AssertionError(
        "执行超时未结束: error=%r logs=%s" % (active.get("error"), active.get("logs", [])[-8:])
    )


# -- 查询 -------------------------------------------------------------------


def test_state_lists_health_and_manual_mode(env):
    state = _get(f"{env.base_url}/api/state")
    assert state["role"] == "backend"
    assert state["mode"] == "manual"
    assert state["executors"]["zcode"]["version"] == "zcode 0.0.0-fake"
    assert state["executors"]["codex"]["ok"] is True


def test_tasks_endpoint_returns_brief(env):
    data = _get(f"{env.base_url}/api/tasks")
    assert data["count"] == 1
    brief = data["tasks"][0]
    assert brief["id"] == env.task["id"]
    assert brief["status"] == "open"
    assert "requirement" not in brief


def test_tasks_brief_shows_frontend_repo_and_without_session(env):
    """前端项目来自任务单 source；没登记前端会话时该列为空，不能报错。"""
    brief = _get(f"{env.base_url}/api/tasks")["tasks"][0]
    assert brief["frontend_repo"] == "org/web"
    assert brief["frontend_executor"] is None


def test_tasks_brief_shows_frontend_executor_from_session(env):
    """前端执行 Agent 取本机登记的会话（codex/dsh），不是后端执行器。"""
    from handoff.frontend_session import FrontendSessions

    FrontendSessions(env.config.notifications.session_path(env.work)).register(
        env.task["id"], thread_id="frontend-thread", workspace=env.work, executor="dsh"
    )
    env.service._frontend_cache = None  # 丢掉 3 秒缓存，立刻看到新登记

    brief = _get(f"{env.base_url}/api/tasks")["tasks"][0]
    assert brief["frontend_executor"] == "dsh"
    assert brief["frontend_repo"] == "org/web"
    # 后端执行器仍是自己那一列，没被前端会话覆盖
    assert brief["executor"] is None

    detail = _get(f"{env.base_url}/api/tasks/{env.task['id']}")
    assert detail["frontend"]["executor"] == "dsh"
    assert detail["frontend"]["thread_id"] == "frontend-thread"


def test_task_detail_includes_events(env):
    data = _get(f"{env.base_url}/api/tasks/{env.task['id']}")
    assert data["task"]["id"] == env.task["id"]
    assert isinstance(data["events"], list) and data["events"]


def test_sessions_endpoint(env, monkeypatch):
    from handoff.panel import sessions as sess

    monkeypatch.setattr(sess, "zcode_sessions", lambda limit=50: [])
    monkeypatch.setattr(sess, "codex_sessions", lambda limit=50: [])
    data = _get(f"{env.base_url}/api/tasks/{env.task['id']}/sessions?executor=zcode")
    assert data == {"executor": "zcode", "sessions": []}


# -- 前端专用面板 -----------------------------------------------------------


def test_frontend_panel_serves_dedicated_page_and_minimal_state(frontend_env):
    status, html = _get_text(f"{frontend_env.base_url}/")
    assert status == 200
    assert "前端任务面板" in html
    for backend_control in ("全自动", "开始执行", "释放租约", "恢复推进", "执行日志"):
        assert backend_control not in html

    state = _get(f"{frontend_env.base_url}/api/state")
    assert state["role"] == "frontend"
    assert "projects" in state and "environments" in state
    for backend_field in ("mode", "auto", "active", "advance", "executors", "executor_options"):
        assert backend_field not in state


def test_frontend_panel_rejects_backend_routes(frontend_env, env):
    denied_posts = (
        "/api/mode",
        "/api/runs/cancel",
        "/api/runs/steer",
        "/api/runs/security",
        f"/api/tasks/{env.task['id']}/run",
        f"/api/tasks/{env.task['id']}/retry",
        f"/api/tasks/{env.task['id']}/release",
        f"/api/tasks/{env.task['id']}/resend-notify",
        f"/api/tasks/{env.task['id']}/advance",
    )
    for path in denied_posts:
        status, body = _post(frontend_env.base_url + path, {})
        assert status == 403, path
        assert body["code"] == "panel_role_forbidden"

    denied_gets = (
        f"/api/tasks/{env.task['id']}/sessions",
        "/api/sessions/codex",
        f"/api/logs?task={env.task['id']}",
    )
    for path in denied_gets:
        status, body = _get_error(frontend_env.base_url + path)
        assert status == 403, path
        assert body["code"] == "panel_role_forbidden"


def test_frontend_panel_allows_frontend_mutations(frontend_env, env, monkeypatch):
    service = frontend_env.service
    monkeypatch.setattr(service, "sync", lambda: {"added": 0, "total": 1, "ids": []})
    monkeypatch.setattr(service, "create_task", lambda payload: {"id": "ho-frontend"})
    monkeypatch.setattr(service, "deliver", lambda task_id: {"task_id": task_id, "started": True})
    monkeypatch.setattr(service, "accept", lambda task_id, reason: {"id": task_id, "reason": reason})

    assert _post(f"{frontend_env.base_url}/api/sync")[0] == 200
    assert _post(f"{frontend_env.base_url}/api/tasks", {"title": "x"})[0] == 200
    assert _post(f"{frontend_env.base_url}/api/deliver", {"task_id": env.task["id"]})[0] == 200
    assert _post(f"{frontend_env.base_url}/api/tasks/{env.task['id']}/accept", {"reason": "ok"})[0] == 200


def test_frontend_service_rejects_backend_operations_directly(frontend_env, env):
    """服务层是公共 API：即使绕过 HTTP allow-list，也不能借 frontend 服务跑后端操作。"""
    service = frontend_env.service
    task_id = env.task["id"]
    calls = (
        (service.set_mode, ({"mode": "auto"},)),
        (service.set_security_mode, ("danger-full-access",)),
        (service.cancel, ()),
        (service.steer, ("继续",)),
        (service.run_task, (task_id,), {"executor": "codex"}),
        (service.retry, (task_id,)),
        (service.release, (task_id,)),
        (service.resend_completed, (task_id,)),
        (service.advance, (task_id,)),
    )
    for call in calls:
        fn = call[0]
        args = call[1]
        kwargs = call[2] if len(call) > 2 else {}
        with pytest.raises(HandoffError, match="前端面板不支持后端操作"):
            fn(*args, **kwargs)


# -- 手动执行 ---------------------------------------------------------------


def test_manual_run_with_zcode_to_awaiting_merge(env):
    status, body = _post(
        f"{env.base_url}/api/tasks/{env.task['id']}/run",
        {"executor": "zcode", "auto_merge": False},
    )
    assert status == 200 and body["ok"] is True
    _wait_run_done(env.service)

    current = env.store.get(env.task["id"])
    assert current["status"] == "awaiting_merge"
    assert current["result"]["executor"] == "zcode"
    assert current["result"]["executor_session_id"] == "sess_fake_zcode"
    branch = current["result"]["branch"]
    assert branch == f"zcode/{env.task['id']}/attempt-1"
    # 分支真的推到了远端
    assert git(env.backend, "rev-parse", f"refs/heads/{branch}") == current["result"]["head_sha"]

    active = env.service.state()["active"]
    assert active["phase"] == "finished"
    assert active["error"] is None


def test_concurrent_run_rejected(env):
    gate = threading.Event()
    slow = FakeZcode(block_event=gate)
    env.service._runner_overrides["zcode"] = slow
    _post(f"{env.base_url}/api/tasks/{env.task['id']}/run", {"executor": "zcode", "auto_merge": False})
    try:
        status, body = _post(f"{env.base_url}/api/tasks/{env.task['id']}/run", {"executor": "codex"})
        assert status == 409
        assert "已有任务在执行" in body.get("message", body.get("error", ""))
    finally:
        gate.set()
        _wait_run_done(env.service)


def test_cancel_kills_run_and_marks_failed_then_retry(env):
    gate = threading.Event()
    slow = FakeZcode(block_event=gate)
    env.service._runner_overrides["zcode"] = slow
    _post(f"{env.base_url}/api/tasks/{env.task['id']}/run", {"executor": "zcode", "auto_merge": False})
    time.sleep(0.3)
    status, _ = _post(f"{env.base_url}/api/runs/cancel", {})
    assert status == 200
    gate.set()  # 假执行器看到 cancel_event 后自行退出
    _wait_run_done(env.service)

    current = env.store.get(env.task["id"])
    assert current["status"] == "failed"

    # 面板重试：failed → open
    status, _ = _post(f"{env.base_url}/api/tasks/{env.task['id']}/retry", {})
    assert status == 200
    assert env.store.get(env.task["id"])["status"] == "open"


def test_resume_session_is_passed_to_runner(env):
    calls = []

    def factory(resume_session_id=None, permission_mode=None):
        calls.append({"resume": resume_session_id, "permission": permission_mode})
        return FakeZcode()

    env.service._runner_overrides["zcode"] = factory
    _post(
        f"{env.base_url}/api/tasks/{env.task['id']}/run",
        {"executor": "zcode", "resume_session_id": "sess_prev", "permission_mode": "yolo", "auto_merge": False},
    )
    _wait_run_done(env.service)
    # run_task 的调用必须带着 resume 与权限档（健康检查也可能调工厂，参数为空）
    assert {"resume": "sess_prev", "permission": "yolo"} in calls


def test_unknown_executor_rejected(env):
    status, body = _post(f"{env.base_url}/api/tasks/{env.task['id']}/run", {"executor": "gpt"})
    assert status != 200
    assert "未知执行器" in body.get("message", body.get("error", ""))


# -- 模式 -------------------------------------------------------------------


def test_mode_switch_and_auto_run(env):
    status, body = _post(
        f"{env.base_url}/api/mode",
        {"mode": "auto", "auto": {"executor": "zcode", "auto_merge": False, "session_strategy": "new"}},
    )
    assert status == 200
    assert body["mode"] == "auto"
    # 自动循环异步启动：先等它认领（状态离开 open），再等这单跑完
    deadline = time.time() + 60
    while time.time() < deadline:
        if env.store.get(env.task["id"])["status"] != "open":
            break
        time.sleep(0.2)
    _wait_run_done(env.service, timeout=90)

    current = env.store.get(env.task["id"])
    assert current["status"] == "awaiting_merge"
    assert current["result"]["executor"] == "zcode"

    # 持久化检查
    saved = json.loads((env.work / ".handoff-run" / "panel-state.json").read_text(encoding="utf-8"))
    assert saved["mode"] == "auto"
    assert saved["auto"]["executor"] == "zcode"
    # 收尾：停掉自动循环，避免影响其他测试
    env.service.set_mode({"mode": "manual"})


def test_mode_validation(env):
    status, body = _post(f"{env.base_url}/api/mode", {"mode": "auto", "auto": {"executor": "nope"}})
    assert status != 200
    status, body = _post(f"{env.base_url}/api/mode", {"mode": "sideways"})
    assert status != 200
    assert env.service.state()["mode"] == "manual"


# -- 干预 -------------------------------------------------------------------


def test_run_after_failure_clears_stale_claim(env):
    """failed 之后 claim 还挂着，此时点执行不能撞 lease_lost。"""
    task_id = env.task["id"]
    env.store.claim(
        task_id,
        worker_id=env.config.panel.worker_id,
        lease_seconds=3600,
        base_sha=env.task["target"]["base_sha"],
    )
    task = env.store.peek(task_id)
    env.store.fail(
        task_id,
        stage="codex",
        message="上一轮失败",
        expected_revision=task["revision"],
    )
    assert env.store.peek(task_id)["status"] == "failed"
    assert env.store.peek(task_id)["claim"], "前提：失败后 claim 仍在"

    status, _body = _post(
        f"{env.base_url}/api/tasks/{task_id}/run", {"executor": "zcode", "auto_merge": False}
    )
    assert status == 200
    _wait_run_done(env.service)
    active = env.service.state()["active"]
    assert active["error"] is None, active.get("error")
    assert env.store.peek(task_id)["status"] == "awaiting_merge"


def test_sync_endpoint_reports_counts(env):
    status, body = _post(f"{env.base_url}/api/sync", {})
    assert status == 200, body
    assert body["ok"] is True
    assert body["added"] == 0
    assert body["total"] >= 1


def test_create_task_from_panel(env):
    status, body = _post(
        f"{env.base_url}/api/tasks",
        {
            "title": "加个健康检查",
            "repo": "org/api",
            "base_ref": "dev",
            "requirement": "新增 /healthz 返回 200",
            "scope_profile": "application",
            "out_of_scope": "不改鉴权\n不改部署配置",
        },
    )
    assert status == 200, body
    created = env.store.peek(body["id"])
    assert created["status"] == "open"
    assert created["target"]["repo"] == "org/api"
    assert created["target"]["base_ref"] == "dev"
    assert created["scope"]["allowed_paths"] == ["src/**", "tests/**"]
    assert created["scope"]["forbidden_paths"] == [".github/**", "infra/prod/**"]
    assert created["scope"]["out_of_scope"] == ["不改鉴权", "不改部署配置"]
    assert created["acceptance"]["environment"] == "dev"
    assert created["source"]["role"] == "frontend"
    assert created["source"]["repo"] == "org/web"
    assert re.fullmatch(r"[0-9a-f]{40}", created["source"]["commit_sha"])
    assert created["source"]["commit_sha"] != "0" * 40


def test_create_task_rejects_forged_source_provenance(env):
    base = {
        "title": "伪造来源",
        "repo": "org/api",
        "requirement": "应被拒绝",
        "scope_profile": "application",
    }
    # 用后端项目冒充来源 → 拒绝
    status, body = _post(f"{env.base_url}/api/tasks", {**base, "source_repo": "org/api"})
    assert status == 400
    assert "frontend" in body.get("message", body.get("error", ""))

    # 显式填错 SHA → 与本地 HEAD 不一致，拒绝
    status, body = _post(
        f"{env.base_url}/api/tasks",
        {**base, "source_repo": "org/web", "source_sha": "f" * 40},
    )
    assert status == 400
    assert "HEAD" in body.get("message", body.get("error", ""))

    # 来源项目没配本地路径 → 拒绝，绝不退回全零 SHA
    from dataclasses import replace
    original_checks = env.config.checks
    try:
        env.config.checks = replace(
            original_checks,
            verifier=replace(original_checks.verifier, repo_paths={}),
        )
        status, body = _post(f"{env.base_url}/api/tasks", base)
        assert status == 400
        assert "本地路径" in body.get("message", body.get("error", ""))
    finally:
        env.config.checks = original_checks


def test_create_task_rejects_raw_path_scope(env):
    status, body = _post(
        f"{env.base_url}/api/tasks",
        {
            "title": "不允许前端传路径",
            "repo": "org/api",
            "requirement": "新增 /healthz 返回 200",
            "allowed_paths": "**",
        },
    )
    assert status == 400
    assert "scope 模板" in body.get("message", body.get("error", ""))


def test_create_task_rejects_unknown_scope_profile(env):
    status, body = _post(
        f"{env.base_url}/api/tasks",
        {
            "title": "未知路径模板",
            "repo": "org/api",
            "requirement": "新增 /healthz 返回 200",
            "scope_profile": "does-not-exist",
        },
    )
    assert status == 400
    assert "does-not-exist" in body.get("message", body.get("error", ""))


def test_create_task_rejects_unknown_or_wrong_role(env):
    status, body = _post(
        f"{env.base_url}/api/tasks",
        {"title": "x", "repo": "org/nope", "requirement": "y"},
    )
    assert status == 400
    assert "未注册" in body.get("message", body.get("error", ""))

    # org/web 是 frontend，面板只给 backend 发单
    status, body = _post(
        f"{env.base_url}/api/tasks",
        {"title": "x", "repo": "org/web", "requirement": "y"},
    )
    assert status == 400
    assert "backend" in body.get("message", body.get("error", ""))


def test_create_task_requires_title_and_requirement(env):
    status, body = _post(f"{env.base_url}/api/tasks", {"repo": "org/api"})
    assert status == 400
    assert "标题" in body.get("message", body.get("error", ""))


def test_workspace_reuse_keeps_dirty_changes(tmp_path):
    """续跑复用上一轮 worktree：未提交改动必须原样还在，分支名跟到新一轮。"""
    from handoff.config import RepoConfig
    from handoff.gitcmd import git_out
    from handoff.workspace import Workspace

    wt = tmp_path / "wt"
    wt.mkdir()
    git(wt, "init", "-q")
    (wt / "a.txt").write_text("base\n", encoding="utf-8")
    git(wt, "add", "-A")
    git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    git(wt, "branch", "-m", "codex/task-1/attempt-1")
    (wt / "a.txt").write_text("changed\n", encoding="utf-8")  # 模拟上一轮没提交的改动

    repo = RepoConfig(id="org/api", role="backend")
    ws = Workspace(repo, tmp_path, "task-1", 2, reuse=wt)
    path = ws.prepare("0" * 40)

    assert path == wt.resolve()
    assert (wt / "a.txt").read_text(encoding="utf-8") == "changed\n"
    assert git_out(wt, "rev-parse", "--abbrev-ref", "HEAD") == "codex/task-1/attempt-2"


def test_task_sessions_include_failed_attempt(tmp_path):
    """失败的 attempt 没写 codex.completed，会话得从执行日志首行捞出来。"""
    from handoff.panel import sessions as panel_sessions

    log_dir = tmp_path / ".handoff-run" / "logs" / "ho-x" / "attempt-1"
    log_dir.mkdir(parents=True)
    (log_dir / "codex-abc.jsonl").write_text(
        '{"type":"thread.started","thread_id":"01a1c433-59da"}\n{"type":"turn.started"}\n',
        encoding="utf-8",
    )
    rows = panel_sessions.task_sessions({"id": "ho-x"}, [], root=tmp_path)
    assert len(rows) == 1
    assert rows[0].id == "01a1c433-59da"
    assert rows[0].source == "log"
    assert rows[0].attempt == 1


def test_run_with_last_session_without_history(env):
    status, body = _post(
        f"{env.base_url}/api/tasks/{env.task['id']}/run",
        {"executor": "zcode", "resume_session_id": "last"},
    )
    assert status == 400
    assert "可续跑" in body.get("message", body.get("error", ""))


def test_clean_path_strips_bom_and_zero_width():
    from handoff.panel.service import _clean_path

    assert _clean_path("\ufeffD:\\project\\x") == "D:\\project\\x"
    assert _clean_path("  D:/x\u200b ") == "D:/x"
    assert _clean_path(None) == ""
    assert _clean_path("") == ""


def test_run_in_direct_workdir(env, tmp_path):
    """指定项目目录：agent 直接改在那个目录，不建 worktree、不推 PR。"""
    direct = tmp_path / "mywork"
    direct.mkdir()
    git(direct, "init", "-q")
    (direct / "src").mkdir()
    (direct / "src" / "keep.go").write_text("package src\n", encoding="utf-8")
    git(direct, "add", "-A")
    git(direct, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    status, _body = _post(
        f"{env.base_url}/api/tasks/{env.task['id']}/run",
        # 故意带个 BOM：从别处复制路径最常见的坑，不清掉 agent 永远读不到文件
        # 这些用例只测交付/提交，关掉 wait_deployment 避免触发自动验收去连测试环境
        {"executor": "zcode", "work_dir": "\ufeff" + str(direct), "auto_merge": True, "wait_deployment": False},
    )
    assert status == 200
    _wait_run_done(env.service)

    active = env.service.state()["active"]
    assert active["error"] is None, active.get("error")
    # 假执行器写的文件真的落到了指定目录
    assert (direct / "src" / "orders" / "Status.java").is_file()

    current = env.store.peek(env.task["id"])
    assert current["status"] == "delivered", current.get("failure")
    assert current["result"]["pr_url"] is None
    assert current["result"]["worktree"] == str(direct)
    assert "未提交" in (current["result"]["notes"] or "")


class FakeZcodeCommit(FakeZcode):
    """假 zcode：可控制结果里有没有 commit_message，测自动提交链路。"""

    def __init__(self, files=None, *, commit_message=None):
        super().__init__(files)
        self.commit_message = commit_message

    def run(self, worktree, prompt, *, task_id, attempt, cancel_event=None):
        outcome = super().run(worktree, prompt, task_id=task_id, attempt=attempt, cancel_event=cancel_event)
        if self.commit_message is not None and outcome.result is not None:
            outcome.result["commit_message"] = self.commit_message
        return outcome


def _make_direct_repo_with_origin(tmp_path):
    remote = make_bare_remote(
        tmp_path, "directapi", {"src/orders/Base.java": "package orders;\n"}, branch="dev"
    )
    direct = tmp_path / "mywork"
    git(tmp_path, "clone", str(remote), str(direct))
    git(direct, "checkout", "dev")
    return direct, remote


def test_direct_auto_commit_pushes_with_ai_message(env, tmp_path):
    """项目目录模式勾自动提交：commit_message 用 AI 写的，提交后推到当前分支。"""
    direct, remote = _make_direct_repo_with_origin(tmp_path)
    env.service._runner_overrides["zcode"] = FakeZcodeCommit(
        files={"src/orders/Status.java": "enum Status { OPEN }\n"},
        commit_message="feat: 订单接口新增 status 字段",
    )
    status, _body = _post(
        f"{env.base_url}/api/tasks/{env.task['id']}/run",
        {"executor": "zcode", "work_dir": str(direct), "auto_commit": True, "wait_deployment": False},
    )
    assert status == 200
    _wait_run_done(env.service)

    current = env.store.peek(env.task["id"])
    assert current["status"] == "delivered", current.get("failure")
    head_sha = current["result"]["head_sha"]
    assert head_sha
    assert current["result"]["branch"] == "dev"
    # 提交信息就是 AI 写的那句
    assert git(direct, "log", "-1", "--format=%s") == "feat: 订单接口新增 status 字段"
    # 远端分支已经指过来
    assert git(remote, "rev-parse", "dev") == head_sha
    assert "已提交" in current["result"]["notes"]
    assert "推送至" in current["result"]["notes"]


def test_direct_auto_commit_without_ai_message_uses_fallback(env, tmp_path):
    """执行器没给 commit_message：退化成带任务 ID 的兜底提交信息，照样提交推送。"""
    direct, remote = _make_direct_repo_with_origin(tmp_path)
    env.service._runner_overrides["zcode"] = FakeZcodeCommit(
        files={"src/orders/Status.java": "enum Status { OPEN }\n"},
    )
    status, _body = _post(
        f"{env.base_url}/api/tasks/{env.task['id']}/run",
        {"executor": "zcode", "work_dir": str(direct), "auto_commit": True, "wait_deployment": False},
    )
    assert status == 200
    _wait_run_done(env.service)

    current = env.store.peek(env.task["id"])
    assert current["status"] == "delivered", current.get("failure")
    body = git(direct, "log", "-1", "--format=%B")
    assert env.task["id"] in body
    assert "订单接口增加状态字段" in body
    assert git(remote, "rev-parse", "dev") == current["result"]["head_sha"]


def test_direct_auto_commit_push_failure_still_delivered(env, tmp_path):
    """推送失败不吞改动：本地已提交，任务照样 delivered，notes 写明要人工推送。"""
    direct = tmp_path / "mywork"
    direct.mkdir()
    git(direct, "init", "-q", "-b", "dev")
    (direct / "src").mkdir()
    (direct / "src" / "keep.go").write_text("package src\n", encoding="utf-8")
    git(direct, "add", "-A")
    git(direct, "commit", "-q", "-m", "init")
    git(direct, "remote", "add", "origin", str(tmp_path / "no-such-remote.git"))
    env.service._runner_overrides["zcode"] = FakeZcodeCommit(
        files={"src/orders/Status.java": "enum Status { OPEN }\n"},
        commit_message="feat: 测试",
    )
    status, _body = _post(
        f"{env.base_url}/api/tasks/{env.task['id']}/run",
        {"executor": "zcode", "work_dir": str(direct), "auto_commit": True, "wait_deployment": False},
    )
    assert status == 200
    _wait_run_done(env.service)

    current = env.store.peek(env.task["id"])
    assert current["status"] == "delivered", current.get("failure")
    assert current["result"]["head_sha"]  # 本地提交是成功的
    assert current["result"]["branch"] is None
    assert "推送失败" in current["result"]["notes"]
    assert git(direct, "log", "-1", "--format=%s") == "feat: 测试"


def test_steer_after_finish_continues_same_session(env, tmp_path):
    """任务跑完后还能在会话里说话：delivered 重新入队，同会话、同工作目录接着执行。"""
    direct = tmp_path / "mywork"
    direct.mkdir()
    git(direct, "init", "-q")
    (direct / "src").mkdir()
    (direct / "src" / "keep.go").write_text("package src\n", encoding="utf-8")
    git(direct, "add", "-A")
    git(direct, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    seen = []

    def factory(resume_session_id=None, permission_mode=None):
        runner = FakeZcode(files={"src/orders/Status.java": "enum Status { OPEN }\n"})
        seen.append({"resume": resume_session_id, "runner": runner})
        return runner

    env.service._runner_overrides["zcode"] = factory
    status, _body = _post(
        f"{env.base_url}/api/tasks/{env.task['id']}/run",
        {"executor": "zcode", "work_dir": str(direct), "wait_deployment": False},
    )
    assert status == 200
    _wait_run_done(env.service)
    first = env.store.peek(env.task["id"])
    assert first["status"] == "delivered", first.get("failure")
    first_session = first["result"]["executor_session_id"]

    # 任务已结束——继续在同一会话说话，让它接着干活
    def factory2(resume_session_id=None, permission_mode=None):
        runner = FakeZcode(files={"src/orders/More.java": "enum More {}\n"})
        seen.append({"resume": resume_session_id, "runner": runner})
        return runner

    env.service._runner_overrides["zcode"] = factory2
    status, body = _post(
        f"{env.base_url}/api/runs/steer",
        {"text": "再补一个 More 枚举"},
    )
    assert status == 200
    assert body.get("continuing") is True
    # 续话发出时上一轮已经 finished，不能用 _wait_run_done（会立刻返回）；
    # 按 attempt 推进等新一轮跑完
    deadline = time.time() + 60
    second = None
    while time.time() < deadline:
        t = env.store.peek(env.task["id"])
        if t.get("attempt", 0) >= first["attempt"] + 1 and t["status"] in (
            "delivered",
            "failed",
            "verified",
        ):
            second = t
            break
        time.sleep(0.1)
    assert second is not None, "续跑没有产生新 attempt"
    assert second["status"] == "delivered", second.get("failure")
    assert second["attempt"] == first["attempt"] + 1
    # 同一会话续跑（resume 拿的是上一轮 session），续话进了 prompt，目录还是那个目录。
    # 注意 seen 里会混入 state() 健康检查创建的 runner（没跑过任务），要按 calls 过滤
    ran = [s for s in seen if s["runner"].calls]
    assert len(ran) == 2
    assert ran[1]["resume"] == first_session
    assert "再补一个 More 枚举" in ran[1]["runner"].calls[0]["prompt"]
    assert (direct / "src" / "orders" / "More.java").is_file()
    assert second["result"]["worktree"] == str(direct)


def test_run_in_missing_workdir_fails_cleanly(env, tmp_path):
    missing = tmp_path / "nope"
    status, _body = _post(
        f"{env.base_url}/api/tasks/{env.task['id']}/run",
        {"executor": "zcode", "work_dir": str(missing)},
    )
    assert status == 200
    _wait_run_done(env.service)
    current = env.store.peek(env.task["id"])
    assert current["status"] == "failed"
    assert "项目目录不存在" in (current.get("failure") or {}).get("message", "")


def test_startup_reclaims_stale_panel_lease(env):
    """面板进程被杀后，自己持有的租约要在下次启动时回收，否则任务永久卡住。"""
    task_id = env.task["id"]
    panel_worker = env.config.panel.worker_id
    env.store.claim(
        task_id,
        worker_id=panel_worker,
        lease_seconds=3600,
        base_sha=env.task["target"]["base_sha"],
    )
    assert env.store.peek(task_id)["status"] == "claimed"

    fresh = PanelService(
        env.work,
        env.config,
        runner_overrides={"zcode": FakeZcode()},
        host=FakeHost(remotes={"org/api": env.backend}),
    )
    try:
        assert [r["task_id"] for r in fresh._reclaimed] == [task_id]
        assert env.store.peek(task_id)["status"] == "open"
    finally:
        fresh.shutdown()


def test_startup_keeps_other_workers_lease(env):
    """别的 worker（比如旧的后台 worker）持有的租约不能碰。"""
    task_id = env.task["id"]
    env.store.claim(
        task_id,
        worker_id="example-backend-01",
        lease_seconds=3600,
        base_sha=env.task["target"]["base_sha"],
    )

    fresh = PanelService(
        env.work,
        env.config,
        runner_overrides={"zcode": FakeZcode()},
        host=FakeHost(remotes={"org/api": env.backend}),
    )
    try:
        assert fresh._reclaimed == []
        assert env.store.peek(task_id)["status"] == "claimed"
    finally:
        fresh.shutdown()


def test_steer_without_running_task_is_conflict(env):
    status, body = _post(f"{env.base_url}/api/runs/steer", {"text": "改这里"})
    assert status == 409
    assert "没有可续跑的会话" in body.get("message", body.get("error", ""))

    status, body = _post(f"{env.base_url}/api/runs/steer", {"text": "   "})
    assert status == 400
    assert "不能为空" in body.get("message", body.get("error", ""))


def test_steer_interrupts_then_resumes_same_session_with_instruction(env):
    from handoff.zcode_runner import session_uuid

    gate = threading.Event()
    slow = FakeZcode(block_event=gate)
    sessions: list = []

    def factory(resume_session_id=None, permission_mode=None):
        sessions.append(resume_session_id)
        return slow

    env.service._runner_overrides["zcode"] = factory
    _post(f"{env.base_url}/api/tasks/{env.task['id']}/run", {"executor": "zcode", "auto_merge": False})
    time.sleep(0.4)

    status, body = _post(f"{env.base_url}/api/runs/steer", {"text": "别动 Status.java，只补 README"})
    assert status == 200 and body["ok"] is True and body["steering"] is True

    # 放行被中断那一轮：假执行器看到 cancel_event 后按"被打断"返回 failed
    gate.set()

    deadline = time.time() + 90
    while time.time() < deadline:
        current = env.store.get(env.task["id"])
        active = env.service.state().get("active") or {}
        if current["status"] == "awaiting_merge" and active.get("phase") == "finished":
            break
        time.sleep(0.2)
    else:
        raise AssertionError(
            "干预续跑未完成: status=%s failure=%s active=%s"
            % (
                env.store.get(env.task["id"])["status"],
                env.store.get(env.task["id"]).get("failure"),
                env.service.state().get("active"),
            )
        )

    # 两轮：第一轮被打断，第二轮带着人话、接续同一会话
    assert len(slow.calls) == 2
    assert "别动 Status.java，只补 README" in slow.calls[1]["prompt"]
    assert "人工干预指令" in slow.calls[1]["prompt"]
    assert session_uuid(env.task["id"], 1) in sessions
    assert env.store.get(env.task["id"])["status"] == "awaiting_merge"


# -- Codex 安全模式（执行前选 / 执行中改） -------------------------------------


def test_mode_switch_back_to_auto_restarts_wake_listener(env):
    """manual → auto 切回来要重新拉起唤醒监听（auto 线程还活着时也必须补）。"""
    calls = []
    service = env.service

    def fake_start():
        calls.append(1)
        service._wake_status = {"ok": True, "url": "http://127.0.0.1:8788/v1/handoff/wake"}

    service._start_wake_server = fake_start
    status, _ = _post(f"{env.base_url}/api/mode", {"mode": "auto"})
    assert status == 200
    for _ in range(50):
        if calls:
            break
        time.sleep(0.05)
    assert len(calls) == 1

    status, _ = _post(f"{env.base_url}/api/mode", {"mode": "manual"})
    assert status == 200
    assert service._wake_server is None

    status, _ = _post(f"{env.base_url}/api/mode", {"mode": "auto"})
    assert status == 200
    for _ in range(50):
        if len(calls) >= 2:
            break
        time.sleep(0.05)
    assert len(calls) == 2, f"manual→auto 没有重新拉起唤醒监听：{calls}"

    _post(f"{env.base_url}/api/mode", {"mode": "manual"})


def test_state_exposes_sandbox_options(env):
    state = _get(f"{env.base_url}/api/state")
    assert state["codex_sandbox_options"] == ["read-only", "workspace-write", "danger-full-access"]
    assert state["codex_sandbox_default"] == "workspace-write"
    assert state["auto"]["codex_sandbox"] is None


def test_runner_sandbox_validation_and_switch():
    from handoff.codex_runner import CODEX_SANDBOX_MODES, CodexRunner
    from handoff.errors import ConfigError

    assert CODEX_SANDBOX_MODES == ("read-only", "workspace-write", "danger-full-access")
    runner = CodexRunner(sandbox="workspace-write")
    assert runner.set_sandbox("read-only") == "read-only"
    with pytest.raises(ConfigError):
        runner.set_sandbox("root")
    with pytest.raises(ConfigError):
        CodexRunner(sandbox="")


def test_security_endpoint_validates_and_persists(env):
    status, body = _post(f"{env.base_url}/api/runs/security", {"codex_sandbox": "read-only"})
    assert status == 200
    assert body["auto"]["codex_sandbox"] == "read-only"
    assert body["security"]["effective"] == "read-only"
    saved = json.loads((env.work / ".handoff-run" / "panel-state.json").read_text(encoding="utf-8"))
    assert saved["auto"]["codex_sandbox"] == "read-only"

    status, body = _post(f"{env.base_url}/api/runs/security", {"codex_sandbox": "nope"})
    assert status == 400
    assert "非法" in body.get("message", body.get("error", ""))
    assert env.service.state()["auto"]["codex_sandbox"] == "read-only", "非法值不能留下半套状态"

    status, body = _post(f"{env.base_url}/api/runs/security", {"codex_sandbox": ""})
    assert status == 200
    assert body["auto"]["codex_sandbox"] is None
    assert body["security"]["effective"] == "workspace-write"


def test_run_task_rejects_unknown_sandbox(env):
    status, body = _post(
        f"{env.base_url}/api/tasks/{env.task['id']}/run",
        {"executor": "codex", "codex_sandbox": "root"},
    )
    assert status == 400
    assert "非法" in body.get("message", body.get("error", ""))


def test_selected_sandbox_reaches_runner(env):
    from helpers import FakeCodex

    seen = []

    def factory(resume_session_id=None, permission_mode=None, codex_sandbox=None):
        seen.append(codex_sandbox)
        runner = FakeCodex({"src/orders/Sandbox.java": "// sandbox\n"})
        runner.name = "codex"
        runner.version = lambda: "codex 0.0.0-fake"
        return runner

    env.service._runner_overrides["codex"] = factory
    status, _ = _post(
        f"{env.base_url}/api/tasks/{env.task['id']}/run",
        {"executor": "codex", "codex_sandbox": "danger-full-access", "auto_merge": False},
    )
    assert status == 200
    _wait_run_done(env.service)
    assert "danger-full-access" in seen


def test_security_mode_apply_now_restarts_with_new_sandbox(env):
    gate = threading.Event()
    slow = FakeZcode(block_event=gate)
    slow.name = "codex"
    seen = []

    def factory(resume_session_id=None, permission_mode=None, codex_sandbox=None):
        seen.append(codex_sandbox)
        return slow

    env.service._runner_overrides["codex"] = factory
    status, _ = _post(
        f"{env.base_url}/api/tasks/{env.task['id']}/run",
        {"executor": "codex", "codex_sandbox": "workspace-write", "auto_merge": False},
    )
    assert status == 200
    time.sleep(0.4)

    status, body = _post(
        f"{env.base_url}/api/runs/security",
        {"codex_sandbox": "read-only", "apply_now": True},
    )
    assert status == 200 and body["security"]["apply_now"] is True
    gate.set()

    deadline = time.time() + 90
    while time.time() < deadline:
        current = env.store.get(env.task["id"])
        active = env.service.state().get("active") or {}
        if current["status"] == "awaiting_merge" and active.get("phase") == "finished":
            break
        time.sleep(0.2)
    else:
        raise AssertionError(
            "安全模式切换续跑未完成: status=%s active=%s"
            % (env.store.get(env.task["id"])["status"], env.service.state().get("active"))
        )

    # 健康检查也会构造 runner（参数为空），所以只比较有值的两次实际执行
    assert seen.count("workspace-write") >= 1
    assert seen.count("read-only") >= 1
    assert seen.index("read-only") > seen.index("workspace-write")
    assert len(slow.calls) == 2
    assert env.service.state()["auto"]["codex_sandbox"] == "read-only"


# -- 自动模式：默认工作目录 + 严格串行 -------------------------------------

def test_panel_yaml_default_work_dir_is_cleaned(env):
    from handoff.config import Config

    # 配置里夹了 BOM 也要清洗干净；同时验证 panel.yaml 能覆盖默认值
    (env.work / "config" / "panel.yaml").write_text(
        "panel:\n  default_work_dir: '" + "\ufeff" + "D:/project/from-panel'\n"
        "  auto_commit: true\n  poll_interval_seconds: 0.2\n",
        encoding="utf-8",
    )
    cfg = Config.load(env.work)
    assert cfg.panel.default_work_dir == "D:/project/from-panel"
    assert cfg.panel.auto_commit is True

    original = env.service.config
    env.service.config = cfg
    try:
        auto = env.service._default_state()["auto"]
        assert auto["work_dir"] == "D:/project/from-panel"
        assert auto["auto_commit"] is True
    finally:
        env.service.config = original


def test_auto_mode_persists_work_dir_and_auto_commit(env):
    raw = "\ufeff" + "D:/project/api"
    status, body = _post(
        f"{env.base_url}/api/mode",
        {"mode": "manual", "auto": {"work_dir": raw, "auto_commit": True}},
    )
    assert status == 200
    assert body["auto"]["work_dir"] == "D:/project/api"
    assert body["auto"]["auto_commit"] is True

    saved = json.loads((env.work / ".handoff-run" / "panel-state.json").read_text(encoding="utf-8"))
    assert saved["auto"]["work_dir"] == "D:/project/api"
    assert saved["auto"]["auto_commit"] is True

    # 清空目录 = 回到 worktree 模式，不能留下空字符串
    status, body = _post(
        f"{env.base_url}/api/mode",
        {"mode": "manual", "auto": {"work_dir": "  ", "auto_commit": False}},
    )
    assert status == 200
    assert body["auto"]["work_dir"] is None
    assert body["auto"]["auto_commit"] is False


def test_auto_work_dir_config_wins_over_repo_paths(env, monkeypatch):
    from types import SimpleNamespace

    repo_id = env.task["target"]["repo"]
    fake = SimpleNamespace(
        checks=SimpleNamespace(
            verifier=SimpleNamespace(repo_paths={repo_id: "D:/project/from-repo"})
        )
    )
    monkeypatch.setattr(env.service, "config", fake)
    assert env.service._auto_work_dir(env.task["id"], {"work_dir": "D:/project/explicit"}) == "D:/project/explicit"
    assert env.service._auto_work_dir(env.task["id"], {"work_dir": None}) == "D:/project/from-repo"
    assert env.service._auto_work_dir(env.task["id"], {}) == "D:/project/from-repo"


def test_auto_run_passes_work_dir_and_commit(env, monkeypatch):
    calls = []
    done = threading.Event()

    def fake_run_task(task_id, **kw):
        calls.append(kw)
        done.set()
        time.sleep(0.05)
        return {"task_id": task_id}

    monkeypatch.setattr(env.service, "run_task", fake_run_task)
    monkeypatch.setattr(env.service, "_pick_open_task", lambda: env.task["id"])
    monkeypatch.setattr(env.service, "_auto_busy_task", lambda: None)
    monkeypatch.setattr(env.service, "_auto_resume_session", lambda *a, **k: None)
    try:
        env.service.set_mode(
            {"mode": "auto", "auto": {"work_dir": "D:/project/shared", "auto_commit": True}}
        )
        assert done.wait(5), "自动循环没有调用 run_task"
        kw = calls[0]
        assert kw["work_dir"] == "D:/project/shared"
        assert kw["auto_commit"] is True
        assert kw["trigger"] == "auto"
    finally:
        env.service.set_mode({"mode": "manual"})


def test_auto_serial_gate_blocks_while_task_busy(env, monkeypatch):
    picks = []
    monkeypatch.setattr(env.service, "_pick_open_task", lambda: picks.append(1) or env.task["id"])
    monkeypatch.setattr(env.service, "_auto_busy_task", lambda: "busy-task")
    try:
        env.service.set_mode({"mode": "auto"})
        time.sleep(0.6)
        assert picks == [], "还有任务在处理中时不该挑下一单"
    finally:
        env.service.set_mode({"mode": "manual"})


def test_auto_serial_gate_waits_for_active_run(env, monkeypatch):
    from handoff.panel.service import ActiveRun
    from handoff.timeutil import iso

    gate = threading.Event()
    run = ActiveRun(
        task_id="fake-running",
        executor="zcode",
        trigger="manual",
        started_at=iso(),
        cancel_event=threading.Event(),
    )
    run.thread = threading.Thread(target=lambda: gate.wait(5), daemon=True)
    run.thread.start()
    env.service._active = run

    picks = []
    monkeypatch.setattr(env.service, "_pick_open_task", lambda: picks.append(1) or env.task["id"])
    monkeypatch.setattr(env.service, "run_task", lambda *a, **k: {"task_id": "fake"})
    try:
        env.service.set_mode({"mode": "auto"})
        time.sleep(0.4)
        assert picks == [], "本面板执行线程没结束时不该挑下一单"
    finally:
        gate.set()
        run.phase = "finished"
        env.service.set_mode({"mode": "manual"})
        env.service._active = None


def test_auto_busy_task_ignores_stale_heartbeat(env):
    from datetime import timedelta

    from handoff.timeutil import iso, now

    path = env.work / "tasks" / f"{env.task['id']}.json"
    original = path.read_text(encoding="utf-8")
    data = json.loads(original)
    try:
        data["status"] = "in_progress"
        data["claim"] = {"worker_id": "other", "heartbeat_at": iso()}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        assert env.service._auto_busy_task() == env.task["id"]

        data["claim"]["heartbeat_at"] = iso(now() - timedelta(hours=3))
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        assert env.service._auto_busy_task() is None, "僵尸租约不该把自动队列卡死"

        del data["claim"]
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        assert env.service._auto_busy_task() is None
    finally:
        path.write_text(original, encoding="utf-8")


# -- 任务列表按创建时间倒序 -------------------------------------------------

def test_list_tasks_sorted_by_created_at_desc(env, monkeypatch):
    rows = [
        {"id": "a-old", "status": "open", "created_at": "2026-01-01T00:00:00Z"},
        {"id": "z-new", "status": "open", "created_at": "2026-09-01T00:00:00Z"},
        {"id": "m-mid", "status": "open", "created_at": "2026-06-01T00:00:00Z"},
    ]

    class FakeStore:
        def peek_tasks(self, *, status=None):
            assert status is None
            return list(rows)

    monkeypatch.setattr(env.service, "_store", lambda: FakeStore())
    assert [t["id"] for t in env.service.list_tasks()] == ["z-new", "m-mid", "a-old"]


def test_tasks_endpoint_returns_newest_first(env):
    from handoff import ids

    base_sha = env.task["target"]["base_sha"]
    older, newer = ids.task_id(), ids.task_id()
    stamps = ((older, "2026-01-01T00:00:00Z"), (newer, "2026-09-01T00:00:00Z"))
    # 先全部创建完再改文件：store.create 会 sync，中途改文件会留下脏工作区
    for tid, _ in stamps:
        env.store.create(make_task(tid, base_sha=base_sha))
    for tid, ts in stamps:
        path = env.work / "tasks" / f"{tid}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["created_at"] = ts
        data["updated_at"] = ts
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = _get(f"{env.base_url}/api/tasks")["tasks"]
    assert len(rows) == 3
    created = [t["created_at"] for t in rows]
    assert created == sorted(created, reverse=True)
    assert rows[0]["id"] == env.task["id"]
    assert rows[-1]["id"] == older


# -- 恢复推进 ---------------------------------------------------------------

class _FakeAdvanceStore:
    def __init__(self, task):
        self.task = json.loads(json.dumps(task))
        self.transitions = []

    def peek(self, task_id):
        return json.loads(json.dumps(self.task))

    def get(self, task_id):
        return self.peek(task_id)

    def task_commit(self, task_id):
        return "e" * 40

    def transition(self, task_id, to, **kw):
        self.transitions.append((to, kw))
        self.task["status"] = to
        self.task["revision"] = int(self.task.get("revision", 1)) + 1
        return self.peek(task_id)


def _wait_advance(service, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = service._advance
        if state and state.get("done"):
            return state
        time.sleep(0.05)
    raise AssertionError("恢复推进未在超时内结束: %r" % (service._advance,))


def _delivered_task(env, **result_patch):
    task = json.loads(json.dumps(env.task))
    task["status"] = "delivered"
    task["result"]["pr_number"] = None
    task["result"]["merge_sha"] = None
    task["result"]["branch"] = "develop"
    task["result"]["head_sha"] = "a" * 40
    task["result"].update(result_patch)
    return task


def test_advance_rejects_non_delivery_status(env):
    status, body = _post(f"{env.base_url}/api/tasks/{env.task['id']}/advance", {})
    assert status >= 400
    assert "不需要恢复推进" in str(body)


def test_advance_direct_mode_skips_merge_to_deliver(env, monkeypatch):
    """目录模式没有 PR / 合并环节：恢复推进直接进部署验收，不再推去 awaiting_merge。"""
    import handoff.cli as cli

    task = _delivered_task(env, worktree="D:/project/api")
    fake = _FakeAdvanceStore(task)
    monkeypatch.setattr(env.service, "_store", lambda *, push=True: fake)

    def fake_push(t, steps):
        steps.append("mock push")
        return "develop", "a" * 40

    monkeypatch.setattr(env.service, "_advance_push", fake_push)
    monkeypatch.setattr(
        cli, "cmd_deliver",
        lambda args, config: {"id": args.task_id, "result": "verified", "message": "ok"},
    )
    env.service.advance(env.task["id"])
    result = _wait_advance(env.service)
    assert result["error"] is None
    assert result["result"] == "verified"
    assert "mock push" in result["steps"]
    assert any("跳过合并" in s for s in result["steps"])
    assert fake.transitions == []


def test_advance_rescues_awaiting_merge_without_pr(env, monkeypatch):
    """卡在 awaiting_merge 的目录模式任务（无 PR / merge_sha）直接救回部署验收。"""
    import handoff.cli as cli

    task = _delivered_task(env, worktree="D:/project/api")
    task["status"] = "awaiting_merge"
    fake = _FakeAdvanceStore(task)
    monkeypatch.setattr(env.service, "_store", lambda *, push=True: fake)
    monkeypatch.setattr(
        cli, "cmd_deliver",
        lambda args, config: {"id": args.task_id, "result": "verified", "message": "ok"},
    )
    env.service.advance(env.task["id"])
    result = _wait_advance(env.service)
    assert result["error"] is None
    assert result["result"] == "verified"
    assert fake.transitions == []


def test_advance_direct_mode_requires_head_sha(env, monkeypatch):
    """目录模式改动没提交（无 head_sha）时明确报错，不去推送无关 HEAD。"""
    task = _delivered_task(env, worktree="D:/project/api", head_sha=None)
    fake = _FakeAdvanceStore(task)
    monkeypatch.setattr(env.service, "_store", lambda *, push=True: fake)
    env.service.advance(env.task["id"])
    result = _wait_advance(env.service)
    assert result["error"] is not None
    assert "head_sha" in result["error"]


def test_accept_api_rejects_non_verified_task(env):
    status, body = _post(f"{env.base_url}/api/tasks/{env.task['id']}/accept", {})
    assert status >= 400
    assert "verified" in str(body)


def test_accept_api_marks_frontend_accepted(env, monkeypatch):
    task = json.loads(json.dumps(env.task))
    task["status"] = "verified"
    task["result"]["merge_sha"] = "b" * 40
    task["deployment"].update(
        {
            "status": "deployed",
            "target_sha": "b" * 40,
            "observed_sha": "c" * 40,
            "updated_at": "2026-11-05T00:00:00Z",
        }
    )
    fake = _FakeAdvanceStore(task)
    monkeypatch.setattr(env.service, "_store", lambda *, push=True: fake)
    status, body = _post(f"{env.base_url}/api/tasks/{env.task['id']}/accept", {"reason": "前端验收通过"})
    assert status == 200 and body["ok"] is True
    assert body["status"] == "frontend_accepted"
    to, kw = fake.transitions[0]
    assert to == "frontend_accepted"
    assert kw["event_type"] == "frontend.accepted"
    assert kw["summary"] == "前端验收通过"


def test_advance_runs_deliver_for_merged_state(env, monkeypatch):
    import handoff.cli as cli

    task = _delivered_task(env)
    task["status"] = "merged"
    fake = _FakeAdvanceStore(task)
    monkeypatch.setattr(env.service, "_store", lambda *, push=True: fake)
    monkeypatch.setattr(
        cli, "cmd_deliver", lambda args, config: {"id": args.task_id, "result": "verified"}
    )
    env.service.advance(env.task["id"])
    result = _wait_advance(env.service)
    assert result["error"] is None
    assert result["result"] == "verified"
    assert any("验收" in s for s in result["steps"])


def test_advance_merge_branch_continues_to_deliver(env, monkeypatch):
    import handoff.cli as cli

    task = _delivered_task(env, pr_number=42)
    fake = _FakeAdvanceStore(task)
    monkeypatch.setattr(env.service, "_store", lambda *, push=True: fake)
    monkeypatch.setattr(env.service, "_advance_merge", lambda tid, pr, steps: steps.append("merged") or True)
    monkeypatch.setattr(
        cli, "cmd_deliver", lambda args, config: {"id": args.task_id, "result": "verified"}
    )
    env.service.advance(env.task["id"])
    result = _wait_advance(env.service)
    assert result["error"] is None
    assert result["result"] == "verified"
    assert "merged" in result["steps"]


def test_advance_rejects_second_run_while_busy(env, monkeypatch):
    from handoff.errors import ConflictError

    fake = _FakeAdvanceStore(_delivered_task(env, worktree="D:/project/api"))
    monkeypatch.setattr(env.service, "_store", lambda *, push=True: fake)
    gate = threading.Event()

    def slow_push(t, steps):
        gate.wait(5)
        return "develop", "a" * 40

    monkeypatch.setattr(env.service, "_advance_push", slow_push)
    try:
        env.service.advance(env.task["id"])
        with pytest.raises(ConflictError):
            env.service.advance(env.task["id"])
    finally:
        gate.set()
    assert _wait_advance(env.service)["error"] is None


def test_resend_notify_api_rejects_non_terminal_task(env):
    status, body = _post(f"{env.base_url}/api/tasks/{env.task['id']}/resend-notify", {})
    assert status >= 400
    assert "verified / failed" in str(body)


def test_resend_notify_api_replays_for_terminal_task(env, monkeypatch):
    """面板按钮走通到 notification.replay_task_completed，目标地址原样回显。"""
    from handoff import notification

    task = json.loads(json.dumps(env.task))
    task["status"] = "verified"
    fake = _FakeAdvanceStore(task)
    monkeypatch.setattr(env.service, "_store", lambda *, push=True: fake)

    calls = []

    def fake_replay(root, config, replayed, *, dry_run=False, store_commit=None):
        calls.append(
            {"task_id": replayed["id"], "dry_run": dry_run, "store_commit": store_commit}
        )
        return {
            "status": "sent",
            "delivered": True,
            "target": "http://192.168.1.3:8789/v1/handoff/completed",
            "event_id": "01" * 15,
            "revision": replayed["revision"],
        }

    monkeypatch.setattr(notification, "replay_task_completed", fake_replay)
    status, body = _post(f"{env.base_url}/api/tasks/{env.task['id']}/resend-notify", {})

    assert status == 200
    assert body["ok"] is True
    assert body["status"] == "sent"
    assert body["target"] == "http://192.168.1.3:8789/v1/handoff/completed"
    assert calls == [
        {"task_id": env.task["id"], "dry_run": False, "store_commit": "e" * 40}
    ]


def test_advance_push_pushes_current_branch(env, tmp_path):
    remote = make_bare_remote(tmp_path, "adv", {"README.md": "# x\n"}, branch="dev")
    work = tmp_path / "adv-work"
    git(tmp_path, "clone", "-b", "dev", str(remote), str(work))
    (work / "NEW.txt").write_text("local change\n", encoding="utf-8")
    git(work, "add", "NEW.txt")
    git(work, "commit", "-m", "local change")
    sha = git(work, "rev-parse", "HEAD")

    steps = []
    branch, pushed_sha = env.service._advance_push(
        {"result": {"worktree": str(work)}}, steps
    )
    assert branch == "dev"
    assert pushed_sha == sha
    assert git(remote, "rev-parse", "dev") == sha
    assert steps and "git push" in steps[0]


class _VersionHandler(BaseHTTPRequestHandler):
    """测试用版本接口：固定返回一个 deploy SHA，供自动验收对齐。"""

    sha = "d" * 40

    def do_GET(self):  # noqa: N802
        if self.path == "/version":
            body = json.dumps({"sha": type(self).sha, "environment": "dev"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass


def _start_version_server(monkeypatch):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _VersionHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(_VersionHandler, "sha", "d" * 40)
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _direct_repo_with_origin(tmp_path):
    remote = make_bare_remote(tmp_path, "autover", {"README.md": "# api\n"}, branch="dev")
    direct = tmp_path / "autover-work"
    git(tmp_path, "clone", "-b", "dev", str(remote), str(direct))
    return direct, remote


def test_direct_wait_deployment_auto_verifies_after_delivery(env, tmp_path, monkeypatch):
    """目录模式开了 wait_deployment：交付后自动验收，一路推进到 verified。

    这是「GitHub Actions 跑完就自动推进、不用人点」的回归用例：
    修前目录模式把 wait_deployment 强制关成 False，且没有自动触发验收，
    任务永远停在 delivered；修后应自动走到 verified。
    """
    httpd, env_url = _start_version_server(monkeypatch)
    try:
        # 让任务的目标分支提交被认定已包含 dev 上观测到的 sha
        monkeypatch.setattr(
            CommitAncestry, "contains", lambda _self, target, observed: (True, None)
        )
        # 把 dev 环境指向本地版本服务器（EnvConfig 是 frozen，用 object.__setattr__ 改）
        object.__setattr__(env.config.environments["dev"], "url", env_url)

        direct, remote = _direct_repo_with_origin(tmp_path)
        env.service._runner_overrides["zcode"] = FakeZcodeCommit(
            files={"src/orders/Status.java": "enum Status { OPEN }\n"},
            commit_message="feat: auto verify",
        )
        status, _body = _post(
            f"{env.base_url}/api/tasks/{env.task['id']}/run",
            {"executor": "zcode", "work_dir": str(direct), "auto_commit": True, "wait_deployment": True},
        )
        assert status == 200
        _wait_run_done(env.service)

        current = env.store.peek(env.task["id"])
        assert current["status"] == "verified", (
            current["status"],
            current.get("failure"),
            env.service.state()["active"].get("logs", [])[-20:],
        )
        assert current["deployment"]["status"] == "deployed"
        deliver_state = env.service.state()["deliver"]
        assert deliver_state and deliver_state.get("auto") is True
        assert deliver_state["result"]["result"] == "verified"
        active_logs = env.service.state()["active"]["logs"]
        assert any("自动验收通过" in line for line in active_logs)
    finally:
        httpd.shutdown()


def test_direct_wait_deployment_false_stays_delivered(env, tmp_path, monkeypatch):
    """关掉 wait_deployment 时不触发自动验收，交付后仍停在 delivered。"""
    direct, remote = _direct_repo_with_origin(tmp_path)
    env.service._runner_overrides["zcode"] = FakeZcodeCommit(
        files={"src/orders/Status.java": "enum Status { OPEN }\n"},
        commit_message="feat: no verify",
    )
    status, _body = _post(
        f"{env.base_url}/api/tasks/{env.task['id']}/run",
        {"executor": "zcode", "work_dir": str(direct), "auto_commit": True, "wait_deployment": False},
    )
    assert status == 200
    _wait_run_done(env.service)

    current = env.store.peek(env.task["id"])
    assert current["status"] == "delivered", current.get("failure")
    assert current["deployment"]["status"] in (None, "pending")


def test_direct_without_auto_commit_skips_auto_verify_instead_of_failing(
    env, tmp_path, monkeypatch
):
    """目录模式没开自动提交：没有可对齐的目标，自动验收应明确跳过而不是报错。

    回归：以前这里照样调 cmd_deliver，它只能抛「任务缺少 merge_sha/head_sha，
    无法对齐部署」，面板上显示成一次验收失败——其实什么都没做错，只是改动
    还没提交。状态应留在 delivered，deliver 记录给出可照做的原因。
    """
    direct, remote = _direct_repo_with_origin(tmp_path)
    env.service._runner_overrides["zcode"] = FakeZcodeCommit(
        files={"src/orders/Status.java": "enum Status { OPEN }\n"},
        commit_message=None,
    )
    status, _body = _post(
        f"{env.base_url}/api/tasks/{env.task['id']}/run",
        {
            "executor": "zcode",
            "work_dir": str(direct),
            "auto_commit": False,
            "wait_deployment": True,
        },
    )
    assert status == 200
    _wait_run_done(env.service)

    current = env.store.peek(env.task["id"])
    # 交付本身成功，只是没提交
    assert current["status"] == "delivered", current.get("failure")
    assert not (current.get("result") or {}).get("head_sha")

    deliver_state = env.service.state()["deliver"]
    assert deliver_state and deliver_state.get("auto") is True
    # 不是错误：是「没有可验证的目标」这一明确结论
    assert deliver_state.get("error") is None
    assert deliver_state["result"]["result"] == "no_target"
    assert "提交" in deliver_state["result"]["message"]

    active_logs = env.service.state()["active"]["logs"]
    assert any("跳过自动验收" in line for line in active_logs)
    assert not any("自动验收失败" in line for line in active_logs)


def test_verify_mutex_blocks_concurrent_advance_and_deliver(env, monkeypatch):
    """恢复推进与手动验收并发时，第二个入口必须被挡住。

    这是「deployed → verified 非法跳转」的回归：两个 cmd_deliver 同时跑，
    后写者拿旧 revision 直接写 verified，撞状态机。现在面板内验收入口
    共用非阻塞互斥锁，并发的第二个入口立刻报冲突，不会破坏任务状态。
    """
    import handoff.cli as cli

    entered = threading.Event()
    release = threading.Event()

    def blocking_deliver(args, config):
        entered.set()
        release.wait(timeout=10)
        return {"id": args.task_id, "result": "verified"}

    monkeypatch.setattr(cli, "cmd_deliver", blocking_deliver)
    # 让 advance 的状态检查通过（任务须处于可恢复推进的交付态）
    fake = _FakeAdvanceStore(_delivered_task(env))
    monkeypatch.setattr(env.service, "_store", lambda *, push=True: fake)

    # 先起一个 deliver（后台线程，cmd_deliver 会阻塞）
    env.service.deliver(env.task["id"])
    assert entered.wait(timeout=5), "第一个验收没启动"

    try:
        # 第二个入口（恢复推进）应被互斥锁挡住，立刻冲突
        with pytest.raises(Exception) as ei:
            env.service.advance(env.task["id"])
        assert "已有部署/验收在进行" in str(ei.value)
    finally:
        release.set()
        # 等第一个 deliver 收尾并释放锁
        for _ in range(100):
            if env.service.state()["deliver"] and env.service.state()["deliver"].get("done"):
                break
            time.sleep(0.02)

    # 锁已释放后，新的验收可以正常启动（不再报冲突）
    monkeypatch.setattr(
        cli, "cmd_deliver", lambda args, config: {"id": args.task_id, "result": "verified"}
    )
    assert env.service.state()["deliver"]["done"] is True


def test_tasks_endpoint_paginates(env):
    """/api/tasks 支持 page/page_size 分页，且保留 tasks 兼容字段。"""
    from handoff import ids

    base_sha = env.task["target"]["base_sha"]
    # 再建 4 个任务（连同 env 自带的共 5 个）， created_at 递增，最新的排最前
    extra = [ids.task_id() for _ in range(4)]
    for tid in extra:
        env.store.create(make_task(tid, base_sha=base_sha))
    for i, tid in enumerate(extra):
        path = env.work / "tasks" / f"{tid}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        ts = f"2026-09-0{i + 1}T00:00:00Z"
        data["created_at"] = ts
        data["updated_at"] = ts
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # 第 1 页 2 条
    p1 = _get(f"{env.base_url}/api/tasks?page=1&page_size=2")
    assert p1["total"] == 5 and p1["pages"] == 3
    assert p1["page"] == 1 and p1["page_size"] == 2
    assert len(p1["tasks"]) == 2 and len(p1["items"]) == 2
    assert p1["tasks"] == p1["items"]  # 兼容字段与分页字段一致

    # 第 3 页只剩 1 条
    p3 = _get(f"{env.base_url}/api/tasks?page=3&page_size=2")
    assert p3["page"] == 3 and len(p3["tasks"]) == 1

    # 越界页码回退到最后一页
    p9 = _get(f"{env.base_url}/api/tasks?page=9&page_size=2")
    assert p9["page"] == 3 and len(p9["tasks"]) == 1

    # 非法参数回退默认（page_size 默认 20 → 一页装下全部）
    bad = _get(f"{env.base_url}/api/tasks?page=abc&page_size=-1")
    assert bad["page"] == 1 and bad["page_size"] == 20
    assert len(bad["tasks"]) == 5

    # page_size 上限 100
    big = _get(f"{env.base_url}/api/tasks?page_size=9999")
    assert big["page_size"] == 100


def test_auto_mode_persists_model_and_effort(env):
    """自动配置能保存执行器模型/思考程度，空串清洗为 None（跟随配置）。"""
    status, body = _post(
        f"{env.base_url}/api/mode",
        {"mode": "manual", "auto": {"model": "gpt-6.1", "effort": "high"}},
    )
    assert status == 200
    assert body["auto"]["model"] == "gpt-6.1"
    assert body["auto"]["effort"] == "high"

    saved = json.loads((env.work / ".handoff-run" / "panel-state.json").read_text(encoding="utf-8"))
    assert saved["auto"]["model"] == "gpt-6.1"
    assert saved["auto"]["effort"] == "high"

    # 留空 = 跟随配置，不留下空字符串；非法类型被拒
    status, body = _post(
        f"{env.base_url}/api/mode",
        {"mode": "manual", "auto": {"model": "  ", "effort": ""}},
    )
    assert status == 200
    assert body["auto"]["model"] is None
    assert body["auto"]["effort"] is None

    status, _body = _post(f"{env.base_url}/api/mode", {"mode": "manual", "auto": {"model": 123}})
    assert status >= 400


def test_auto_loop_passes_model_and_effort_to_run_task(env, monkeypatch):
    """auto 模式把配置的模型/思考程度透传给执行器。"""
    calls = []
    done = threading.Event()

    def fake_run_task(task_id, **kw):
        calls.append(kw)
        done.set()
        return {"task_id": task_id}

    monkeypatch.setattr(env.service, "run_task", fake_run_task)
    monkeypatch.setattr(env.service, "_pick_open_task", lambda: env.task["id"])
    monkeypatch.setattr(env.service, "_auto_busy_task", lambda: None)
    monkeypatch.setattr(env.service, "_auto_resume_session", lambda *a, **k: None)
    try:
        env.service.set_mode({
            "mode": "auto",
            "auto": {"model": "gpt-6.1", "effort": "xhigh"},
        })
        assert done.wait(5), "自动循环没有调用 run_task"
        kw = calls[0]
        assert kw["model"] == "gpt-6.1"
        assert kw["effort"] == "xhigh"
        assert kw["trigger"] == "auto"
    finally:
        env.service.set_mode({"mode": "manual"})


# -- 交付链自动推进 ---------------------------------------------------------


def test_auto_advance_candidates_selection(env):
    from datetime import timedelta

    from handoff.timeutil import iso, now

    service = env.service
    current = now()
    fresh = iso(current)
    stale = iso(current - timedelta(seconds=600))

    def task(tid, status, updated, *, pr=None, head="a" * 40, checks=None, merge=None):
        result = {"pr_number": pr, "head_sha": head, "merge_sha": merge, "checks": checks or []}
        return {"id": tid, "status": status, "updated_at": updated, "result": result}

    # 刚交付的先不动：运行收尾的自动验收可能正在路上
    assert service._auto_advance_candidates([task("t1", "delivered", fresh)], current) == []
    # 停够 90s 的 delivered（目录模式：无 PR、有 head_sha）→ 接管
    assert service._auto_advance_candidates([task("t2", "delivered", stale)], current) == ["t2"]
    # 目录模式没有提交：没有可对齐的部署目标，不接管
    assert (
        service._auto_advance_candidates([task("t3", "delivered", stale, head=None, merge=None)], current) == []
    )
    # 带 PR 且 auto_merge 开启 → 接管（走自动合并）；关掉则不碰
    assert service._auto_advance_candidates([task("t4", "delivered", stale, pr=7)], current) == ["t4"]
    service._panel_state["auto"]["auto_merge"] = False
    try:
        assert service._auto_advance_candidates([task("t5", "delivered", stale, pr=7)], current) == []
    finally:
        service._panel_state["auto"]["auto_merge"] = True
    # merged / deployed 停够时间 → 接管
    assert service._auto_advance_candidates([task("t6", "merged", stale), task("t7", "deployed", stale)], current) == [
        "t6",
        "t7",
    ]
    # verifying：checks 落盘了=验收线程活着，不碰；没落盘但停够 10min=线程死了，接管
    assert (
        service._auto_advance_candidates(
            [task("t8", "verifying", stale, checks=[{"id": "x", "status": "passed"}])], current
        )
        == []
    )
    fresh_verifying = iso(current - timedelta(seconds=120))
    assert service._auto_advance_candidates([task("t9", "verifying", fresh_verifying)], current) == []
    assert service._auto_advance_candidates([task("t10", "verifying", stale)], current) == ["t10"]
    # 冷却中的任务不重复接管
    service._sweep_cooldown["t7"] = current
    try:
        assert service._auto_advance_candidates([task("t7", "deployed", stale)], current) == []
    finally:
        service._sweep_cooldown.clear()


def test_auto_advance_sweep_uses_advance_and_records_history(env):
    from handoff.panel.service import PanelService  # noqa: F401  确认可导入即可

    service = env.service
    fired = service.auto_advance_sweep()
    # fixture 里只有一个 open 任务：本轮不应有任何可推进对象，也不该留下报错记录
    assert fired == []
    assert all(record.get("error") is None for record in service._sweep_history)
