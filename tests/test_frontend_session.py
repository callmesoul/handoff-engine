"""后端完成事件回推到原前端 Codex 会话。"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace

from handoff import notification
from handoff.cli import main as cli_main
from handoff.config import Config, NotificationConfig
from handoff.frontend_session import (
    FrontendDispatcher,
    FrontendSessions,
    discover_codex_desktop_env,
)
from handoff.gitcmd import Result
from helpers import make_bare_remote, make_store_repo, repo_entry, write_config


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


def test_completion_round_trip_queues_original_thread_once(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_FRONTEND_SESSION_SECRET", SECRET)
    monkeypatch.setenv("HANDOFF_CODEX", "/bin/true")
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return Result(argv, 0, "", "")

    cfg = config(tmp_path)
    dispatcher = FrontendDispatcher(
        tmp_path,
        cfg,
        runner=runner,
        codex_env_resolver=lambda: {
            "CODEX_HOME": "/desktop/codex",
            "CODEX_APP_TOOLS_PIPE_PATH": r"\\.\pipe\codex-app-tools-current",
        },
    )
    dispatcher.register(TASK_ID, thread_id="thread-original", workspace=tmp_path)
    server = notification.WakeServer(tmp_path, cfg, on_event=dispatcher.enqueue)
    host, port = server.start()
    try:
        sender = notification.Notifier(
            tmp_path,
            replace(cfg, url=f"http://{host}:{port}{notification.COMPLETE_PATH}"),
        )
        payload = completed()
        event_id = sender.enqueue(payload)
        assert sender.send_due(event_id=event_id)["sent"] == 1
        assert dispatcher.send_due() == {"sent": 1, "failed": 0, "pending": 0, "errors": []}

        history = dispatcher.sessions.completion_history(task_id=TASK_ID, dispatch_state="dispatched")
        assert history["summary"] == {"total": 1, "pending": 0, "failed": 0, "dispatched": 1}
        assert history["matched"] == 1
        assert history["records"][0]["event_id"] == event_id
        assert history["records"][0]["dispatch_state"] == "dispatched"
        assert history["records"][0]["task_status"] == "verified"
        assert history["records"][0]["dispatched_at"] is not None

        argv, kwargs = calls[0]
        assert argv[:4] == ["/bin/true", "queue", "--thread", "thread-original"]
        assert "handoffctl show" in argv[5]
        assert TASK_ID in argv[5]
        # 前端验收通过后要标记 frontend_accepted（前端已验收）
        assert "handoffctl accept" in argv[5]
        assert "前端已验收" in argv[5]
        assert kwargs["cwd"] == str(tmp_path)
        assert kwargs["env"] == {
            "CODEX_HOME": "/desktop/codex",
            "CODEX_APP_TOOLS_PIPE_PATH": r"\\.\pipe\codex-app-tools-current",
        }

        # 至少一次网络投递可以重复，但 event_id 幂等，不能再次 queue。
        sender._send(sender.outbox.status(event_id), SECRET.encode("utf-8"))
        assert dispatcher.send_due()["sent"] == 0
        assert len(calls) == 1
    finally:
        server.stop()


def test_completion_without_session_stays_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_CODEX", "/bin/true")
    cfg = config(tmp_path)
    dispatcher = FrontendDispatcher(tmp_path, cfg)
    dispatcher.enqueue(completed())

    result = dispatcher.send_due()

    assert result["sent"] == 0
    assert result["failed"] == 1
    assert result["pending"] == 1
    assert "未登记原前端会话" in result["errors"][0]["error"]

    history = dispatcher.sessions.completion_history(dispatch_state="failed")
    assert history["summary"] == {"total": 1, "pending": 0, "failed": 1, "dispatched": 0}
    assert history["records"][0]["attempts"] == 1
    assert "未登记原前端会话" in history["records"][0]["last_error"]
    assert history["records"][0]["last_stderr"] is None


def test_codex_dispatch_persists_sanitized_stderr(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_CODEX", "/bin/false")

    def runner(argv, **kwargs):
        return Result(
            argv,
            1,
            "",
            "no rollout found for thread id; auth_token=do-not-store-this-value",
        )

    cfg = config(tmp_path)
    dispatcher = FrontendDispatcher(
        tmp_path,
        cfg,
        runner=runner,
        codex_env_resolver=lambda: {"CODEX_HOME": "/desktop/codex"},
    )
    dispatcher.register(TASK_ID, thread_id="thread-original", workspace=tmp_path)
    assert dispatcher.enqueue(completed())

    result = dispatcher.send_due()

    assert result["failed"] == 1
    assert result["errors"][0]["stderr"] == "no rollout found for thread id; auth_token=[redacted]"
    history = dispatcher.sessions.completion_history(task_id=TASK_ID, dispatch_state="failed")
    assert history["records"][0]["last_error"].startswith("恢复原前端会话失败")
    assert history["records"][0]["last_stderr"] == result["errors"][0]["stderr"]


def test_discovers_active_desktop_env_instead_of_stale_service_pipe(tmp_path):
    proc = tmp_path / "proc"
    stale = proc / "900"
    active = proc / "100"
    stale.mkdir(parents=True)
    active.mkdir(parents=True)
    stale.joinpath("environ").write_bytes(
        b"CODEX_HOME=/stale\0"
        b"CODEX_APP_TOOLS_PIPE_PATH=stale-pipe\0"
        b"CODEX_INTERNAL_ORIGINATOR_OVERRIDE=Codex Desktop\0"
    )
    active.joinpath("environ").write_bytes(
        b"CODEX_HOME=/desktop\0"
        b"CODEX_SQLITE_HOME=/desktop/sqlite\0"
        b"CODEX_APP_TOOLS_PIPE_PATH=current-pipe\0"
        b"CODEX_INTERNAL_ORIGINATOR_OVERRIDE=Codex Desktop\0"
        b"CODEX_THREAD_ID=thread-current\0"
        b"CODEX_TOOL_CALL_ID=tool-current\0"
    )

    found = discover_codex_desktop_env(
        environ={
            "CODEX_HOME": "/service",
            "CODEX_APP_TOOLS_PIPE_PATH": "service-stale-pipe",
            "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop",
        },
        proc_root=proc,
        current_pid=999,
    )

    assert found == {
        "CODEX_HOME": "/desktop",
        "CODEX_SQLITE_HOME": "/desktop/sqlite",
        "CODEX_APP_TOOLS_PIPE_PATH": "current-pipe",
        "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop",
    }


def test_existing_completion_database_adds_stderr_column(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (
                task_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                workspace TEXT NOT NULL,
                registered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE completions (
                event_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL,
                last_error TEXT,
                created_at TEXT NOT NULL,
                dispatched_at TEXT
            );
            """
        )

    FrontendSessions(database)

    with sqlite3.connect(database) as conn:
        completion_columns = {row[1] for row in conn.execute("PRAGMA table_info(completions)")}
        session_columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    assert "last_stderr" in completion_columns
    assert "executor" in session_columns


def test_frontend_completions_cli_queries_dispatched_history(tmp_path, capsys):
    work, _ = make_store_repo(tmp_path)
    api = make_bare_remote(tmp_path, "api", {"README.md": "api"})
    write_config(work, repos=[repo_entry("org/api", "backend", api)])
    cfg = Config.load(work)
    sessions = FrontendSessions(cfg.notifications.session_path(work))
    payload = completed()
    assert sessions.enqueue_completion(payload)
    sessions.dispatched(payload["event_id"])

    code = cli_main(
        [
            "--root",
            str(work),
            "--json",
            "frontend-completions",
            "--task-id",
            TASK_ID,
            "--state",
            "dispatched",
        ]
    )

    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["summary"] == {"total": 1, "pending": 0, "failed": 0, "dispatched": 1}
    assert output["matched"] == 1
    assert output["returned"] == 1
    assert output["records"][0]["event_id"] == payload["event_id"]
    assert output["records"][0]["store_commit"] == "c" * 40


def test_task_created_inbox_remembers_signed_reply_url(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_FRONTEND_SESSION_SECRET", SECRET)
    wake = threading.Event()
    cfg = config(tmp_path)
    server = notification.WakeServer(tmp_path, cfg, wake_event=wake)
    host, port = server.start()
    try:
        reply_url = "http://192.168.1.3:8789/v1/handoff/completed"
        payload = notification.task_created_payload(
            task_id=TASK_ID,
            revision=1,
            store_commit="d" * 40,
            reply_url=reply_url,
        )
        sender = notification.Notifier(
            tmp_path,
            replace(cfg, url=f"http://{host}:{port}{notification.WAKE_PATH}"),
        )
        event_id = sender.enqueue(payload)
        assert sender.send_due(event_id=event_id)["sent"] == 1
        assert wake.wait(1)
        assert server.inbox.completion_url(TASK_ID) == reply_url
    finally:
        server.stop()
