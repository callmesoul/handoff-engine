"""脱敏与截断。

任何写回任务单、索引库或 HTTP 响应的外部文本都必须先过这里。
先脱敏再存储，不是展示时才遮盖。
"""

from __future__ import annotations

import re

DEFAULT_LIMIT = 4000

_KEY = r"(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|authorization|auth[_-]?token|private[_-]?key)"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[redacted private key]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{8,}"), "Bearer [redacted]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "[redacted github token]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "[redacted github token]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "[redacted api key]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "[redacted jwt]"),
    (re.compile(rf"(?i)({_KEY})(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&)]+)"), r"\1\2[redacted]"),
    (re.compile(r"(?i)([?&](?:token|key|sig|signature|access_token|id_token|X-Amz-Signature)=)[^&\s]+"), r"\1[redacted]"),
    (re.compile(r"(?i)\b(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1[redacted]@"),
    (re.compile(r"(?i)\b(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^/\s:@]+:[^/\s@]+@"), r"\1://[redacted]@"),
]

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MULTI_QMARK = re.compile(r"\?{2,}")


def redact(text: str, extra_patterns: list[re.Pattern[str]] | None = None) -> str:
    if not text:
        return ""
    out = _CONTROL.sub("", str(text))
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    for pattern in extra_patterns or []:
        out = pattern.sub("[redacted]", out)
    return out


def truncate(text: str, limit: int = DEFAULT_LIMIT) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    head = limit - 200
    tail = 150
    return f"{text[:head]}\n...[truncated {len(text) - limit} chars]...\n{text[-tail:]}"


def clean(text: str | bytes | None, limit: int = DEFAULT_LIMIT, extra_patterns=None) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return truncate(redact(text, extra_patterns), limit)


def compact(text: str | bytes | None, limit: int = 160) -> str:
    """压成一行、按尾部截断。

    给命令这类文本用：开头往往是 `"...\\powershell.exe" -NoProfile -Command "`
    这种启动器噪音，真正有信息的是后面那段。首尾保留的 truncate() 会把一条命令
    切成三段，面板上看着就像乱码。
    """
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    flat = " ".join(redact(text).split())
    if len(flat) <= limit:
        return flat
    cut = flat[-(limit - 1):]
    # 尽量从词边界开始，别把命令切成 "…hildItem" 这种半截
    space = cut.find(" ")
    if 0 <= space < 24:
        cut = cut[space + 1:]
    return "…" + cut


def repair_mojibake(text: str | None) -> str:
    """修复「UTF-8 被按 GBK 解码」的乱码。

    Windows 上 PowerShell 子进程默认按系统代码页（CP936）输出，codex 再按
    UTF-8/GBK 混着解一遍，中文就成了 `绯荤粺 OTA 鐨勬湰鍦版牎楠?` 这种。

    做法是把文本按 GBK 编回字节、再按 UTF-8 解码。只有解码结果的非 ASCII 字符
    以汉字为主时才采用——否则「系统」这种正常中文会被误还原成希腊字母 `ϵͳ`。
    个别被替换成 `?` 的字节信息已丢，还原不回来，但整体可读。
    """
    if not text or text.isascii():
        return text or ""
    try:
        raw = text.encode("gbk", errors="replace")
    except (UnicodeEncodeError, LookupError):
        return text
    try:
        repaired = raw.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return text
    if repaired == text:
        return text
    non_ascii = [ch for ch in repaired if ord(ch) > 127]
    if not non_ascii:
        return text
    hanzi = sum(1 for ch in non_ascii if "\u4e00" <= ch <= "\u9fff")
    if hanzi / len(non_ascii) < 0.6:
        return text
    return _MULTI_QMARK.sub("?", repaired.replace("\ufffd", "?")).strip()


def redact_obj(value):
    """递归脱敏字典/列表里的字符串。metadata 也要过一遍。"""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    return value
