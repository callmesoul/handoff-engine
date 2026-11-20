"""doctor：配置自检。"""

from __future__ import annotations

import yaml

from handoff import doctor
from handoff.config import Config
from helpers import make_bare_remote, make_store_repo, repo_entry, write_config


def test_doctor_reports_ready_environment(tmp_path):
    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api", {"src/a.txt": "a\n"})
    write_config(
        work,
        repos=[repo_entry("org/api", "backend", backend), repo_entry("org/web", "frontend", backend)],
    )
    config = Config.load(work)

    checks = doctor.run(config)
    by_name = {c.name: c for c in checks}

    assert by_name["bin:git"].status == doctor.OK
    assert by_name["store:git"].status == doctor.OK
    assert by_name["store:clean"].status == doctor.OK
    assert by_name["store:remote"].status == doctor.OK
    assert by_name["repo:org/api"].status == doctor.OK
    assert by_name["repo:org/api:scope"].status == doctor.OK
    assert by_name["repo:org/web"].status == doctor.OK
    assert by_name["env:dev"].status == doctor.OK

    summary = doctor.summarize(checks)
    assert summary["ok"] is True
    assert summary["failed"] == 0


def test_doctor_fails_when_backend_scope_template_is_missing(tmp_path):
    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api", {"src/a.txt": "a\n"})
    entry = repo_entry("org/api", "backend", backend)
    entry.pop("scope")
    write_config(work, repos=[entry])

    checks = {check.name: check for check in doctor.run(Config.load(work), role="backend")}
    assert checks["repo:org/api:scope"].status == doctor.FAIL
    assert "新任务会 fail closed" in checks["repo:org/api:scope"].detail


def test_doctor_flags_unreachable_remote(tmp_path):
    work, _bare = make_store_repo(tmp_path)
    missing = tmp_path / "nope.git"
    write_config(
        work,
        repos=[
            repo_entry("org/api", "backend", missing),
            repo_entry("org/web", "frontend", missing),
        ],
    )
    config = Config.load(work)

    checks = doctor.run(config)
    by_name = {c.name: c for c in checks}
    assert by_name["repo:org/api"].status == doctor.FAIL
    assert "示例值" in by_name["repo:org/api"].detail

    summary = doctor.summarize(checks)
    assert summary["ok"] is False
    assert summary["failed"] >= 2

    text = doctor.render(checks)
    assert "FAIL" in text
    assert "失败" in text


def test_doctor_frontend_role_skips_unreachable_backend(tmp_path):
    work, _bare = make_store_repo(tmp_path)
    frontend = make_bare_remote(tmp_path, "web", {"src/a.ts": "a\n"})
    missing_backend = tmp_path / "private-api.git"
    write_config(
        work,
        repos=[
            repo_entry("org/api", "backend", missing_backend),
            repo_entry("org/web", "frontend", frontend),
        ],
    )

    checks = doctor.run(Config.load(work), role="frontend")
    by_name = {check.name: check for check in checks}
    assert "repo:org/api" not in by_name
    assert by_name["repo:org/web"].status == doctor.OK
    assert doctor.summarize(checks)["ok"] is True


def test_doctor_warns_when_store_has_no_remote(tmp_path):
    work, bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api", {"src/a.txt": "a\n"})
    write_config(work, repos=[repo_entry("org/api", "backend", backend), repo_entry("org/web", "frontend", backend)])
    config = Config.load(work)

    from helpers import git

    git(work, "remote", "remove", "origin")
    checks = doctor.run(config)
    by_name = {c.name: c for c in checks}
    assert by_name["store:remote"].status == doctor.WARN
    assert by_name["repo:org/api"].status == doctor.OK


def test_doctor_warns_for_unset_verifier_repo_paths(tmp_path):
    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api", {"src/a.txt": "a\n"})
    write_config(
        work,
        repos=[repo_entry("org/api", "backend", backend), repo_entry("org/web", "frontend", backend)],
        checks={
            "test_suites": {"backend-unit": {"repo": "org/api", "argv": ["./gradlew", "test"]}},
            "repo_scripts": {},
            "gate": {"high_risk_paths": [".github/**"]},
            "verifier": {"output_limit_chars": 2000},
        },
    )
    config = Config.load(work)
    checks = doctor.run(config)
    by_name = {c.name: c for c in checks}
    assert by_name["check:backend-unit"].status == doctor.WARN
    assert "repo_paths" in by_name["check:backend-unit"].detail


def test_doctor_checks_lan_notification_secret_and_allowlist(tmp_path, monkeypatch):
    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api", {"src/a.txt": "a\n"})
    write_config(work, repos=[repo_entry("org/api", "backend", backend), repo_entry("org/web", "frontend", backend)])
    (work / "config" / "local.yaml").write_text(
        yaml.safe_dump(
            {
                "notifications": {
                    "url": "http://192.168.1.20:8788/v1/handoff/wake",
                    "listen_host": "0.0.0.0",
                    "listen_port": 8788,
                    "allowed_cidrs": ["192.168.1.10/32"],
                    "secret_env": "HANDOFF_TEST_NOTIFY_SECRET",
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("HANDOFF_TEST_NOTIFY_SECRET", raising=False)
    checks = doctor.run(Config.load(work), role="backend")
    by_name = {c.name: c for c in checks}
    assert by_name["notify:receiver"].status == doctor.OK
    assert by_name["notify:secret"].status == doctor.FAIL

    checks = doctor.run(Config.load(work), role="frontend")
    by_name = {c.name: c for c in checks}
    assert by_name["notify:sender"].status == doctor.OK
    assert by_name["notify:secret"].status == doctor.WARN
    assert "create 只入队" in by_name["notify:secret"].detail

    monkeypatch.setenv("HANDOFF_TEST_NOTIFY_SECRET", "doctor-secret-that-is-at-least-32-bytes")
    checks = doctor.run(Config.load(work), role="backend")
    by_name = {c.name: c for c in checks}
    assert by_name["notify:secret"].status == doctor.OK


def test_doctor_flags_terminal_tasks_without_completion_notification(tmp_path, monkeypatch):
    """09c 事故：verified 了却从没入队 task.completed，必须能在 doctor 里被看见。"""
    from handoff import ids, notification
    from handoff.store import Actor, TaskStore
    from helpers import git, make_task

    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api", {"src/a.txt": "a\n"})
    write_config(
        work,
        repos=[
            repo_entry("org/api", "backend", backend),
            repo_entry("org/web", "frontend", backend),
        ],
    )
    base_sha = git(backend, "rev-parse", "dev")
    store = TaskStore(work, Config.load(work), actor=Actor("user", "alice"))
    created = store.create(make_task(ids.task_id(), base_sha=base_sha))

    checks = {c.name: c for c in doctor.run(Config.load(work))}
    # 没有终态任务时不该误报
    assert checks["notify:completion-backlog"].status == doctor.OK

    # 复刻事故：进入终态时完成通知没进 outbox（旧的 push_enabled 前置条件）
    monkeypatch.setattr(
        notification,
        "emit_task_completed",
        lambda *a, **k: {"status": "disabled", "reason": "not_terminal"},
    )
    store.fail(created["id"], stage="codex", message="模型超时")
    monkeypatch.undo()

    check = {c.name: c for c in doctor.run(Config.load(work))}["notify:completion-backlog"]
    assert check.status == doctor.WARN
    assert created["id"] in check.detail
    assert "reconcile-completed --apply" in check.detail
