"""密钥文件配置：值只放文件，框架读文件注入进程环境。"""

from __future__ import annotations

import pytest
import yaml

from handoff.config import Config
from handoff.errors import ConfigError
from handoff.secrets import agent_env, config_secret, resolve_secret
from helpers import make_bare_remote, make_store_repo, repo_entry, write_config


def build(tmp_path):
    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api", {"src/a.txt": "a\n"})
    write_config(
        work,
        repos=[repo_entry("org/api", "backend", backend), repo_entry("org/web", "frontend", backend)],
    )
    return work


def _write_local(work, secrets: dict):
    (work / "config" / "local.yaml").write_text(
        yaml.safe_dump({"secrets": secrets}, allow_unicode=True), encoding="utf-8"
    )


def test_secrets_loaded_from_local_yaml(tmp_path):
    work = build(tmp_path)
    (work / "notify.key").write_text("notify-secret-value-from-file\n", encoding="utf-8")
    (work / "api.key").write_text("api-key-value-from-file\n", encoding="utf-8")
    _write_local(work, {
        "HANDOFF_NOTIFY_SECRET_FILE": "notify.key",
        "OPENAPI_API_KEY_FILE": "api.key",
    })

    config = Config.load(work)

    assert config.secrets.HANDOFF_NOTIFY_SECRET_FILE == "notify.key"
    assert config.secrets.OPENAPI_API_KEY_FILE == "api.key"
    assert config_secret(config, "HANDOFF_NOTIFY_SECRET") == "notify-secret-value-from-file"
    assert config_secret(config, "OPENAPI_API_KEY") == "api-key-value-from-file"


def test_agent_env_injects_both_secrets(tmp_path):
    work = build(tmp_path)
    (work / "notify.key").write_text("notify-secret-value\n", encoding="utf-8")
    (work / "api.key").write_text("api-key-value\n", encoding="utf-8")
    _write_local(work, {
        "HANDOFF_NOTIFY_SECRET_FILE": "notify.key",
        "OPENAPI_API_KEY_FILE": "api.key",
    })
    config = Config.load(work)

    env = agent_env(config)

    assert env == {
        "HANDOFF_NOTIFY_SECRET": "notify-secret-value",
        "OPENAPI_API_KEY": "api-key-value",
    }


def test_agent_env_missing_file_is_omitted(tmp_path):
    work = build(tmp_path)
    _write_local(work, {
        "OPENAPI_API_KEY_FILE": "does-not-exist.key",
    })
    config = Config.load(work)

    assert agent_env(config) == {}


def test_resolve_secret_env_wins(tmp_path, monkeypatch):
    work = build(tmp_path)
    (work / "api.key").write_text("from-file\n", encoding="utf-8")
    _write_local(work, {"OPENAPI_API_KEY_FILE": "api.key"})
    config = Config.load(work)
    monkeypatch.setenv("OPENAPI_API_KEY", "from-env")

    assert resolve_secret(config, "OPENAPI_API_KEY") == "from-env"


def test_secrets_rejects_unknown_key(tmp_path):
    work = build(tmp_path)
    _write_local(work, {"SOME_OTHER_SECRET_FILE": "x"})

    with pytest.raises(ConfigError) as excinfo:
        Config.load(work)
    assert "secrets 有未知键" in str(excinfo.value)


def test_secrets_rejects_value_not_path(tmp_path):
    work = build(tmp_path)
    _write_local(work, {"OPENAPI_API_KEY_FILE": 12345})

    with pytest.raises(ConfigError) as excinfo:
        Config.load(work)
    assert "必须是文件路径字符串" in str(excinfo.value)


def test_verifier_resolves_docs_api_key_from_config(tmp_path, monkeypatch):
    """任务单里的 {env:DOCS_API_KEY} 与 OPENAPI_API_KEY 是同一把 key。"""
    from handoff.verifier import _substitute

    work = build(tmp_path)
    (work / "api.key").write_text("shared-key\n", encoding="utf-8")
    _write_local(work, {"OPENAPI_API_KEY_FILE": "api.key"})
    config = Config.load(work)
    monkeypatch.delenv("DOCS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAPI_API_KEY", raising=False)

    resolved = _substitute(
        "{base_url}/docs/openapi.json?apikey={env:DOCS_API_KEY}",
        "https://api.example.com",
        config,
    )
    assert resolved == "https://api.example.com/docs/openapi.json?apikey=shared-key"


def test_verifier_env_still_wins(tmp_path, monkeypatch):
    from handoff.verifier import _substitute

    work = build(tmp_path)
    (work / "api.key").write_text("from-file\n", encoding="utf-8")
    _write_local(work, {"OPENAPI_API_KEY_FILE": "api.key"})
    config = Config.load(work)
    monkeypatch.setenv("DOCS_API_KEY", "from-env")

    resolved = _substitute(
        "{base_url}/docs/openapi.json?apikey={env:DOCS_API_KEY}",
        "https://api.example.com",
        config,
    )
    assert "apikey=from-env" in resolved


def test_notification_secret_falls_back_to_config_file(tmp_path, monkeypatch):
    """通知签名密钥从配置的文件读取，不依赖启动进程的环境变量。"""
    from handoff.notification import load_secret

    work = build(tmp_path)
    secret = "notify-secret-at-least-32-bytes-long!!"
    (work / "notify.key").write_text(f"{secret}\n", encoding="utf-8")
    _write_local(work, {"HANDOFF_NOTIFY_SECRET_FILE": "notify.key"})
    config = Config.load(work)
    monkeypatch.delenv("HANDOFF_NOTIFY_SECRET", raising=False)
    monkeypatch.delenv("HANDOFF_NOTIFY_SECRET_FILE", raising=False)

    # 只把 NotificationConfig 交给通知模块（真实调用路径），它靠 _app_config 回指取文件
    assert load_secret(config.notifications) == secret.encode("utf-8")
