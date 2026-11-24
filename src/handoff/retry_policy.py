"""可重试失败的识别与退避排程。

额度用尽、被限流、服务端故障这类失败，**过一段时间重试就会好**；它们不该像
「代码写错了」那样把任务按死在 `failed` 等人点按钮。这个模块负责两件事：

1. 判断一次失败属不属于这一类（{@link is_retryable}）；
2. 给出下一次该在什么时候重试（{@link next_retry_at}），以及还能重试几次
   （{@link should_retry}）。

判定依据是宿主回传的**结构化失败码**（`turn/end.reason.error.code`，取值来自
`@deepseek-ai/dsh-llm` 的 `LLM_FAILURE_CODES`），不是猜文案——措辞会变，码不会。
只有在拿不到稳定码时才退回看 HTTP 状态与少数措辞。

退避策略：优先相信 provider 自己给出的「多久以后再来」（`Try again in ~125 min`
这类提示），因为那是唯一准确的答案；没有提示时按指数退避兜底。无论哪条路都封顶，
避免一次额度问题让任务无限重试。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

#: 一定属于「过一会儿可能就好」的失败码。
RETRYABLE_CODES = frozenset(
    {
        # 额度 / 限流
        "QUOTA",
        "ACCOUNT_QUOTA",
        "RATE_LIMIT",
        # 服务端 / 传输：换一次请求往往就通了
        "SERVER",
        "TIMEOUT",
        "TRANSPORT",
        "STREAM_CLOSED",
        "OVERLOADED",
        "SERVICE_UNAVAILABLE",
    }
)

#: 拿不到稳定码时，这几类 HTTP 状态仍可视为「等一下再试」。
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

#: 只有 UNKNOWN/空码时才看措辞。
_RETRYABLE_MARKERS = (
    "quota",
    "rate limit",
    "ratelimit",
    "too many requests",
    "usage limit",
    "try again",
    "overloaded",
    "temporarily unavailable",
    "insufficient balance",
    "额度",
    "限流",
)

#: 明确**不该**重试的码：任务本身或请求本身有问题，重试只会把一次明确的失败拖长。
NON_RETRYABLE_CODES = frozenset(
    {
        "CONTENT_REFUSED",
        "INVALID_REQUEST",
        "ABORTED",
        "CANCELLED",
        "INVALID_CREDENTIAL",
        "MISSING_CREDENTIAL",
        "NO_CREDENTIAL_STORE",
        "UNKNOWN_MODEL",
        "MODEL_UNSUPPORTED",
        "NO_ADAPTER",
        "INVALID_ADAPTER",
        "INVALID_CATALOG",
        "INVALID_MODEL_INFO",
        "DISCOVERY_FAILED",
        "DISCOVERY_UNSUPPORTED",
        "UNSUPPORTED_REASONING_EFFORT",
        "CONTEXT_WINDOW_EXCEEDED",
        "AUTH",
        "ACCOUNT_SIGN_IN_REQUIRED",
        "ACCOUNT_TOKEN_INVALID",
    }
)

#: 默认最多自动重试几次（不含第一次执行）。
DEFAULT_MAX_RETRIES = 3
#: 没有 provider 提示时的初始退避。
DEFAULT_BASE_SECONDS = 300
#: 单次退避上限（1 小时）。provider 说更久也照它说的，但兜底不会超过这个的两倍。
MAX_BACKOFF_SECONDS = 3600

#: 「Try again in ~125 min」「retry after 90 seconds」「请在 2 小时后重试」…
_DURATION_PATTERNS = (
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b", re.I), 3600.0),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:m|min|mins|minute|minutes)\b", re.I), 60.0),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds)\b", re.I), 1.0),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:小时|钟头)"), 3600.0),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:分钟|分)"), 60.0),
    (re.compile(r"(\d+(?:\.\d+)?)\s*秒"), 1.0),
)

#: 绝对时刻形式的恢复时间，例如 workbuddy 的
#: 「将在 2026-11-24 16:40:38 UTC+8 重置」。
#:
#: 这类提示比相对时长更精确，但也更容易被误读：必须连时区一起认，否则会把
#: 本地时间当成 UTC，差出好几个小时。只认带明确时区标记的写法；没有时区的
#: 日期一律不猜（宁可退回退避，也不要在错误的时间点撞额度）。
_RESET_UTC_OFFSET = re.compile(
    r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)\s*(?:UTC|GMT)\s*([+-]\d{1,2})(?::?(\d{2}))?",
    re.I,
)
_RESET_ISO_OFFSET = re.compile(
    r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)(Z|[+-]\d{2}:?\d{2})\b"
)


def _message_of(failure: dict | None) -> str:
    return str((failure or {}).get("message") or "")


def is_retryable(failure: dict | None) -> bool:
    """这次失败值得「过一会儿自动再来一次」吗？

    只看结构化的 `code` / `status`；两者都没有结论时才看措辞。刻意不把
    `CONTENT_REFUSED`、`INVALID_REQUEST` 这类算进来——那是任务或请求本身有问题，
    自动重试只会让一次明确的失败变成好几次。
    """
    if not failure:
        return False
    code = str(failure.get("code") or "").strip()
    if code and code != "UNKNOWN":
        if code in NON_RETRYABLE_CODES:
            return False
        return code in RETRYABLE_CODES
    status = failure.get("status")
    if isinstance(status, int) and status in RETRYABLE_STATUSES:
        return True
    message = _message_of(failure).lower()
    return any(marker in message for marker in _RETRYABLE_MARKERS)


def parse_retry_hint_seconds(failure: dict | None) -> float | None:
    """从失败描述里读出 provider 自己给的「多久以后再来」。

    这是唯一准确的答案：`You have hit your ChatGPT usage limit (team plan).
    Try again in ~125 min.` 里的 125 分钟比任何指数退避都准。读不到就返回 None，
    交给 {@link next_retry_at} 用指数退避兜底。
    """
    message = _message_of(failure)
    if not message:
        return None
    best: float | None = None
    for pattern, unit in _DURATION_PATTERNS:
        for match in pattern.finditer(message):
            try:
                value = float(match.group(1)) * unit
            except (TypeError, ValueError):
                continue
            if value <= 0:
                continue
            # 同一句话里可能出现多个时长（"1 hour 30 min"），取最大那个更安全：
            # 早于额度恢复时刻重试必然再失败一次。
            if best is None or value > best:
                best = value
    return best


def parse_retry_reset_at(
    failure: dict | None, *, now: datetime | None = None
) -> datetime | None:
    """从失败描述里读出 provider 给出的**绝对恢复时刻**（UTC）。

    有些 provider 不给「多久以后」，而是直接说「将在 2026-11-24 16:40:38 UTC+8
    重置」。这类信息更准，但必须连时区一起认——把本地时间当 UTC 会差出几个小时，
    重试要么太早（必然再撞），要么太晚（白等）。

    只认带明确时区标记的写法；没有时区的日期不猜，返回 None 交给退避兜底。
    解析出的时刻若已经过去，也返回 None（当作没给提示）。
    """
    message = _message_of(failure)
    if not message:
        return None
    current = now or datetime.now(timezone.utc)

    # 形如「2026-11-24 16:40:38 UTC+8」：日期 + 时间 + UTC/GMT + 带符号小时[:分钟]
    for match in _RESET_UTC_OFFSET.finditer(message):
        date_part, time_part, hour_part, minute_part = match.groups()
        offset = _offset_of(hour_part, minute_part)
        resolved = _to_utc(date_part, time_part, offset)
        if resolved is not None and resolved > current:
            return resolved

    # 形如「2026-11-24T16:40:38+08:00」或「…Z」
    for match in _RESET_ISO_OFFSET.finditer(message):
        date_part, time_part, tail = match.groups()
        if tail.upper() == "Z":
            offset = timezone.utc
        else:
            sign = "-" if tail.startswith("-") else "+"
            digits = tail.lstrip("+-").replace(":", "")
            offset = _offset_of(f"{sign}{int(digits[:2])}", digits[2:])
        resolved = _to_utc(date_part, time_part, offset)
        if resolved is not None and resolved > current:
            return resolved

    return None


def _offset_of(hour_part: str, minute_part: str | None) -> timezone:
    """把「+8」/「-05」这样的小时段和可选分钟段拼成 tzinfo。"""
    hours = int(hour_part)
    minutes = int(minute_part or 0)
    sign = -1 if hours < 0 else 1
    return timezone(timedelta(minutes=sign * (abs(hours) * 60 + minutes)))


def _to_utc(date_part: str, time_part: str, offset: timezone) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(f"{date_part}T{time_part}")
    except ValueError:
        return None
    return stamp.replace(tzinfo=offset).astimezone(timezone.utc)


def backoff_seconds(retry_count: int, *, base: int = DEFAULT_BASE_SECONDS) -> int:
    """第 `retry_count` 次自动重试前该等多久（指数退避，封顶）。"""
    count = max(0, int(retry_count))
    seconds = float(base) * (2 ** count)
    return int(min(seconds, MAX_BACKOFF_SECONDS))


def next_retry_at(
    failure: dict | None,
    retry_count: int,
    *,
    now: datetime | None = None,
    base: int = DEFAULT_BASE_SECONDS,
) -> datetime | None:
    """算出下一次重试的时刻（UTC）。不可重试时返回 None。

    优先级：provider 给的**绝对恢复时刻** > provider 给的**相对时长** > 指数退避。
    绝对时刻最准（它连日期都说了）；相对时长次之；都没有才自己退避。
    """
    if not is_retryable(failure):
        return None
    current = now or datetime.now(timezone.utc)
    # 绝对时刻优先：多留 60 秒余量，避免卡在恢复的同一秒再撞一次
    reset_at = parse_retry_reset_at(failure, now=current)
    if reset_at is not None:
        return reset_at + timedelta(seconds=60)
    hint = parse_retry_hint_seconds(failure)
    if hint is not None:
        # 提示是「还没好之前别来」，多给 60 秒余量；但别因此把封顶放大太多
        delay = min(hint + 60.0, MAX_BACKOFF_SECONDS * 2)
    else:
        delay = backoff_seconds(retry_count, base=base)
    return current + timedelta(seconds=delay)


def should_retry(retry_count: int, *, max_retries: int = DEFAULT_MAX_RETRIES) -> bool:
    """这个任务还能自动重试吗？`retry_count` 是**已经**重试过的次数。"""
    return max(0, int(retry_count)) < max(0, int(max_retries))


def retry_plan(
    failure: dict | None,
    retry_count: int,
    *,
    now: datetime | None = None,
    base: int = DEFAULT_BASE_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict:
    """一次失败结果的完整处置建议，供调用方直接写进任务单。

    返回 `{retryable, retry_at, retry_count, reason}`：
    - `retryable=False`：不该自动重试（任务本身的问题，或已超过次数上限）；
    - `retry_at` 非空：到点把任务放回 open，自动循环会重新认领。
    """
    current_retry = max(0, int(retry_count))
    if not is_retryable(failure):
        return {
            "retryable": False,
            "retry_at": None,
            "retry_count": current_retry,
            "reason": "失败原因不属于可自动重试的类别",
        }
    if not should_retry(current_retry, max_retries=max_retries):
        return {
            "retryable": False,
            "retry_at": None,
            "retry_count": current_retry,
            "reason": f"已达到自动重试上限（{max_retries} 次）",
        }
    at = next_retry_at(failure, current_retry, now=now, base=base)
    return {
        "retryable": True,
        "retry_at": at,
        "retry_count": current_retry,
        "reason": "等待额度/服务恢复后自动重新入队",
    }


__all__ = [
    "DEFAULT_BASE_SECONDS",
    "DEFAULT_MAX_RETRIES",
    "MAX_BACKOFF_SECONDS",
    "NON_RETRYABLE_CODES",
    "RETRYABLE_CODES",
    "RETRYABLE_STATUSES",
    "backoff_seconds",
    "is_retryable",
    "next_retry_at",
    "parse_retry_hint_seconds",
    "parse_retry_reset_at",
    "retry_plan",
    "should_retry",
]
