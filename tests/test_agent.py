"""会话层：codex 会话保留、resume 指引、把消息推回会话。"""

from __future__ import annotations

import argparse

import pytest

from handoff import executil, ids
from handoff.cli import cmd_notify, cmd_session
from handoff.codex_runner import CodexRunner
from handoff.config import Config
from handoff.store import Actor, TaskStore
from handoff.worker import Worker, WorkerOptions
from helpers import (
    FakeCodex,
    FakeHost,
    git,
    make_bare_remote,
    make_store_repo,
    make_task,
    repo_entry,
    write_config,
)


def build(tmp_path, *, codex=None):
    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api", {"src/orders/Order.java": "class Order {}\n"})
    write_config(
        work,
        repos=[repo_entry("org/api", "backend", backend), repo_entry("org/web", "frontend", backend)],
        codex=codex,
    )
    config = Config.load(work)
    store = TaskStore(work, config, actor=Actor("worker", "w1"))
    task = make_task(ids.task_id(), base_sha=git(backend, "rev-parse", "dev"))
    store.create(task)
    return work, config, backend, store, task


def test_ephemeral_flag_follows_persist(tmp_path):
    ephemeral = CodexRunner(persist=False)._command(tmp_path)
    persisted = CodexRunner(persist=True)._command(tmp_path)
    assert "--ephemeral" in ephemeral
    assert "--ephemeral" not in persisted
    assert persisted[:2][1] == "exec"
    assert "--sandbox" in persisted


def test_worker_records_thread_and_keeps_worktree(tmp_path):
    codex_cfg = {"persist_thread": True, "keep_worktree": True, "timeout_seconds": 60}
    work, config, backend, store, task = build(tmp_path, codex=codex_cfg)
    assert config.codex.persist_thread is True

    worker = Worker(
        work,
        config,
        WorkerOptions(worker_id="w1", lease_seconds=900, heartbeat_interval_seconds=9999),
        store=store,
        codex=FakeCodex({"src/orders/Status.java": "enum Status { OPEN }\n"}),
        host=FakeHost(remotes={"org/api": backend}),
        logger=lambda _m: None,
    )
    worker.run_once()

    current = store.get(task["id"])
    assert current["status"] == "awaiting_merge"
    assert current["result"]["codex_thread_id"] == "thread-fake"
    worktree = current["result"]["worktree"]
    assert worktree and worktree.endswith(f"attempt-{current['attempt']}")
    from pathlib import Path

    assert Path(worktree).is_dir()


def test_worktree_removed_when_not_persisted(tmp_path):
    work, config, backend, store, task = build(tmp_path)
    worker = Worker(
        work,
        config,
        WorkerOptions(worker_id="w1", lease_seconds=900, heartbeat_interval_seconds=9999),
        store=store,
        codex=FakeCodex(),
        host=FakeHost(remotes={"org/api": backend}),
        logger=lambda _m: None,
    )
    worker.run_once()
    from pathlib import Path

    current = store.get(task["id"])
    assert not Path(current["result"]["worktree"]).exists()


def test_session_prints_resume_command(tmp_path):
    codex_cfg = {"persist_thread": True, "keep_worktree": True}
    work, config, backend, store, task = build(tmp_path, codex=codex_cfg)
    worker = Worker(
        work,
        config,
        WorkerOptions(worker_id="w1", lease_seconds=900, heartbeat_interval_seconds=9999),
        store=store,
        codex=FakeCodex(),
        host=FakeHost(remotes={"org/api": backend}),
        logger=lambda _m: None,
    )
    worker.run_once()

    out = cmd_session(argparse.Namespace(task_id=task["id"]), config)
    assert out["thread_id"] == "thread-fake"
    assert out["worktree_exists"] is True
    assert out["resume"].startswith("cd ")
    assert "codex resume thread-fake" in out["resume"]
    assert out["resume_exec"].startswith("codex exec resume thread-fake")


def test_session_without_thread_is_honest(tmp_path):
    """没跑过 codex 的任务不该编出一个 resume 命令。"""
    _work, config, _backend, _store, task = build(tmp_path)
    out = cmd_session(argparse.Namespace(task_id=task["id"]), config)
    assert out["thread_id"] is None
    assert out["worktree"] is None
    assert out["resume"] is None


def test_notify_dry_run(tmp_path):
    if executil.resolve("codex", env_var="HANDOFF_CODEX") is None:
        pytest.skip("本机没有 codex")
    _work, config, *_ = build(tmp_path)
    out = cmd_notify(
        argparse.Namespace(thread="thread-abc", message="任务已交付", dry_run=True),
        config,
    )
    assert out["sent"] is False
    assert "queue" in out["command"]
    assert "--thread thread-abc" in out["command"]
