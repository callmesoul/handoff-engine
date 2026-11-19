"""密钥文件：配置里只放文件路径，值从文件读入后注入进程环境。

各 agent（codex/zcode）拿不到启动面板时的系统环境变量，因此把密钥值放进
executor 子进程的 env，而不是写进 prompt / 任务单 / Git。配置里只允许出现
文件路径，绝不出现密钥本身。
"""

from __future__ import annotations

import os
from pathlib import Path

#: 要注入 agent 进程环境的密钥。键 = 注入的环境变量名；值 = config.secrets 下
#: 保存文件路径的配置项名（与用户要求的两项配置一致）。
AGENT_SECRETS: dict[str, str] = {
    "HANDOFF_NOTIFY_SECRET": "HANDOFF_NOTIFY_SECRET_FILE",
    "OPENAPI_API_KEY": "OPENAPI_API_KEY_FILE",
}


def _config_secret_path(config, env_name: str) -> str | None:
    """从配置里取 env_name 对应的文件路径；env_name 为注入的环境变量名。"""
    secrets = getattr(config, "secrets", None)
    if secrets is None:
        return None
    file_key = AGENT_SECRETS.get(env_name)
    if not file_key:
        return None
    return getattr(secrets, file_key, None)


def read_secret_file(path: str | Path | None, root: Path | None = None) -> str:
    """读取配置指定的密钥文件；未配置或读不到返回空串。

    读不到时返回空串而不是抛错：配置项可能还没落地，透明降级给调用方决定
    是报错（通知签名缺密钥必须报错）还是跳过（agent 环境变量可缺省）。
    """
    if not path:
        return ""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() and root is not None:
        candidate = Path(root) / candidate
    try:
        return candidate.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def config_secret(config, env_name: str) -> str:
    """从配置的文件路径读密钥值。"""
    return read_secret_file(_config_secret_path(config, env_name), getattr(config, "root", None))


def resolve_secret(config, env_name: str) -> str:
    """进程环境变量优先，其次配置的文件。"""
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    return config_secret(config, env_name)


def agent_env(config) -> dict[str, str]:
    """要注入 codex/zcode 子进程的环境变量（值来自配置的文件）。

    sanitized_env() 会剥掉带 SECRET/KEY/TOKEN 的继承变量，但在最后重新合并
    extra，所以这里的密钥能通过 extra 安全注入，不会从父进程意外泄漏。
    """
    out: dict[str, str] = {}
    for env_name in AGENT_SECRETS:
        value = config_secret(config, env_name)
        if value:
            out[env_name] = value
    return out
