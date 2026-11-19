"""路径 gate。

`out_of_scope` 是给人/agent 看的语义边界，不能当自动 gate。
自动判断只看 allowed_paths / forbidden_paths 两组 glob。

glob 语义：
    *      匹配路径段内任意字符，不跨 /
    **     跨 / 匹配任意层
    **/    匹配零层或多层目录
    ?      单个非 / 字符
    [abc]  字符集
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


def _translate(pattern: str) -> str:
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if pattern.startswith("**", i):
                i += 2
                if i < n and pattern[i] == "/":
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
                i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[":
            close = pattern.find("]", i + 1)
            if close == -1:
                out.append(r"\[")
                i += 1
            else:
                body = pattern[i + 1 : close]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", r"\\") + "]")
                i = close + 1
        else:
            out.append(re.escape(ch))
            i += 1
    return "".join(out)


_cache: dict[str, re.Pattern] = {}


def compile_glob(pattern: str) -> re.Pattern:
    cached = _cache.get(pattern)
    if cached is not None:
        return cached
    if not pattern or pattern.startswith("/"):
        raise ValueError(f"非法 glob: {pattern!r}")
    parts = pattern.split("/")
    if any(p == ".." for p in parts):
        raise ValueError(f"glob 不允许包含 ..: {pattern!r}")
    if "//" in pattern:
        raise ValueError(f"glob 不允许出现空路径段: {pattern!r}")
    compiled = re.compile("^" + _translate(pattern) + "$")
    _cache[pattern] = compiled
    return compiled


def matches(path: str, pattern: str) -> bool:
    normalized = path.strip("/")
    if compile_glob(pattern).match(normalized):
        return True
    # 目录模式：infra/prod 同时覆盖 infra/prod/**（不含尾部 ** 时也算命中）
    if not pattern.endswith("/**"):
        prefix = pattern.rstrip("/")
        if normalized.startswith(prefix + "/"):
            return True
    return False


@dataclass
class GateResult:
    ok: bool
    violations: list[str] = field(default_factory=list)
    allowed_hits: list[str] = field(default_factory=list)
    forbidden_hits: list[str] = field(default_factory=list)


def evaluate(
    changed_paths: list[str],
    allowed_paths: list[str],
    forbidden_paths: list[str],
) -> GateResult:
    if not changed_paths:
        return GateResult(ok=False, violations=["没有检测到任何改动"])
    if not allowed_paths:
        return GateResult(ok=False, violations=["scope.allowed_paths 为空，gate 拒绝放行"])

    forbidden_hits: list[str] = []
    outside: list[str] = []
    for path in changed_paths:
        normalized = path.strip("/")
        if normalized.startswith("/") or ".." in normalized.split("/"):
            outside.append(f"{path}（非法路径）")
            continue
        hit = next((p for p in forbidden_paths if matches(normalized, p)), None)
        if hit is not None:
            forbidden_hits.append(f"{path} 命中禁止规则 {hit}")
            continue
        if not any(matches(normalized, p) for p in allowed_paths):
            outside.append(f"{path} 不在 allowed_paths 内")

    violations = forbidden_hits + outside
    return GateResult(
        ok=not violations,
        violations=violations,
        forbidden_hits=forbidden_hits,
    )


def high_risk_hits(changed_paths: list[str], patterns: list[str]) -> list[str]:
    hits = []
    for path in changed_paths:
        hit = next((p for p in patterns if matches(path.strip("/"), p)), None)
        if hit:
            hits.append(f"{path} 命中高风险规则 {hit}")
    return hits
