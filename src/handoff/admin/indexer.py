"""把 Git 里的任务快照和不可变事件索引成可查询的 SQLite。

按 commit 顺序消费，非法记录隔离开，不阻断整个索引。
支持全量 rebuild，重建后任务数/revision/事件数必须一致。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .. import schema
from ..config import Config
from ..gitcmd import git, git_out
from ..sanitize import clean, redact_obj
from ..timeutil import iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    title TEXT,
    source_repo TEXT,
    target_repo TEXT,
    target_base_ref TEXT,
    worker_id TEXT,
    failure_stage TEXT,
    pr_url TEXT,
    merge_sha TEXT,
    observed_sha TEXT,
    migration_required INTEGER,
    created_at TEXT,
    updated_at TEXT,
    task_json TEXT,
    commit_sha TEXT,
    path TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at, id);
CREATE INDEX IF NOT EXISTS idx_tasks_target ON tasks(target_repo);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    task_revision INTEGER,
    attempt INTEGER,
    type TEXT,
    actor_kind TEXT,
    actor_id TEXT,
    occurred_at TEXT,
    from_status TEXT,
    to_status TEXT,
    summary TEXT,
    metadata_json TEXT,
    commit_sha TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, occurred_at);
CREATE TABLE IF NOT EXISTS quarantined_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT,
    path TEXT,
    commit_sha TEXT,
    errors TEXT,
    raw TEXT,
    recorded_at TEXT
);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    task_id TEXT,
    action TEXT,
    revision INTEGER,
    created_at TEXT
);
"""


class Indexer:
    def __init__(self, config: Config):
        self.config = config
        self.root: Path = Path(config.root)
        self.db_path: Path = config.admin.database_path(self.root)

    # -- 连接 --------------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        return conn

    # -- 入口 --------------------------------------------------------------
    def index_incremental(self) -> dict:
        with closing(self.connect()) as conn:
            head = self._head()
            last = self._meta(conn, "last_indexed_commit")
            if last:
                ancestor = git(self.root, "merge-base", "--is-ancestor", last, head)
                if not ancestor.ok:
                    return self._run(conn, rebuild=True, reason="索引游标不在当前分支历史上")
            return self._run(conn, rebuild=False)

    def rebuild(self) -> dict:
        with closing(self.connect()) as conn:
            conn.executescript("DELETE FROM tasks; DELETE FROM events; DELETE FROM quarantined_records;")
            conn.execute("DELETE FROM meta WHERE key='last_indexed_commit'")
            conn.commit()
            return self._run(conn, rebuild=True)

    # -- 内部 --------------------------------------------------------------
    def _head(self) -> str:
        head = git_out(self.root, "rev-parse", self.config.store.branch)
        return head

    @staticmethod
    def _meta(conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    @staticmethod
    def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def _run(self, conn: sqlite3.Connection, *, rebuild: bool, reason: str | None = None) -> dict:
        head = self._head()
        last = None if rebuild else self._meta(conn, "last_indexed_commit")
        rev_args = ["rev-list", "--reverse", head] if not last else ["rev-list", "--reverse", f"{last}..{head}"]
        commits = [c for c in git_out(self.root, *rev_args).splitlines() if c]
        parsed_tasks: set[str] = set()
        parsed_events: set[str] = set()
        quarantined = 0
        for commit in commits:
            paths = self._changed_paths(commit)
            for status, path in paths:
                kind = self._kind(path)
                if kind is None:
                    continue
                if status == "D":
                    self._delete_row(conn, kind, path)
                    continue
                blob = self._read_blob(commit, path)
                if blob is None:
                    continue
                try:
                    payload = json.loads(blob)
                except json.JSONDecodeError as exc:
                    self._quarantine(conn, kind, path, commit, [f"JSON 解析失败: {exc}"], blob)
                    quarantined += 1
                    continue
                errors = schema.errors_for(kind, payload)
                if errors:
                    self._quarantine(conn, kind, path, commit, errors, blob)
                    quarantined += 1
                    continue
                if kind == "task":
                    self._upsert_task(conn, payload, commit, path)
                    parsed_tasks.add(payload["id"])
                else:
                    self._upsert_event(conn, payload, commit)
                    parsed_events.add(payload["event_id"])
        self._set_meta(conn, "last_indexed_commit", head)
        self._set_meta(conn, "last_indexed_at", iso())
        conn.commit()
        return {
            "head": head,
            "commits": len(commits),
            "tasks": len(parsed_tasks),
            "events": len(parsed_events),
            "quarantined": quarantined,
            "rebuild": rebuild,
            "reason": reason,
        }

    @staticmethod
    def _kind(path: str) -> str | None:
        if path.startswith("tasks/") and path.endswith(".json"):
            return "task"
        if path.startswith("events/") and path.endswith(".json"):
            return "event"
        return None

    def _changed_paths(self, commit: str) -> list[tuple[str, str]]:
        raw = git(self.root, "diff-tree", "--no-commit-id", "--root", "--name-status", "-r", "-z", commit).stdout
        out: list[tuple[str, str]] = []
        parts = raw.split("\0")
        i = 0
        while i < len(parts):
            status = parts[i].strip()
            if not status:
                i += 1
                continue
            if status.startswith("R") or status.startswith("C"):
                if i + 2 >= len(parts):
                    break
                out.append((status[0], parts[i + 2]))
                i += 3
            else:
                if i + 1 >= len(parts):
                    break
                out.append((status[0], parts[i + 1]))
                i += 2
        return out

    def _read_blob(self, commit: str, path: str) -> str | None:
        res = git(self.root, "show", f"{commit}:{path}")
        return res.stdout if res.ok else None

    def _delete_row(self, conn: sqlite3.Connection, kind: str, path: str) -> None:
        if kind == "task":
            conn.execute("DELETE FROM tasks WHERE path=?", (path,))
        else:
            conn.execute("DELETE FROM events WHERE event_id=?", (path.rsplit("/", 1)[-1][:-5],))

    def _upsert_task(self, conn: sqlite3.Connection, task: dict, commit: str, path: str) -> None:
        conn.execute(
            """
            INSERT INTO tasks(id, revision, status, attempt, title, source_repo, target_repo, target_base_ref,
                              worker_id, failure_stage, pr_url, merge_sha, observed_sha, migration_required,
                              created_at, updated_at, task_json, commit_sha, path)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                revision=excluded.revision, status=excluded.status, attempt=excluded.attempt,
                title=excluded.title, source_repo=excluded.source_repo, target_repo=excluded.target_repo,
                target_base_ref=excluded.target_base_ref, worker_id=excluded.worker_id,
                failure_stage=excluded.failure_stage, pr_url=excluded.pr_url, merge_sha=excluded.merge_sha,
                observed_sha=excluded.observed_sha, migration_required=excluded.migration_required,
                created_at=excluded.created_at, updated_at=excluded.updated_at,
                task_json=excluded.task_json, commit_sha=excluded.commit_sha, path=excluded.path
            """,
            (
                task["id"],
                task["revision"],
                task["status"],
                task["attempt"],
                task["title"],
                task["source"]["repo"],
                task["target"]["repo"],
                task["target"]["base_ref"],
                (task.get("claim") or {}).get("worker_id"),
                (task.get("failure") or {}).get("stage"),
                task["result"].get("pr_url"),
                task["result"].get("merge_sha"),
                task["deployment"].get("observed_sha"),
                1 if task["migration"].get("required") else 0,
                task["created_at"],
                task["updated_at"],
                json.dumps(task, ensure_ascii=False),
                commit,
                path,
            ),
        )

    def _upsert_event(self, conn: sqlite3.Connection, event: dict, commit: str) -> None:
        conn.execute(
            """
            INSERT INTO events(event_id, task_id, task_revision, attempt, type, actor_kind, actor_id,
                               occurred_at, from_status, to_status, summary, metadata_json, commit_sha)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_id) DO UPDATE SET
                task_revision=excluded.task_revision, type=excluded.type, summary=excluded.summary,
                metadata_json=excluded.metadata_json, commit_sha=excluded.commit_sha
            """,
            (
                event["event_id"],
                event["task_id"],
                event["task_revision"],
                event["attempt"],
                event["type"],
                event["actor"]["kind"],
                event["actor"]["id"],
                event["occurred_at"],
                event.get("from_status"),
                event.get("to_status"),
                event["summary"],
                json.dumps(redact_obj(event.get("metadata") or {}), ensure_ascii=False),
                commit,
            ),
        )

    def _quarantine(self, conn: sqlite3.Connection, kind: str, path: str, commit: str, errors: list[str], raw: str) -> None:
        conn.execute(
            "INSERT INTO quarantined_records(kind, path, commit_sha, errors, raw, recorded_at) VALUES(?,?,?,?,?,?)",
            (kind, path, commit, clean("\n".join(errors), 2000), clean(raw, 4000), iso()),
        )
