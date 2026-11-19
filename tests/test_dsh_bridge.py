"""DSH 前端：完成事件经本机桥接插件排回原会话。

桥接插件的 HTTP 服务不在 Python 侧，所以这里用一个假桥接对象代替真实插件，
断言的是「哪些参数被投递了」以及「失败后如何退避重试」。
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from handoff import notification
from handoff.config import NotificationConfig
from handoff.dsh_bridge import BridgeOrigin, DshBridge, bridge_token, bridge_url, request_id_for
from handoff.errors import ConfigError, ExecutionError
from handoff.frontend_session import FrontendDispatcher


SECRET = "frontend-session-secret-at-least-32-bytes"
TASK_ID = "ho-20261111-01M7457KQ5370WY645XZQXF57M"


def config(tmp_path, **overrides):
    base = NotificationConfig(
        secret_env="HANDOFF_FRONTEND_SESSION_SECRET",
        outbox_database=str(tmp_path / "outbox.sqlite3"),
        inbox_database=str(tmp_path / "inbox.sqlite3"),
        session_database=str(tmp_path / "sessions.sqlite3"),
        listen_host="127.0.0.1",
        listen_port=0,
        request_timeout_seconds=2,
        retry_initial_seconds=0.01,
        retry_max_seconds=0.1,
    )
    return replace(base, **overrides)


def completed():
    return notification.task_completed_payload(
        task={
            "id": TASK_ID,
            "revision": 8,
            "status": "verified",
            "source": {"repo": "org/web"},
            "deployment": {"observed_sha": "b" * 40},
        },
        store_commit="c" * 40,
    )


class FakeBridge:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def post(self, session_id, text, *, request_id, mode="queue"):
        self.calls.append(
            {"session_id": session_id, "text": text, "request_id": request_id, "mode": mode}
        )
        if self.error is not None:
            raise self.error
        return {"accepted": True}


def test_dsh_completion_posts_to_bridge_and_marks_dispatched(tmp_path):
    bridge = FakeBridge()
    dispatcher = FrontendDispatcher(tmp_path, config(tmp_path), bridge=bridge)
    dispatcher.register(TASK_ID, thread_id="session-abc", workspace=tmp_path, executor="dsh")
    event_id = "evt-1"
    payload = completed()
    payload["event_id"] = event_id
    dispatcher.enqueue(payload)

    result = dispatcher.send_due()

    assert result == {"sent": 1, "failed": 0, "pending": 0, "errors": []}
    assert len(bridge.calls) == 1
    call = bridge.calls[0]
    assert call["session_id"] == "session-abc"
    assert call["request_id"] == request_id_for(event_id)
    assert call["mode"] == "queue"
    assert TASK_ID in call["text"]
    assert "handoffctl show" in call["text"]
    # 事件幂等：同一条完成事件不会再排一次。
    assert dispatcher.send_due()["sent"] == 0
    assert len(bridge.calls) == 1


def test_dsh_bridge_failure_keeps_event_pending(tmp_path):
    bridge = FakeBridge(error=ExecutionError("DSH 桥接返回 HTTP 500"))
    dispatcher = FrontendDispatcher(tmp_path, config(tmp_path), bridge=bridge)
    dispatcher.register(TASK_ID, thread_id="session-abc", workspace=tmp_path, executor="dsh")
    dispatcher.enqueue(completed())

    result = dispatcher.send_due()

    assert result["sent"] == 0
    assert result["failed"] == 1
    assert result["pending"] == 1
    assert "HTTP 500" in result["errors"][0]["error"]


def test_register_defaults_to_codex_and_rejects_unknown_executor(tmp_path):
    dispatcher = FrontendDispatcher(tmp_path, config(tmp_path))
    dispatcher.register(TASK_ID, thread_id="thread-original", workspace=tmp_path)
    row = dispatcher.sessions.session(TASK_ID)
    assert row["executor"] == "codex"

    with pytest.raises(ConfigError):
        dispatcher.register(TASK_ID, thread_id="x", workspace=tmp_path, executor="claude")


def test_legacy_database_rows_treated_as_codex(tmp_path):
    """老库没有 executor 列；补列后按 codex 解释。"""
    import sqlite3

    path = tmp_path / "sessions.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            task_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            workspace TEXT NOT NULL,
            queue_remote TEXT,
            queue_remote_auth_token_env TEXT,
            registered_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions VALUES(?,?,?,?,?,?,?)",
        (TASK_ID, "thread-original", str(tmp_path), None, None, "t", "t"),
    )
    conn.commit()
    conn.close()

    dispatcher = FrontendDispatcher(tmp_path, config(tmp_path))
    row = dispatcher.sessions.session(TASK_ID)
    assert row["executor"] == "codex"
    assert row["thread_id"] == "thread-original"


def test_bridge_rebuilds_when_endpoint_port_changes(tmp_path, monkeypatch):
    """DSH 重启换端口后，长驻 listener 应在下次投递时改用新端点。"""
    import handoff.frontend_session as fs

    url_holder = {"url": "http://127.0.0.1:64633/v1/handoff/dsh/prompt"}

    def fake_discover(explicit=None, *, path=None):
        return BridgeOrigin(url_holder["url"], "endpoint")

    monkeypatch.setattr(fs, "discover_bridge", fake_discover)
    # 可管理实例（无注入 bridge）：由 resolver 按当前 url 造实例。
    resolver_calls = []

    def resolver():
        resolver_calls.append(url_holder["url"])
        return FakeBridge()

    dispatcher = FrontendDispatcher(tmp_path, config(tmp_path), bridge_resolver=resolver)
    first = dispatcher.bridge()
    assert first is dispatcher.bridge()  # url 没变时复用缓存
    assert len(resolver_calls) == 1

    # 端点文件刷新到新端口。
    url_holder["url"] = "http://127.0.0.1:62508/v1/handoff/dsh/prompt"
    second = dispatcher.bridge()
    assert second is not first  # 重建了实例
    assert resolver_calls[-1].endswith(":62508/v1/handoff/dsh/prompt")


def test_bridge_injected_is_not_rebuilt(tmp_path, monkeypatch):
    """显式注入的 bridge 应原样保留（测试/特殊部署）。"""
    import handoff.frontend_session as fs

    def fake_discover(explicit=None, *, path=None):
        return BridgeOrigin("http://127.0.0.1:62508/v1/handoff/dsh/prompt", "endpoint")

    monkeypatch.setattr(fs, "discover_bridge", fake_discover)
    bridge = FakeBridge()
    dispatcher = FrontendDispatcher(tmp_path, config(tmp_path), bridge=bridge)
    assert dispatcher.bridge() is bridge


def test_dsh_send_retries_once_with_fresh_bridge_on_timeout(tmp_path):
    """连接超时是端点漂移的典型表现：重建桥接后用稳定 requestId 重投一次。"""
    from handoff.dsh_bridge import request_id_for as rid

    cfg = config(tmp_path)
    dispatcher = FrontendDispatcher(tmp_path, cfg)
    dispatcher.register(TASK_ID, thread_id="session-abc", workspace=tmp_path, executor="dsh")

    timeouts = FakeBridge(error=ExecutionError("请求失败 http://127.0.0.1:64633/...: timed out"))
    healthy = FakeBridge()
    built = iter([timeouts, healthy])

    def resolver():
        return next(built)

    dispatcher._bridge_resolver = resolver
    dispatcher._bridge_manageable = False  # 避免 bridge() 走端点发现重建
    dispatcher._bridge = resolver()  # 已用掉 timeouts

    event_id = "evt-timeout"
    payload = completed()
    payload["event_id"] = event_id
    dispatcher.enqueue(payload)

    result = dispatcher.send_due()
    assert result["sent"] == 1
    assert result["failed"] == 0
    assert len(timeouts.calls) == 1
    assert len(healthy.calls) == 1
    assert healthy.calls[0]["request_id"] == rid(event_id)


def test_bridge_url_defaults_to_dsh_web_url(tmp_path, monkeypatch):
    # 隔离 $DSH_HOME：否则会读到本机真实插件公布的端点。
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    monkeypatch.setenv("DSH_WEB_URL", "http://127.0.0.1:56378")
    assert bridge_url(None) == "http://127.0.0.1:56378/v1/handoff/dsh/prompt"


def test_bridge_url_rejects_remote_host(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    monkeypatch.delenv("DSH_WEB_URL", raising=False)
    with pytest.raises(ConfigError):
        bridge_url("http://10.1.2.3:56378/v1/handoff/dsh")


def test_bridge_url_requires_a_hint(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    monkeypatch.delenv("DSH_WEB_URL", raising=False)
    with pytest.raises(ConfigError):
        bridge_url(None)


def test_bridge_token_reads_file(tmp_path, monkeypatch):
    monkeypatch.delenv("HANDOFF_DSH_BRIDGE_TOKEN", raising=False)
    token_file = tmp_path / "handoff-bridge.token"
    token_file.write_text("s3cret\n", encoding="utf-8")
    assert bridge_token(file=token_file) == "s3cret"


def test_bridge_token_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_DSH_BRIDGE_TOKEN", "from-env")
    token_file = tmp_path / "handoff-bridge.token"
    token_file.write_text("s3cret\n", encoding="utf-8")
    assert bridge_token(file=token_file) == "from-env"


def _fake_client(status: int, payload: str):
    """返回一个固定响应的假 HttpClient。

    注意参数别叫 `body`：`request()` 的 `body` 关键字是**请求体**，会遮蔽它。
    """

    class Client:
        def request(self, method, url, *, headers=None, body=None, max_redirects=3):
            class Response:
                pass

            response = Response()
            response.status = status
            response.body = payload
            response.json = lambda: json.loads(payload)
            return response

    return Client()


def test_bridge_reports_missing_session_separately():
    """会话不存在（404 + session/not-found）和插件没启用要分开报。"""
    client = _fake_client(404, '{"error":"session \\"s\\" not found","code":"session/not-found"}')
    bridge = DshBridge("http://127.0.0.1:1/v1/handoff/dsh/prompt", "tok", client=client)
    with pytest.raises(ExecutionError) as info:
        bridge.post("session-gone", "hi", request_id="handoff-x")
    assert "会话不存在" in info.value.message
    assert "session-gone" in info.value.message


def test_bridge_reports_missing_route_on_404():
    """空路由 404（body 里没有 session/not-found）说明插件没启用。"""
    client = _fake_client(404, '{"error":"unknown bridge endpoint"}')
    bridge = DshBridge("http://127.0.0.1:1/v1/handoff/dsh/prompt", "tok", client=client)
    with pytest.raises(ExecutionError) as info:
        bridge.post("session-x", "hi", request_id="handoff-x")
    assert "插件" in info.value.message


def test_bridge_reports_missing_route_on_405():
    """插件没启用时 POST 落到 SPA fallback，回 405 而不是 404。"""
    client = _fake_client(405, "")
    bridge = DshBridge("http://127.0.0.1:1/v1/handoff/dsh/prompt", "tok", client=client)
    with pytest.raises(ExecutionError) as info:
        bridge.post("session-x", "hi", request_id="handoff-x")
    assert "插件" in info.value.message


def test_bridge_posts_expected_envelope(tmp_path):
    seen = {}

    class Client:
        def request(self, method, url, *, headers=None, body=None, max_redirects=3):
            seen.update({"method": method, "url": url, "headers": headers, "body": body})

            class Response:
                status = 200
                body = '{"accepted": true}'

                def json(self):
                    return json.loads(self.body)

            return Response()

    bridge = DshBridge("http://127.0.0.1:56378/v1/handoff/dsh/prompt", "tok", client=Client())
    bridge.post("session-abc", "hello", request_id="handoff-evt-1")

    assert seen["method"] == "POST"
    assert seen["headers"]["Authorization"] == "Bearer tok"
    body = json.loads(seen["body"])
    assert body == {
        "sessionId": "session-abc",
        "requestId": "handoff-evt-1",
        "mode": "queue",
        "text": "hello",
    }
