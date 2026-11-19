"""HttpClient 连接错误的有限重试：网络抖动不该一次就判任务死刑。"""

from __future__ import annotations

from urllib.error import URLError

import pytest

from handoff.errors import ExecutionError
from handoff.http_client import HttpClient, Response


def test_retries_connection_error_then_succeeds(monkeypatch):
    """SSL 握手超时这类连接错误重试后成功，不抛异常。"""
    client = HttpClient(retries=2, retry_backoff=0.5, allow_private=True)
    attempts = {"n": 0}
    sleeps: list[float] = []
    monkeypatch.setattr(client, "_sleep", lambda s: sleeps.append(s))

    def flaky(_req, _current):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise URLError("handshake operation timed out")
        return Response(status=200, url="https://x/version", headers={}, body='{"sha": "' + "a" * 40 + '"}')

    monkeypatch.setattr(client, "_request_once", flaky)
    resp = client.request("GET", "https://x/version")
    assert resp.status == 200
    assert attempts["n"] == 3
    assert sleeps == [0.5, 1.0]  # 指数退避


def test_gives_up_after_retries_with_clear_error(monkeypatch):
    """重试耗尽后抛 ExecutionError，错误信息带重试次数。"""
    client = HttpClient(retries=2, retry_backoff=0.0, allow_private=True)
    attempts = {"n": 0}
    monkeypatch.setattr(client, "_sleep", lambda s: None)

    def always_fail(_req, _current):
        attempts["n"] += 1
        raise URLError("connection refused")

    monkeypatch.setattr(client, "_request_once", always_fail)
    with pytest.raises(ExecutionError) as ei:
        client.request("GET", "https://x/version")
    assert attempts["n"] == 3  # 首次 + 2 次重试
    assert "重试 2 次" in ei.value.message


def test_http_error_response_not_retried(monkeypatch):
    """HTTP 4xx/5xx 是确定响应，不重试。"""
    client = HttpClient(retries=3, allow_private=True)
    attempts = {"n": 0}

    def http_500(_req, _current):
        attempts["n"] += 1
        return Response(status=503, url="https://x/v", headers={}, body="down")

    monkeypatch.setattr(client, "_request_once", http_500)
    resp = client.request("GET", "https://x/v")
    assert resp.status == 503
    assert attempts["n"] == 1


def test_retries_disabled(monkeypatch):
    """retries=0 时只请求一次。"""
    client = HttpClient(retries=0, allow_private=True)
    attempts = {"n": 0}

    def fail(_req, _current):
        attempts["n"] += 1
        raise URLError("timeout")

    monkeypatch.setattr(client, "_request_once", fail)
    with pytest.raises(ExecutionError):
        client.request("GET", "https://x/v")
    assert attempts["n"] == 1
