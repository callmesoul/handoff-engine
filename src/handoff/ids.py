"""ULID：48 位毫秒时间戳 + 80 位随机数，Crockford Base32，字典序即时间序。

任务 ID 与事件 ID 都用它。不做目录序号扫描，避免多写者撞号。
"""

from __future__ import annotations

import os
import re
import threading
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE = {c: i for i, c in enumerate(_ALPHABET)}
_ENCODE_TIME_LEN = 10
_ENCODE_RANDOM_LEN = 16
_lock = threading.Lock()
_last_ms = -1
_last_random = 0
_TASK_ID_RE = re.compile(r"^ho-[0-9]{8}-[0-9A-HJKMNP-TV-Z]{26}$")


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def _random_bits() -> int:
    return int.from_bytes(os.urandom(10), "big")


def ulid(timestamp_ms: int | None = None) -> str:
    """生成单调递增的 ULID。同一毫秒内递增随机段，保证严格有序。"""
    global _last_ms, _last_random
    with _lock:
        ms = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
        if ms == _last_ms:
            _last_random = (_last_random + 1) & ((1 << 80) - 1)
        else:
            _last_ms = ms
            _last_random = _random_bits()
        return _encode(ms, _ENCODE_TIME_LEN) + _encode(_last_random, _ENCODE_RANDOM_LEN)


def is_ulid(value: str) -> bool:
    if not isinstance(value, str) or len(value) != 26:
        return False
    return all(c in _DECODE for c in value)


def ulid_time(value: str) -> int:
    if not is_ulid(value):
        raise ValueError(f"not a ULID: {value!r}")
    ms = 0
    for c in value[:_ENCODE_TIME_LEN]:
        ms = (ms << 5) | _DECODE[c]
    return ms


def task_id(when_ms: int | None = None) -> str:
    """ho-YYYYMMDD-<ULID>。日期段只用于可读性，唯一性由 ULID 保证。"""
    ms = int(time.time() * 1000) if when_ms is None else int(when_ms)
    day = time.strftime("%Y%m%d", time.gmtime(ms / 1000))
    return f"ho-{day}-{ulid(ms)}"


def is_task_id(value: str) -> bool:
    """任务 ID 必须是固定格式，避免用户输入被当成文件路径。"""
    return isinstance(value, str) and _TASK_ID_RE.fullmatch(value) is not None
