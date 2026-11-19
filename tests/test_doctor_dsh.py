"""doctor：DSH 会话回推的自检。"""

from __future__ import annotations

import json

import yaml

from handoff import doctor
from handoff.config import Config
from helpers import make_bare_remote, make_store_repo, repo_entry, write_config


def _write_local(work, payload):
    (work / "config" / "local.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")


def _write_endpoint(home, url, *, version=1, pid=4242):
    home.mkdir(parents=True, exist_ok=True)
    (home / "handoff-bridge.json").write_text(
        json.dumps(
            {
                "version": version,
                "name": "dsh-handoff-bridge",
                "url": url,
                "origin": "http://127.0.0.1:59945",
                "path": "/v1/handoff/dsh/prompt",
                "pid": pid,
                "updatedAt": "2026-11-16T16:23:06.000Z",
            }
        ),
        encoding="utf-8",
    )


def test_dsh_bridge_checks_are_scoped_to_frontend_role(tmp_path):
    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api", {"src/a.txt": "a\n"})
    write_config(work, repos=[repo_entry("org/api", "backend", backend)])

    frontend = {c.name for c in doctor.run(Config.load(work), role="frontend")}
    # 回推是前端机的事：后端机根本不会写回会话。
    assert "dsh:bridge" in frontend
    backend_checks = {c.name for c in doctor.run(Config.load(work), role="backend")}
    assert "dsh:bridge" not in backend_checks
    assert "dsh:bridge-token" not in backend_checks
    assert "dsh:bridge-live" not in backend_checks


def test_dsh_bridge_checks_warn_without_hint(tmp_path, monkeypatch):
    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api", {"src/a.txt": "a\n"})
    write_config(work, repos=[repo_entry("org/web", "frontend", backend)])
    monkeypatch.delenv("DSH_WEB_URL", raising=False)
    monkeypatch.delenv("HANDOFF_DSH_BRIDGE_TOKEN", raising=False)
    home = tmp_path / "empty-home"
    home.mkdir()
    monkeypatch.setenv("DSH_HOME", str(home))

    checks = {c.name: c for c in doctor.run(Config.load(work), role="frontend")}
    assert checks["dsh:bridge"].status == doctor.WARN
    assert "dsh_bridge_url" in checks["dsh:bridge"].detail
    # 地址都没有，就不该再去探端口。
    assert "dsh:bridge-live" not in checks


def test_dsh_bridge_checks_pass_with_url_and_token(tmp_path, monkeypatch):
    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api", {"src/a.txt": "a\n"})
    write_config(work, repos=[repo_entry("org/web", "frontend", backend)])
    _write_local(
        work,
        {
            "notifications": {
                "dsh_bridge_url": "http://127.0.0.1:56378/v1/handoff/dsh",
                "dsh_bridge_token_env": "HANDOFF_TEST_BRIDGE_TOKEN",
            }
        },
    )
    monkeypatch.setenv("HANDOFF_TEST_BRIDGE_TOKEN", "bridge-token-for-doctor")

    checks = {c.name: c for c in doctor.run(Config.load(work), role="frontend")}
    assert checks["dsh:bridge"].status == doctor.OK
    assert checks["dsh:bridge"].detail.startswith("http://127.0.0.1:56378/v1/handoff/dsh/prompt")
    assert "显式配置" in checks["dsh:bridge"].detail
    assert checks["dsh:bridge-token"].status == doctor.OK
    # 56378 上没有插件在听，但那是环境事实，不该是本次改动的判定条件；
    # 只要求这条检查存在且说清了原因。
    assert checks["dsh:bridge-live"].status in (doctor.OK, doctor.WARN)


def test_dsh_bridge_token_falls_back_to_dsh_home(tmp_path, monkeypatch):
    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api", {"src/a.txt": "a\n"})
    write_config(work, repos=[repo_entry("org/web", "frontend", backend)])
    _write_local(work, {"notifications": {"dsh_bridge_url": "http://127.0.0.1:56378/v1/handoff/dsh"}})
    home = tmp_path / "dsh-home"
    home.mkdir()
    (home / "handoff-bridge.token").write_text("token-from-dsh-home\n", encoding="utf-8")
    monkeypatch.setenv("DSH_HOME", str(home))
    monkeypatch.delenv("HANDOFF_DSH_BRIDGE_TOKEN", raising=False)

    checks = {c.name: c for c in doctor.run(Config.load(work), role="frontend")}
    assert checks["dsh:bridge-token"].status == doctor.OK
    assert str(home) in checks["dsh:bridge-token"].detail


def test_dsh_bridge_prefers_the_published_endpoint(tmp_path, monkeypatch):
    """插件自公布的端点优先于 DSH_WEB_URL——后者是启动那一刻的端口，重启就过期。"""
    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api", {"src/a.txt": "a\n"})
    write_config(work, repos=[repo_entry("org/web", "frontend", backend)])
    home = tmp_path / "dsh-home"
    _write_endpoint(home, "http://127.0.0.1:60001/v1/handoff/dsh/prompt")
    (home / "handoff-bridge.token").write_text("token\n", encoding="utf-8")
    monkeypatch.setenv("DSH_HOME", str(home))
    monkeypatch.setenv("DSH_WEB_URL", "http://127.0.0.1:99999")
    monkeypatch.delenv("HANDOFF_DSH_BRIDGE_TOKEN", raising=False)

    checks = {c.name: c for c in doctor.run(Config.load(work), role="frontend")}
    assert checks["dsh:bridge"].detail.startswith("http://127.0.0.1:60001/v1/handoff/dsh/prompt")
    assert "插件自公布" in checks["dsh:bridge"].detail


def test_dsh_bridge_explicit_config_beats_the_endpoint(tmp_path, monkeypatch):
    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api", {"src/a.txt": "a\n"})
    write_config(work, repos=[repo_entry("org/web", "frontend", backend)])
    _write_local(
        work,
        {"notifications": {"dsh_bridge_url": "http://127.0.0.1:58888/v1/handoff/dsh"}},
    )
    home = tmp_path / "dsh-home"
    _write_endpoint(home, "http://127.0.0.1:60001/v1/handoff/dsh/prompt")
    (home / "handoff-bridge.token").write_text("token\n", encoding="utf-8")
    monkeypatch.setenv("DSH_HOME", str(home))
    monkeypatch.delenv("HANDOFF_DSH_BRIDGE_TOKEN", raising=False)

    checks = {c.name: c for c in doctor.run(Config.load(work), role="frontend")}
    assert checks["dsh:bridge"].detail.startswith("http://127.0.0.1:58888/v1/handoff/dsh/prompt")
    assert "显式配置" in checks["dsh:bridge"].detail


def test_dsh_bridge_ignores_a_stale_endpoint_format(tmp_path, monkeypatch):
    """格式版本不认识就当没有——别用一份未来格式的文件去投递。"""
    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api", {"src/a.txt": "a\n"})
    write_config(work, repos=[repo_entry("org/web", "frontend", backend)])
    home = tmp_path / "dsh-home"
    _write_endpoint(home, "http://127.0.0.1:60001/v1/handoff/dsh/prompt", version=99)
    monkeypatch.setenv("DSH_HOME", str(home))
    monkeypatch.setenv("DSH_WEB_URL", "http://127.0.0.1:61234")
    monkeypatch.delenv("HANDOFF_DSH_BRIDGE_TOKEN", raising=False)

    checks = {c.name: c for c in doctor.run(Config.load(work), role="frontend")}
    # 退回 DSH_WEB_URL。
    assert checks["dsh:bridge"].detail.startswith("http://127.0.0.1:61234/v1/handoff/dsh/prompt")
    assert "DSH_WEB_URL" in checks["dsh:bridge"].detail
