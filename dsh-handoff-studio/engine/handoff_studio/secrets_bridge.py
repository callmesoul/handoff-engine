"""Studio 侧的密钥发现：把 `HANDOFF_NOTIFY_SECRET` 补进本进程环境。

## 为什么需要这一层

`handoff.notification.load_secret()` 只认两条路：进程环境变量，或
`config/local.yaml` 的 `secrets.*_file`。原面板与 worker 由 `run-panel.ps1`
/ `run-backend.ps1` 启动，脚本先把 `secrets/notify-secret.txt` 读进
`HANDOFF_NOTIFY_SECRET` 再起进程，所以那两条路总能命中。

studio 引擎不是那两个脚本起的：它是 DSH 插件用 `spawn` 拉起来的，继承的是
DSH 宿主的环境。宿主的进程环境里没有这个变量，而本机交接仓的
`config/local.yaml` 也没有写 `secrets:` 段——于是每一次用到通知的地方
（自动模式的唤醒监听、面板的「重发完成通知」）都撞上：

    HANDOFF_NOTIFY_SECRET 或 HANDOFF_NOTIFY_SECRET_FILE 指定的密钥缺失或少于 32 字节

这不是密钥真的缺了，而是**发现路径少了一跳**。本模块补上那一跳：按可靠性
顺序找到密钥文件，把值注入 `os.environ`，让 `load_secret()` 原来那条路自己
命中——不改进程外任何东西，也不把值写进配置、日志或任务单。

## 顺序

    1. 环境变量 HANDOFF_NOTIFY_SECRET            （已注入，直接用）
    2. 环境变量 HANDOFF_NOTIFY_SECRET_FILE       （app 代码自己会读，这里只登记）
    3. HANDOFF_STUDIO_NOTIFY_SECRET_FILE         （studio 专属覆盖，便于换机器）
    4. config/local.yaml 的 secrets.HANDOFF_NOTIFY_SECRET_FILE
    5. 交接仓根下的 secrets/notify-secret.txt
    6. 交接仓根的**上一级** secrets/notify-secret.txt（本机正是这种布局：
       D:\\handoff\\store-github + D:\\handoff\\secrets）
    7. 同上两级（仓放在更深一层时）

读到的值一律 strip 掉行尾空白；长度不足 32 字节不注入，照旧让 app 报错——
短密钥是配置错误，静默降级比报错更危险。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from handoff.errors import ConfigError

#: 通知 HMAC 密钥注入的环境变量名（与 app 的 secrets.AGENT_SECRETS 一致）。
NOTIFY_SECRET_ENV = "HANDOFF_NOTIFY_SECRET"
NOTIFY_FILE_ENV = "HANDOFF_NOTIFY_SECRET_FILE"
#: studio 自己的覆盖项：显式指定密钥文件路径。
STUDIO_FILE_ENV = "HANDOFF_STUDIO_NOTIFY_SECRET_FILE"

#: 密钥文件常见文件名（按优先级）。
_SECRET_FILENAMES = ("notify-secret.txt", "notify-secret", "handoff-notify-secret")

#: app 要求的最短长度；短于它不注入，交给 app 报它自己的错。
MIN_SECRET_BYTES = 32


@dataclass(frozen=True)
class SecretStatus:
    """一次发现的结果：面板据此显示「密钥从哪来 / 为什么没有」。"""

    ok: bool
    source: str | None = None
    length: int = 0
    error: str | None = None
    tried: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "source": self.source,
            "length": self.length,
            "error": self.error,
            "tried": list(self.tried),
        }


def _candidate_files(root: Path) -> list[Path]:
    """密钥文件候选路径（去重、保持顺序）。"""
    out: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path | None) -> None:
        if path is None:
            return
        try:
            resolved = path.expanduser()
        except (OSError, RuntimeError):
            return
        key = Path(os.path.normpath(str(resolved)))
        if key in seen:
            return
        seen.add(key)
        out.append(resolved)

    # 3. studio 专属覆盖
    add(Path(os.environ[STUDIO_FILE_ENV]) if os.environ.get(STUDIO_FILE_ENV, "").strip() else None)
    # 5-7. 交接仓根及其上两级的 secrets/
    roots = [root, root.parent, root.parent.parent]
    for base in roots:
        for name in _SECRET_FILENAMES:
            add(base / "secrets" / name)
    return out


def _config_secret_path(root: Path, config) -> Path | None:
    """config/local.yaml 的 secrets.HANDOFF_NOTIFY_SECRET_FILE（第 4 条）。"""
    raw = getattr(getattr(config, "secrets", None), NOTIFY_FILE_ENV, None)
    if not raw:
        return None
    path = Path(str(raw)).expanduser()
    return path if path.is_absolute() else root / path


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def discover_notify_secret(root: str | Path, config=None) -> SecretStatus:
    """找通知密钥；找到就写进 `os.environ`，让 app 的 `load_secret()` 自己命中。

    返回值只带来源与长度，**绝不含密钥本身**，可以安全进 `/api/state`。
    """
    root = Path(root)
    tried: list[Path] = []
    file_env = os.environ.get(NOTIFY_FILE_ENV, "").strip()
    if file_env:
        tried.append(Path(file_env))
    config_path = _config_secret_path(root, config)
    if config_path is not None:
        tried.append(config_path)

    # 1. 环境变量里已经有值：什么都不用做。长度交给 app 判断。
    existing = os.environ.get(NOTIFY_SECRET_ENV, "").strip()
    if existing:
        return SecretStatus(ok=True, source=f"env:{NOTIFY_SECRET_ENV}", length=len(existing))

    # 2. app 自己会读 *_FILE；这里只登记来源，避免重复搬运同一个文件。
    if file_env:
        value = _read(Path(file_env))
        if value:
            return SecretStatus(ok=True, source=f"env:{NOTIFY_FILE_ENV}", length=len(value))

    # 3-7. 按候选顺序找文件。
    tried.extend(_candidate_files(root))
    for path in tried:
        value = _read(path)
        if not value:
            continue
        if len(value) < MIN_SECRET_BYTES:
            return SecretStatus(
                ok=False,
                source=str(path),
                length=len(value),
                error=(
                    f"密钥文件 {path} 只有 {len(value)} 字节，少于 {MIN_SECRET_BYTES} 字节；"
                    "请换成随机生成的长密钥（例如 32 字节随机值的十六进制编码）"
                ),
                tried=tuple(str(item) for item in tried),
            )
        os.environ[NOTIFY_SECRET_ENV] = value
        return SecretStatus(ok=True, source=str(path), length=len(value))

    return SecretStatus(
        ok=False,
        error=(
            f"找不到通知密钥：请设置环境变量 {NOTIFY_SECRET_ENV}，或写 "
            f"{STUDIO_FILE_ENV} / config/local.yaml 的 secrets.{NOTIFY_FILE_ENV} 指向密钥文件。"
            "studio 引擎由 DSH 插件拉起，不会继承 run-panel.ps1 注入的环境变量。"
        ),
        tried=tuple(str(item) for item in tried),
    )


def ensure_notify_secret(root: str | Path, config=None) -> SecretStatus:
    """`discover_notify_secret` 的严格版：失败直接抛可照做的 ConfigError。

    面板「重发完成通知」这类显式动作要用它——静默返回「没找到」只会让人对着
    一句原始报错发呆，而真因（引擎不继承启动脚本的环境）根本没写在里面。
    """
    status = discover_notify_secret(root, config)
    if not status.ok:
        raise ConfigError(status.error or "通知密钥不可用")
    return status


#: verifier 的 `{env:DOCS_API_KEY}` 与配置里的 OPENAPI_API_KEY 是同一把钥匙。
_DOCS_ALIASES = ("DOCS_API_KEY", "OPENAPI_API_KEY")


def discover_docs_key(root: str | Path) -> SecretStatus:
    """顺带补上验收用的 DOCS_API_KEY：同一套「脚本注入、引擎拿不到」问题。"""
    for name in _DOCS_ALIASES:
        existing = os.environ.get(name, "").strip()
        if existing:
            return SecretStatus(ok=True, source=f"env:{name}", length=len(existing))
    root = Path(root)
    for base in (root, root.parent, root.parent.parent):
        for stem in ("docs-api-key.txt", "openapi-api-key.txt"):
            path = base / "secrets" / stem
            value = _read(path)
            if value:
                for name in _DOCS_ALIASES:
                    os.environ.setdefault(name, value)
                return SecretStatus(ok=True, source=str(path), length=len(value))
    return SecretStatus(ok=False, error="未找到 docs-api-key.txt（验收 openapi_diff 才需要）")


__all__ = [
    "MIN_SECRET_BYTES",
    "NOTIFY_FILE_ENV",
    "NOTIFY_SECRET_ENV",
    "SecretStatus",
    "STUDIO_FILE_ENV",
    "discover_docs_key",
    "discover_notify_secret",
    "ensure_notify_secret",
]
