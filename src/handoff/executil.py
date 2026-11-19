"""可执行文件解析。

不要依赖调用方进程的 PATH：从 systemd、cron、WSL interop 或某些终端里启动时，
PATH 可能只有别的东西，`subprocess` 就会抛 `FileNotFoundError`，而错误信息只说「命令不存在: git」。
这里显式解析绝对路径，并允许用环境变量覆盖。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .errors import ExecutionError

_EXTRA_DIRS = (
    "~/bin",
    "~/.local/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/opt/homebrew/bin",
    "/snap/bin",
)


def _versioned_candidates(name: str) -> list[Path]:
    """自更新 CLI 会把二进制放在 bin/<hash>/ 下，hash 一变写死的路径就失效。

    codex 就是这种：`Codex\\bin\\a1599...\\codex.exe` 更新后整个目录被换掉。
    这里扫一遍已知安装根，按修改时间倒序返回，让配置里的老路径能自动落到新版上。
    """
    roots: list[Path] = []
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots.append(Path(local) / "OpenAI" / "Codex" / "bin")
    found: list[Path] = []
    for root in roots:
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            for suffix in (".exe", ""):
                candidate = child / f"{name}{suffix}"
                try:
                    if candidate.is_file():
                        found.append(candidate)
                except OSError:
                    continue
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found


def _nearby_versioned_candidates(path: Path) -> list[Path]:
    """Find replacements next to a stale ``bin/<version>/<name>`` path."""
    bin_root = path.parent.parent
    if bin_root.name.lower() != "bin":
        return []
    found: list[Path] = []
    try:
        children = list(bin_root.iterdir())
    except OSError:
        return []
    for child in children:
        candidate = child / path.name
        try:
            if candidate.is_file():
                found.append(candidate)
        except OSError:
            continue
    found.sort(key=lambda candidate: candidate.stat().st_mtime, reverse=True)
    return found


def _candidates(name: str) -> list[Path]:
    out: list[Path] = []
    for directory in _EXTRA_DIRS:
        base = Path(directory).expanduser()
        for suffix in ("", ".exe"):
            out.append(base / f"{name}{suffix}")
    if sys.platform.startswith("win"):
        out.extend(
            [
                Path(r"C:\Program Files\Git\cmd") / f"{name}.exe",
                Path(r"C:\Program Files\Git\bin") / f"{name}.exe",
                Path(r"C:\Program Files\Git\mingw64\bin") / f"{name}.exe",
            ]
        )
    out.extend(_versioned_candidates(name))
    return out


def resolve(name: str, *, env_var: str | None = None) -> str | None:
    """按顺序找：环境变量 → PATH → 常见绝对路径。找不到返回 None。"""
    if env_var:
        override = os.environ.get(env_var)
        if override:
            path = Path(override).expanduser()
            if path.is_file():
                return str(path)
    # 配置里写死绝对路径时，文件可能已被自更新搬走：退回按裸名再找一轮
    if os.sep in name or "/" in name:
        path = Path(name)
        if path.is_file():
            return str(path)
        # Self-updating CLIs commonly replace the version/hash directory. Prefer
        # the newest sibling before falling back to an unrelated global binary.
        nearby = _nearby_versioned_candidates(path)
        if nearby:
            return str(nearby[0])
        name = path.stem
    found = shutil.which(name)
    if found:
        return found
    for candidate in _candidates(name):
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        except OSError:
            continue
    return None


def require(name: str, *, env_var: str | None = None, hint: str = "") -> str:
    resolved = resolve(name, env_var=env_var)
    if resolved:
        return resolved
    parts = [
        f"找不到可执行文件 {name!r}",
        "PATH 里没有" + (f"，{env_var} 也没指到有效文件" if env_var else ""),
        "已尝试：" + ", ".join(str(p) for p in _candidates(name)[:6]) + " …",
    ]
    if hint:
        parts.append(hint)
    if env_var:
        parts.append(f"可以设置 {env_var}=/absolute/path/to/{name}")
    raise ExecutionError("；".join(parts))


def with_binary_dir(env: dict[str, str], binary: str) -> dict[str, str]:
    """把可执行文件所在目录塞进 PATH，好让它的子进程（git hooks、helper）能被找到。"""
    directory = str(Path(binary).resolve().parent)
    current = env.get("PATH", "")
    parts = [p for p in current.split(os.pathsep) if p]
    if directory not in parts:
        env["PATH"] = os.pathsep.join([directory, *parts]) if parts else directory
    return env
