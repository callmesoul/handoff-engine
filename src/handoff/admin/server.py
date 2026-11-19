"""后台查询 API 与最小页面。

约束：
    - 只读查询走索引；写操作必须走 TaskStore，产生合法状态转换和审计事件；
    - 页面不渲染未清洗的 Markdown/HTML，全部 HTML 转义；
    - 索引延迟要显式展示（索引 commit vs 仓库 HEAD）。
"""

from __future__ import annotations

import base64
import html
import json
import sqlite3
from contextlib import closing
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from ..config import Config
from ..errors import HandoffError, SchemaError
from ..sanitize import clean
from ..store import Actor, TaskStore
from ..timeutil import iso, now
from .auth import AuthError, Principal, authenticate


class ApiError(HandoffError):
    code = "api_error"
    exit_code = 12


class Handler(BaseHTTPRequestHandler):
    config: Config
    server_version = "handoff-admin/0.1"

    # -- 路由 --------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        try:
            principal = self._principal()
            parts = urlsplit(self.path)
            path = parts.path.rstrip("/") or "/"
            query = parse_qs(parts.query)
            if path == "/":
                self._send_html(self._dashboard(principal, query))
                return
            if path == "/api/tasks":
                self._send_json(self._list_tasks(principal, query))
                return
            if path.startswith("/api/tasks/"):
                segments = [unquote(s) for s in path.split("/") if s][2:]
                self._task_route(principal, segments, query)
                return
            if path == "/api/workers":
                self._send_json({"workers": self._workers()})
                return
            if path == "/api/metrics/summary":
                self._send_json(self._metrics())
                return
            raise ApiError(f"未找到路由: {path}")
        except AuthError as exc:
            self._send_json({"error": exc.message}, HTTPStatus.UNAUTHORIZED)
        except HandoffError as exc:
            self._send_json({"error": exc.message, "code": exc.code}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        try:
            principal = self._principal()
            if self.config.admin.read_only:
                raise ApiError("后台处于只读模式")
            if not principal.can("operate"):
                raise AuthError(f"{principal.id} 没有操作权限")
            parts = urlsplit(self.path)
            segments = [unquote(s) for s in parts.path.split("/") if s]
            if len(segments) < 5 or segments[:2] != ["api", "tasks"] or segments[3] != "actions":
                raise ApiError(f"未找到路由: {parts.path}")
            task_id, action = segments[2], segments[4]
            payload = self._read_json()
            self._send_json(self._action(principal, task_id, action, payload))
        except AuthError as exc:
            self._send_json({"error": exc.message}, HTTPStatus.FORBIDDEN)
        except HandoffError as exc:
            status = HTTPStatus.CONFLICT if exc.code == "revision_conflict" else HTTPStatus.BAD_REQUEST
            self._send_json({"error": exc.message, "code": exc.code}, status)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    # -- 基础 --------------------------------------------------------------
    def _principal(self) -> Principal:
        header = self.headers.get("Authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else self.headers.get("X-API-Token")
        if not token:
            query = parse_qs(urlsplit(self.path).query)
            token = (query.get("token") or [None])[0]
        return authenticate(self.config, token)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ApiError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ApiError("请求体必须是 JSON 对象")
        return data

    def _send_json(self, payload, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, text: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = text.encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = text.encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        print(f"[admin] {self.address_string()} {fmt % args}", flush=True)

    # -- 查询 --------------------------------------------------------------
    def _db(self) -> sqlite3.Connection:
        from .indexer import Indexer

        return Indexer(self.config).connect()

    def _list_tasks(self, principal: Principal, query: dict) -> dict:
        limit = min(int((query.get("limit") or ["50"])[0]), 200)
        cursor = (query.get("cursor") or [None])[0]
        where = ["1=1"]
        params: list = []
        for field, column in (
            ("status", "status"),
            ("target_repo", "target_repo"),
            ("worker", "worker_id"),
            ("failure_stage", "failure_stage"),
        ):
            values = query.get(field) or []
            if values:
                placeholders = ",".join("?" for _ in values)
                where.append(f"{column} IN ({placeholders})")
                params.extend(values)
        if (query.get("has_migration") or [None])[0] in ("true", "1"):
            where.append("migration_required = 1")
        if (query.get("q") or [None])[0]:
            where.append("title LIKE ?")
            params.append(f"%{query['q'][0]}%")
        if (query.get("since") or [None])[0]:
            where.append("updated_at >= ?")
            params.append(query["since"][0])
        if (query.get("until") or [None])[0]:
            where.append("updated_at <= ?")
            params.append(query["until"][0])
        repos = [r for r in principal.repos if r != "*"]
        if repos:
            placeholders = ",".join("?" for _ in repos)
            where.append(f"(target_repo IN ({placeholders}) OR source_repo IN ({placeholders}))")
            params.extend(repos * 2)
        if cursor:
            updated, task_id = _decode_cursor(cursor)
            where.append("(updated_at < ? OR (updated_at = ? AND id < ?))")
            params.extend([updated, updated, task_id])
        sql = (
            "SELECT id, revision, status, attempt, title, source_repo, target_repo, worker_id, failure_stage, "
            "pr_url, merge_sha, observed_sha, migration_required, created_at, updated_at FROM tasks "
            f"WHERE {' AND '.join(where)} ORDER BY updated_at DESC, id DESC LIMIT ?"
        )
        params.append(limit + 1)
        with closing(self._db()) as conn:
            rows = conn.execute(sql, params).fetchall()
            head = self._repo_head()
            indexed = conn.execute("SELECT value FROM meta WHERE key='last_indexed_commit'").fetchone()
        items = [dict(row) for row in rows[:limit]]
        next_cursor = _encode_cursor(items[-1]) if len(rows) > limit and items else None
        index_head = indexed["value"] if indexed else None
        return {
            "items": items,
            "next_cursor": next_cursor,
            "index": {
                "last_indexed_commit": index_head,
                "repo_head": head,
                "stale": bool(index_head and head and index_head != head),
                "generated_at": iso(),
            },
        }

    def _task_route(self, principal: Principal, segments: list[str], query: dict) -> None:
        if not segments:
            raise ApiError("缺少任务 id")
        task_id = segments[0]
        with closing(self._db()) as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ApiError(f"任务不存在: {task_id}")
        if not principal.sees(row["target_repo"]) and not principal.sees(row["source_repo"]):
            raise AuthError("无权访问该仓库的任务")
        task = json.loads(row["task_json"])

        if len(segments) == 1:
            self._send_json({"task": task, "indexed_commit": row["commit_sha"]})
            return
        section = segments[1]
        if section == "events":
            self._send_json({"task_id": task_id, "events": self._events(task_id)})
        elif section == "attempts":
            self._send_json({"task_id": task_id, "attempts": self._attempts(task_id)})
        elif section == "checks":
            self._send_json({"task_id": task_id, "checks": task["result"].get("checks") or []})
        elif section == "logs":
            if not principal.can("operate"):
                raise AuthError("原始日志需要 operator 以上权限")
            if len(segments) < 3:
                raise ApiError("缺少 log-id")
            self._send_text(self._read_log(task_id, "/".join(segments[2:])))
        else:
            raise ApiError(f"未知子资源: {section}")

    def _events(self, task_id: str, limit: int = 500) -> list[dict]:
        with closing(self._db()) as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE task_id=? ORDER BY occurred_at ASC, event_id ASC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            out.append(item)
        return out

    def _attempts(self, task_id: str) -> list[dict]:
        events = self._events(task_id, limit=2000)
        attempts: dict[int, dict] = {}
        for event in events:
            bucket = attempts.setdefault(
                event["attempt"],
                {"attempt": event["attempt"], "started_at": None, "ended_at": None, "events": 0, "result": None},
            )
            bucket["events"] += 1
            bucket["started_at"] = bucket["started_at"] or event["occurred_at"]
            bucket["ended_at"] = event["occurred_at"]
            if event["type"] in ("codex.completed", "codex.failed", "codex.timed_out", "task.status_changed"):
                bucket["result"] = event["to_status"] or bucket["result"]
        return [attempts[k] for k in sorted(attempts)]

    def _read_log(self, task_id: str, log_id: str) -> str:
        base = (self.config.root / ".handoff-run" / "logs").resolve()
        target = (base / task_id / log_id).resolve()
        if not str(target).startswith(str(base)) or not target.is_file():
            raise ApiError(f"日志不存在: {log_id}")
        text = target.read_text(encoding="utf-8", errors="replace")
        return clean(text, 20000)

    def _workers(self) -> list[dict]:
        with closing(self._db()) as conn:
            rows = conn.execute(
                """
                SELECT actor_id AS worker_id, MAX(occurred_at) AS last_event_at, COUNT(*) AS events
                FROM events WHERE actor_kind='worker' GROUP BY actor_id
                """
            ).fetchall()
            current = conn.execute(
                "SELECT worker_id, id, status, updated_at FROM tasks WHERE worker_id IS NOT NULL "
                "AND status IN ('claimed','in_progress')"
            ).fetchall()
        active = {row["worker_id"]: dict(row) for row in current}
        out = []
        for row in rows:
            item = dict(row)
            item["current_task"] = active.get(item["worker_id"])
            out.append(item)
        return out

    def _metrics(self) -> dict:
        with closing(self._db()) as conn:
            status_rows = conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
            failure_rows = conn.execute(
                "SELECT failure_stage, COUNT(*) AS n FROM tasks WHERE failure_stage IS NOT NULL GROUP BY failure_stage"
            ).fetchall()
            stuck = conn.execute(
                "SELECT id, status, updated_at, worker_id FROM tasks WHERE status IN ('claimed','in_progress') "
                "AND updated_at < ?",
                (iso(now()),),
            ).fetchall()
            quarantined = conn.execute("SELECT COUNT(*) AS n FROM quarantined_records").fetchone()["n"]
            verified_today = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE status='verified' AND updated_at >= ?",
                (iso(now())[:10] + "T00:00:00Z",),
            ).fetchone()["n"]
        return {
            "by_status": {row["status"]: row["n"] for row in status_rows},
            "failures_by_stage": {row["failure_stage"]: row["n"] for row in failure_rows},
            "stuck_tasks": [dict(row) for row in stuck],
            "quarantined_records": quarantined,
            "verified_today": verified_today,
        }

    def _repo_head(self) -> str | None:
        from ..gitcmd import git

        res = git(self.config.root, "rev-parse", self.config.store.branch)
        return res.stdout.strip() if res.ok else None

    # -- 写操作 ------------------------------------------------------------
    def _action(self, principal: Principal, task_id: str, action: str, payload: dict) -> dict:
        key = payload.get("idempotency_key")
        reason = (payload.get("reason") or "").strip()
        expected_revision = payload.get("expected_revision")
        if not isinstance(expected_revision, int):
            raise ApiError("必须提供 expected_revision（整数），否则拒绝写入")
        if not reason:
            raise ApiError("必须提供操作理由 reason")
        if not key:
            raise ApiError("必须提供 idempotency_key")

        with closing(self._db()) as conn:
            seen = conn.execute("SELECT revision FROM idempotency WHERE key=?", (key,)).fetchone()
            if seen:
                return {"id": task_id, "replayed": True, "revision": seen["revision"]}

        store = TaskStore(self.config.root, self.config, actor=Actor("user", principal.id))
        task = store.get(task_id)
        if not principal.sees(task["target"]["repo"]):
            raise AuthError("无权操作该仓库的任务")

        if action == "retry":
            if task["status"] != "failed":
                raise SchemaError(f"只有 failed 任务可以重试（当前 {task['status']}）")
            updated = store.transition(
                task_id,
                "open",
                expected_revision=expected_revision,
                summary=f"重试：{reason}",
                actor=Actor("user", principal.id),
                event_type="admin.retry_requested",
                mutate=lambda t: (
                    t.__setitem__("attempt", int(t.get("attempt", 0)) + 1),
                    t.__setitem__("claim", None),
                ),
            )
        elif action == "cancel":
            updated = store.transition(
                task_id,
                "cancelled",
                expected_revision=expected_revision,
                summary=f"取消：{reason}",
                actor=Actor("user", principal.id),
                event_type="admin.cancel_requested",
                mutate=lambda t: t.__setitem__("claim", None),
            )
        elif action == "reverify":
            updated = store.request_reverify(
                task_id,
                expected_revision=expected_revision,
                reason=reason,
                actor=Actor("user", principal.id),
            )
        elif action == "release-lease":
            if principal.role == "viewer":
                raise AuthError("释放租约需要 operator 以上权限")
            updated = store.release_lease(task_id, actor=Actor("user", principal.id), reason=reason)
        else:
            raise ApiError(f"未知操作: {action}")

        with closing(self._db()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO idempotency(key, task_id, action, revision, created_at) VALUES(?,?,?,?,?)",
                (key, task_id, action, updated["revision"], iso()),
            )
            conn.commit()
        return {
            "id": task_id,
            "action": action,
            "revision": updated["revision"],
            "status": updated["status"],
            "commit": updated.get("_commit"),
        }

    # -- 页面 --------------------------------------------------------------
    def _dashboard(self, principal: Principal, query: dict) -> str:
        try:
            data = self._list_tasks(principal, query)
            metrics = self._metrics()
            error = None
        except Exception as exc:  # noqa: BLE001
            data, metrics, error = {"items": [], "index": {}}, {}, f"{type(exc).__name__}: {exc}"

        rows = []
        for item in data["items"]:
            rows.append(
                "<tr>"
                f"<td><code>{html.escape(item['id'])}</code></td>"
                f"<td>{html.escape(item['status'])}</td>"
                f"<td>{html.escape(item['title'] or '')}</td>"
                f"<td>{html.escape(item['target_repo'] or '')}</td>"
                f"<td>{html.escape(item['worker_id'] or '')}</td>"
                f"<td>{html.escape(item['failure_stage'] or '')}</td>"
                f"<td>{html.escape(item['updated_at'] or '')}</td>"
                "</tr>"
            )
        index = data.get("index") or {}
        stale_banner = ""
        if index.get("stale"):
            stale_banner = (
                '<p class="warn">索引落后：索引 commit '
                f"{html.escape(str(index.get('last_indexed_commit'))[:12])}，仓库 HEAD "
                f"{html.escape(str(index.get('repo_head'))[:12])}。数据可能延迟。</p>"
            )
        warn = f'<p class="warn">{html.escape(error)}</p>' if error else ""
        status_chips = " ".join(
            f"<span class='chip'>{html.escape(k)}: {v}</span>" for k, v in (metrics.get("by_status") or {}).items()
        )
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Handoff 任务台</title>
<style>
:root {{ color-scheme: dark; }}
body {{ margin:0; padding:24px; background:#111318; color:#e6e8ee;
  font: 14px/1.6 -apple-system, "Segoe UI", "Noto Sans SC", sans-serif; }}
h1 {{ font-size:18px; margin:0 0 4px; }}
.sub {{ color:#8b93a7; margin:0 0 18px; font-size:12px; }}
.chip {{ display:inline-block; padding:2px 8px; border-radius:999px; background:#1d2230;
  border:1px solid #2b3242; margin-right:6px; font-size:12px; }}
table {{ width:100%; border-collapse:collapse; margin-top:16px; }}
th, td {{ text-align:left; padding:8px 10px; border-bottom:1px solid #232936; }}
th {{ color:#8b93a7; font-weight:500; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size:12px; color:#9cc4ff; }}
.warn {{ background:#3a2417; border:1px solid #7a4a2a; padding:10px 12px; border-radius:8px; color:#ffd7b0; }}
form {{ margin-top:12px; }}
input {{ background:#161a23; border:1px solid #2b3242; color:#e6e8ee; padding:6px 10px; border-radius:6px; }}
button {{ background:#22406b; border:1px solid #2f5b96; color:#dce8ff; padding:6px 12px; border-radius:6px; }}
</style></head><body>
<h1>Handoff 任务台</h1>
<p class="sub">身份 {html.escape(principal.id)} · 角色 {html.escape(principal.role)} · 索引 {html.escape(str(index.get('generated_at') or '未建索引'))}</p>
{stale_banner}{warn}
<div>{status_chips}</div>
<form method="get" action="/">
  <input type="hidden" name="token" value="{html.escape((query.get('token') or [''])[0])}">
  <input name="status" placeholder="status" value="{html.escape((query.get('status') or [''])[0])}">
  <input name="target_repo" placeholder="target_repo" value="{html.escape((query.get('target_repo') or [''])[0])}">
  <input name="q" placeholder="标题关键字" value="{html.escape((query.get('q') or [''])[0])}">
  <button type="submit">筛选</button>
</form>
<table>
<thead><tr><th>ID</th><th>状态</th><th>标题</th><th>目标仓库</th><th>worker</th><th>失败阶段</th><th>更新时间</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan="7">没有记录</td></tr>'}</tbody>
</table>
<p class="sub">原始日志、prompt 与请求正文不在此页展示。管理动作走 POST /api/tasks/:id/actions/*。</p>
</body></html>"""


def _encode_cursor(item: dict) -> str:
    raw = f"{item['updated_at']}|{item['id']}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        updated, task_id = raw.split("|", 1)
        return updated, task_id
    except Exception as exc:  # noqa: BLE001
        raise ApiError(f"非法游标: {cursor}") from exc


def serve(config: Config, *, host: str | None = None, port: int | None = None) -> None:
    handler = type("BoundHandler", (Handler,), {"config": config})
    address = (host or config.admin.host, port or config.admin.port)
    httpd = ThreadingHTTPServer(address, handler)
    print(f"handoff admin 监听 http://{address[0]}:{address[1]}（只读={config.admin.read_only}）", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
