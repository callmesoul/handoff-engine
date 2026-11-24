"""状态机白名单。

一套定义，不允许出现第二种。任何状态写入都必须经过 assert_transition。

    PR 模式：  open → claimed → in_progress → delivered → awaiting_merge → merged ─┐
                                                                                  ├→ deployed → verifying → verified
    目录模式： open → claimed → in_progress → delivered ──────────────────────────┘

    目录模式（无 PR）：改动直接提交在目标分支，没有合并环节，
    delivered 直接 → deployed；误入 awaiting_merge 的目录模式任务同样从 deployed 走。
    merged 仅属于 PR 模式（带 merge_sha）。

    任意非终态 → failed / cancelled
    failed → open（重新入队，attempt+1）/ claimed（原地重试，attempt+1）
    failed(verification) → deployed（修正验收契约后重新验收，不增加 attempt）
    failed(交付已部署) → deployed（重新验收，不增加 attempt；如执行器被取消但产物已上线）
    delivered / verified / frontend_accepted → open（会话续跑：任务结束后用户在会话里继续派活，attempt+1）

    前端验收：verified → frontend_accepted。
    后端验收通过（verified）后，前端收到完成通知并自行验收成功，再把任务标记为
    frontend_accepted（终态记录）。可经 `handoffctl accept` 或交接面板按钮触发。
"""

from __future__ import annotations

from .errors import TransitionError

STATUSES = (
    "open",
    "claimed",
    "in_progress",
    "delivered",
    "awaiting_merge",
    "merged",
    "deployed",
    "verifying",
    "verified",
    "frontend_accepted",
    "failed",
    "cancelled",
)

TERMINAL = frozenset({"verified", "frontend_accepted", "cancelled"})

ALLOWED: dict[str, frozenset[str]] = {
    "open": frozenset({"claimed", "failed", "cancelled"}),
    "claimed": frozenset({"in_progress", "open", "failed", "cancelled"}),
    "in_progress": frozenset({"delivered", "open", "failed", "cancelled"}),
    "delivered": frozenset({"awaiting_merge", "deployed", "failed", "cancelled", "open"}),
    "awaiting_merge": frozenset({"merged", "deployed", "failed", "cancelled"}),
    "merged": frozenset({"deployed", "failed", "cancelled"}),
    "deployed": frozenset({"verifying", "failed", "cancelled"}),
    "verifying": frozenset({"verified", "failed", "cancelled"}),
    # 重新验收：verified 先回 deployed，再跑 verifying
    "verified": frozenset({"deployed", "open", "frontend_accepted"}),
    # 前端已验收：终态记录；会话续跑时仍可回到 open
    "frontend_accepted": frozenset({"open"}),
    "failed": frozenset({"open", "claimed", "cancelled"}),
    "cancelled": frozenset(),
}

# 这些状态必须带租约
CLAIMED_STATES = frozenset({"claimed", "in_progress"})

# 这些状态不允许再改代码
POST_MERGE_STATES = frozenset({"merged", "deployed", "verifying", "verified", "frontend_accepted"})


def can_transition(src: str, dst: str) -> bool:
    return dst in ALLOWED.get(src, frozenset())


def assert_transition(src: str, dst: str, task_id: str = "") -> None:
    if src == dst:
        raise TransitionError(f"{task_id}: 状态没有变化（{src}）", task_id=task_id, src=src, dst=dst)
    if src not in STATUSES:
        raise TransitionError(f"{task_id}: 未知源状态 {src!r}", task_id=task_id, src=src, dst=dst)
    if not can_transition(src, dst):
        allowed = ", ".join(sorted(ALLOWED[src])) or "<终态>"
        raise TransitionError(
            f"{task_id}: 非法状态跳转 {src} → {dst}（允许：{allowed}）",
            task_id=task_id,
            src=src,
            dst=dst,
        )


def assert_reverify_transition(task: dict, task_id: str = "") -> None:
    """重新验收的窄入口；实现失败不能借此绕过 worker。

    failed 但交付已部署对齐（deployment.status=deployed 且有 target/observed_sha）
    的情况——典型是执行器中途被取消、验收没跑完——同样放行：重新验收只是重跑
    环境检查，不重做实现，不构成绕过 worker；否则这种任务只能重新入队，为了一句
    验收结果重跑一次完整执行。
    """
    src = task.get("status")
    if src in ("deployed", "verified"):
        return
    failure = task.get("failure") or {}
    deployment = task.get("deployment") or {}
    if src == "failed" and failure.get("stage") == "verification":
        return
    if (
        src == "failed"
        and deployment.get("status") == "deployed"
        and deployment.get("target_sha")
        and deployment.get("observed_sha")
    ):
        return
    raise TransitionError(
        f"{task_id}: 只有 deployed/verified、failed(verification) 或 failed 但交付已部署的任务可以重新验收"
        f"（当前 status={src!r}, failure.stage={failure.get('stage')!r}, deployment={deployment.get('status')!r}）",
        task_id=task_id,
        src=src,
        dst="deployed",
    )


def describe_all() -> dict[str, list[str]]:
    return {src: sorted(dst) for src, dst in ALLOWED.items()}
