"""端点自公布的单元测试：兜底顺序、过期文件、坏格式。"""

from __future__ import annotations

import json

import pytest

from handoff.dsh_bridge import (
    ENDPOINT_VERSION,
    BridgeOrigin,
    discover_bridge,
    endpoint_file,
    read_endpoint,
    request_id_for,
)
from handoff.errors import ConfigError


def _write(home, payload):
    home.mkdir(parents=True, exist_ok=True)
    (home / "handoff-bridge.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
    )


def _endpoint(url="http://127.0.0.1:60001/v1/handoff/dsh/prompt", *, version=ENDPOINT_VERSION, pid=1):
    return {
        "version": version,
        "name": "dsh-handoff-bridge",
        "url": url,
        "origin": "http://127.0.0.1:60001",
        "path": "/v1/handoff/dsh/prompt",
        "pid": pid,
        "updatedAt": "2026-11-16T16:23:06.000Z",
    }


def test_read_endpoint_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    assert endpoint_file() == tmp_path / "handoff-bridge.json"
    assert read_endpoint() is None


def test_read_endpoint_parses_a_published_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    _write(tmp_path, _endpoint())
    parsed = read_endpoint()
    assert parsed is not None
    assert parsed["url"] == "http://127.0.0.1:60001/v1/handoff/dsh/prompt"
    assert parsed["pid"] == 1


def test_read_endpoint_ignores_unknown_versions(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    _write(tmp_path, _endpoint(version=ENDPOINT_VERSION + 1))
    assert read_endpoint() is None


def test_read_endpoint_ignores_broken_json(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    _write(tmp_path, "{not json")
    assert read_endpoint() is None


def test_read_endpoint_ignores_missing_url(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    _write(tmp_path, {"version": ENDPOINT_VERSION, "name": "x"})
    assert read_endpoint() is None


def test_discover_prefers_the_endpoint_over_dsh_web_url(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    monkeypatch.setenv("DSH_WEB_URL", "http://127.0.0.1:99999")
    _write(tmp_path, _endpoint())

    found = discover_bridge()
    assert isinstance(found, BridgeOrigin)
    assert found.source == "endpoint"
    assert found.url == "http://127.0.0.1:60001/v1/handoff/dsh/prompt"
    assert found.endpoint is not None


def test_discover_falls_back_to_dsh_web_url(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    monkeypatch.setenv("DSH_WEB_URL", "http://127.0.0.1:61234")

    found = discover_bridge()
    assert found.source == "env"
    assert found.url == "http://127.0.0.1:61234/v1/handoff/dsh/prompt"


def test_discover_explicit_config_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    monkeypatch.setenv("DSH_WEB_URL", "http://127.0.0.1:99999")
    _write(tmp_path, _endpoint())

    found = discover_bridge("http://127.0.0.1:58888/v1/handoff/dsh")
    assert found.source == "config"
    assert found.url == "http://127.0.0.1:58888/v1/handoff/dsh/prompt"


def test_discover_raises_when_nothing_is_available(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    monkeypatch.delenv("DSH_WEB_URL", raising=False)
    with pytest.raises(ConfigError) as info:
        discover_bridge()
    assert "dsh-handoff-bridge" in info.value.message
    assert "handoff-bridge.json" in info.value.message


def test_published_endpoint_must_be_loopback(tmp_path, monkeypatch):
    """插件写错了地址也不能把完成事件发到公网去。"""
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    _write(tmp_path, _endpoint("http://evil.example.com/v1/handoff/dsh/prompt"))
    with pytest.raises(ConfigError) as info:
        discover_bridge()
    assert "loopback" in info.value.message


def test_endpoint_url_gets_prompt_suffix(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    _write(tmp_path, _endpoint("http://127.0.0.1:60001/v1/handoff/dsh"))
    assert discover_bridge().url.endswith("/v1/handoff/dsh/prompt")


def test_request_id_is_stable_and_readable():
    assert request_id_for("evt-abc") == "handoff-evt-abc"


def test_token_file_follows_the_file_not_the_directory(tmp_path, monkeypatch):
    """目录存在但里面没有令牌时，不该就此认定它是 DSH home。

    真实故障：WSL 里 `~/.dsh` 被一次测试运行建出来，只留下一个过期的端点
    文件；老实现看见目录在就用它，令牌于是找错地方。
    """
    from handoff import dsh_bridge

    stale_home = tmp_path / "stale"
    real_home = tmp_path / "real"
    stale_home.mkdir()
    real_home.mkdir()
    (real_home / "handoff-bridge.token").write_text("b" * 64, encoding="utf-8")

    monkeypatch.setattr(dsh_bridge, "_candidate_homes", lambda: [stale_home, real_home])

    assert dsh_bridge.token_file() == real_home / "handoff-bridge.token"


def test_token_file_defaults_to_the_first_candidate_when_nothing_exists(tmp_path, monkeypatch):
    from handoff import dsh_bridge

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    monkeypatch.setattr(dsh_bridge, "_candidate_homes", lambda: [first, second])

    assert dsh_bridge.token_file() == first / "handoff-bridge.token"


def test_endpoint_file_prefers_the_one_that_exists(tmp_path, monkeypatch):
    from handoff import dsh_bridge

    stale_home = tmp_path / "stale"
    real_home = tmp_path / "real"
    stale_home.mkdir()
    real_home.mkdir()
    (real_home / "handoff-bridge.json").write_text(
        '{"version": 1, "url": "http://127.0.0.1:59945/v1/handoff/dsh/prompt"}',
        encoding="utf-8",
    )

    monkeypatch.setattr(dsh_bridge, "_candidate_homes", lambda: [stale_home, real_home])

    assert dsh_bridge.endpoint_file() == real_home / "handoff-bridge.json"
    assert dsh_bridge.read_endpoint()["url"].endswith(":59945/v1/handoff/dsh/prompt")


def test_explicit_dsh_home_wins_over_file_lookup(tmp_path, monkeypatch):
    from handoff import dsh_bridge

    pinned = tmp_path / "pinned"
    pinned.mkdir()
    monkeypatch.setenv("DSH_HOME", str(pinned))

    assert dsh_bridge._candidate_homes() == [pinned]
    assert dsh_bridge.token_file() == pinned / "handoff-bridge.token"
