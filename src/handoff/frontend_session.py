"""前端会话登记与完成通知调度。

thread id、工作区和远程 app-server 地址只保存在前端本机 SQLite，不写入 Git
任务仓。完成事件只携带任务定位信息；真正开始下一轮前先由原会话拉取任务仓。

前端有两种：Codex 用 `codex queue --thread` 把消息排回原会话；DSH 没有等价
命令，改由本机 `dsh-handoff-bridge` 插件在宿主进程里调 `sessionController.prompt()`。
两者在 sessions 表的 executor 列区分。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from . import executil
from .config import NotificationConfig
from .dsh_bridge import DshBridge, discover_bridge, request_id_for
from .errors import ConfigError, ExecutionError, HandoffError, SchemaError
from .gitcmd import Result, run
from .sanitize import clean
from .timeutil import iso

_AUTH_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 前端执行器。Codex 走 `codex queue`，DSH 走本机桥接插件。
EXECUTOR_CODEX = "codex"
EXECUTOR_DSH = "dsh"
FRONTEND_EXECUTORS = (EXECUTOR_CODEX, EXECUTOR_DSH)

#: 老库没有这些列，打开时补上。
_SESSIONS_COLUMNS = {"executor": "TEXT NOT NULL DEFAULT 'codex'"}
_COMPLETIONS_COLUMNS = {"last_stderr": "TEXT"}

# `codex queue` 连接 Desktop app-server 所需的非敏感环境。systemd user service
# 不会继承 Desktop 启动后的动态 named pipe，因此派发时从同用户活动进程刷新。
_CODEX_DESKTOP_ENV_KEYS = (
    "CODEX_HOME",
    "CODEX_SQLITE_HOME",
    "CODEX_APP_TOOLS_PIPE_PATH",
    "CODEX_VERSION",
    "CODEX_CI",
    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
)
_CODEX_DESKTOP_MARKERS = ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_TOOL_CALL_ID")
_PROC_ENV_LIMIT = 1024 * 1024


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """给已有库补列；历史会话按 codex 处理。"""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    for column, ddl in _SESSIONS_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} {ddl}")  # noqa: S608
    existing = {row[1] for row in conn.execute("PRAGMA table_info(completions)")}
    for column, ddl in _COMPLETIONS_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE completions ADD COLUMN {column} {ddl}")  # noqa: S608


def _selected_codex_env(source: dict[str, str]) -> dict[str, str]:
    return {key: source[key] for key in _CODEX_DESKTOP_ENV_KEYS if source.get(key)}


def _read_proc_codex_env(path: Path) -> dict[str, str]:
    """只从 /proc environ 读取白名单字段和候选标记，不接触 token/secret。"""
    try:
        raw = path.read_bytes()
    except (OSError, PermissionError):
        return {}
    if len(raw) > _PROC_ENV_LIMIT:
        return {}
    wanted = {*_CODEX_DESKTOP_ENV_KEYS, *_CODEX_DESKTOP_MARKERS}
    result: dict[str, str] = {}
    for item in raw.split(b"\0"):
        key, separator, value = item.partition(b"=")
        if not separator:
            continue
        name = key.decode("ascii", errors="ignore")
        if name in wanted:
            result[name] = value.decode("utf-8", errors="replace")
    return result


def discover_codex_desktop_env(
    *,
    environ: dict[str, str] | None = None,
    proc_root: str | Path = "/proc",
    current_pid: int | None = None,
) -> dict[str, str]:
    """发现当前 Codex Desktop 连接环境。

    user systemd 的环境可能带着上一次 Desktop 的旧 pipe，也可能完全没有 Codex
    变量。活动 agent 进程会同时带 pipe 与 thread/session 标记，优先级高于常驻
    listener；找不到活动进程时回退到调用方已有环境，让队列继续按退避重试。
    """
    base = dict(os.environ if environ is None else environ)
    selected = _selected_codex_env(base)
    best = selected
    best_score = (
        sum(bool(base.get(key)) for key in _CODEX_DESKTOP_MARKERS),
        int(base.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE") == "Codex Desktop"),
        -1,
    )
    root = Path(proc_root)
    own_pid = os.getpid() if current_pid is None else current_pid
    try:
        entries = list(root.iterdir())
    except OSError:
        return selected
    getuid = getattr(os, "getuid", None)
    uid = getuid() if getuid else None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == own_pid:
            continue
        if uid is not None:
            try:
                if entry.stat().st_uid != uid:
                    continue
            except OSError:
                continue
        candidate = _read_proc_codex_env(entry / "environ")
        if not candidate.get("CODEX_APP_TOOLS_PIPE_PATH") or not candidate.get("CODEX_HOME"):
            continue
        score = (
            sum(bool(candidate.get(key)) for key in _CODEX_DESKTOP_MARKERS),
            int(candidate.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE") == "Codex Desktop"),
            pid,
        )
        if score > best_score:
            best = _selected_codex_env(candidate)
            best_score = score
    return {**selected, **best}


def session_executor(row: sqlite3.Row) -> str:
    """取登记时记录的执行器；缺列的旧行按 codex 算。"""
    try:
        value = row["executor"]
    except (IndexError, KeyError):
        return EXECUTOR_CODEX
    return (value or EXECUTOR_CODEX).strip() or EXECUTOR_CODEX


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


class FrontendSessions:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        with _connect(self.path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    task_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    executor TEXT NOT NULL DEFAULT 'codex',
                    queue_remote TEXT,
                    queue_remote_auth_token_env TEXT,
                    registered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS completions (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL,
                    last_error TEXT,
                    last_stderr TEXT,
                    created_at TEXT NOT NULL,
                    dispatched_at TEXT
                );
                CREATE INDEX IF NOT EXISTS completions_due
                    ON completions(dispatched_at, next_attempt_at);
                """
            )
            _ensure_columns(conn)

    def register(
        self,
        task_id: str,
        *,
        thread_id: str,
        workspace: str | Path,
        executor: str = EXECUTOR_CODEX,
        queue_remote: str | None = None,
        queue_remote_auth_token_env: str | None = None,
    ) -> None:
        thread_id = thread_id.strip()
        if not thread_id or len(thread_id) > 200 or "\n" in thread_id or "\r" in thread_id:
            raise SchemaError("前端 thread id 非法")
        executor = (executor or EXECUTOR_CODEX).strip()
        if executor not in FRONTEND_EXECUTORS:
            raise ConfigError(f"前端执行器只能是 {' / '.join(FRONTEND_EXECUTORS)}")
        root = Path(workspace).expanduser().resolve()
        if not root.is_dir():
            raise SchemaError(f"前端工作区不存在: {root}")
        if queue_remote:
            parts = urlsplit(queue_remote)
            if parts.scheme not in ("ws", "wss", "unix") or parts.username or parts.password:
                raise ConfigError("queue_remote 必须是无账号密码的 ws://、wss:// 或 unix:// 地址")
        if queue_remote_auth_token_env:
            if not queue_remote or not _AUTH_ENV_RE.fullmatch(queue_remote_auth_token_env):
                raise ConfigError("queue_remote_auth_token_env 非法或缺少 queue_remote")
        if executor == EXECUTOR_DSH:
            # DSH 的会话 id 形如 session-<uuid>；远程 app-server 那套参数对它没有意义。
            queue_remote = None
            queue_remote_auth_token_env = None
        stamp = iso()
        with _connect(self.path) as conn:
            conn.execute(
                """
                INSERT INTO sessions(
                    task_id, thread_id, workspace, executor, queue_remote,
                    queue_remote_auth_token_env, registered_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET
                    thread_id=excluded.thread_id,
                    workspace=excluded.workspace,
                    executor=excluded.executor,
                    queue_remote=excluded.queue_remote,
                    queue_remote_auth_token_env=excluded.queue_remote_auth_token_env,
                    updated_at=excluded.updated_at
                """,
                (
                    task_id,
                    thread_id,
                    str(root),
                    executor,
                    queue_remote,
                    queue_remote_auth_token_env,
                    stamp,
                    stamp,
                ),
            )

    def session(self, task_id: str) -> sqlite3.Row | None:
        with _connect(self.path) as conn:
            return conn.execute("SELECT * FROM sessions WHERE task_id=?", (task_id,)).fetchone()

    def registered(self, *, limit: int = 1000) -> list[sqlite3.Row]:
        """所有已登记的前端会话快照（面板任务列表的「前端执行 Agent」列用）。"""
        with _connect(self.path) as conn:
            return list(
                conn.execute(
                    "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?",
                    (max(1, int(limit)),),
                )
            )

    def enqueue_completion(self, payload: dict) -> bool:
        if payload.get("type") != "task.completed":
            raise SchemaError("前端会话队列只接受 task.completed")
        now = time.time()
        with _connect(self.path) as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO completions(
                    event_id, task_id, payload, next_attempt_at, created_at
                ) VALUES(?,?,?,?,?)
                """,
                (
                    payload["event_id"],
                    payload["task_id"],
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    now,
                    iso(),
                ),
            )
            return cur.rowcount == 1

    def due(self, limit: int = 20) -> list[sqlite3.Row]:
        with _connect(self.path) as conn:
            return list(
                conn.execute(
                    """
                    SELECT * FROM completions
                    WHERE dispatched_at IS NULL AND next_attempt_at <= ?
                    ORDER BY created_at, event_id LIMIT ?
                    """,
                    (time.time(), limit),
                )
            )

    def dispatched(self, event_id: str) -> None:
        with _connect(self.path) as conn:
            conn.execute(
                "UPDATE completions SET dispatched_at=?, last_error=NULL, last_stderr=NULL WHERE event_id=?",
                (iso(), event_id),
            )

    def failed(self, event_id: str, *, error: str, stderr: str = "", delay: float) -> None:
        with _connect(self.path) as conn:
            conn.execute(
                """
                UPDATE completions
                SET attempts=attempts+1, last_error=?, last_stderr=?, next_attempt_at=?
                WHERE event_id=?
                """,
                (
                    clean(error, 1000),
                    clean(stderr, 2000) or None,
                    time.time() + max(0.0, delay),
                    event_id,
                ),
            )

    def pending_count(self) -> int:
        with _connect(self.path) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM completions WHERE dispatched_at IS NULL").fetchone()[0])

    def completion_history(
        self,
        *,
        task_id: str | None = None,
        dispatch_state: str = "all",
        limit: int = 50,
    ) -> dict:
        """查询 task.completed 的本地持久化及派发历史。"""
        states = {"all", "pending", "failed", "dispatched"}
        if dispatch_state not in states:
            raise SchemaError(f"未知 completion 派发状态: {dispatch_state}")
        if limit < 1:
            raise SchemaError("completion 历史查询 limit 必须大于 0")

        base_where: list[str] = []
        base_params: list[object] = []
        if task_id:
            base_where.append("task_id = ?")
            base_params.append(task_id)

        state_where = {
            "all": None,
            "pending": "dispatched_at IS NULL AND last_error IS NULL",
            "failed": "dispatched_at IS NULL AND last_error IS NOT NULL",
            "dispatched": "dispatched_at IS NOT NULL",
        }[dispatch_state]
        record_where = [*base_where]
        if state_where:
            record_where.append(state_where)

        def where_sql(parts: list[str]) -> str:
            return " WHERE " + " AND ".join(parts) if parts else ""

        with _connect(self.path) as conn:
            summary = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN dispatched_at IS NULL AND last_error IS NULL THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN dispatched_at IS NULL AND last_error IS NOT NULL THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN dispatched_at IS NOT NULL THEN 1 ELSE 0 END) AS dispatched
                FROM completions
                """
                + where_sql(base_where),
                base_params,
            ).fetchone()
            matched = conn.execute(
                "SELECT COUNT(*) AS n FROM completions" + where_sql(record_where),
                base_params,
            ).fetchone()["n"]
            rows = conn.execute(
                "SELECT * FROM completions"
                + where_sql(record_where)
                + " ORDER BY created_at DESC, event_id DESC LIMIT ?",
                [*base_params, limit],
            ).fetchall()

        records = []
        for row in rows:
            payload = json.loads(row["payload"])
            state = (
                "dispatched"
                if row["dispatched_at"] is not None
                else "failed"
                if row["last_error"] is not None
                else "pending"
            )
            records.append(
                {
                    "event_id": row["event_id"],
                    "task_id": row["task_id"],
                    "task_status": payload.get("status"),
                    "revision": payload.get("revision"),
                    "store_commit": payload.get("store_commit"),
                    "observed_sha": payload.get("observed_sha"),
                    "source_repo": payload.get("source_repo"),
                    "occurred_at": payload.get("occurred_at"),
                    "dispatch_state": state,
                    "queued_at": row["created_at"],
                    "dispatched_at": row["dispatched_at"],
                    "attempts": row["attempts"],
                    "next_attempt_at": row["next_attempt_at"],
                    "last_error": row["last_error"],
                    "last_stderr": row["last_stderr"],
                }
            )

        return {
            "database": str(self.path.resolve()),
            "filters": {"task_id": task_id, "dispatch_state": dispatch_state, "limit": limit},
            "summary": {
                "total": int(summary["total"] or 0),
                "pending": int(summary["pending"] or 0),
                "failed": int(summary["failed"] or 0),
                "dispatched": int(summary["dispatched"] or 0),
            },
            "matched": int(matched),
            "returned": len(records),
            "records": records,
        }

    def delete_tasks(self, task_ids: list[str]) -> int:
        if not task_ids:
            return 0
        placeholders = ",".join("?" for _ in task_ids)
        with _connect(self.path) as conn:
            completions = conn.execute(
                f"DELETE FROM completions WHERE task_id IN ({placeholders})", task_ids  # noqa: S608
            ).rowcount
            sessions = conn.execute(
                f"DELETE FROM sessions WHERE task_id IN ({placeholders})", task_ids  # noqa: S608
            ).rowcount
        return int(completions) + int(sessions)


class FrontendDispatcher:
    def __init__(
        self,
        root: str | Path,
        config: NotificationConfig,
        *,
        codex_binary: str = "codex",
        runner: Callable[..., Result] = run,
        bridge: DshBridge | None = None,
        codex_env_resolver: Callable[[], dict[str, str]] = discover_codex_desktop_env,
        bridge_resolver: Callable[[], DshBridge] | None = None,
    ):
        self.root = Path(root).resolve()
        self.config = config
        self.codex_binary = codex_binary
        self.sessions = FrontendSessions(config.session_path(self.root))
        self.runner = runner
        self._bridge = bridge
        self.codex_env_resolver = codex_env_resolver
        # 只有显式注入 bridge 对象时才视为不可变。注入 resolver（测试用）仍参与
        # 端点重解析——它只是替换「造实例」的方式，不改变 URL 变化即重建的策略。
        self._bridge_manageable = bridge is None
        self._bridge_resolver = bridge_resolver
        self._bridge_url: str | None = None

    def _resolve_bridge(self) -> DshBridge:
        return DshBridge.from_config(
            url=self.config.dsh_bridge_url,
            token_env=self.config.dsh_bridge_token_env,
            timeout=self.config.request_timeout_seconds,
        )

    def _resolve(self) -> DshBridge:
        return self._resolve_bridge() if self._bridge_resolver is None else self._bridge_resolver()

    def _discovered_url(self) -> str | None:
        """当前应使用的桥接 URL；解析不出来时返回 None，沿用已缓存实例。"""
        try:
            return discover_bridge(self.config.dsh_bridge_url).url
        except HandoffError:
            return None

    def bridge(self) -> DshBridge:
        """DSH 桥接；每次投递都按端点文件重解析，端口漂移时自动重建。

        listener 是长驻进程，DSH 重启会把桥接端口换掉并刷新
        `$DSH_HOME/handoff-bridge.json`。如果这里的实例进程级缓存，重启后
        仍会往旧端口投递直到人工重启 listener。这里在取用前重新发现地址，
        URL 变化就重建实例：端点文件读取是本地小 I/O，对 interval 驱动的
        listener 可忽略；URL 没变时复用缓存（令牌、HttpClient 连接不改）。
        显式注入的 bridge 对象（测试/特殊部署）视为不可变，不做重解析。
        """
        if self._bridge is None:
            self._bridge = self._resolve()
            self._bridge_url = self._discovered_url()
            return self._bridge
        if not self._bridge_manageable:
            return self._bridge
        current = self._discovered_url()
        if current is not None and current != self._bridge_url:
            self._bridge = self._resolve()
            self._bridge_url = current
        return self._bridge

    def register(
        self,
        task_id: str,
        *,
        thread_id: str,
        workspace: str | Path,
        executor: str = EXECUTOR_CODEX,
    ) -> None:
        self.sessions.register(
            task_id,
            thread_id=thread_id,
            workspace=workspace,
            executor=executor,
            queue_remote=self.config.queue_remote,
            queue_remote_auth_token_env=self.config.queue_remote_auth_token_env,
        )

    def enqueue(self, payload: dict) -> bool:
        return self.sessions.enqueue_completion(payload)

    def send_due(self, *, limit: int = 20) -> dict:
        sent = 0
        failed = 0
        errors: list[dict] = []
        for row in self.sessions.due(limit):
            try:
                self._send(row)
                self.sessions.dispatched(row["event_id"])
                sent += 1
            except HandoffError as exc:
                attempts = int(row["attempts"])
                delay = min(
                    self.config.retry_initial_seconds * (2**attempts),
                    self.config.retry_max_seconds,
                )
                stderr = clean(
                    str(exc.context.get("stderr") or getattr(exc, "stderr", "") or ""),
                    2000,
                )
                self.sessions.failed(row["event_id"], error=exc.message, stderr=stderr, delay=delay)
                failed += 1
                detail = {"event_id": row["event_id"], "error": exc.message, "retry_in_seconds": delay}
                if stderr:
                    detail["stderr"] = stderr
                errors.append(detail)
        return {
            "sent": sent,
            "failed": failed,
            "pending": self.sessions.pending_count(),
            "errors": errors,
        }

    def _send(self, row: sqlite3.Row) -> None:
        payload = json.loads(row["payload"])
        session = self.sessions.session(payload["task_id"])
        if session is None:
            raise ExecutionError(f"任务 {payload['task_id']} 未登记原前端会话")
        if session_executor(session) == EXECUTOR_DSH:
            self._send_dsh(session, payload, row["event_id"])
            return
        self._send_codex(session, payload)

    def _send_dsh(self, session: sqlite3.Row, payload: dict, event_id: str) -> None:
        """DSH 走宿主插件：消息由 sessionController.prompt() 入队。

        连接类失败（超时/拒绝）时，端点文件可能刚刚因 DSH 重启换了端口。
        先丢弃缓存实例、强制重解析再投一次：requestId 稳定，插件按它查重，
        所以重投不会在 Codex 会话里产生重复消息。
        """
        request_id = request_id_for(event_id)
        text = self._message(payload)
        dsh_model = self.config.restore_models.dsh
        model_args = (
            {}
            if dsh_model is None
            else {"provider": dsh_model.provider, "model": dsh_model.model}
        )
        try:
            self.bridge().post(session["thread_id"], text, request_id=request_id, **model_args)
        except ExecutionError as exc:
            if not self._is_connection_error(exc):
                raise
            # 端点可能已漂移；强制重建后再试一次。
            self._bridge = None
            self.bridge().post(session["thread_id"], text, request_id=request_id, **model_args)

    @staticmethod
    def _is_connection_error(exc: ExecutionError) -> bool:
        """连接类失败（端点漂移的典型表现）才值得重建桥接重试。"""
        message = exc.message
        return any(
            token in message
            for token in ("请求失败", "timed out", "timeout", "Connection refused", "连接拒绝", "URLError")
        )

    def _send_codex(self, session: sqlite3.Row, payload: dict) -> None:
        codex = executil.require(self.codex_binary, env_var="HANDOFF_CODEX")
        message = self._message(payload)
        argv = [codex, "queue", "--thread", session["thread_id"], "--message", message]
        if self.config.restore_models.codex:
            argv.extend(["--model", self.config.restore_models.codex])
        if session["queue_remote"]:
            argv.extend(["--remote", session["queue_remote"]])
        if session["queue_remote_auth_token_env"]:
            env_name = session["queue_remote_auth_token_env"]
            if not os.environ.get(env_name):
                raise ConfigError(f"远程 Codex 认证环境变量 {env_name} 未设置")
            argv.extend(["--remote-auth-token-env", env_name])
        env = None if session["queue_remote"] else self.codex_env_resolver()
        result = self.runner(
            argv,
            cwd=session["workspace"],
            timeout=60,
            check_env=False,
            env=env,
        )
        if not result.ok:
            raise ExecutionError(
                "恢复原前端会话失败（会话可能已删除、归档或 app-server 不可达）",
                stderr=clean(result.stderr, 800),
            )

    def _message(self, payload: dict) -> str:
        task_id = payload["task_id"]
        status = payload["status"]
        revision = payload["revision"]
        commit = payload["store_commit"]
        if status == "verified":
            action = (
                "读取修订后的 contract 与 checks；确认状态仍为 verified 后，转入当前前端工作区的契约对齐和 dev 联调。"
                f"前端验收通过后，务必执行 handoffctl accept {task_id} --reason '前端验收通过'，"
                "把任务标记为 frontend_accepted（前端已验收）；也可在交接面板任务抽屉点「前端已验收」按钮。"
            )
        else:
            action = "读取 failure 与 checks，向用户原样报告阻塞；不要把部分通过说成已完成。"
        return (
            f"后端交接任务 {task_id} 已更新为 {status}。"
            f"请先通过 handoffctl show {task_id} --json 拉取交接仓 revision {revision}，"
            f"并核对 store commit {commit}。{action}"
        )
