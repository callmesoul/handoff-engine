"""合并 gate。

自动合并只对低风险任务开放。任一条不满足就留给人。
注意：`gh pr merge --auto` 成功只代表「已开启自动合并」，不代表已合并。
必须继续轮询，直到平台明确返回 MERGED，并读到完整 merge commit OID。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from .config import ChecksConfig
from .errors import ExecutionError
from .git_host import PRState
from .pathgate import high_risk_hits


@dataclass
class GateDecision:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)


def evaluate(task: dict, pr: PRState, checks: ChecksConfig) -> GateDecision:
    reasons: list[str] = []
    passed: list[str] = []
    gate = checks.gate
    result = task["result"]

    if task["status"] != "awaiting_merge":
        reasons.append(f"任务状态是 {task['status']}，不是 awaiting_merge")
    else:
        passed.append("任务处于 awaiting_merge")

    if pr.state.upper() != "OPEN":
        reasons.append(f"PR 状态是 {pr.state}，不是 OPEN")
    else:
        passed.append("PR 处于 OPEN")

    if pr.head_sha and result.get("head_sha") and pr.head_sha != result["head_sha"]:
        reasons.append(f"PR head {pr.head_sha[:12]} 与任务单记录 {result['head_sha'][:12]} 不一致")
    else:
        passed.append("PR head SHA 与任务单一致")

    if gate.required_checks:
        missing = []
        red = []
        for required in gate.required_checks:
            matched = [c for c in pr.checks if required.lower() in c.name.lower()]
            if not matched:
                missing.append(required)
                continue
            for check in matched:
                if check.head_sha and result.get("head_sha") and check.head_sha != result["head_sha"]:
                    red.append(f"{check.name} 的检查结果属于 {check.head_sha[:12]}，不是当前 head")
                elif not check.green:
                    red.append(f"{check.name} 结论是 {check.conclusion}")
        if missing:
            reasons.append(f"缺少必需的 CI 检查: {', '.join(missing)}")
        if red:
            reasons.extend(red)
        if not missing and not red:
            passed.append("必需 CI 检查全绿且对应当前 head")

    if (pr.mergeable or "").upper() != "MERGEABLE":
        reasons.append(f"平台报告 PR 不可自动合并（mergeable={pr.mergeable}, state={pr.merge_state_status}）")
    else:
        passed.append("平台报告可合并")

    migration = task.get("migration") or {}
    if migration.get("required"):
        if gate.deny_auto_merge_if_migration_required:
            reasons.append("任务需要数据库迁移，自动合并关闭")
        elif migration.get("manual_approval_required"):
            reasons.append("迁移要求人工批准")
        else:
            passed.append("迁移已声明且允许自动合并")

    risky = high_risk_hits(result.get("changed_paths") or [], list(gate.high_risk_paths))
    if risky:
        reasons.extend(risky)
    else:
        passed.append("改动路径未命中高风险规则")

    return GateDecision(ok=not reasons, reasons=reasons, passed=passed)


def wait_for_merge(
    host,
    repo: str,
    pr_number: int,
    *,
    timeout_seconds: float = 1800,
    interval_seconds: float = 15,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    logger: Callable[[str], None] | None = None,
) -> PRState:
    """轮询直到 MERGED。超时仍 open/queued 时返回最后一次状态，调用方不得写 merged。"""
    started = clock()
    last = host.get_pr(repo, pr_number)
    while True:
        if last.merged:
            return last
        if last.state.upper() in ("CLOSED",):
            return last
        if clock() - started >= timeout_seconds:
            if logger:
                logger(f"等待 PR #{pr_number} 合并超时（状态 {last.state}）")
            return last
        if logger:
            logger(f"PR #{pr_number} 状态 {last.state}，继续等待合并")
        sleep(interval_seconds)
        last = host.get_pr(repo, pr_number)


def assert_merged(pr: PRState) -> str:
    if not pr.merged or not pr.merge_commit_sha:
        raise ExecutionError(f"PR 尚未合并（state={pr.state}），不能写 merged")
    return pr.merge_commit_sha
