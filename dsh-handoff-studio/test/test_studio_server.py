"""端到端：studio 引擎的 JSON API 形状必须与原面板一致。

起一个**真的** studio 服务（随机端口），用 http.client 打，验证：
    - 路由与原来同名同形（这样新界面就是原面板的等价重写）
    - 面板能拿到任务列表 / 详情 / 状态 / 模型目录
    - dsh 出现在执行器表里，且默认就是它

不连 DSH：`dsh` 的健康检查会失败（插件没在这个进程里），这是预期结果，
正好也验证了「插件不可用不阻止面板工作」。
"""

from __future__ import annotations

import json
import shutil
import sys
from http.client import HTTPConnection
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = Path(__file__).resolve().parents[1] / "engine"
for extra in (str(REPO_ROOT / "src"), str(ENGINE_DIR)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from handoff.config import Config  # noqa: E402
from handoff_studio.server import StudioServer  # noqa: E402
from handoff_studio.service import StudioService  # noqa: E402


@pytest.fixture()
def live_server(tmp_path: Path, monkeypatch):
    root = tmp_path / "store"
    root.mkdir()
    for name in ("config", "schemas"):
        shutil.copytree(REPO_ROOT / name, root / name)
    for name in ("tasks", "events", "contracts"):
        (root / name).mkdir()

    # 一个 open 任务，好让列表/详情有东西可看
    task = {
        "schema_version": 1,
        "id": "ho-20260101-TESTTASK0000000000000001",
        "revision": 1,
        "attempt": 0,
        "title": "示例任务",
        "source": {"role": "frontend", "repo": "example-frontend", "commit_sha": "0" * 40},
        "target": {"role": "backend", "repo": "example-backend", "base_ref": "dev"},
        "requirement": "写一个接口",
        "contract": None,
        "repro": None,
        "scope": {"out_of_scope": [], "allowed_paths": ["**"], "forbidden_paths": []},
        "acceptance": {"environment": "dev", "checks": []},
        "migration": {
            "required": False,
            "backward_compatible": True,
            "plan": "",
            "rollback": "",
            "manual_approval_required": False,
        },
        "status": "open",
        "claim": None,
        "result": {
            "branch": None,
            "head_sha": None,
            "pr_url": None,
            "pr_number": None,
            "merge_sha": None,
            "codex_run_id": None,
            "codex_cli_version": None,
            "prompt_version": None,
            "diff_summary": None,
            "changed_paths": [],
            "worktree": None,
            "codex_thread_id": None,
            "checks": [],
            "notes": None,
        },
        "deployment": {
            "status": "pending",
            "target_sha": None,
            "observed_sha": None,
            "environment": "dev",
            "url": None,
            "updated_at": None,
            "error": None,
        },
        "failure": None,
    }
    (root / "tasks" / f"{task['id']}.json").write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setenv("HANDOFF_STUDIO_URL", "http://127.0.0.1:59999/handoff-studio")
    config = Config.load(root)
    service = StudioService(root, config)
    service.remember_catalog(
        {
            "default": {"provider": "workbuddy", "model": "deepseek-v4.1-flash", "reasoningEffort": "high"},
            "groups": [
                {
                    "id": "workbuddy",
                    "name": "WorkBuddy",
                    "models": [
                        {
                            "id": "deepseek-v4.1-flash",
                            "name": "DeepSeek-V4.1-Flash",
                            "reasoning": {"efforts": [{"id": "low"}, {"id": "high"}]},
                        }
                    ],
                }
            ],
        }
    )
    server = StudioServer(service, host="127.0.0.1", port=0)
    host, port = server.start()
    yield {"service": service, "host": host, "port": port, "task_id": task["id"]}
    server.stop()


def call(port, method, path, body=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=15)
    raw = None if body is None else json.dumps(body, ensure_ascii=False)
    headers = {"accept": "application/json"}
    if raw is not None:
        headers["content-type"] = "application/json; charset=utf-8"
    conn.request(method, path, body=raw, headers=headers)
    response = conn.getresponse()
    payload = response.read().decode("utf-8")
    conn.close()
    return response.status, (json.loads(payload) if payload else None)


def test_tasks_route_is_paginated(live_server):
    status, body = call(live_server["port"], "GET", "/api/tasks?page=1&page_size=10")
    assert status == 200
    # 与原面板同形的分页元数据
    for key in ("count", "total", "tasks", "items", "page", "page_size", "pages"):
        assert key in body, key
    assert body["page"] == 1
    assert body["page_size"] == 10
    assert body["pages"] >= 1
    assert body["count"] == body["total"]


def test_page_size_is_capped(live_server):
    status, body = call(live_server["port"], "GET", "/api/tasks?page_size=9999")
    assert status == 200
    assert body["page_size"] == 100


def test_bad_page_falls_back_to_one(live_server):
    status, body = call(live_server["port"], "GET", "/api/tasks?page=0&page_size=abc")
    assert status == 200
    assert body["page"] == 1
    assert body["page_size"] == 20


def test_run_options_route(live_server):
    status, body = call(live_server["port"], "POST", "/api/studio/options", {})
    assert status == 200
    ids = [item["id"] for item in body["executors"]]
    assert ids == ["dsh", "codex", "zcode"]
    dsh = body["executors"][0]
    assert dsh["label"] == "DSH Agent"
    assert dsh["models"][0]["id"] == "deepseek-v4.1-flash"
    assert dsh["efforts"] == ["low", "high"]
    # 原执行器的选项也都在
    assert "codex_sandbox_options" in body
    assert "permission_modes" in body
    assert "projects" in body
    assert "environments" in body


def test_run_options_follow_a_model_switch(live_server):
    status, body = call(live_server["port"], "POST", "/api/studio/options", {"model": "deepseek-v4.1-flash"})
    assert status == 200
    assert body["executors"][0]["selected_model"] == "deepseek-v4.1-flash"
    assert body["executors"][0]["efforts"] == ["low", "high"]


def test_security_mode_route(live_server):
    status, body = call(
        live_server["port"], "POST", "/api/runs/security", {"codex_sandbox": "read-only", "apply_now": False}
    )
    assert status == 200
    assert body["security"]["codex_sandbox"] == "read-only"


def test_security_mode_rejects_invalid(live_server):
    status, body = call(live_server["port"], "POST", "/api/runs/security", {"codex_sandbox": "nope"})
    assert status == 400


def test_mode_route_round_trips(live_server):
    status, body = call(live_server["port"], "POST", "/api/mode", {"mode": "auto"})
    assert status == 200
    assert body["mode"] == "auto"
    status, body = call(live_server["port"], "POST", "/api/mode", {"mode": "manual"})
    assert status == 200
    assert body["mode"] == "manual"


def test_mode_route_rejects_unknown(live_server):
    status, body = call(live_server["port"], "POST", "/api/mode", {"mode": "turbo"})
    assert status == 400


def test_create_task_route_validates_input(live_server):
    status, body = call(live_server["port"], "POST", "/api/tasks/create", {"title": "", "requirement": "x"})
    assert status == 400
    status, body = call(
        live_server["port"], "POST", "/api/tasks/create", {"title": "t", "requirement": "r", "repo": "nope"}
    )
    assert status == 400
    assert "未注册" in body["message"]


def test_resend_notify_route_exists(live_server):
    # verified / failed 才有通知可发；这里只验证路由接通、错误可读
    status, body = call(
        live_server["port"], "POST", f"/api/tasks/{live_server['task_id']}/resend-notify", {}
    )
    assert status != 404


def test_deliver_route_starts_background_verification(live_server):
    # deliver 是后台任务：接口立刻返回 started，结果写进 state 的 deliver 面板块。
    # 与原面板一致——同步等会让请求挂住几分钟。
    status, body = call(live_server["port"], "POST", "/api/deliver", {"task_id": live_server["task_id"]})
    assert status == 200
    assert body["task_id"] == live_server["task_id"]
    assert body["done"] is False


def test_state_route_shape(live_server):
    status, body = call(live_server["port"], "GET", "/api/state")
    assert status == 200
    # 原面板 /api/state 的关键字段一个不少
    for key in ("mode", "auto", "active", "executors", "projects", "store_root", "now"):
        assert key in body, key
    assert body["default_executor"] == "dsh"
    assert body["executors_order"] == ["dsh", "codex", "zcode"]
    # 插件没装时 dsh 健康检查失败，但服务照常响应
    assert body["executors"]["dsh"]["ok"] is False
    # studio 自己的模型信息
    assert body["studio"]["model"] == "deepseek-v4.1-flash"
    assert body["studio"]["efforts"] == ["low", "high"]


def test_tasks_route_lists_the_task(live_server):
    status, body = call(live_server["port"], "GET", "/api/tasks")
    assert status == 200
    ids = [task["id"] for task in body["tasks"]]
    assert live_server["task_id"] in ids
    brief = next(task for task in body["tasks"] if task["id"] == live_server["task_id"])
    assert brief["status"] == "open"
    assert brief["title"] == "示例任务"


def test_task_detail_and_events(live_server):
    status, body = call(live_server["port"], "GET", f"/api/tasks/{live_server['task_id']}")
    assert status == 200
    assert body["task"]["id"] == live_server["task_id"]
    assert isinstance(body["events"], list)


def test_logs_route_is_empty_but_valid(live_server):
    status, body = call(live_server["port"], "GET", f"/api/logs?task={live_server['task_id']}")
    assert status == 200
    assert body["attempts"] == []
    assert body["files"] == []


def test_studio_models_route(live_server):
    status, body = call(live_server["port"], "GET", "/api/studio/models")
    assert status == 200
    assert body["models"][0]["id"] == "deepseek-v4.1-flash"
    assert body["route"] == "http://127.0.0.1:59999/handoff-studio"


def test_select_model_route(live_server):
    status, body = call(
        live_server["port"],
        "POST",
        "/api/studio/select",
        {"model": "deepseek-v4.1-flash", "effort": "low"},
    )
    assert status == 200
    assert body["model"] == "deepseek-v4.1-flash"
    assert body["effort"] == "low"


def test_select_model_rejects_unsupported_effort(live_server):
    status, body = call(
        live_server["port"],
        "POST",
        "/api/studio/select",
        {"model": "deepseek-v4.1-flash", "effort": "max"},
    )
    assert status == 400
    assert "不支持思考程度" in body["message"]


def test_invalid_task_id_is_rejected(live_server):
    status, body = call(live_server["port"], "GET", "/api/tasks/../evil")
    assert status in (400, 404)


def test_unknown_route_is_404(live_server):
    status, body = call(live_server["port"], "GET", "/api/nope")
    assert status == 404


def test_run_without_executor_defaults_to_dsh(live_server):
    """执行入口默认 dsh：插件没装时要给出能看懂的错，而不是静默不动。"""
    status, body = call(
        live_server["port"],
        "POST",
        f"/api/tasks/{live_server['task_id']}/run",
        {},
    )
    # 认领会真去打远端仓库；这里只要求「不是 404 路由错误」且带明确原因
    assert status != 404
    assert isinstance(body, dict)


def test_session_enumeration_for_dsh_is_empty_not_an_error(live_server):
    status, body = call(live_server["port"], "GET", f"/api/tasks/{live_server['task_id']}/sessions?executor=dsh")
    assert status == 200
    assert body["sessions"] == []


def test_session_enumeration_rejects_unknown_executor(live_server):
    status, body = call(live_server["port"], "GET", "/api/sessions/nope")
    assert status == 400
    assert "未知执行器" in body["message"]
