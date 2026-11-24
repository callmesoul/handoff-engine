"""PanelService：桌面面板的后端核心。

职责：
    - 查询任务 / 事件 / 日志 / 会话；
    - 手动执行：指定任务 + 执行器（zcode/codex）+ 可选接续会话；
    - 全自动模式：局域网唤醒 + 兜底轮询，按默认执行器端到端跑新任务；
    - 辅助操作：重试、释放租约、触发验收。

单并发：同一时刻只跑一个任务（与原 worker 一致）。面板状态持久化在
.handoff-run/panel-state.json，不进 Git。
"""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import re
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from .. import frontend_session, ids, notification, secrets as secret_files
from ..codex_runner import (
    CODEX_SANDBOX_MODES,
    CodexRunner,
    humanize_event_stream,
    validate_sandbox,
)
from ..config import Config
from ..errors import ConfigError, ConflictError, ExecutionError, HandoffError, SchemaError
from ..frontend_session import FrontendSessions
from ..gitcmd import git, git_out
from ..scope import profile_options, resolve_scope
from ..store import Actor, TaskStore
from ..timeutil import iso, now, parse
from ..worker import Worker, WorkerOptions
from ..zcode_runner import ZcodeRunner, session_uuid
from . import sessions

EXECUTORS = ("zcode", "codex")
_HEALTH_TTL_SECONDS = 30.0
# 前端会话登记表的查询缓存：任务表格 4 秒一刷，别每次都重开 SQLite
_FRONTEND_CACHE_TTL_SECONDS = 3.0
# 自动模式的串行闸：这些状态表示「有任务正在被处理」，一个没处理完不接下一单。
# 心跳超过 2 小时没动过的视为僵尸租约，不再阻塞自动队列。
_AUTO_BUSY_STATUSES = ("claimed", "in_progress", "verifying")
_AUTO_BUSY_MAX_AGE_SECONDS = 7200.0
# 「恢复推进」只接手交付链路中途断掉的任务；其余状态各有自己的入口
_ADVANCE_STATES = frozenset(
    {"delivered", "awaiting_merge", "merged", "deployed", "verifying"}
)


def _stamp() -> str:
    """面板日志时间戳用本地时间：UTC 会让人对不上手表。"""
    return time.strftime("%H:%M:%S")


def _read_codex_defaults() -> dict:
    """读 codex 全局配置里的默认模型/思考程度，给面板做「跟随配置」的展示。"""
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python < 3.11
        return {}
    path = Path.home() / ".codex" / "config.toml"
    if not path.is_file():
        return {}
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return {}
    return {
        "model": data.get("model"),
        "effort": data.get("model_reasoning_effort"),
    }


_INVISIBLE_CHARS = "\ufeff\u200b\u200c\u200d\u2060\u00a0"


def _clean_path(value) -> str:
    """去掉复制来的路径里夹带的 BOM / 零宽字符。

    例如复制来的 `D:\\project\\api` 开头可能藏着 \\ufeff，
    肉眼完全看不出来，但 agent 拿它去 Read 永远 "File does not exist"。
    """
    return str(value or "").strip().strip(_INVISIBLE_CHARS).strip()


def _path_list(value) -> list[str]:
    """把表单里的「换行 / 逗号分隔字符串」或数组统一成路径列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        items = re.split(r"[\n,]", value)
    elif isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        return []
    out = []
    for item in items:
        cleaned = _clean_path(item)
        if cleaned:
            out.append(cleaned)
    return out


@dataclass
class ActiveRun:
    task_id: str
    executor: str
    trigger: str  # manual | auto | steer
    started_at: str
    cancel_event: threading.Event
    permission_mode: str | None = None
    # codex 安全模式（--sandbox 档位）；None = 跟随 config.codex.sandbox
    codex_sandbox: str | None = None
    sandbox_restart_pending: bool = False
    resume_session_id: str | None = None
    phase: str = "running"  # running | finished
    finished_at: str | None = None
    error: str | None = None
    logs: deque = field(default_factory=lambda: deque(maxlen=1200))
    thread: threading.Thread | None = None
    # 面板干预：用户打断时留的话，跑完后自动带话续跑
    steer_text: str | None = None
    steer_pending: bool = False
    session_id: str | None = None
    # 续跑时沿用本轮的执行选项，别退回配置默认值
    auto_merge: bool | None = None
    wait_deployment: bool | None = None
    auto_commit: bool | None = None
    model: str | None = None
    effort: str | None = None
    # 访问权限隔离（dsh 专用，走 session 的 sandbox/mode）；None = 跟随部署默认
    sandbox: str | None = None
    # 干预续跑要沿用同一个工作目录，否则会默默跑回隔离 worktree
    work_dir: str | None = None

    def snapshot(self, log_lines: int = 80) -> dict:
        return {
            "task_id": self.task_id,
            "executor": self.executor,
            "trigger": self.trigger,
            "permission_mode": self.permission_mode,
            "codex_sandbox": self.codex_sandbox,
            "sandbox_restart_pending": self.sandbox_restart_pending,
            "resume_session_id": self.resume_session_id,
            "session_id": self.session_id,
            "phase": self.phase,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "steer_pending": self.steer_pending,
            "steer_text": self.steer_text,
            "auto_commit": self.auto_commit,
            "logs": list(self.logs)[-log_lines:],
            "log_total": len(self.logs),
        }


class PanelService:
    def __init__(self, root: str | Path, config: Config, *, runner_overrides: dict | None = None, host=None):
        self.root = Path(root)
        self.config = config
        # PR 平台注入点：None 时 worker 按仓库配置现场构造（github/local）；测试传 FakeHost
        self._host = host
        self.state_path = self.root / ".handoff-run" / "panel-state.json"
        self._lock = threading.RLock()
        # cmd_deliver 会经历 delivered→deployed→verifying→verified 多步状态跳转，
        # 两个入口（验收/恢复推进）并发跑时，后写者拿旧 revision 直接写 verified，
        # 会撞「deployed→verified」非法跳转。面板内所有验收入口共用这把非阻塞互斥锁。
        self._verify_mutex = threading.Lock()
        self._active: ActiveRun | None = None
        self._deliver: dict | None = None
        self._advance: dict | None = None
        self._auto_stop = threading.Event()
        self._auto_thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._wake_server: notification.WakeServer | None = None
        self._wake_status: dict = {"ok": False, "reason": "未启动（手动模式）"}
        self._health_cache: dict[str, tuple[float, dict]] = {}
        # 前端会话登记表快照：表格 4 秒轮询一次，别每次都开 SQLite
        self._frontend_cache: tuple[float, dict[str, dict]] | None = None
        # 测试注入假执行器：{executor_name: runner}
        self._runner_overrides = runner_overrides or {}
        # 全服务共用一个 TaskStore：它自带实例级事务锁，避免多个线程在同一个
        # checkout 上并发 sync（reset --hard）互相冲突。执行器 claim 等默认
        # actor 用 worker 身份；面板自己的操作显式传 PANEL_ACTOR。
        self._shared_store = TaskStore(
            self.root, config, actor=Actor("worker", config.panel.worker_id)
        )
        self._panel_state = self._load_state()
        # codex 全局配置里的默认模型/思考程度，启动时读一次用于「跟随配置」展示
        self._codex_defaults = _read_codex_defaults()
        # 上次面板进程被杀时执行线程一并没了，任务会卡在 in_progress；
        # 启动时先把自己留下的僵尸租约放回 open，否则人只能干瞪眼。
        self._reclaimed: list[dict] = []
        self._reclaim_stale_leases()

    # -- 状态持久化 ----------------------------------------------------------
    def _default_state(self) -> dict:
        panel = self.config.panel
        return {
            "mode": "manual",
            "auto": {
                "executor": panel.default_executor,
                "permission_mode": None,
                "codex_sandbox": None,
                # 自动模式用的模型/思考程度；None = 跟随执行器配置（config 或 ~/.codex）
                "model": None,
                "effort": None,
                # 访问权限隔离（dsh 专用）：三档沙箱；None = 跟随部署默认
                "sandbox": None,
                "auto_merge": panel.auto_merge,
                "wait_deployment": panel.wait_deployment,
                "session_strategy": panel.session_strategy,
                # 填了就用这个目录直接跑，不再为每个任务建隔离 worktree
                "work_dir": _clean_path(panel.default_work_dir) or None,
                # 共享目录串行执行时，可选每单结束自动提交，避免改动堆到下一单
                "auto_commit": bool(panel.auto_commit),
            },
        }

    def _load_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("mode") in ("manual", "auto"):
                merged = self._default_state()
                merged["mode"] = data["mode"]
                if isinstance(data.get("auto"), dict):
                    merged["auto"].update({k: v for k, v in data["auto"].items() if k in merged["auto"]})
                # 旧状态文件里若留了非法档位，直接退回「跟随配置」，别把后续执行带崩
                try:
                    merged["auto"]["codex_sandbox"] = validate_sandbox(
                        merged["auto"].get("codex_sandbox")
                    )
                except ConfigError:
                    merged["auto"]["codex_sandbox"] = None
                # 旧状态文件没有这两个键时由 _default_state 补齐；有值时统一清洗
                merged["auto"]["work_dir"] = _clean_path(merged["auto"].get("work_dir")) or None
                merged["auto"]["auto_commit"] = bool(merged["auto"].get("auto_commit"))
                # 与 set_mode 一致：model/effort 只保留非空字符串，手改坏的值退回「跟随配置」
                for key in ("model", "effort"):
                    raw = merged["auto"].get(key)
                    merged["auto"][key] = raw.strip() if isinstance(raw, str) and raw.strip() else None
                # 访问权限隔离只认已知的三档；其它值一律当作「跟随默认」，免得把
                # 一个不存在的档位一路发到执行器才开始报错
                sandbox = merged["auto"].get("sandbox")
                merged["auto"]["sandbox"] = (
                    sandbox.strip() if isinstance(sandbox, str) and sandbox.strip() in CODEX_SANDBOX_MODES else None
                )
                return merged
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        return self._default_state()

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self._panel_state, ensure_ascii=False, indent=2), encoding="utf-8")

    # -- 查询 ----------------------------------------------------------------
    def _store(self, *, push: bool = True) -> TaskStore:
        return self._shared_store

    def state(self) -> dict:
        with self._lock:
            mode = self._panel_state["mode"]
            auto_cfg = dict(self._panel_state["auto"])
            active = self._active.snapshot() if self._active else None
        if active is not None:
            active["codex_sandbox_effective"] = (
                active.get("codex_sandbox") or self.config.codex.sandbox
            )
        return {
            "mode": mode,
            "auto": auto_cfg,
            "codex_sandbox_options": list(CODEX_SANDBOX_MODES),
            "codex_sandbox_default": self.config.codex.sandbox,
            "worker_id": self.config.panel.worker_id,
            "active": active,
            "deliver": self._deliver,
            "advance": dict(self._advance) if self._advance else None,
            "executors": {name: self._executor_health(name) for name in EXECUTORS},
            "wake_listener": dict(self._wake_status),
            "reclaimed": list(getattr(self, "_reclaimed", [])[-5:]),
            "projects": self._projects(),
            "environments": list(self.config.environments.keys()),
            "executor_options": self._executor_options(),
            "store_root": str(self.root),
            "now": iso(),
        }

    def _executor_options(self) -> dict:
        """面板执行对话框用：每个执行器可选的模型/思考程度。"""
        codex_def = getattr(self, "_codex_defaults", None) or {}
        return {
            "codex": {
                "default_model": self.config.codex.model or codex_def.get("model"),
                "default_effort": self.config.codex.effort or codex_def.get("effort"),
                "models": list(self.config.codex.models),
                "efforts": list(self.config.codex.efforts)
                # gpt-6.1 系列只认 low/medium/high/xhigh/max，minimal 会 400
                or ["low", "medium", "high", "xhigh", "max"],
            },
            "zcode": {
                "default_model": self.config.zcode.model,
                "default_effort": self.config.zcode.effort,
                "models": list(self.config.zcode.models),
                "efforts": list(self.config.zcode.efforts)
                or ["low", "medium", "high", "xhigh", "max"],
            },
        }

    def frontend_sessions(self) -> dict[str, dict]:
        """任务 → 登记的前端会话（谁创建的单、用哪个前端 Agent 收完成事件）。

        表格 4 秒轮询一次，这里照旧做 3 秒内存缓存：SQLite 每次开连接虽然快，
        但几十条任务轮着开也不值得。查不到就返回空表，不能因为前端会话库
        损坏/不存在把整个任务列表打挂。
        """
        cached = self._frontend_cache
        mono = time.monotonic()
        if cached is not None and mono - cached[0] < _FRONTEND_CACHE_TTL_SECONDS:
            return cached[1]
        out: dict[str, dict] = {}
        try:
            rows = FrontendSessions(
                self.config.notifications.session_path(self.root)
            ).registered()
        except (sqlite3.Error, OSError, HandoffError):
            rows = []
        for row in rows:
            try:
                task_id = str(row["task_id"])
                out[task_id] = {
                    "executor": frontend_session.session_executor(row),
                    "thread_id": row["thread_id"],
                    "workspace": row["workspace"],
                    "updated_at": row["updated_at"],
                }
            except (IndexError, KeyError, TypeError):
                continue
        self._frontend_cache = (mono, out)
        return out

    def _projects(self) -> list[dict]:
        """给面板下拉用：已注册项目 + 它在本地对应的目录（若配置了 repo_paths）。"""
        paths = {}
        try:
            paths = dict(self.config.checks.verifier.repo_paths or {})
        except AttributeError:
            paths = {}
        out: list[dict] = []
        for repo_id, repo in self.config.repositories.items():
            local = paths.get(repo_id)
            out.append(
                {
                    "id": repo_id,
                    "role": repo.role,
                    "host": repo.host,
                    "base_refs": list(repo.allowed_base_refs or ()),
                    "default_branch": repo.default_branch,
                    "local_path": str(local) if local else None,
                    "scope_profiles": profile_options(repo),
                }
            )
        return out

    def _executor_health(self, name: str) -> dict:
        cached = self._health_cache.get(name)
        now = time.monotonic()
        if cached and now - cached[0] < _HEALTH_TTL_SECONDS:
            return cached[1]
        try:
            runner = self._make_runner(name)
            version = runner.version()
            info = {"ok": version is not None, "version": version}
        except (HandoffError, AttributeError) as exc:
            info = {"ok": False, "version": None, "error": getattr(exc, "message", str(exc))}
        self._health_cache[name] = (now, info)
        return info

    def _make_runner(
        self,
        executor: str,
        *,
        resume_session_id: str | None = None,
        permission_mode: str | None = None,
        codex_sandbox: str | None = None,
        on_event=None,
        model: str | None = None,
        effort: str | None = None,
        sandbox: str | None = None,
    ):
        """测试可注入执行器实例或工厂；工厂能拿到 resume/权限档/安全模式参数。"""
        override = self._runner_overrides.get(executor)
        if callable(override):
            kwargs = {
                "resume_session_id": resume_session_id,
                "permission_mode": permission_mode,
            }
            # 老测试工厂只收两个参数：只在它明确接受 codex_sandbox 时传，保持兼容
            try:
                params = inspect.signature(override).parameters
                if "codex_sandbox" in params or any(
                    p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
                ):
                    kwargs["codex_sandbox"] = codex_sandbox
            except (TypeError, ValueError):
                pass
            return override(**kwargs)
        if override is not None:
            return override
        return self._build_runner(
            executor,
            resume_session_id=resume_session_id,
            permission_mode=permission_mode,
            codex_sandbox=codex_sandbox,
            on_event=on_event,
            model=model,
            effort=effort,
            sandbox=sandbox,
        )

    def list_tasks(self, *, status: str | list[str] | None = None) -> list[dict]:
        # 只读快照：轮询不走 git sync（一次 fetch 6 秒，会把面板拖成"点不动"）
        # 面板表格按创建时间倒序：最新任务排最上面；同一时刻用任务 id 兜底，
        # 避免顺序在两次轮询之间跳动。这里只影响展示，自动认领顺序不受影响
        # （_pick_open_task 走 store.list_tasks，仍按 created_at 正序，老单优先）。
        tasks = self._store().peek_tasks(status=status)
        return sorted(
            tasks,
            key=lambda t: (str(t.get("created_at") or ""), str(t.get("id") or "")),
            reverse=True,
        )

    def _last_session_for(self, task_id: str, executor: str) -> tuple[str, int] | None:
        """本任务最近一次执行的 (会话 id, attempt 号)，含失败的那轮。"""
        try:
            data = self.get_task(task_id)
        except HandoffError:
            return None
        rows = sessions.task_sessions(
            data["task"], data.get("events") or [], root=self.root
        )
        for row in rows:
            if row.executor == executor:
                return (row.id, int(row.attempt or 0))
        return None

    def sync(self) -> dict:
        """从交接仓远端拉最新任务单。

        面板轮询走 peek（只读本地、毫秒级），所以新单不会自己冒出来，
        需要这个显式动作。
        """
        store = self._shared_store
        branch = getattr(self.config.store, "branch", "main") or "main"
        ahead = git_out(store.root, "rev-list", "--count", f"origin/{branch}..HEAD")
        if ahead and ahead.strip().isdigit() and int(ahead) > 0:
            raise ConflictError(
                f"本地有 {ahead.strip()} 个未推送提交，先推上去再拉取（否则会被 reset 掉）"
            )
        before = {t["id"] for t in store.peek_tasks()}
        store.sync()
        after = store.peek_tasks()
        added = [t for t in after if t["id"] not in before]
        return {
            "added": len(added),
            "total": len(after),
            "ids": [t["id"] for t in added[:20]],
        }

    def get_task(self, task_id: str) -> dict:
        store = self._store()
        task = store.peek(task_id)
        events = self._read_events(task_id)
        return {
            "task": task,
            "events": events,
            "frontend": self.frontend_sessions().get(task_id),
        }

    def _read_events(self, task_id: str) -> list[dict]:
        directory = self.root / self.config.store.events_dir / task_id
        events: list[dict] = []
        if directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                # 与 store._scan 同理：跳过 _atomic_write 被硬杀时残留的 .tmp-*.json，
                # 否则同一事件会重复读出。
                if path.name.startswith("."):
                    continue
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        events.append(json.load(fh))
                except (OSError, json.JSONDecodeError):
                    continue
        return events

    def sessions(self, executor: str, *, task_id: str | None = None, limit: int = 50) -> list[dict]:
        if executor not in EXECUTORS:
            raise ConfigError(f"未知执行器: {executor!r}（可选: {', '.join(EXECUTORS)}）")
        result: list[sessions.SessionInfo] = []
        if task_id:
            try:
                data = self.get_task(task_id)
                result.extend(sessions.task_sessions(data["task"], data["events"], root=self.root))
            except HandoffError:
                pass
            result = [s for s in result if s.executor == executor]
        if executor == "codex":
            result.extend(sessions.codex_sessions(limit=limit))
        else:
            result.extend(sessions.zcode_sessions(limit=limit))
        seen: set[str] = set()
        unique: list[sessions.SessionInfo] = []
        for info in result:
            if info.id in seen:
                continue
            seen.add(info.id)
            if info.cwd is None and info.executor == "codex":
                info.cwd = sessions.codex_session_cwd(info.id)
            unique.append(info)
        return [s.as_dict() for s in unique[:limit]]

    def logs(self, task_id: str, attempt: int | None = None, tail: int = 200) -> dict:
        base = self.root / ".handoff-run" / "logs" / task_id
        if not base.is_dir():
            return {"task_id": task_id, "attempts": [], "files": []}
        attempts = sorted((p.name for p in base.iterdir() if p.is_dir()), reverse=True)
        chosen = f"attempt-{attempt}" if attempt is not None else (attempts[0] if attempts else None)
        files: list[dict] = []
        if chosen:
            for path in sorted((base / chosen).glob("*"), reverse=True):
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                # jsonl 事件流翻成人话再给面板；原始行留在 raw 里备查。
                shown = humanize_event_stream(text, tail=tail) if path.suffix == ".jsonl" else "\n".join(
                    text.splitlines()[-tail:]
                )
                files.append(
                    {
                        "name": path.name,
                        "attempt": chosen,
                        "size": path.stat().st_size,
                        "content": shown,
                        "raw": "\n".join(text.splitlines()[-tail:]) if path.suffix == ".jsonl" else None,
                    }
                )
        return {"task_id": task_id, "attempts": attempts, "files": files}

    # -- 执行 ----------------------------------------------------------------
    def _build_runner(
        self,
        executor: str,
        *,
        resume_session_id: str | None = None,
        permission_mode: str | None = None,
        codex_sandbox: str | None = None,
        on_event=None,
        model: str | None = None,
        effort: str | None = None,
        sandbox: str | None = None,
    ):
        log_dir = self.root / ".handoff-run" / "logs"
        # 密钥值从配置的文件读入，注入子进程 env；agent 拿不到面板的系统环境。
        secret_env = secret_files.agent_env(self.config)
        if executor == "codex":
            cfg = self.config.codex
            env_extra = dict(cfg.env or {})
            env_extra.update(secret_env)
            return CodexRunner(
                binary=cfg.binary,
                timeout_seconds=cfg.timeout_seconds,
                log_dir=log_dir,
                model=model or cfg.model,
                effort=effort or cfg.effort,
                persist=cfg.persist_thread,
                # 面板选的档位优先；留空才回落到 config/codex.yaml
                sandbox=codex_sandbox or cfg.sandbox,
                resume_thread=resume_session_id,
                # 代理等环境变量走配置，别指望从哪个终端起的面板都有
                env_extra=env_extra,
                # codex exec 本来就吐 --json 事件流，挂上就能实时看
                on_event=on_event,
            )
        if executor == "zcode":
            cfg = self.config.zcode
            env_extra = dict(cfg.env or {})
            env_extra.update(secret_env)
            return ZcodeRunner(
                binary=cfg.binary,
                timeout_seconds=cfg.timeout_seconds,
                log_dir=log_dir,
                model=model or cfg.model,
                effort=effort or cfg.effort,
                permission_mode=permission_mode or cfg.permission_mode,
                resume_session=resume_session_id,
                # 面板执行一律用 stream-json，执行过程能实时看到
                stream=on_event is not None,
                on_event=on_event,
                env_extra=env_extra,
            )
        raise ConfigError(f"未知执行器: {executor!r}（可选: {', '.join(EXECUTORS)}）")

    def run_task(
        self,
        task_id: str,
        *,
        executor: str,
        resume_session_id: str | None = None,
        permission_mode: str | None = None,
        codex_sandbox: str | None = None,
        auto_merge: bool | None = None,
        wait_deployment: bool | None = None,
        auto_commit: bool | None = None,
        trigger: str = "manual",
        extra_instruction: str | None = None,
        work_dir: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        sandbox: str | None = None,
        carry_logs: list[str] | None = None,
        reuse_worktree: str | None = None,
    ) -> dict:
        if executor not in EXECUTORS:
            raise ConfigError(f"未知执行器: {executor!r}（可选: {', '.join(EXECUTORS)}）")
        codex_sandbox = validate_sandbox(codex_sandbox)
        work_dir = _clean_path(work_dir) or None
        reuse_worktree = (reuse_worktree or "").strip() or None
        if resume_session_id == "last":
            info = self._last_session_for(task_id, executor)
            if not info:
                raise SchemaError("这个任务还没有可续跑的会话（历史里找不到执行记录）")
            resume_session_id, last_attempt = info
            if not work_dir and not reuse_worktree and last_attempt:
                # 续跑必须落在上一轮那个工作区里：新开一份的话 agent 以为改过、
                # 文件却是原始状态，接着跑等于白跑。
                candidate = (
                    self.root / ".handoff-run" / "worktrees" / task_id / f"attempt-{last_attempt}"
                )
                if candidate.is_dir():
                    reuse_worktree = str(candidate)
        if resume_session_id and not reuse_worktree and not work_dir:
            # 上下文说"我改过"，磁盘上是干净的，不点破它会直接跳到下一步
            note = (
                "注意：本轮跑在全新的工作区，你上一轮对文件的修改不在里面。"
                "动手前先用 git status 核对这些改动是否还在，缺了要重做。"
            )
            extra_instruction = f"{extra_instruction}\n\n{note}" if extra_instruction else note
        with self._lock:
            if self._active and self._active.phase == "running":
                raise ConflictError(
                    f"已有任务在执行: {self._active.task_id}（{self._active.executor}），等它结束或先取消"
                )
            cancel = threading.Event()
            # failed 的任务通常还挂着上次的 claim，不清掉一 claim 就撞 lease_lost
            self._release_stale_claim(task_id)
            run = ActiveRun(
                task_id=task_id,
                executor=executor,
                trigger=trigger,
                started_at=iso(),
                cancel_event=cancel,
                permission_mode=permission_mode,
                codex_sandbox=codex_sandbox,
                resume_session_id=resume_session_id,
                model=model,
                effort=effort,
                sandbox=sandbox,
                work_dir=work_dir,
            )
            # 干预续跑时把上一轮的尾巴带过来，否则界面突然只剩一行，像被清空了
            for line in carry_logs or []:
                run.logs.append(line)

            def log_fn(message: str) -> None:
                stamp = _stamp()
                for line in str(message).splitlines() or [""]:
                    run.logs.append(f"[{stamp}] {line}")

            if extra_instruction:
                log_fn(f"带上干预指令执行：{extra_instruction}")
            if executor == "codex":
                log_fn(
                    f"Codex 安全模式：{codex_sandbox or self.config.codex.sandbox}"
                    "（--sandbox；执行中可在顶部横幅随时改）"
                )
            auto_commit = self.config.panel.auto_commit if auto_commit is None else auto_commit
            if work_dir:
                # 目录模式没有 PR，自动合并无从谈起；但「等部署 + 自动验收」仍然适用：
                # 11-16 起目录模式会提交并推送当前分支，CI 部署后 /version 对齐 head_sha，
                # 这时必须等部署并把任务推进到 verified，否则永远卡在 delivered。
                # wait_deployment 沿用配置/调用方传值，不再强制关闭。
                auto_merge = False
                if auto_commit:
                    log_fn(
                        f"工作目录模式：{work_dir}（不建隔离 worktree，执行完自动提交并推送当前分支，提交信息由 AI 写）"
                    )
                else:
                    log_fn(f"工作目录模式：{work_dir}（不建隔离 worktree，不提交不推送）")
            else:
                # 隔离 worktree 走分支 + PR 链路，本来就会提交推送，此选项只对项目目录模式有意义
                auto_commit = False
            if reuse_worktree:
                log_fn(f"复用上一轮 worktree：{reuse_worktree}（带上未提交的改动继续）")
            runner = self._make_runner(
                executor,
                resume_session_id=resume_session_id,
                permission_mode=permission_mode,
                codex_sandbox=codex_sandbox,
                on_event=log_fn,
                model=model,
                effort=effort,
                sandbox=sandbox,
            )
            options = WorkerOptions(
                worker_id=self.config.panel.worker_id,
                lease_seconds=3600,
                codex_timeout_seconds=getattr(runner, "timeout_seconds", 3600),
                auto_merge=self.config.panel.auto_merge if auto_merge is None else auto_merge,
                wait_deployment=self.config.panel.wait_deployment if wait_deployment is None else wait_deployment,
                auto_commit=auto_commit,
                target_role="backend",
                keep_worktree=True,
                workdir=work_dir,
                reuse_worktree=reuse_worktree,
                # 额度用尽/限流/服务端故障：在任务单上排好重试时刻，由自动循环
                # 到点放回 open。关掉就退回「停在 failed 等人点按钮」。
                auto_retry=self.config.panel.auto_retry,
                max_auto_retries=self.config.panel.max_auto_retries,
                retry_base_seconds=self.config.panel.retry_base_seconds,
            )
            run.auto_merge = options.auto_merge
            run.wait_deployment = options.wait_deployment
            run.auto_commit = options.auto_commit
            worker = Worker(
                self.root,
                self.config,
                options,
                store=self._shared_store,
                codex=runner,
                host=self._host,
                logger=log_fn,
                cancel_event=cancel,
                prompt_note=extra_instruction,
            )
            self._active = run
            run.thread = threading.Thread(
                target=self._execute, args=(worker, run), name=f"panel-run-{task_id}", daemon=True
            )
            run.thread.start()
        return run.snapshot(log_lines=0)

    def _release_stale_claim(self, task_id: str) -> None:
        """执行前把失败任务遗留的 claim 清掉。

        失败时 worker 只写 status=failed，claim 仍留着（租约还没到期）。
        这时直接 claim 会撞 `lease_lost: 已被 … 持有，租约未过期`，
        表现为"点了执行没反应/立刻出错"。
        """
        try:
            task = self._shared_store.peek(task_id)
        except HandoffError:
            return
        if not task.get("claim"):
            return
        if task.get("status") not in ("failed", "cancelled", "open"):
            return
        try:
            self._shared_store.release_lease(
                task_id,
                actor=self.PANEL_ACTOR,
                reason="面板执行前释放残留租约",
                sync=False,
            )
        except HandoffError:
            return

    def _execute(self, worker: Worker, run: ActiveRun) -> None:
        try:
            worker.claim_and_process(run.task_id)
            # 交付成功后自动接上验收：worker 只做到提交/推送/等部署（deployed 为止），
            # 真正跑 acceptance checks 的 cmd_deliver 原本只能靠人点按钮，
            # 任务因此永远卡在 delivered。开启等待部署时就自动推进到 verified。
            self._auto_deliver(run)
        except HandoffError as exc:
            run.error = f"{exc.code}: {exc.message}"
            run.logs.append(f"[{_stamp()}] 执行出错：{run.error}")
        except Exception as exc:  # noqa: BLE001 - 面板线程不能崩
            run.error = f"{type(exc).__name__}: {exc}"
            run.logs.append(f"[{_stamp()}] 未预期错误：{run.error}")
        finally:
            run.phase = "finished"
            run.finished_at = iso()
            run.session_id = self._run_session_id(run)
            if run.cancel_event.is_set() and run.error is None:
                run.logs.append(f"[{_stamp()}] 已中断")

    def _auto_deliver(self, run: ActiveRun) -> None:
        """交付成功后自动验收，把任务从 delivered/deployed 推进到 verified。

        worker 的交付流程只做到「提交/推送 + 等部署对齐」，状态停在 deployed；
        真正跑 acceptance checks 的 cmd_deliver 原先只能靠面板按钮手动触发，
        没人点就一直卡着。这里在 run 正常结束后、且本轮开了 wait_deployment 时
        自动接上，失败不影响交付本身（只记日志/notes）。
        """
        if run.error is not None or run.cancel_event.is_set() or not run.wait_deployment:
            return
        task_id = run.task_id
        try:
            task = self._store().get(task_id)
        except HandoffError as exc:
            run.logs.append(f"[{_stamp()}] 自动验收前置读任务失败，跳过：{exc.message}")
            return
        status = task.get("status")
        # 只有进入交付链路的状态才验收；in_progress/open/failed 一律不碰。
        # delivered 且带未合并 PR 时 cmd_deliver 自己会返回 not_ready，这里不重复判断。
        if status not in ("delivered", "awaiting_merge", "merged", "deployed", "verifying"):
            run.logs.append(f"[{_stamp()}] 状态 {status} 无需自动验收")
            return
        # 目录模式的手动执行默认不开自动提交（auto_commit 默认 False），此时的
        # delivered 任务没有 head_sha / merge_sha，也就没有可对齐的部署目标。
        # 以前这里照样往下调 cmd_deliver，它只能抛
        # 「任务缺少 merge_sha/head_sha，无法对齐部署」，在面板上显示成一次
        # **验收失败**——其实什么都没做错，只是没有可验证的东西。
        #
        # 所以先看有没有目标：没有就明确跳过，并把人该做什么写进 deliver 状态。
        # 这不是把失败藏起来：状态仍是 delivered，日志与面板都说明「未提交」。
        target = (
            (task.get("deployment") or {}).get("target_sha")
            or (task.get("result") or {}).get("merge_sha")
            or (task.get("result") or {}).get("head_sha")
        )
        if not target:
            message = (
                "改动尚未提交/推送（目录模式未开自动提交），没有可对齐的部署目标；"
                "先在工作目录提交并推送，再「恢复推进」重新验收"
            )
            run.logs.append(f"[{_stamp()}] 跳过自动验收：{message}")
            with self._lock:
                self._deliver = {
                    "task_id": task_id,
                    "done": True,
                    "started_at": iso(),
                    "finished_at": iso(),
                    "result": {"id": task_id, "result": "no_target", "message": message},
                    "error": None,
                    "auto": True,
                }
            return
        from ..cli import cmd_deliver

        # 与手动验收/恢复推进共用互斥锁：拿不到说明已有验收在跑，本轮不重复推进
        if not self._verify_mutex.acquire(blocking=False):
            run.logs.append(f"[{_stamp()}] 已有验收在进行，跳过自动验收（避免状态并发跳转）")
            return
        run.logs.append(f"[{_stamp()}] 交付完成（{status}），自动验收中…")
        args = SimpleNamespace(task_id=task_id, no_push=False)
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stderr(buffer):
                result = cmd_deliver(args, self.config)
            outcome = result.get("result")
            if outcome == "verified":
                run.logs.append(f"[{_stamp()}] ✓ 自动验收通过，任务已 verified")
            else:
                run.logs.append(
                    f"[{_stamp()}] 自动验收未通过（{outcome}）：{result.get('message') or result.get('summary') or ''}"
                )
            # 同步到面板 deliver 面板块，方便 UI 直接显示本次自动验收结果
            with self._lock:
                self._deliver = {
                    "task_id": task_id,
                    "done": True,
                    "started_at": iso(),
                    "finished_at": iso(),
                    "result": result,
                    "error": None,
                    "auto": True,
                }
        except HandoffError as exc:
            run.logs.append(f"[{_stamp()}] 自动验收失败：{exc.code}: {exc.message}")
            with self._lock:
                self._deliver = {
                    "task_id": task_id,
                    "done": True,
                    "error": f"{exc.code}: {exc.message}",
                    "auto": True,
                }
        except Exception as exc:  # noqa: BLE001 - 验收失败不能让整个 run 线程崩
            run.logs.append(f"[{_stamp()}] 自动验收异常：{type(exc).__name__}: {exc}")
            with self._lock:
                self._deliver = {
                    "task_id": task_id,
                    "done": True,
                    "error": f"{type(exc).__name__}: {exc}",
                    "auto": True,
                }
        finally:
            self._verify_mutex.release()

    def _run_session_id(self, run: ActiveRun) -> str | None:
        """本轮执行用的会话 id，供打断后续跑接上下文。"""
        if run.resume_session_id:
            return run.resume_session_id
        try:
            task = self._store().peek(run.task_id)
        except HandoffError:
            task = None
        if isinstance(task, dict):
            result = task.get("result") or {}
            stored = result.get("executor_session_id") or result.get("codex_thread_id")
            if stored:
                return str(stored)
            if run.executor == "zcode" and task.get("attempt"):
                # zcode 新会话 id 是按 (任务, 次数) 确定性预生成的
                try:
                    return session_uuid(run.task_id, int(task["attempt"]))
                except (TypeError, ValueError):
                    return None
        return None

    def cancel(self) -> dict:
        with self._lock:
            run = self._active
        if run is None or run.phase != "running":
            raise ConflictError("没有正在执行的任务")
        run.cancel_event.set()
        run.logs.append(f"[{_stamp()}] 已请求取消，正在终止执行器进程树…")
        return {"cancelling": run.task_id}

    def steer(self, text: str) -> dict:
        """会话里说话驱动执行：执行中=打断并带话续跑；已结束=直接带话续跑同一会话。"""
        text = (text or "").strip()
        if not text:
            raise SchemaError("干预指令不能为空")
        with self._lock:
            run = self._active
            if run is None:
                raise ConflictError("没有可续跑的会话（先跑一单）")
            if run.phase == "running":
                run.steer_text = text
                run.steer_pending = True
                run.cancel_event.set()
                run.logs.append(f"[{_stamp()}] ⏸ 收到干预：{text}")
                run.logs.append(f"[{_stamp()}] 正在中断当前执行，随后带这句话续跑同一会话…")
                target = self._resume_after_steer
                name = f"panel-steer-{run.task_id}"
            else:
                # 任务已结束：不开新对话，接着同一会话/工作区继续派活
                run.logs.append(f"[{_stamp()}] ▶ 上一轮已结束，续话：{text}")
                run.logs.append(f"[{_stamp()}] 正在带这句话续跑同一会话（上下文不丢）…")
                target = self._continue_after_finish
                name = f"panel-continue-{run.task_id}"
        threading.Thread(
            target=target,
            args=(run, text),
            name=name,
            daemon=True,
        ).start()
        return {
            "task_id": run.task_id,
            "text": text,
            "steering": run.phase == "running",
            "continuing": run.phase != "running",
        }

    def _resume_after_steer(self, run: ActiveRun, text: str) -> None:
        """等被中断的 run 收尾 → 任务置回 open → 用同一会话继续跑。"""
        carry = [
            f"[{_stamp()}] —— 上一轮被干预打断（invalid_output 是打断产物，不是故障）",
            f"[{_stamp()}] 干预指令：{text}",
        ]
        try:
            self._restart_run(
                run,
                extra_instruction=text,
                carry_logs=carry,
                trigger="steer",
                failure_note="这条指令没生效，需要时请手动再执行。",
                label="干预",
                requeue_finished=True,
            )
        finally:
            run.steer_pending = False

    def _continue_after_finish(self, run: ActiveRun, text: str) -> None:
        """任务自然结束后用户续话：重新入队，用同一会话/工作区接着执行。"""
        carry = [
            f"[{_stamp()}] —— 上一轮已自然结束，用户续话执行（attempt 接着累加）",
            f"[{_stamp()}] 续话指令：{text}",
        ]
        self._restart_run(
            run,
            extra_instruction=text,
            carry_logs=carry,
            trigger="continue",
            failure_note="这条指令没生效，需要时请手动再执行。",
            label="续话",
            requeue_finished=True,
        )

    def _restart_run(
        self,
        run: ActiveRun,
        *,
        extra_instruction: str | None,
        carry_logs: list[str],
        trigger: str,
        failure_note: str,
        label: str,
        requeue_finished: bool = False,
    ) -> bool:
        """打断/续话后的公共续跑：沿用本轮全部执行选项接同一会话。"""
        if run.thread is not None:
            run.thread.join(timeout=180)
        task_id = run.task_id
        try:
            task = self._store().get(task_id)
        except HandoffError as exc:
            run.logs.append(f"[{_stamp()}] {label}失败：读任务出错 {exc.message}")
            return False
        status = task.get("status")
        session_id = run.session_id or self._run_session_id(run)
        try:
            if status == "failed":
                self.retry(task_id)
            elif requeue_finished and status in ("delivered", "verified"):
                self._requeue_finished(task_id)
            else:
                run.logs.append(
                    f"[{_stamp()}] 当前执行已自然结束（{status}），未自动续跑；"
                    f"{failure_note}"
                )
                return False
        except HandoffError as exc:
            run.logs.append(f"[{_stamp()}] {label}失败：重新入队出错 {exc.message}")
            return False
        prev_attempt = int(task.get("attempt") or 0)
        prev_worktree = None
        if prev_attempt:
            candidate = self.root / ".handoff-run" / "worktrees" / task_id / f"attempt-{prev_attempt}"
            if candidate.is_dir():
                prev_worktree = str(candidate)
        try:
            self.run_task(
                task_id,
                executor=run.executor,
                resume_session_id=session_id,
                permission_mode=run.permission_mode,
                codex_sandbox=run.codex_sandbox,
                auto_merge=run.auto_merge,
                wait_deployment=run.wait_deployment,
                auto_commit=run.auto_commit,
                model=run.model,
                effort=run.effort,
                work_dir=run.work_dir,
                trigger=trigger,
                extra_instruction=extra_instruction,
                carry_logs=carry_logs,
                # 打断续跑也复用刚结束那轮的工作区，否则刚改的东西同样会丢
                reuse_worktree=prev_worktree,
            )
        except HandoffError as exc:
            run.logs.append(f"[{_stamp()}] {label}续跑失败：{exc.message}")
            return False
        return True

    def _requeue_finished(self, task_id: str) -> None:
        """delivered/verified 重新入队（会话续跑）：先清残留租约，再回到 open。"""
        store = self._store(push=True)
        task = store.get(task_id)
        if task.get("claim"):
            # release_lease 会把状态一并置回 open（delivered/verified → open）
            store.release_lease(
                task_id,
                actor=self.PANEL_ACTOR,
                reason="面板：会话续跑释放残留租约",
                sync=False,
            )
            return
        task = store.get(task_id, sync=False)
        if task["status"] in ("delivered", "verified"):
            store.transition(
                task_id,
                "open",
                expected_revision=task["revision"],
                summary="面板：会话续跑重新入队",
                actor=self.PANEL_ACTOR,
            )

    # -- 安全模式（执行中随时可改） ------------------------------------------
    def set_security_mode(self, codex_sandbox, *, apply_now: bool = False) -> dict:
        """改 codex 的 --sandbox 档位，并写成面板默认（持久化）。

        `codex exec` 的 --sandbox 是进程启动参数，正在跑的进程无法热改：
        apply_now=False → 存为默认，从下一轮执行/干预续跑开始生效；
        apply_now=True  → 打断当前 codex，用新档位续跑同一会话，当场生效。
        """
        try:
            mode = validate_sandbox(codex_sandbox)
        except ConfigError as exc:
            raise SchemaError(exc.message) from exc
        effective = mode or self.config.codex.sandbox
        notice = ""
        restarted = False
        with self._lock:
            self._panel_state["auto"]["codex_sandbox"] = mode
            self._save_state()
            run = self._active
            if run is not None:
                run.codex_sandbox = mode
            if run is not None and run.phase == "running":
                run.logs.append(
                    f"[{_stamp()}] 安全模式已设为 {effective}（已存为默认；"
                    "正在跑的 codex 进程参数是启动时固定的，改动从下一轮生效）"
                )
                if apply_now and run.executor != "codex":
                    notice = "当前跑的是 zcode，安全模式只对 codex 有效；已存为默认，下个 codex 任务生效。"
                elif apply_now and run.sandbox_restart_pending:
                    notice = "上一次切换正在中断续跑，无需重复点击。"
                elif apply_now:
                    run.sandbox_restart_pending = True
                    restarted = True
        if restarted and run is not None:
            run.cancel_event.set()
            run.logs.append(f"[{_stamp()}] ⏸ 正在中断当前 codex，改用 {effective} 续跑同一会话…")
            threading.Thread(
                target=self._restart_after_security,
                args=(run,),
                name=f"panel-security-{run.task_id}",
                daemon=True,
            ).start()
        result = self.state()
        result["security"] = {
            "codex_sandbox": mode,
            "effective": effective,
            "apply_now": restarted,
            "notice": notice or ("已中断并按新档位续跑" if restarted else "已存为默认，下一轮执行生效"),
        }
        return result

    def _restart_after_security(self, run: ActiveRun) -> None:
        carry = [
            f"[{_stamp()}] —— 上一轮为切换安全模式被打断（invalid_output 是打断产物，不是故障）",
            f"[{_stamp()}] 新安全模式：{run.codex_sandbox or self.config.codex.sandbox}",
        ]
        try:
            self._restart_run(
                run,
                extra_instruction=None,
                carry_logs=carry,
                trigger="security",
                failure_note="新模式已存为默认，下一轮执行会自动用它。",
                label="安全模式切换",
            )
        finally:
            run.sandbox_restart_pending = False

    # -- 模式 ----------------------------------------------------------------
    def set_mode(self, payload: dict) -> dict:
        mode = payload.get("mode")
        if mode not in ("manual", "auto"):
            raise SchemaError("mode 只能是 manual / auto")
        auto_patch = payload.get("auto")
        if auto_patch is not None and not isinstance(auto_patch, dict):
            raise SchemaError("auto 必须是对象")
        # 先校验补丁再落状态：校验失败不能留下半套配置
        if isinstance(auto_patch, dict):
            merged = dict(self._panel_state["auto"])
            merged.update({k: v for k, v in auto_patch.items() if k in merged})
            if merged.get("executor") not in EXECUTORS:
                raise SchemaError(f"auto.executor 只能是 {'/'.join(EXECUTORS)}")
            if merged.get("session_strategy") not in ("new", "continue"):
                raise SchemaError("auto.session_strategy 只能是 new / continue")
            if "codex_sandbox" in auto_patch:
                try:
                    merged["codex_sandbox"] = validate_sandbox(auto_patch["codex_sandbox"])
                except ConfigError as exc:
                    raise SchemaError(exc.message) from exc
            if "work_dir" in auto_patch:
                merged["work_dir"] = _clean_path(auto_patch["work_dir"]) or None
            if "auto_commit" in auto_patch:
                merged["auto_commit"] = bool(auto_patch["auto_commit"])
            for key in ("model", "effort"):
                if key in auto_patch:
                    value = auto_patch[key]
                    if value is not None and not isinstance(value, str):
                        raise SchemaError(f"auto.{key} 只能是字符串或 null")
                    value = str(value).strip() if isinstance(value, str) else ""
                    merged[key] = value or None
            if "sandbox" in auto_patch:
                value = auto_patch["sandbox"]
                if value is None or (isinstance(value, str) and not value.strip()):
                    merged["sandbox"] = None
                elif not isinstance(value, str) or value.strip() not in CODEX_SANDBOX_MODES:
                    # 只允许宿主真正认识的档位，拼错就明确报错而不是静默变成「默认」
                    raise SchemaError(
                        f"auto.sandbox 只能是 {'/'.join(CODEX_SANDBOX_MODES)} 或 null"
                    )
                else:
                    merged["sandbox"] = value.strip()
        with self._lock:
            self._panel_state["mode"] = mode
            if isinstance(auto_patch, dict):
                self._panel_state["auto"].update(merged)
            self._save_state()
        if mode == "auto":
            self._ensure_auto_loop()
        else:
            self._stop_wake_server()
        return self.state()

    def _ensure_auto_loop(self) -> None:
        need_wake = False
        with self._lock:
            if self._auto_thread is not None and self._auto_thread.is_alive():
                # manual → auto 切回来：auto 线程还活着，不会重跑启动逻辑，
                # 切手动时被停掉的唤醒监听要在这里补起来，否则 8788 一直是黑的
                need_wake = self._wake_server is None
            else:
                self._auto_stop.clear()
                self._auto_thread = threading.Thread(target=self._auto_loop, name="panel-auto", daemon=True)
                self._auto_thread.start()
        if need_wake:
            self._start_wake_server()

    def _auto_work_dir(self, task_id: str, auto_cfg: dict) -> str | None:
        """自动模式的工作目录：配置优先，其次按任务仓库查 repo_paths。"""
        configured = _clean_path(auto_cfg.get("work_dir")) or None
        if configured:
            return configured
        try:
            task = self._store().peek(task_id)
        except HandoffError:
            return None
        repo_id = (task.get("target") or {}).get("repo")
        verifier = getattr(self.config.checks, "verifier", None)
        paths = dict(getattr(verifier, "repo_paths", None) or {})
        return _clean_path(paths.get(repo_id)) or None

    def _auto_busy_task(self) -> str | None:
        """串行闸：仓库里还有正在处理的任务就返回它的 id，自动队列不接新单。

        只认带新鲜心跳的 claim，避免别的 worker 挂掉后留下的僵尸 in_progress
        把自动队列永久卡死。peek_tasks 不触发 sync，不会和正在写的 worker 抢 checkout。
        """
        try:
            tasks = self._store().peek_tasks(status=list(_AUTO_BUSY_STATUSES))
        except HandoffError:
            return None
        for task in tasks:
            heartbeat_at = (task.get("claim") or {}).get("heartbeat_at")
            if not heartbeat_at:
                continue
            try:
                age = (now() - parse(heartbeat_at)).total_seconds()
            except (ValueError, TypeError):
                continue
            if 0 <= age < _AUTO_BUSY_MAX_AGE_SECONDS:
                return task["id"]
        return None

    def _requeue_due_retries(self) -> list[str]:
        """把到点的可重试失败放回 open，返回本轮放回的任务 id。

        额度用尽、限流、服务端故障这类失败不是「做不成」，只是「现在不行」。
        worker 失败时已经在 `failure.retry_at` 记下了该重试的时刻，这里到点就把它
        转回 open——自动循环下一轮自然会捡起来，并按 session_strategy 续跑原会话。

        为什么用 `failed → open` 而不是新状态：状态机本来就有这条合法转换
        （「重新入队，attempt+1」），语义完全一致，没必要为它多开一个状态去扩散
        影响面。为什么不用 `claimed`：那是 worker 认领时的转移，面板不该抢。
        """
        reopened: list[str] = []
        try:
            candidates = self._store().peek_tasks(status="failed")
        except HandoffError:
            return reopened
        current = now()
        for task in candidates:
            failure = task.get("failure") or {}
            if not failure.get("retryable"):
                continue
            retry_at = failure.get("retry_at")
            if not retry_at:
                continue
            try:
                due = parse(retry_at) <= current
            except (ValueError, TypeError):
                continue
            if not due:
                continue
            task_id = task.get("id")
            try:
                # 先把 claim 清掉：失败时 claim 还留着，直接 transition 会在
                # claim 校验处被挡（租约未过期）。release_lease 本身就会把状态
                # 一并置回 open。
                if task.get("claim"):
                    self._store(push=False).release_lease(
                        task_id,
                        actor=self.PANEL_ACTOR,
                        reason="额度/服务恢复，自动重试",
                        sync=False,
                    )
                else:
                    self._store(push=False).transition(
                        task_id,
                        "open",
                        expected_revision=task["revision"],
                        summary="额度/服务恢复，自动重试重新入队",
                        actor=self.PANEL_ACTOR,
                    )
                reopened.append(task_id)
            except HandoffError:
                # 单个任务重排失败（revision 变了、别人抢先了）不该拖垮整轮
                continue
        return reopened

    def _auto_loop(self) -> None:
        self._start_wake_server()
        panel = self.config.panel
        poll = max(panel.poll_interval_seconds, 1.0)
        while not self._auto_stop.is_set():
            self._wake.clear()
            with self._lock:
                mode = self._panel_state["mode"]
                auto_cfg = dict(self._panel_state["auto"])
            # 两种模式都要跑：任务可能是人在手动模式下点执行时撞上额度耗尽的，
            # 那时不该因为「没切到自动模式」就把重试一起丢掉。这一步只做状态
            # 回收（failed → open），真正执行仍然受下面 auto 分支的串行闸约束。
            for retried in self._requeue_due_retries():
                # 面板服务没有自己的 logger；这行只作诊断，写到 stderr 免得
                # 依赖一个不存在的属性把整个自动循环打挂。
                print(f"[panel] 额度/服务已恢复，重新入队：{retried}", flush=True)
            if mode == "auto":
                # 串行闸 1：本面板的执行线程还没收尾，等它跑完，本轮绝不挑新任务
                with self._lock:
                    active = self._active
                if active is not None and active.phase == "running":
                    if active.thread is not None:
                        active.thread.join()
                    continue
                # 串行闸 2：仓库里还有别的任务在处理（含常驻 worker 的活），不抢下一单
                if self._auto_busy_task() is not None:
                    self._wake.wait(timeout=poll)
                    continue
                task_id = self._pick_open_task()
                if task_id is not None:
                    resume = self._auto_resume_session(task_id, auto_cfg)
                    try:
                        self.run_task(
                            task_id,
                            executor=auto_cfg["executor"],
                            resume_session_id=resume,
                            permission_mode=auto_cfg.get("permission_mode"),
                            codex_sandbox=auto_cfg.get("codex_sandbox"),
                            auto_merge=auto_cfg.get("auto_merge"),
                            wait_deployment=auto_cfg.get("wait_deployment"),
                            auto_commit=auto_cfg.get("auto_commit"),
                            work_dir=self._auto_work_dir(task_id, auto_cfg),
                            model=auto_cfg.get("model"),
                            effort=auto_cfg.get("effort"),
                            sandbox=auto_cfg.get("sandbox"),
                            trigger="auto",
                        )
                        # 单并发：等这一单彻底跑完再取下一单
                        with self._lock:
                            active = self._active
                        if active is not None and active.thread is not None:
                            active.thread.join()
                        continue
                    except HandoffError:
                        time.sleep(max(panel.poll_interval_seconds, 2))
                        continue
            self._wake.wait(timeout=poll)
        self._stop_wake_server()

    def _pick_open_task(self) -> str | None:
        try:
            store = self._store()
            tasks = store.list_tasks(status="open")
        except HandoffError:
            return None
        candidates = []
        for task in tasks:
            repo = self.config.repositories.get(task.get("target", {}).get("repo"))
            if repo is not None and repo.role == "backend":
                candidates.append(task)
        if not candidates:
            return None
        candidates.sort(key=lambda t: t["created_at"])
        return candidates[0]["id"]

    def _auto_resume_session(self, task_id: str, auto_cfg: dict) -> str | None:
        if auto_cfg.get("session_strategy") != "continue":
            return None
        try:
            task = self._store().get(task_id)
        except HandoffError:
            return None
        result = task.get("result") or {}
        # 只有同执行器才能接续；跨执行器换新会话
        if result.get("executor") == auto_cfg["executor"] and result.get("executor_session_id"):
            return result["executor_session_id"]
        if auto_cfg["executor"] == "codex" and result.get("codex_thread_id"):
            return result["codex_thread_id"]
        return None

    def _start_wake_server(self) -> None:
        cfg = self.config.notifications
        event = threading.Event()
        try:
            server = notification.WakeServer(self.root, cfg, event)
            host, port = server.start(cfg.listen_host, cfg.listen_port)
        except (OSError, HandoffError) as exc:
            self._wake_status = {
                "ok": False,
                "reason": f"唤醒监听未启动（{exc}）；旧 worker 还占着 {cfg.listen_port} 端口？自动模式退回轮询",
            }
            return
        self._wake_server = server
        # WakeServer 置位的是它自己的 event，桥接到面板 _wake
        threading.Thread(target=self._bridge_wake, args=(event,), name="panel-wake-bridge", daemon=True).start()
        self._wake_status = {"ok": True, "url": f"http://{host}:{port}{notification.WAKE_PATH}"}

    def _bridge_wake(self, event: threading.Event) -> None:
        while not self._auto_stop.is_set():
            if event.wait(timeout=1.0):
                self._wake.set()
                event.clear()

    def _stop_wake_server(self) -> None:
        server = self._wake_server
        self._wake_server = None
        if server is not None:
            server.stop()
        if self._panel_state.get("mode") != "auto":
            self._wake_status = {"ok": False, "reason": "未启动（手动模式）"}

    # -- 任务辅助 ------------------------------------------------------------
    PANEL_ACTOR = Actor("user", "handoff-panel")

    def create_task(self, payload: dict) -> dict:
        """面板手工发单：选项目 + 写需求 → 一张 open 任务单。"""
        title = str(payload.get("title") or "").strip()
        if not title:
            raise SchemaError("标题不能为空")
        requirement = str(payload.get("requirement") or "").strip()
        if not requirement:
            raise SchemaError("需求正文不能为空")

        repo_id = str(payload.get("repo") or "").strip()
        repo = self.config.repositories.get(repo_id)
        if repo is None:
            raise SchemaError(f"目标项目未注册: {repo_id!r}")
        if repo.role != "backend":
            raise SchemaError(f"面板只能给 backend 项目发单（{repo_id} 的角色是 {repo.role}）")

        allowed_refs = list(repo.allowed_base_refs or ())
        base_ref = str(payload.get("base_ref") or (allowed_refs[0] if allowed_refs else repo.default_branch))
        if allowed_refs and base_ref not in allowed_refs:
            raise SchemaError(f"基线分支 {base_ref!r} 不在 {repo_id} 允许的分支 {allowed_refs} 内")

        environments = list(self.config.environments.keys())
        environment = str(payload.get("environment") or "").strip()
        if not environment:
            environment = "dev" if "dev" in environments else (environments[0] if environments else "dev")
        if environment not in environments:
            raise SchemaError(f"未知环境: {environment!r}（可选: {', '.join(environments) or '无'}）")

        source_repo_id = str(payload.get("source_repo") or "").strip()
        source_sha = str(payload.get("source_sha") or "").strip() or "0" * 40
        if not re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", source_sha):
            raise SchemaError("来源提交必须是 40 位十六进制 sha（可留空）")
        if source_repo_id:
            source_repo = self.config.repositories.get(source_repo_id)
            if source_repo is None:
                raise SchemaError(f"来源项目未注册: {source_repo_id!r}")
            source = {"role": source_repo.role, "repo": source_repo_id, "commit_sha": source_sha}
        else:
            # 没有前端提交时用目标仓库占位，保证 schema/语义校验都能过
            source = {"role": repo.role, "repo": repo_id, "commit_sha": source_sha}

        if _path_list(payload.get("allowed_paths")) or _path_list(payload.get("forbidden_paths")):
            raise SchemaError("路径范围由后端仓库的 scope 模板管理；请提交 scope_profile 和语义边界")
        scope_profile = str(payload.get("scope_profile") or "").strip() or None
        resolved_scope = resolve_scope(
            repo,
            profile_name=scope_profile,
            out_of_scope=_path_list(payload.get("out_of_scope")),
        )

        task = {
            "schema_version": 1,
            "id": ids.task_id(),
            "revision": 1,
            "attempt": 0,
            "title": title[:200],
            "source": source,
            "target": {"role": "backend", "repo": repo_id, "base_ref": base_ref},
            "requirement": requirement,
            "contract": None,
            "repro": None,
            "scope": resolved_scope,
            "acceptance": {"environment": environment, "checks": []},
            "migration": {
                "required": False,
                "backward_compatible": True,
                "plan": "",
                "rollback": "",
                "manual_approval_required": False,
            },
            "status": "open",
            "claim": None,
            "result": {
                "branch": None,
                "head_sha": None,
                "pr_url": None,
                "pr_number": None,
                "merge_sha": None,
                "codex_run_id": None,
                "codex_cli_version": None,
                "prompt_version": None,
                "diff_summary": None,
                "changed_paths": [],
                "worktree": None,
                "codex_thread_id": None,
                "checks": [],
                "notes": None,
            },
            "deployment": {
                "status": "pending",
                "target_sha": None,
                "observed_sha": None,
                "environment": environment,
                "url": None,
                "updated_at": None,
                "error": None,
            },
            "failure": None,
        }
        return self._shared_store.create(
            task, summary=f"面板创建任务：{title[:60]}", actor=self.PANEL_ACTOR
        )

    def _reclaim_stale_leases(self) -> None:
        """回收「本面板持有、但没有线程在跑」的租约。

        面板进程被关掉/杀掉时，`_execute` 线程随之消失，任务永远停在
        in_progress；而「重新入队」只认 failed，人就卡死了。启动时清一遍。
        """
        panel_worker = self.config.panel.worker_id
        try:
            tasks = self._shared_store.peek_tasks(status=["claimed", "in_progress"])
        except HandoffError:
            return
        for task in tasks:
            claim = task.get("claim") or {}
            if claim.get("worker_id") != panel_worker:
                continue
            active = self._active
            if active is not None and active.task_id == task["id"] and active.phase == "running":
                continue
            try:
                self._shared_store.release_lease(
                    task["id"],
                    actor=self.PANEL_ACTOR,
                    reason="面板启动：回收上次进程残留的租约",
                    sync=False,  # 启动时逐个 fetch 会把面板拖到二三十秒才可用
                )
            except HandoffError:
                continue
            self._reclaimed.append({"task_id": task["id"], "at": iso()})

    def retry(self, task_id: str) -> dict:
        store = self._store(push=True)
        task = store.get(task_id)
        if task["status"] != "failed":
            raise ConflictError(f"只有 failed 状态才能重试（当前 {task['status']}）")
        if task.get("claim"):
            # 失败时 claim 还留着，不清掉下次 claim 会撞"租约未过期"
            return store.release_lease(
                task_id, actor=self.PANEL_ACTOR, reason="面板：重新入队"
            )
        return store.transition(
            task_id,
            "open",
            expected_revision=task["revision"],
            summary="面板：重新入队",
            actor=self.PANEL_ACTOR,
        )

    def release(self, task_id: str, reason: str = "面板释放租约") -> dict:
        store = self._store(push=True)
        return store.release_lease(task_id, actor=self.PANEL_ACTOR, reason=reason)

    def accept(self, task_id: str, reason: str = "面板：前端验收通过") -> dict:
        """前端验收通过：verified → frontend_accepted（终态记录，不重发完成通知）。"""
        store = self._store(push=True)
        task = store.get(task_id)
        if task["status"] != "verified":
            raise ConflictError(f"只有 verified 状态才能标记前端已验收（当前 {task['status']}）")
        return store.transition(
            task_id,
            "frontend_accepted",
            expected_revision=task["revision"],
            summary=reason,
            actor=self.PANEL_ACTOR,
            event_type="frontend.accepted",
        )

    def resend_completed(self, task_id: str) -> dict:
        """手动补发一次完成通知到创建任务的前端监听端口。"""
        from .. import notification

        store = self._store(push=False)
        task = store.get(task_id)
        return notification.replay_task_completed(
            self.root,
            self.config.notifications,
            task,
            store_commit=store.task_commit(task_id),
        )

    def deliver(self, task_id: str) -> dict:
        with self._lock:
            if self._deliver and not self._deliver.get("done"):
                raise ConflictError(f"已有验收在进行: {self._deliver.get('task_id')}")
            if not self._verify_mutex.acquire(blocking=False):
                raise ConflictError("已有部署/验收在进行（含恢复推进），请等它结束")
            self._deliver = {"task_id": task_id, "done": False, "started_at": iso(), "result": None, "error": None}
        threading.Thread(
            target=self._deliver_task, args=(task_id,), name=f"panel-deliver-{task_id}", daemon=True
        ).start()
        return dict(self._deliver)

    def _deliver_task(self, task_id: str) -> None:
        from ..cli import cmd_deliver

        args = SimpleNamespace(task_id=task_id, no_push=False)
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stderr(buffer):
                result = cmd_deliver(args, self.config)
            self._deliver.update(done=True, finished_at=iso(), result=result)
        except HandoffError as exc:
            self._deliver.update(done=True, finished_at=iso(), error=f"{exc.code}: {exc.message}")
        except Exception as exc:  # noqa: BLE001
            self._deliver.update(done=True, finished_at=iso(), error=f"{type(exc).__name__}: {exc}")
        finally:
            self._verify_mutex.release()

    # -- 恢复推进 ------------------------------------------------------------
    def advance(self, task_id: str) -> dict:
        """把中断在交付链路上的任务重新接上后续流程（后台执行，进度进 state）。"""
        # 同步挡掉明显不合法的状态，别让按钮点了只回一句"已启动"再默默失败
        try:
            current = self._store(push=True).peek(task_id)
        except (HandoffError, AttributeError):
            current = None
        if isinstance(current, dict) and current.get("status") not in _ADVANCE_STATES:
            raise ConflictError(
                f"当前状态 {current.get('status')} 不需要恢复推进"
                f"（仅 {', '.join(sorted(_ADVANCE_STATES))} 可用）"
            )
        with self._lock:
            if self._advance and not self._advance.get("done"):
                raise ConflictError(f"已有恢复推进在进行: {self._advance.get('task_id')}")
            if not self._verify_mutex.acquire(blocking=False):
                raise ConflictError("已有部署/验收在进行（含自动验收），请等它结束")
            self._advance = {
                "task_id": task_id,
                "done": False,
                "started_at": iso(),
                "steps": [],
                "result": None,
                "message": None,
                "error": None,
            }
        threading.Thread(
            target=self._advance_task, args=(task_id,), name=f"panel-advance-{task_id}", daemon=True
        ).start()
        return dict(self._advance)

    def _advance_task(self, task_id: str) -> None:
        from ..cli import cmd_deliver

        store = self._store(push=True)
        steps: list[str] = self._advance["steps"] if self._advance else []
        try:
            task = store.get(task_id)
            status = task["status"]
            if status not in _ADVANCE_STATES:
                raise ConflictError(
                    f"当前状态 {status} 不需要恢复推进（仅 {', '.join(sorted(_ADVANCE_STATES))} 可用）"
                )

            # 1) 断在合并前：有 PR 走自动合并；目录模式（无 PR）没有合并环节，直接部署验收
            if status in ("delivered", "awaiting_merge"):
                pr_number = (task.get("result") or {}).get("pr_number")
                if pr_number:
                    if not self._advance_merge(task_id, pr_number, steps):
                        self._advance.update(
                            done=True,
                            result="blocked",
                            message="PR 尚未合并（gate 未通过或还在等待），状态未改；处理后可再点一次",
                        )
                        return
                    status = "merged"
                else:
                    # 目录模式：改动已直接提交在目标分支（没有 PR / 合并环节）。
                    if not (task.get("result") or {}).get("head_sha"):
                        raise ExecutionError(
                            "任务缺少 head_sha（改动尚未提交），无法部署验收；请先在工作目录提交改动"
                        )
                    # delivered 时先补推未推上去的提交；已推送则是 no-op。
                    if status == "delivered":
                        self._advance_push(task, steps)
                    args = SimpleNamespace(task_id=task_id, no_push=False)
                    with contextlib.redirect_stderr(io.StringIO()):
                        outcome = cmd_deliver(args, self.config)
                    steps.append(f"目录模式跳过合并，直接部署验收：{outcome.get('result', 'unknown')}")
                    self._advance.update(
                        done=True,
                        result=outcome.get("result"),
                        message=outcome.get("message"),
                        deliver=outcome,
                    )
                    return

            # 2) 合并之后断掉：复用现有验收命令（部署对齐 + checks → verified）
            if status in ("merged", "deployed", "verifying"):
                args = SimpleNamespace(task_id=task_id, no_push=False)
                with contextlib.redirect_stderr(io.StringIO()):
                    outcome = cmd_deliver(args, self.config)
                steps.append(f"验收：{outcome.get('result', 'unknown')}")
                self._advance.update(
                    done=True,
                    result=outcome.get("result"),
                    message=outcome.get("message"),
                    deliver=outcome,
                )
                return

            self._advance.update(done=True, result="noop", message=f"状态 {status} 无需进一步动作")
        except HandoffError as exc:
            self._advance.update(done=True, error=f"{exc.code}: {exc.message}")
        except Exception as exc:  # noqa: BLE001 - 后台线程不能崩
            self._advance.update(done=True, error=f"{type(exc).__name__}: {exc}")
        finally:
            self._verify_mutex.release()

    def _advance_merge(self, task_id: str, pr_number, steps: list[str]) -> bool:
        """复用 worker 的自动合并子流程；返回是否真的推进到 merged。"""
        from ..worker import Worker, WorkerOptions

        task = self._store(push=True).get(task_id)
        repo = self.config.repo(task["target"]["repo"])
        options = WorkerOptions(
            worker_id=self.config.panel.worker_id,
            auto_merge=True,
            wait_deployment=False,  # 部署对齐 + 验收统一交给 cmd_deliver
            merge_timeout_seconds=300,
        )
        worker = Worker(
            self.root,
            self.config,
            options,
            store=self._shared_store,
            host=self._host,
            logger=lambda msg: steps.append(str(msg)[:200]),
        )
        worker._try_auto_merge(task_id, repo)
        return self._store(push=True).get(task_id)["status"] == "merged"

    def _advance_push(self, task: dict, steps: list[str]) -> tuple[str, str]:
        """目录模式：把工作目录里已提交但没推上去的改动补推到当前分支。"""
        worktree = _clean_path((task.get("result") or {}).get("worktree"))
        path = Path(worktree) if worktree else None
        if path is None or not path.is_dir() or not git(path, "rev-parse", "--git-dir").ok:
            raise ExecutionError("任务没有可用的工作目录（result.worktree），无法重新推送")
        branch = git_out(path, "rev-parse", "--abbrev-ref", "HEAD")
        if not branch or branch == "HEAD":
            raise ExecutionError("工作目录处于 detached HEAD，无法确定推送目标分支")
        sha = git_out(path, "rev-parse", "HEAD")
        pushed = git(path, "push", "origin", f"HEAD:{branch}", timeout=120)
        if not pushed.ok:
            raise ExecutionError(f"重新推送失败：{str(pushed.stderr or '')[:300]}")
        steps.append(f"git push origin HEAD:{branch} 成功（{sha[:12]}）")
        return branch, sha or ""

    # -- 重新验收 ------------------------------------------------------------
    def reverify_task(self, task_id: str, reason: str = "面板：重新验收") -> dict:
        """重新验收：清除上一次验收结果回到 deployed，随即重跑部署对齐 + checks。

        与恢复推进共用 `_advance` 槽位和 `_verify_mutex`（所以进度也走同一个
        state.advance 读数）；差别只在入口先过 `store.request_reverify`，让
        deployed/verified/failed(verification)/failed(交付已部署) 都能在**不重跑
        执行器、不加 attempt** 的情况下重新验收。
        """
        from ..state import assert_reverify_transition

        try:
            current = self._store(push=True).peek(task_id)
        except (HandoffError, AttributeError):
            current = None
        if isinstance(current, dict):
            # 同步挡掉明显不合法的状态，别让按钮点了只回一句"已启动"再默默失败
            assert_reverify_transition(current, task_id)
        with self._lock:
            if self._advance and not self._advance.get("done"):
                raise ConflictError(f"已有恢复推进在进行: {self._advance.get('task_id')}")
            if not self._verify_mutex.acquire(blocking=False):
                raise ConflictError("已有部署/验收在进行（含自动验收），请等它结束")
            self._advance = {
                "task_id": task_id,
                "done": False,
                "started_at": iso(),
                "steps": [],
                "result": None,
                "message": None,
                "error": None,
            }
        threading.Thread(
            target=self._reverify_task, args=(task_id, reason), name=f"panel-reverify-{task_id}", daemon=True
        ).start()
        return dict(self._advance)

    def _reverify_task(self, task_id: str, reason: str) -> None:
        from ..cli import cmd_deliver

        store = self._store(push=True)
        steps: list[str] = self._advance["steps"] if self._advance else []
        try:
            task = store.get(task_id)
            updated = store.request_reverify(
                task_id,
                expected_revision=task["revision"],
                reason=reason,
                actor=self.PANEL_ACTOR,
            )
            steps.append(f"重新验收：已清除验收结果回到 deployed（r{updated['revision']}）")
            args = SimpleNamespace(task_id=task_id, no_push=False)
            with contextlib.redirect_stderr(io.StringIO()):
                outcome = cmd_deliver(args, self.config)
            steps.append(f"验收：{outcome.get('result', 'unknown')}")
            self._advance.update(
                done=True,
                result=outcome.get("result"),
                message=outcome.get("message"),
                deliver=outcome,
            )
        except HandoffError as exc:
            self._advance.update(done=True, error=f"{exc.code}: {exc.message}")
        except Exception as exc:  # noqa: BLE001 - 后台线程不能崩
            self._advance.update(done=True, error=f"{type(exc).__name__}: {exc}")
        finally:
            self._verify_mutex.release()

    # -- 生命周期 ------------------------------------------------------------
    def shutdown(self) -> None:
        self._auto_stop.set()
        self._wake.set()
        with self._lock:
            active = self._active
        if active is not None and active.phase == "running":
            active.cancel_event.set()
            if active.thread is not None:
                active.thread.join(timeout=15)
        self._stop_wake_server()
