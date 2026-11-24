"""配置加载：codex.yaml 与本机覆盖 local.yaml。"""

from __future__ import annotations

import pytest
import yaml

from handoff.config import Config
from handoff.errors import ConfigError
from helpers import make_bare_remote, make_store_repo, repo_entry, write_config


def build(tmp_path, *, codex=None):
    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api", {"src/a.txt": "a\n"})
    write_config(
        work,
        repos=[repo_entry("org/api", "backend", backend), repo_entry("org/web", "frontend", backend)],
        codex=codex,
    )
    return work, Config.load(work)


def test_local_yaml_overrides_machine_paths(tmp_path):
    work, _config = build(tmp_path)
    (work / "config" / "local.yaml").write_text(
        yaml.safe_dump(
            {
                "repo_paths": {"org/web": "/mnt/c/code/web"},
                "environments": {"dev": {"url": "http://127.0.0.1:9999"}},
                "codex": {"binary": "/opt/codex/bin/codex", "timeout_seconds": 120},
                "repositories": {"org/api": {"cache_dir": "/tmp/cache/api"}},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    config = Config.load(work)

    assert config.checks.verifier.repo_paths["org/web"] == "/mnt/c/code/web"
    assert config.env("dev").url == "http://127.0.0.1:9999"
    assert config.codex.binary == "/opt/codex/bin/codex"
    assert config.codex.timeout_seconds == 120
    assert config.repo("org/api").cache_path(work).as_posix() == "/tmp/cache/api"


def test_local_yaml_rejects_typos(tmp_path):
    work, _config = build(tmp_path)
    (work / "config" / "local.yaml").write_text(
        yaml.safe_dump({"repo_path": {"org/web": "/x"}}, allow_unicode=True), encoding="utf-8"
    )
    with pytest.raises(ConfigError) as excinfo:
        Config.load(work)
    assert "未知键" in str(excinfo.value)


def test_local_yaml_rejects_unknown_repo(tmp_path):
    work, _config = build(tmp_path)
    (work / "config" / "local.yaml").write_text(
        yaml.safe_dump({"repo_paths": {"org/nope": "/x"}}, allow_unicode=True), encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        Config.load(work)


def test_codex_yaml_defaults(tmp_path):
    work, config = build(tmp_path)
    assert config.codex.persist_thread is False
    assert config.codex.sandbox == "workspace-write"

    (work / "config" / "codex.yaml").write_text(
        yaml.safe_dump(
            {"codex": {"persist_thread": True, "keep_worktree": True, "timeout_seconds": 90}},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    config = Config.load(work)
    assert config.codex.persist_thread is True
    assert config.codex.keep_worktree is True
    assert config.codex.timeout_seconds == 90


def test_duplicate_keys_are_rejected(tmp_path):
    work, _config = build(tmp_path)
    (work / "config" / "checks.yaml").write_text(
        """
test_suites:
  web-a-unit:
    repo: org/api
    argv: ["./gradlew", "test"]
  web-a-unit:
    repo: org/api
    argv: ["./gradlew", "check"]
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        Config.load(work)
    assert "重复的键" in str(excinfo.value)


def test_local_gate_file_limit_is_bounded(tmp_path):
    work, _config = build(tmp_path)
    local = work / "config/local.yaml"
    local.write_text("gate:\n  max_file_bytes: 4194304\n", encoding="utf-8")
    cfg = Config.load(work)
    assert cfg.checks.gate.max_file_bytes == 4194304
    for override in ({"max_file_bytes": 0}, {"max_file_bytes": True}, {"max_changed_files": 1000}):
        local.write_text(yaml.safe_dump({"gate": override}), encoding="utf-8")
        with pytest.raises(ConfigError):
            Config.load(work)


def test_local_notifications_are_machine_specific(tmp_path):
    work, _config = build(tmp_path)
    (work / "config" / "local.yaml").write_text(
        yaml.safe_dump(
            {
                "notifications": {
                    "url": "http://192.168.1.20:8788/v1/handoff/wake",
                    "completion_url": "http://192.168.1.10:8789/v1/handoff/completed",
                    "listen_host": "0.0.0.0",
                    "listen_port": 8788,
                    "allowed_cidrs": ["192.168.1.10/32"],
                    "secret_env": "HANDOFF_NOTIFY_SECRET",
                    "restore_models": {
                        "codex": "gpt-5.3-codex",
                        "dsh": {"provider": "deepseek-account", "model": "deepseek-chat"},
                    },
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    cfg = Config.load(work)
    assert cfg.notifications.url.endswith("/v1/handoff/wake")
    assert cfg.notifications.completion_url.endswith("/v1/handoff/completed")
    assert cfg.notifications.listen_host == "0.0.0.0"
    assert cfg.notifications.listen_port == 8788
    assert cfg.notifications.allowed_cidrs == ("192.168.1.10/32",)
    assert cfg.notifications.restore_models.codex == "gpt-5.3-codex"
    assert cfg.notifications.restore_models.dsh.provider == "deepseek-account"
    assert cfg.notifications.restore_models.dsh.model == "deepseek-chat"


def test_restore_models_default_to_session_selection(tmp_path):
    _work, cfg = build(tmp_path)
    assert cfg.notifications.restore_models.codex is None
    assert cfg.notifications.restore_models.dsh is None


@pytest.mark.parametrize(
    "restore_models",
    [
        "gpt-5.3-codex",
        {"codex": ""},
        {"claude": "opus"},
        {"dsh": "deepseek-chat"},
        {"dsh": {"provider": "deepseek-account"}},
        {"dsh": {"provider": "deepseek-account", "model": ""}},
        {"dsh": {"provider": "deepseek-account", "model": "deepseek-chat", "effort": "high"}},
    ],
)
def test_restore_models_reject_malformed_values(tmp_path, restore_models):
    work, _cfg = build(tmp_path)
    (work / "config" / "local.yaml").write_text(
        yaml.safe_dump({"notifications": {"restore_models": restore_models}}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="notifications.restore_models"):
        Config.load(work)


def test_panel_port_defaults_off_the_notification_port(tmp_path):
    """面板默认端口必须避开 frontend-listener 的 8789（后端回推 task.completed 打这里）。"""
    work, config = build(tmp_path)
    assert config.panel.port == 8790
    assert config.notifications.listen_port == 8788

    (work / "config" / "panel.yaml").write_text(
        yaml.safe_dump({"panel": {"port": 8899}}, allow_unicode=True), encoding="utf-8"
    )
    assert Config.load(work).panel.port == 8899

    (work / "config" / "local.yaml").write_text(
        yaml.safe_dump({"panel": {"port": 8791}}, allow_unicode=True), encoding="utf-8"
    )
    assert Config.load(work).panel.port == 8791


def test_panel_port_may_not_collide_with_the_listener_port(tmp_path):
    """面板和 frontend-listener 撞端口会让面板起不来，加载时就报错。"""
    work, _config = build(tmp_path)
    (work / "config" / "local.yaml").write_text(
        yaml.safe_dump({"panel": {"port": 8788}}, allow_unicode=True), encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="panel.port 不能等于 notifications.listen_port"):
        Config.load(work)


def test_local_notification_paths_are_exact(tmp_path):
    work, _config = build(tmp_path)
    local = work / "config" / "local.yaml"
    for patch in (
        {"url": "http://127.0.0.1:8788/wrong"},
        {"completion_url": "http://127.0.0.1:8789/v1/handoff/completed?token=bad"},
    ):
        local.write_text(yaml.safe_dump({"notifications": patch}), encoding="utf-8")
        with pytest.raises(ConfigError):
            Config.load(work)


def test_repo_scope_profiles_are_loaded(tmp_path):
    work, config = build(tmp_path)
    scope = config.repo("org/api").scope
    assert scope is not None
    assert scope.default_profile == "application"
    assert scope.profiles["application"].allowed_paths == ("src/**", "tests/**")
    assert scope.forbidden_paths == (".github/**", "infra/prod/**")


@pytest.mark.parametrize(
    "scope,match",
    [
        ({"default_profile": "application", "profiles": {}}, "至少要配置一个"),
        (
            {
                "default_profile": "missing",
                "profiles": {"application": {"allowed_paths": ["src/**"]}},
            },
            "不在 profiles 中",
        ),
        (
            {
                "default_profile": "application",
                "profiles": {"application": {"allowed_paths": ["../src/**"]}},
            },
            "非法",
        ),
        (
            {
                "default_profile": "application",
                "profiles": {"application": {"allowed_paths": []}},
            },
            "至少要有一条",
        ),
    ],
)
def test_repo_scope_profiles_fail_closed_on_invalid_config(tmp_path, scope, match):
    work, _config = build(tmp_path)
    path = work / "config" / "repositories.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["repositories"][0]["scope"] = scope
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(ConfigError, match=match):
        Config.load(work)
