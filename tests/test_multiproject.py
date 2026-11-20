"""多个前端项目：source.repo 自动识别、目标/环境覆盖。"""

from __future__ import annotations

import json
import threading

import pytest
import yaml

from handoff import ids, notification, schema
from handoff.cli import detect_source_repo, main as cli_main, slug_from_remote
from handoff.config import Config, NotificationConfig
from handoff.frontend_session import FrontendSessions
from handoff.store import TaskStore
from helpers import (
    git,
    make_bare_remote,
    make_store_repo,
    make_task,
    repo_entry,
    write_config,
)


def build(tmp_path):
    work, _bare = make_store_repo(tmp_path)
    api = make_bare_remote(tmp_path, "api", {"src/orders/Order.java": "class Order {}\n"})
    web_a = make_bare_remote(tmp_path, "web-a", {"src/a.ts": "a\n"})
    web_b = make_bare_remote(tmp_path, "web-b", {"src/b.ts": "b\n"})
    write_config(
        work,
        repos=[
            repo_entry("org/api", "backend", api),
            repo_entry("org/web-a", "frontend", web_a),
            repo_entry("org/web-b", "frontend", web_b),
        ],
        environments=[
            {"name": "dev", "url": "https://dev.example.com", "version_path": "/version"},
            {"name": "preview", "url": "https://pr-1.example.com", "version_path": "/version"},
        ],
    )
    config = Config.load(work)
    clone_a = tmp_path / "web-a-work"
    clone_b = tmp_path / "web-b-work"
    git(tmp_path, "clone", str(web_a), str(clone_a))
    git(tmp_path, "clone", str(web_b), str(clone_b))
    return work, config, api, clone_a, clone_b


def write_draft(path, task):
    path.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture(autouse=True)
def frontend_session_env(monkeypatch):
    """create 的所有成功路径都必须带原前端会话。"""
    monkeypatch.setenv("CODEX_THREAD_ID", "frontend-origin-thread")
    monkeypatch.delenv("DSH_SESSION_ID", raising=False)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@github.com:org/web-a.git", "org/web-a"),
        ("https://github.com/org/web-a.git", "org/web-a"),
        ("https://github.com/org/web-a", "org/web-a"),
        ("ssh://git@github.com/org/web-a.git", "org/web-a"),
        ("/srv/git/repo.git", None),
        ("", None),
    ],
)
def test_slug_from_remote(url, expected):
    assert slug_from_remote(url) == expected


def test_detect_source_repo_matches_registered_remote(tmp_path):
    _work, config, _api, clone_a, _clone_b = build(tmp_path)
    assert detect_source_repo(config, clone_a) == "org/web-a"


def test_create_overrides_stale_draft_repo(tmp_path):
    work, _config, api, clone_a, _clone_b = build(tmp_path)
    task = make_task(ids.task_id(), base_sha=git(api, "rev-parse", "dev"), source_repo="org/web-b")
    draft = tmp_path / "draft.json"
    write_draft(draft, task)

    code = cli_main(["--root", str(work), "--json", "create", "--from-file", str(draft), "--source-path", str(clone_a)])
    assert code == 0

    store = TaskStore(work, Config.load(work))
    saved = store.get(task["id"])
    # 草稿里写的是 web-b，但 --source-path 指向 web-a，以实际仓库为准
    assert saved["source"]["repo"] == "org/web-a"
    assert saved["source"]["commit_sha"] == git(clone_a, "rev-parse", "HEAD")
    assert "base_sha" not in saved["target"]
    # 旧草稿里的 raw glob 不再决定后端路径，创建时统一固化仓库默认 profile。
    assert saved["scope"]["allowed_paths"] == ["src/**", "tests/**"]
    assert saved["scope"]["forbidden_paths"] == [".github/**", "infra/prod/**"]
    assert schema.errors_for("task", saved) == []


def test_create_registers_dsh_session_before_success(tmp_path, monkeypatch):
    work, _config, api, clone_a, _clone_b = build(tmp_path)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.setenv("DSH_SESSION_ID", "session-11111111-2222-3333-4444-555555555555")
    task = make_task(ids.task_id(), base_sha=git(api, "rev-parse", "dev"), source_repo="org/web-a")
    draft = tmp_path / "draft-dsh-session.json"
    write_draft(draft, task)

    code = cli_main(
        ["--root", str(work), "--json", "create", "--from-file", str(draft), "--source-path", str(clone_a)]
    )

    assert code == 0
    cfg = Config.load(work)
    session = FrontendSessions(cfg.notifications.session_path(work)).session(task["id"])
    assert session["thread_id"] == "session-11111111-2222-3333-4444-555555555555"
    assert session["executor"] == "dsh"
    assert session["workspace"] == str(clone_a.resolve())


def test_create_requires_frontend_session(tmp_path, monkeypatch, capsys):
    work, _config, api, clone_a, _clone_b = build(tmp_path)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("DSH_SESSION_ID", raising=False)
    task = make_task(ids.task_id(), base_sha=git(api, "rev-parse", "dev"), source_repo="org/web-a")
    draft = tmp_path / "draft-no-session.json"
    write_draft(draft, task)

    code = cli_main(
        ["--root", str(work), "--json", "create", "--from-file", str(draft), "--source-path", str(clone_a)]
    )

    assert code != 0
    output = json.loads(capsys.readouterr().out)
    assert "创建任务必须登记原前端会话" in output["message"]
    assert not (work / "tasks" / f"{task['id']}.json").exists()


def test_create_rejects_session_registration_bypass(tmp_path, capsys):
    work, _config, api, clone_a, _clone_b = build(tmp_path)
    task = make_task(ids.task_id(), base_sha=git(api, "rev-parse", "dev"), source_repo="org/web-a")
    draft = tmp_path / "draft-no-register.json"
    write_draft(draft, task)

    code = cli_main(
        [
            "--root",
            str(work),
            "--json",
            "create",
            "--from-file",
            str(draft),
            "--source-path",
            str(clone_a),
            "--no-session-register",
        ]
    )

    assert code != 0
    output = json.loads(capsys.readouterr().out)
    assert "--no-session-register 已禁用" in output["message"]
    assert not (work / "tasks" / f"{task['id']}.json").exists()


def test_create_does_not_write_task_when_session_registration_fails(tmp_path, capsys):
    work, _config, api, clone_a, _clone_b = build(tmp_path)
    task = make_task(ids.task_id(), base_sha=git(api, "rev-parse", "dev"), source_repo="org/web-a")
    draft = tmp_path / "draft-invalid-workspace.json"
    write_draft(draft, task)

    code = cli_main(
        [
            "--root",
            str(work),
            "--json",
            "create",
            "--from-file",
            str(draft),
            "--source-path",
            str(clone_a),
            "--source-workspace",
            str(tmp_path / "missing-workspace"),
        ]
    )

    assert code != 0
    output = json.loads(capsys.readouterr().out)
    assert "前端工作区不存在" in output["message"]
    assert not (work / "tasks" / f"{task['id']}.json").exists()


def test_create_without_base_sha_does_not_access_backend_remote(tmp_path):
    work, _config, api, clone_a, _clone_b = build(tmp_path)
    task = make_task(ids.task_id(), base_sha=None, source_repo="org/web-a")
    draft = tmp_path / "draft.json"
    write_draft(draft, task)

    # 模拟前端机器完全无法访问后端仓；create 仍应只写交接仓。
    api.rename(tmp_path / "api-unreachable.git")
    code = cli_main(
        ["--root", str(work), "--json", "create", "--from-file", str(draft), "--source-path", str(clone_a)]
    )
    assert code == 0
    saved = TaskStore(work, Config.load(work)).get(task["id"])
    assert "base_sha" not in saved["target"]
    assert saved["status"] == "open"


def test_create_replaces_id_placeholder_in_expected_path(tmp_path):
    """草稿里的 contracts/<id>/ 是占位，create 必须换成真实任务 id 路径。

    回归 ho-20261116-01M7H33VTSEYSD74JKB7S7QHMP：expected_path 留着字面
    `<id>`，验收时 openapi_diff 按字面路径找不到契约文件。
    """
    from handoff.cli import resolve_id_placeholders

    task_id = "ho-20261116-01TEST0000000000000000"
    draft = {
        "id": task_id,
        "contract": {"kind": "openapi", "path": "contracts/<id>/api.yaml", "sha256": "a" * 64},
        "acceptance": {
            "environment": "dev",
            "checks": [
                {
                    "id": "contract",
                    "type": "openapi_diff",
                    "expected_path": "contracts/<id>/campaign.openapi.yaml",
                    "actual_url": "{base_url}/docs/openapi.json",
                }
            ],
        },
        "repro": {
            "attachments": [
                {"kind": "store_path", "ref": "contracts/<id>/extra.md", "sha256": "b" * 64}
            ]
        },
    }
    resolve_id_placeholders(task_id, draft)
    assert draft["contract"]["path"] == f"contracts/{task_id}/api.yaml"
    assert draft["acceptance"]["checks"][0]["expected_path"] == f"contracts/{task_id}/campaign.openapi.yaml"
    assert draft["repro"]["attachments"][0]["ref"] == f"contracts/{task_id}/extra.md"


def test_resolve_id_placeholders_leaves_real_paths_alone():
    from handoff.cli import resolve_id_placeholders

    task = {
        "contract": {"kind": "openapi", "path": "contracts/ho-real/api.yaml", "sha256": "a" * 64},
        "acceptance": {
            "checks": [
                {
                    "id": "contract",
                    "type": "openapi_diff",
                    "expected_path": "contracts/ho-real/api.yaml",
                    "actual_url": "{base_url}/docs/openapi.json",
                }
            ]
        },
    }
    before = repr(task)
    resolve_id_placeholders("ho-other", task)
    assert repr(task) == before


def test_create_with_explicit_source_repo_wins(tmp_path):
    work, _config, api, clone_a, _clone_b = build(tmp_path)
    task = make_task(ids.task_id(), base_sha=git(api, "rev-parse", "dev"), source_repo="org/web-b")
    draft = tmp_path / "draft.json"
    write_draft(draft, task)

    code = cli_main(
        [
            "--root",
            str(work),
            "--json",
            "create",
            "--from-file",
            str(draft),
            "--source-path",
            str(clone_a),
            "--source-repo",
            "org/web-b",
        ]
    )
    assert code == 0
    saved = TaskStore(work, Config.load(work)).get(task["id"])
    assert saved["source"]["repo"] == "org/web-b"


def test_create_target_and_environment_override(tmp_path):
    work, _config, api, clone_a, _clone_b = build(tmp_path)
    task = make_task(ids.task_id(), base_sha=git(api, "rev-parse", "dev"))
    draft = tmp_path / "draft.json"
    write_draft(draft, task)

    code = cli_main(
        [
            "--root",
            str(work),
            "--json",
            "create",
            "--from-file",
            str(draft),
            "--source-path",
            str(clone_a),
            "--target",
            "org/api",
            "--environment",
            "preview",
        ]
    )
    assert code == 0
    saved = TaskStore(work, Config.load(work)).get(task["id"])
    assert saved["target"]["repo"] == "org/api"
    assert saved["acceptance"]["environment"] == "preview"


def test_unregistered_frontend_lists_candidates(tmp_path):
    work, _config, api, _clone_a, _clone_b = build(tmp_path)
    stray = make_bare_remote(tmp_path, "web-c", {"src/c.ts": "c\n"})
    clone_c = tmp_path / "web-c-work"
    git(tmp_path, "clone", str(stray), str(clone_c))
    # 本地 remote 解析不出 org/repo，换成 GitHub 形式来命中「未注册」分支
    git(clone_c, "remote", "set-url", "origin", "git@github.com:org/web-c.git")

    task = make_task(ids.task_id(), base_sha=git(api, "rev-parse", "dev"), source_repo="org/web-a")
    draft = tmp_path / "draft.json"
    write_draft(draft, task)

    code = cli_main(["--root", str(work), "create", "--from-file", str(draft), "--source-path", str(clone_c)])
    assert code != 0


def test_create_pushes_then_background_notifier_notifies_backend(tmp_path, monkeypatch):
    work, _config, api, clone_a, _clone_b = build(tmp_path)
    secret = "integration-secret-that-is-at-least-32-bytes"
    monkeypatch.setenv("HANDOFF_NOTIFY_SECRET", secret)
    monkeypatch.setenv("CODEX_THREAD_ID", "frontend-origin-thread")

    receiver_config = NotificationConfig(
        secret_env="HANDOFF_NOTIFY_SECRET",
        inbox_database=str(tmp_path / "receiver-inbox.sqlite3"),
        listen_host="127.0.0.1",
        listen_port=0,
    )
    wake = threading.Event()
    receiver = notification.WakeServer(tmp_path, receiver_config, wake)
    host, port = receiver.start()
    try:
        (work / "config" / "local.yaml").write_text(
            yaml.safe_dump(
                {
                    "notifications": {
                        "url": f"http://{host}:{port}{notification.WAKE_PATH}",
                        "secret_env": "HANDOFF_NOTIFY_SECRET",
                        "outbox_database": str(tmp_path / "sender-outbox.sqlite3"),
                        "session_database": str(tmp_path / "frontend-sessions.sqlite3"),
                        "completion_url": "http://192.168.1.3:8789/v1/handoff/completed",
                    }
                }
            ),
            encoding="utf-8",
        )
        task = make_task(ids.task_id(), base_sha=git(api, "rev-parse", "dev"), source_repo="org/web-a")
        draft = tmp_path / "draft-notify.json"
        write_draft(draft, task)

        code = cli_main(
            ["--root", str(work), "--json", "create", "--from-file", str(draft), "--source-path", str(clone_a)]
        )
        assert code == 0
        sender = notification.Notifier(work, Config.load(work).notifications)
        assert receiver.inbox.count() == 0
        assert sender.outbox.pending_count() == 1
        assert sender.send_due() == {"sent": 1, "failed": 0, "pending": 0, "errors": []}
        assert wake.wait(1)
        assert receiver.inbox.count() == 1
        assert receiver.inbox.completion_url(task["id"]) == "http://192.168.1.3:8789/v1/handoff/completed"
        session = FrontendSessions(tmp_path / "frontend-sessions.sqlite3").session(task["id"])
        assert session["thread_id"] == "frontend-origin-thread"
        assert session["workspace"] == str(clone_a.resolve())
    finally:
        receiver.stop()


def test_create_remains_successful_when_notification_is_pending(tmp_path, monkeypatch, capsys):
    work, _config, api, clone_a, _clone_b = build(tmp_path)
    monkeypatch.delenv("HANDOFF_NOTIFY_SECRET", raising=False)
    (work / "config" / "local.yaml").write_text(
        yaml.safe_dump(
            {
                "notifications": {
                    "url": f"http://127.0.0.1:9{notification.WAKE_PATH}",
                    "secret_env": "HANDOFF_NOTIFY_SECRET",
                    "outbox_database": str(tmp_path / "pending-outbox.sqlite3"),
                }
            }
        ),
        encoding="utf-8",
    )
    task = make_task(ids.task_id(), base_sha=git(api, "rev-parse", "dev"), source_repo="org/web-a")
    draft = tmp_path / "draft-pending.json"
    write_draft(draft, task)

    code = cli_main(
        ["--root", str(work), "--json", "create", "--from-file", str(draft), "--source-path", str(clone_a)]
    )
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["status"] == "open"
    assert output["notification"]["status"] == "pending"
    assert output["notification"]["delivery"] == "background_notifier"
    assert "error" not in output["notification"]
    assert TaskStore(work, Config.load(work)).get(task["id"])["status"] == "open"


def test_resolve_id_placeholders_normalizes_bare_openapi_url():
    """裸 {base_url}/openapi.json（404）落库前规范化为带 apikey 的 docs 端点。"""
    from handoff.cli import resolve_id_placeholders

    task_id = "ho-20261117-01TEST0000000000000000"
    task = {
        "id": task_id,
        "acceptance": {
            "checks": [
                {
                    "id": "contract",
                    "type": "openapi_diff",
                    "expected_path": "contracts/<id>/spec.yaml",
                    "actual_url": "{base_url}/openapi.json",
                },
                {
                    "id": "other",
                    "type": "http",
                    "url": "{base_url}/healthz",
                },
            ]
        },
    }
    resolve_id_placeholders(task_id, task)
    assert task["acceptance"]["checks"][0]["actual_url"] == (
        "{base_url}/docs/openapi.json?apikey={env:DOCS_API_KEY}"
    )
    assert task["acceptance"]["checks"][0]["expected_path"] == f"contracts/{task_id}/spec.yaml"
    # 非 openapi_diff / 显式其它地址不受影响
    assert task["acceptance"]["checks"][1]["url"] == "{base_url}/healthz"

    # 幂等：再跑一次不再变化
    before = repr(task)
    resolve_id_placeholders(task_id, task)
    assert repr(task) == before
