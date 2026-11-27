#!/usr/bin/env python3
"""交接系统自身的自动更新。

只认核心代码的变更。tasks/ events/ contracts/ artifacts/ 天天都在变，那些是数据，
不触发任何动作——这是这份脚本和「业务仓自动部署」最根本的区别。

必须在**被更新目录之外**运行。留在 APP 里的话，git merge 会覆盖正在执行的脚本；
装到 /opt/handoff/selfupdate/ 或 ~/.local/lib/handoff-selfupdate/ 再用 timer 调。

环境变量：
  APP        代码 checkout，只读部署副本（必需）
  STORE      交接仓。后端用它判断有没有任务在跑；前端可留空
  BRANCH     默认 main
  ROLE       frontend | backend
  SERVICES   空格分隔。Linux 填 unit 名，前缀 user: 表示 systemctl --user；
             Windows 填计划任务名
  PY         解释器，默认 APP/.venv/bin/python（Windows: APP/.venv-win/Scripts/python.exe）
  DEFER_MAX  有任务在跑时最多推迟几小时，默认 6。超过就强制重启，宁可丢一轮 attempt
  FORCE      1 = 不看 in_progress，直接更新

退出码：0 无变更或更新成功 / 3 有别的实例在跑 / 4 被 in_progress 推迟
        5 工作区脏或分叉，拒绝 / 6 更新失败（已回滚）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP = Path(os.environ["APP"]).resolve() if os.environ.get("APP") else None
STORE = Path(os.environ["STORE"]).resolve() if os.environ.get("STORE") else None
BRANCH = os.environ.get("BRANCH", "main")
ROLE = os.environ.get("ROLE", "frontend")
SERVICES = [s for s in os.environ.get("SERVICES", "").split() if s]
DEFER_MAX = float(os.environ.get("DEFER_MAX", "6"))
FORCE = os.environ.get("FORCE") == "1"

WORK = APP / ".handoff-run" if APP else Path(".")
STATE_FILE = WORK / "selfupdate-state.json"
LOG_FILE = WORK / "log" / "selfupdate.log"

# 改了就得重启的路径。其余一律不算「核心代码」。
CODE_PREFIXES = ("src/", "bin/", "schemas/")
DEP_FILES = ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg")
# skills 靠符号链接生效，下个 Codex 会话自动读到，不用重启进程
SKILL_PREFIX = ".agents/skills/"


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')} [{ROLE}] {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def die(code: int, msg: str) -> None:
    log(msg)
    sys.exit(code)


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(APP), *args], capture_output=True, text=True, timeout=300
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


def git_ok(*args: str) -> bool:
    """只要退出码，不关心输出（merge-base --is-ancestor 用）。"""
    return subprocess.run(
        ["git", "-C", str(APP), *args],
        capture_output=True, text=True, timeout=120,
    ).returncode == 0


# ---- 锁 --------------------------------------------------------------------
# 跨平台 O_EXCL。Windows 没有 flock，O_EXCL 在两边语义一致。


def lock_path() -> Path:
    return WORK / "selfupdate.lock"


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def acquire() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    path = lock_path()
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return
        except FileExistsError:
            try:
                holder = int(path.read_text().strip() or "0")
            except (OSError, ValueError):
                holder = 0
            if holder and pid_alive(holder):
                die(3, f"另一个实例正在跑（pid {holder}）")
            path.unlink(missing_ok=True)  # 上一轮崩了，接管
    die(3, "拿不到锁")


def release() -> None:
    lock_path().unlink(missing_ok=True)


# ---- state -----------------------------------------------------------------


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(data: dict) -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---- 判定 ------------------------------------------------------------------


def classify(files: list[str]) -> tuple[list[str], list[str], list[str]]:
    """拆成三类：要重启的 / 要装依赖的 / 只 pull 不重启的。"""
    restart: list[str] = []
    install: list[str] = []
    pull_only: list[str] = []
    for f in files:
        if f in DEP_FILES:
            restart.append(f)
            install.append(f)
        elif f.startswith(CODE_PREFIXES):
            restart.append(f)
        elif f.startswith("config/") and f.endswith((".yaml", ".yml")) and not f.endswith("local.yaml"):
            restart.append(f)  # 配置在启动时加载，改了要重启才生效
        else:
            pull_only.append(f)
    return restart, install, pull_only


def in_progress() -> list[str]:
    """读本地 tasks/ 数未完成的任务。不用 CLI：那会触发 store sync，和 worker 抢目录。"""
    if not STORE:
        return []
    tasks_dir = STORE / "tasks"
    if not tasks_dir.is_dir():
        return []
    busy: list[str] = []
    for path in tasks_dir.glob("*.json"):
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if task.get("status") in ("claimed", "in_progress"):
            busy.append(task.get("id", path.stem))
    return busy


# ---- 动作 ------------------------------------------------------------------


def default_python() -> str:
    explicit = os.environ.get("PY")
    if explicit:
        return explicit
    if os.name == "nt":
        cand = APP / ".venv-win" / "Scripts" / "python.exe"
    else:
        cand = APP / ".venv" / "bin" / "python"
    return str(cand) if cand.exists() else sys.executable


def install_deps(py: str) -> None:
    freeze = subprocess.run([py, "-m", "pip", "freeze"], capture_output=True, text=True)
    backup = WORK / "pip-freeze.before.txt"
    backup.write_text(freeze.stdout, encoding="utf-8")
    log(f"依赖有变更，重装（快照 {backup.name}）")
    result = subprocess.run(
        [py, "-m", "pip", "install", "-r", str(APP / "requirements.txt")],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log("重装失败，恢复快照")
        subprocess.run([py, "-m", "pip", "install", "-r", str(backup)],
                       capture_output=True, text=True)
        raise RuntimeError(f"pip install 失败: {result.stderr.strip()[:300]}")


def restart(services: list[str], wait: int = 60) -> None:
    env = dict(os.environ)
    # systemctl --user 从 timer 里跑时常常没有这两个
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}" if hasattr(os, "getuid") else "")
    for svc in services:
        if os.name == "nt":
            cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                   f"Stop-ScheduledTask -TaskName '{svc}' -ErrorAction SilentlyContinue; "
                   f"Start-ScheduledTask -TaskName '{svc}' -ErrorAction Stop"]
        elif svc.startswith("user:"):
            cmd = ["systemctl", "--user", "restart", svc[5:]]
        else:
            cmd = ["systemctl", "restart", svc]
        log(f"重启 {svc}")
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if result.returncode != 0:
            raise RuntimeError(f"重启 {svc} 失败: {result.stderr.strip()[:300]}")

    # 只对 Linux 做存活检查；Windows 计划任务没有等价概念，看退出码就够
    if os.name == "nt" or not services:
        return
    deadline = time.time() + wait
    for svc in services:
        name = svc[5:] if svc.startswith("user:") else svc
        flag = ["--user"] if svc.startswith("user:") else []
        while True:
            probe = subprocess.run(["systemctl", *flag, "is-active", name],
                                   capture_output=True, text=True, env=env)
            if probe.stdout.strip() == "active":
                break
            if time.time() > deadline:
                raise RuntimeError(f"{name} 未回到 active: {probe.stdout.strip()}")
            time.sleep(2)
        log(f"{name} active")


def verify(py: str) -> bool:
    """更新完跑一次 doctor。它一次列出所有缺口，比逐个试快。

    只作为观测：doctor 会把与本次更新无关的环境缺口一并报出来，拿它当门禁
    会让一次「其实成功」的更新被回滚掉。
    """
    result = subprocess.run(
        [py, "-m", "handoff.cli", *(["--root", str(STORE)] if STORE else []),
         "doctor", "--role", ROLE],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(APP / "src")},
    )
    for line in (result.stdout or result.stderr).strip().splitlines()[-3:]:
        log(f"doctor: {line}")
    return result.returncode == 0


# ---- 主流程 ----------------------------------------------------------------


def main() -> int:
    if not APP or not (APP / ".git").exists():
        die(2, "APP 不是 git 仓库")
    acquire()
    try:
        return run()
    finally:
        release()


def run() -> int:
    state = load_state()
    # 先只拿 ref（约 1KB），不下载对象。和上次处理过的一样就直接退出——
    # 轮询的「无变化」路径几乎零成本，fetch 只在 ref 真动了之后才发生。
    remote_sha = git("ls-remote", "origin", f"refs/heads/{BRANCH}").split()[0]
    if remote_sha == state.get("last_seen_sha"):
        return 0
    # 熔断放在 fetch 之前：同一个提交反复失败时连 fetch 都省了。
    if state.get("last_failed_sha") == remote_sha and state.get("fail_count", 0) >= 3:
        log(f"{remote_sha[:12]} 已连续失败 {state['fail_count']} 次，停手等人处理")
        return 0
    git("fetch", "--quiet", "--prune", "origin")
    remote = f"origin/{BRANCH}"
    local = git("rev-parse", "HEAD")
    target = git("rev-parse", remote)

    if local == target:
        return 0

    # 三点 diff：只取远端相对分叉点带来的变化。两点 diff 会把本地领先的提交也算进来。
    changed = [f for f in git("diff", "--name-only", f"{local}...{remote}").splitlines() if f]
    restart_files, install_files, pull_only = classify(changed)

    if not restart_files and not any(f.startswith(SKILL_PREFIX) for f in pull_only):
        log(f"{target[:12]} 只动了数据/文档（{len(changed)} 个文件），不更新")
        save_state({**state, "last_seen_sha": target, "last_ignored_sha": target,
                    "checked_at": datetime.now(timezone.utc).isoformat()})
        return 0

    if not restart_files:
        log(f"{target[:12]} 只有 skill/文档变更，拉取但不重启")
    else:
        log(f"核心代码有变更 {local[:12]} -> {target[:12]}")
        for f in restart_files[:20]:
            log(f"  {f}")
        if len(restart_files) > 20:
            log(f"  ... 另有 {len(restart_files) - 20} 个")

    # 后端 worker 正在跑 codex 时重启 = 白烧一轮额度 + attempt+1
    if ROLE == "backend" and not FORCE:
        busy = in_progress()
        if busy:
            since = state.get("deferred_since")
            now = datetime.now(timezone.utc)
            if since:
                waited = now - datetime.fromisoformat(since)
            else:
                waited, since = timedelta(0), now.isoformat()
            if waited < timedelta(hours=DEFER_MAX):
                log(f"{len(busy)} 个任务在跑（{', '.join(busy[:3])}），推迟到此轮结束")
                save_state({**state, "deferred_since": since, "pending_sha": target})
                return 4
            log(f"已推迟 {waited.total_seconds() / 3600:.1f}h，超过上限，强制更新")

    # 绝不用 reset --hard 冲掉人的改动。分叉或脏工作区一律交给人。
    dirty = git("status", "--porcelain", "-uno")
    if dirty:
        die(5, f"工作区有未提交改动，拒绝更新：{dirty.splitlines()[0][:120]}")

    if not git_ok("merge-base", "--is-ancestor", "HEAD", remote):
        die(5, "本地与远端分叉，拒绝自动更新")

    log(f"快进到 {target[:12]}")
    try:
        git("merge", "--ff-only", "--quiet", remote)
        if install_files:
            install_deps(default_python())
        restart(SERVICES)
    except Exception as exc:  # noqa: BLE001 — 任何一步挂了都要回滚
        log(f"更新失败：{exc}")
        log(f"回滚到 {local[:12]}")
        git("reset", "--hard", "--quiet", local, check=False)
        if install_files:
            try:
                install_deps(default_python())
            except Exception as exc2:  # noqa: BLE001
                log(f"依赖回滚也失败，venv 可能已损坏：{exc2}")
        try:
            restart(SERVICES)
        except Exception as exc3:  # noqa: BLE001
            log(f"回滚后重启失败，环境状态未知：{exc3}")
            (WORK / "BROKEN").write_text(f"{datetime.now(timezone.utc).isoformat()} {target}\n")
            return 6
        save_state({**state, "fail_count": state.get("fail_count", 0) + 1,
                    "last_failed_sha": target,
                    "checked_at": datetime.now(timezone.utc).isoformat()})
        return 6

    # doctor 只记录，不作为回滚条件：它会把与本次更新无关的环境缺口一并报出来，
    # 拿它当门禁会让一次「其实成功」的更新被回滚掉。
    if not verify(default_python()):
        log("doctor 报告了失败项，见上；本次更新不回滚，按环境缺口单独处理")

    log(f"更新完成 {target[:12]}")
    save_state({
        "deployed_sha": target,
        "last_seen_sha": target,
        "deployed_at": datetime.now(timezone.utc).isoformat(),
        "restarted": bool(SERVICES),
        "changed_files": len(restart_files),
        "fail_count": 0,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
