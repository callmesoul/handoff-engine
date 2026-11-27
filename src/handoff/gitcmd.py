"""subprocess 封装。不经过 shell，不拼接字符串，不做 eval。

git 的绝对路径显式解析，不依赖调用方 PATH（见 executil）。
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import executil
from .errors import GitError, SchemaError
from .sanitize import clean


def git_env(force_windows: bool | None = None) -> dict[str, str]:
    """git 子进程的固定环境。Windows 上没有叫 true 的程序，不设 GIT_ASKPASS。"""
    windows = (os.name == "nt") if force_windows is None else force_windows
    env = {
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        "LC_ALL": "C.UTF-8",
    }
    if not windows:
        env["GIT_ASKPASS"] = "true"
    return env

_git_bin: str | None = None


def git_binary() -> str:
    global _git_bin
    if _git_bin is None:
        _git_bin = executil.require(
            "git",
            env_var="HANDOFF_GIT",
            hint="安装 git，或把 git 目录加入 PATH",
        )
    return _git_bin


def reset_binary_cache() -> None:
    global _git_bin
    _git_bin = None


@dataclass
class Result:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run(
    argv: list[str],
    cwd: str | Path | None = None,
    timeout: float = 120,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    check_env: bool = True,
    inherit_env: bool = True,
) -> Result:
    if inherit_env:
        full_env = dict(os.environ)
    else:
        # verifier 脚本只拿到进程启动所需的非敏感基础环境，以及调用方显式注入项。
        safe_names = (
            "PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT",
            "HOME", "USERPROFILE", "TMP", "TEMP", "TMPDIR",
            "LANG", "LC_ALL", "TERM",
        )
        full_env = {name: os.environ[name] for name in safe_names if name in os.environ}
    if check_env:
        full_env.update(git_env())
    if env:
        full_env.update(env)
    if os.path.isabs(argv[0]):
        executil.with_binary_dir(full_env, argv[0])
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=full_env,
            input=input_text,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise GitError(
            f"命令不存在: {argv[0]}（cwd={cwd}，PATH={full_env.get('PATH', '<empty>')}）",
            args=argv,
        ) from exc
    except NotADirectoryError as exc:
        raise GitError(f"工作目录不存在或不是目录: {cwd}", args=argv) from exc
    except PermissionError as exc:
        raise GitError(f"无法执行 {argv[0]}（权限不足）", args=argv) from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(
            f"命令超时（{timeout}s）: {' '.join(argv[:3])}",
            args=argv,
            stderr=clean(exc.stderr),
        ) from exc
    return Result(argv=argv, returncode=proc.returncode, stdout=proc.stdout or "", stderr=proc.stderr or "")


def git(cwd: str | Path, *args: str, timeout: float = 120, input_text: str | None = None, check: bool = False) -> Result:
    argv = [git_binary(), *args]
    result = run(argv, cwd=cwd, timeout=timeout, input_text=input_text)
    if check and not result.ok:
        raise GitError(
            f"git {' '.join(args[:3])} 失败（exit {result.returncode}）",
            args=argv,
            stderr=clean(result.stderr),
            returncode=result.returncode,
        )
    return result


def git_out(cwd: str | Path, *args: str, timeout: float = 120) -> str:
    return git(cwd, *args, timeout=timeout, check=True).stdout.strip()


def resolve_remote_ref(repo, ref: str) -> str:
    """在有后端凭据的机器上把分支固定为完整提交 SHA。"""
    remote = repo.remote_url
    result = git(".", "ls-remote", remote, f"refs/heads/{ref}", timeout=120)
    if not result.ok or not result.stdout.strip():
        raise SchemaError(
            f"无法在后端远端仓库 {repo.id}（{remote}）上找到分支 {ref}: "
            f"{clean(result.stderr, 500)}\n"
            "提示：worker 必须能只读后端仓库，且 config/repositories.yaml 的 remote/分支必须正确。"
        )
    sha = result.stdout.split()[0].strip()
    if len(sha) not in (40, 64):
        raise SchemaError(f"{repo.id}:{ref} 返回的 SHA 不完整: {sha}")
    return sha


def push_rejected(result: Result) -> bool:
    text = (result.stderr + result.stdout).lower()
    markers = (
        "non-fast-forward",
        "fetch first",
        "rejected",
        "failed to push some refs",
        "updates were rejected",
        "stale info",
    )
    return any(m in text for m in markers)
