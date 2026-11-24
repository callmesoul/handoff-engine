"""写入冲突重试：基准 revision 必须跟着刷新。

真实故障：心跳线程每次续租都会 +1 revision，worker 主线程拿着认领时读到的旧
revision 去写失败状态，三次重试全部撞同一个冲突，任务莫名停在
`failed(internal): 写入冲突，已重试 3 次`——其实一次有效重试都没发生。
"""

from __future__ import annotations

import pytest

from handoff import ids
from handoff.config import Config
from handoff.errors import ConflictError, HandoffError
from handoff.store import Actor, TaskStore
from handoff.timeutil import iso, now
from helpers import git, make_bare_remote, make_store_repo, make_task, repo_entry, write_config


def _heartbeat_stamp() -> str:
    """一个「晚于 claimed_at」的合法心跳时刻（固定字符串会随真实时钟失效）。"""
    return iso(now())


def _store(tmp_path):
    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api", {"src/orders/Order.java": "class Order {}\n"})
    write_config(
        work,
        repos=[repo_entry("org/api", "backend", backend), repo_entry("org/web", "frontend", backend)],
    )
    config = Config.load(work)
    store = TaskStore(work, config, actor=Actor("worker", "w1"))
    task = make_task(ids.task_id(), base_sha=git(backend, "rev-parse", "dev"), checks=[])
    store.create(task)
    return work, config, store, task


def test_write_succeeds_when_revision_advanced_meanwhile(tmp_path):
    """拿旧 revision 去写，但中途 revision 已经前进：应当重试后成功，而不是报冲突。

    这条直接复现「心跳抢占 revision → 主线程写入失败」的事故。
    """
    _work, _config, store, task = _store(tmp_path)
    claimed = store.claim(task["id"], worker_id="w1", base_sha=task["target"]["base_sha"])
    store.transition(
        claimed["id"],
        "in_progress",
        expected_revision=claimed["revision"],
        summary="开始执行",
        actor=Actor("worker", "w1"),
    )
    before = store.get(task["id"])

    # 模拟心跳线程先写了一次（revision 前进）
    store.update(
        task["id"],
        expected_revision=before["revision"],
        mutate=lambda t: t["claim"].__setitem__("heartbeat_at", _heartbeat_stamp()),
        event_type="claim.heartbeat",
        summary="续租",
        actor=Actor("worker", "w1"),
    )
    advanced = store.get(task["id"])
    assert advanced["revision"] == before["revision"] + 1

    # 主线程仍拿着旧 revision 写 —— 修复前这里必然抛「写入冲突」
    written = store.update(
        task["id"],
        expected_revision=before["revision"],
        mutate=lambda t: t["result"].__setitem__("notes", "写进去了"),
        event_type="task.status_changed",
        summary="带着旧 revision 写入",
        actor=Actor("worker", "w1"),
    )
    assert written["result"]["notes"] == "写进去了"
    assert written["revision"] == before["revision"] + 2


def test_real_conflict_still_raises_when_mutate_rejects(tmp_path):
    """基准可以放开，但业务前置条件不能被绕过：非法状态跳转仍然要抛。"""
    _work, _config, store, task = _store(tmp_path)

    claimed = store.claim(task["id"], worker_id="w1", base_sha=task["target"]["base_sha"])
    store.transition(
        claimed["id"],
        "in_progress",
        expected_revision=claimed["revision"],
        summary="开始执行",
        actor=Actor("worker", "w1"),
    )
    earlier = store.get(task["id"])

    # 心跳让 revision 前进
    store.update(
        task["id"],
        expected_revision=earlier["revision"],
        mutate=lambda t: t["claim"].__setitem__("heartbeat_at", _heartbeat_stamp()),
        event_type="claim.heartbeat",
        summary="续租",
        actor=Actor("worker", "w1"),
    )
    current = store.get(task["id"])
    store.fail(
        task["id"],
        stage="codex",
        message="boom",
        expected_revision=current["revision"],
        actor=Actor("worker", "w1"),
    )

    # 现在从 failed 直接转 delivered 是非法的；基准放开也不许写进去
    with pytest.raises(HandoffError):
        store.transition(
            task["id"],
            "delivered",
            expected_revision=earlier["revision"],
            summary="非法跳转",
            actor=Actor("worker", "w1"),
        )
