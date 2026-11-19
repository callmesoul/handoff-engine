"""执行器会话枚举：面板「接着哪个会话处理」的数据源。

三个来源：
    - 任务历史：task.result 与事件时间线里的 codex.completed 记录（attempt 逐条）；
    - codex 全局：~/.codex/session_index.jsonl（id/标题/时间），cwd 从会话文件首行补；
    - zcode 全局：~/.zcode/cli/db/db.sqlite 的 session 表（只读连接）。

只做只读查询；任何解析失败都降级为空列表，不让面板崩。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..sanitize import clean

CODEX_HOME = Path.home() / ".codex"
ZCODE_DB = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"


@dataclass
class SessionInfo:
    executor: str  # codex | zcode
    id: str
    title: str = ""
    cwd: str | None = None
    updated_at: str | None = None
    source: str = "global"  # task | global
    task_id: str | None = None
    attempt: int | None = None
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "executor": self.executor,
            "id": self.id,
            "title": clean(self.title, 200),
            "cwd": self.cwd,
            "updated_at": self.updated_at,
            "source": self.source,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "note": self.note,
        }


# -- 任务历史 ---------------------------------------------------------------


def _log_sessions(root, task_id: str) -> list[SessionInfo]:
    """从面板自己的执行日志里捞会话 id。

    失败的 attempt 不会写 codex.completed 事件，result 里也没 thread_id，
    只靠事件表的话「上次那轮」就彻底消失了——恰恰是失败后最想接着跑的那轮。
    日志首行是 {"type":"thread.started","thread_id":...}，直接拿。
    """
    out: list[SessionInfo] = []
    if root is None:
        return out
    base = Path(root) / ".handoff-run" / "logs" / task_id
    if not base.is_dir():
        return out
    for attempt_dir in base.glob("attempt-*"):
        match = re.search(r"attempt-(\d+)$", attempt_dir.name)
        if not match:
            continue
        attempt = int(match.group(1))
        for log in sorted(attempt_dir.glob("*.jsonl")):
            try:
                with open(log, "r", encoding="utf-8", errors="replace") as fh:
                    first = json.loads(fh.readline() or "{}")
            except (OSError, json.JSONDecodeError):
                continue
            if first.get("type") != "thread.started":
                continue
            thread_id = str(first.get("thread_id") or "").strip()
            if not thread_id:
                continue
            executor = "zcode" if log.name.startswith("zcode") else "codex"
            out.append(
                SessionInfo(
                    executor=executor,
                    id=thread_id,
                    title=f"attempt-{attempt}",
                    source="log",
                    task_id=task_id,
                    attempt=attempt,
                    updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(log.stat().st_mtime)),
                )
            )
    return out


def task_sessions(task: dict, events: list[dict] | None = None, root=None) -> list[SessionInfo]:
    """任务跑过的所有会话（按 attempt 倒序），面板里置顶展示。"""
    out: dict[str, SessionInfo] = {}
    for event in events or []:
        if event.get("type") != "codex.completed":
            continue
        meta = event.get("metadata") or {}
        session = meta.get("run_id")
        if not session:
            continue
        executor = meta.get("executor") or "codex"
        out.setdefault(
            f"{executor}:{session}",
            SessionInfo(
                executor=executor,
                id=str(session),
                title=f"attempt-{event.get('attempt', '?')} 完成",
                source="task",
                task_id=task.get("id"),
                attempt=event.get("attempt"),
                cwd=task.get("result", {}).get("worktree") if event.get("attempt") == task.get("attempt") else None,
            ),
        )
    # 日志里捞出来的会话（含失败的 attempt），事件表里没有的补上
    for info in _log_sessions(root, str(task.get("id") or "")):
        out.setdefault(f"{info.executor}:{info.id}", info)
    result = task.get("result") or {}
    executor = result.get("executor") or ("codex" if result.get("codex_thread_id") else None)
    session = result.get("executor_session_id") or result.get("codex_thread_id")
    if executor and session:
        info = out.setdefault(
            f"{executor}:{session}",
            SessionInfo(executor=executor, id=str(session), source="task", task_id=task.get("id")),
        )
        info.title = f"attempt-{task.get('attempt', '?')}（最新）"
        info.attempt = task.get("attempt")
        info.cwd = result.get("worktree")
    ordered = sorted(out.values(), key=lambda s: (s.attempt or 0), reverse=True)
    return ordered


# -- codex 全局 -------------------------------------------------------------


def codex_sessions(limit: int = 50) -> list[SessionInfo]:
    index = CODEX_HOME / "session_index.jsonl"
    rows: list[SessionInfo] = []
    if not index.is_file():
        return rows
    try:
        lines = index.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return rows
    seen: set[str] = set()
    for line in reversed(lines):  # 新的在后
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = obj.get("id")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        rows.append(
            SessionInfo(
                executor="codex",
                id=str(sid),
                title=str(obj.get("thread_name") or ""),
                updated_at=obj.get("updated_at"),
            )
        )
        if len(rows) >= limit:
            break
    return rows


def codex_session_cwd(session_id: str) -> str | None:
    """从明文会话文件首行的 session_meta 读 cwd。.zst 存档不读（无 zstandard）。"""
    sessions_dir = CODEX_HOME / "sessions"
    if not sessions_dir.is_dir():
        return None
    matches = [
        p
        for p in sessions_dir.rglob(f"*{session_id}*")
        if p.suffix == ".jsonl"
    ]
    if not matches:
        return None
    try:
        with open(matches[0], "r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
        meta = json.loads(first)
        payload = meta.get("payload") or {}
        cwd = payload.get("cwd")
        return str(cwd) if cwd else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


# -- zcode 全局 -------------------------------------------------------------


def zcode_sessions(limit: int = 50) -> list[SessionInfo]:
    if not ZCODE_DB.is_file():
        return []
    uri = ZCODE_DB.as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        query = "SELECT id, title, directory, time_created, time_updated FROM session"
        try:
            rows = conn.execute(query + " WHERE parent_id IS NULL ORDER BY time_updated DESC LIMIT ?", (limit,)).fetchall()
        except sqlite3.Error:
            rows = conn.execute(query + " ORDER BY time_updated DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out: list[SessionInfo] = []
    for row in rows:
        out.append(
            SessionInfo(
                executor="zcode",
                id=str(row["id"] or ""),
                title=str(row["title"] or ""),
                cwd=str(row["directory"]) if row["directory"] else None,
                updated_at=_normalize_ts(row["time_updated"]),
            )
        )
    return [s for s in out if s.id]


def _normalize_ts(value) -> str | None:
    """zcode db 里的时间可能是毫秒整数或 ISO 字符串，统一成可展示字符串。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000.0 if abs(float(value)) > 1e12 else float(value)
        try:
            from datetime import datetime, timezone

            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return str(value)
    return str(value)
