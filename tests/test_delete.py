"""任务删除 CLI：显式确认、关联数据、通知与索引一致性。"""

from __future__ import annotations

import argparse

import pytest

from handoff import ids, notification
from handoff.admin.indexer import Indexer
from handoff.cli import cmd_delete
from handoff.config import Config
from handoff.errors import SchemaError
from handoff.frontend_session import FrontendSessions
from handoff.store import TaskStore
from helpers import git, make_bare_remote, make_store_repo, make_task, repo_entry, write_config


def build(tmp_path):
    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api", {"src/orders/Order.java": "class Order {}\n"})
    write_config(
        work,
        repos=[repo_entry("org/api", "backend", backend), repo_entry("org/web", "frontend", backend)],
    )
    config = Config.load(work)
    store = TaskStore(work, config)
    task = make_task(ids.task_id(), base_sha=git(backend, "rev-parse", "dev"))
    created = store.create(task)
    return work, config, task, created


def test_delete_cli_requires_yes(tmp_path):
    _work, config, task, _created = build(tmp_path)
    args = argparse.Namespace(task_ids=[task["id"]], all=False, yes=False, no_push=False)
    with pytest.raises(SchemaError, match="--yes"):
        cmd_delete(args, config)


def test_delete_cli_cleans_notification_and_existing_index(tmp_path):
    work, config, task, created = build(tmp_path)
    outbox = notification.Outbox(config.notifications.outbox_path(work))
    payload = notification.task_created_payload(
        task_id=task["id"],
        revision=1,
        store_commit=created["_commit"],
    )
    outbox.enqueue(payload, "http://127.0.0.1:8788/v1/handoff/wake")
    sessions = FrontendSessions(config.notifications.session_path(work))
    sessions.register(task["id"], thread_id="frontend-thread", workspace=work)
    index = Indexer(config)
    index.index_incremental()

    result = cmd_delete(
        argparse.Namespace(task_ids=[], all=True, yes=True, no_push=False),
        config,
    )

    assert result["deleted"] == [task["id"]]
    assert result["notifications_deleted"] == 1
    assert result["frontend_sessions_deleted"] == 1
    assert outbox.status(payload["event_id"]) is None
    assert sessions.session(task["id"]) is None
    with index.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert git(work, "rev-parse", "HEAD") == git(work, "rev-parse", "origin/main")
