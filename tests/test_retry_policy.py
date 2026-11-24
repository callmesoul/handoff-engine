"""额度/限流失败：记录重试时刻，到点自动重新入队。

这是在真实故障上长出来的：一次 ChatGPT team plan 额度用尽让任务直接判死，
第二天人回来点「重新入队」才继续。这类失败不是「做不成」，只是「现在不行」，
应该自动等、自动再来。

验的是三件事：
1. 可重试失败把 retry_at 写进任务单，不可重试的保持空；
2. 到点后面板后台把他放回 open（失败时遗留的 claim 要一并清掉）；
3. 未到点、已超上限、非可重试的都不许被放回。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from handoff import ids, retry_policy
from handoff.config import Config
from handoff.store import Actor, TaskStore
from handoff.timeutil import iso
from helpers import git, make_bare_remote, make_store_repo, make_task, repo_entry, write_config


QUOTA_FAILURE = {
    "code": "ACCOUNT_QUOTA",
    # 真实措辞：provider 自己给出了恢复时间
    "message": "You have hit your ChatGPT usage limit (team plan). Try again in ~125 min.",
    "status": 429,
}


def _store(tmp_path):
    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api", {"src/orders/Order.java": "class Order {}\n"})
    write_config(
        work,
        repos=[repo_entry("org/api", "backend", backend), repo_entry("org/web", "frontend", backend)],
    )
    config = Config.load(work)
    store = TaskStore(work, config, actor=Actor("worker", "w1"))
    return work, config, store, backend


def _claimed_task(store, backend):
    """造一个「已认领、正在跑」的任务：失败只能从 in_progress 写。"""
    task = make_task(ids.task_id(), base_sha=git(backend, "rev-parse", "dev"), checks=[])
    store.create(task)
    claimed = store.claim(task["id"], worker_id="w1", base_sha=git(backend, "rev-parse", "dev"))
    store.transition(
        claimed["id"],
        "in_progress",
        expected_revision=claimed["revision"],
        summary="开始执行",
        actor=Actor("worker", "w1"),
    )
    return store.get(task["id"])


def test_quota_failure_records_a_retry_time(tmp_path):
    """可重试失败要把 retry_at 与次数写到任务单上，人能直接看到还要等多久。"""
    from handoff import retry_policy as rp

    _work, _config, store, backend = _store(tmp_path)
    task = _claimed_task(store, backend)

    plan = rp.retry_plan(QUOTA_FAILURE, 0)
    assert plan["retryable"] is True
    assert plan["retry_at"] is not None

    failed = store.fail(
        task["id"],
        stage="codex",
        message="DSH 执行失败: Insufficient Balance",
        expected_revision=task["revision"],
        retry=plan,
    )
    failure = failed["failure"]
    assert failed["status"] == "failed"
    assert failure["retryable"] is True
    assert failure["retry_at"]
    assert failure["retry_count"] == 0


def test_non_retryable_failure_leaves_no_retry_time(tmp_path):
    """任务本身的问题不该排队重试，否则一次明确的失败会被拖成好几次。"""
    from handoff import retry_policy as rp

    _work, _config, store, backend = _store(tmp_path)
    task = _claimed_task(store, backend)

    plan = rp.retry_plan({"code": "CONTENT_REFUSED", "message": "blocked by policy"}, 0)
    assert plan["retryable"] is False
    assert plan["retry_at"] is None

    failed = store.fail(
        task["id"],
        stage="codex",
        message="内容被拒",
        expected_revision=task["revision"],
        retry=plan,
    )
    assert failed["failure"]["retryable"] is False
    assert failed["failure"]["retry_at"] is None


def test_due_retry_is_requeued_and_clears_the_stale_claim(tmp_path):
    """到点回收：failed → open，且失败时遗留的 claim 必须清掉。

    不清 claim 的话，下一次 claim 会撞「租约未过期」，重试等于没做。
    """
    work, config, store, backend = _store(tmp_path)
    task = _claimed_task(store, backend)

    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    store.fail(
        task["id"],
        stage="codex",
        message="额度用尽",
        expected_revision=task["revision"],
        retry={"retryable": True, "retry_at": past, "retry_count": 0},
    )
    assert store.get(task["id"])["status"] == "failed"

    from handoff.panel.service import PanelService

    service = PanelService(work, config)
    try:
        reopened = service._requeue_due_retries()
        assert task["id"] in reopened
    finally:
        service.shutdown()

    final = store.get(task["id"])
    assert final["status"] == "open"
    assert final["claim"] is None


def test_future_retry_is_not_requeued_early(tmp_path):
    """没到点不许提前放回，否则等于把额度又撞一次。"""
    work, config, store, backend = _store(tmp_path)
    task = _claimed_task(store, backend)

    later = datetime.now(timezone.utc) + timedelta(hours=2)
    store.fail(
        task["id"],
        stage="codex",
        message="额度用尽",
        expected_revision=task["revision"],
        retry={"retryable": True, "retry_at": later, "retry_count": 0},
    )

    from handoff.panel.service import PanelService

    service = PanelService(work, config)
    try:
        assert service._requeue_due_retries() == []
    finally:
        service.shutdown()
    assert store.get(task["id"])["status"] == "failed"


def test_exhausted_retries_are_never_requeued(tmp_path):
    """超过上限之后必须停手，不能无限重试。"""
    from handoff import retry_policy as rp

    _work, _config, store, backend = _store(tmp_path)
    task = _claimed_task(store, backend)

    plan = rp.retry_plan(QUOTA_FAILURE, 3, max_retries=3)
    assert plan["retryable"] is False
    assert plan["retry_at"] is None
    assert "上限" in plan["reason"]

    failed = store.fail(
        task["id"],
        stage="codex",
        message="额度用尽",
        expected_revision=task["revision"],
        retry=plan,
    )
    assert failed["failure"]["retryable"] is False


@pytest.mark.parametrize(
    "code",
    ["QUOTA", "ACCOUNT_QUOTA", "RATE_LIMIT", "SERVER", "TIMEOUT", "TRANSPORT", "STREAM_CLOSED"],
)
def test_retryable_codes(code):
    assert retry_policy.is_retryable({"code": code, "message": ""}) is True


@pytest.mark.parametrize(
    "code",
    ["CONTENT_REFUSED", "INVALID_REQUEST", "ABORTED", "UNKNOWN_MODEL", "AUTH", "INVALID_CREDENTIAL"],
)
def test_non_retryable_codes(code):
    assert retry_policy.is_retryable({"code": code, "message": ""}) is False


def test_provider_hint_wins_over_backoff():
    """provider 说「125 分钟后再来」时，别自作聪明 5 分钟就撞一次。"""
    hint = retry_policy.parse_retry_hint_seconds(QUOTA_FAILURE)
    assert hint == pytest.approx(125 * 60, abs=1)
    delay = retry_policy.backoff_seconds(0)
    assert hint > delay
