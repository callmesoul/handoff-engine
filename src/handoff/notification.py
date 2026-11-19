"""局域网任务事件：持久化 outbox、HMAC webhook、幂等 inbox。

通知只携带任务定位信息，任务内容仍以 Git 交接仓为准。task.created 唤醒
后端 worker；task.completed 由前端恢复原 Codex 会话并拉取交接仓。
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from . import ids
from .config import NotificationConfig
from .errors import ConfigError, ExecutionError, SchemaError
from . import secrets as secret_files
from .timeutil import iso

WAKE_PATH = "/v1/handoff/wake"
COMPLETE_PATH = "/v1/handoff/completed"
MAX_BODY_BYTES = 16 * 1024
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TASK_RE = re.compile(r"^ho-[0-9]{8}-[0-9A-HJKMNP-TV-Z]{26}$")


def task_created_payload(
    *,
    task_id: str,
    revision: int,
    store_commit: str,
    reply_url: str | None = None,
) -> dict:
    payload = {
        "schema_version": 1,
        "event_id": ids.ulid(),
        "type": "task.created",
        "task_id": task_id,
        "revision": revision,
        "store_commit": store_commit,
        "occurred_at": iso(),
    }
    if reply_url:
        payload["reply_url"] = reply_url
    return payload


def task_completed_payload(*, task: dict, store_commit: str) -> dict:
    return {
        "schema_version": 1,
        "event_id": ids.ulid(),
        "type": "task.completed",
        "task_id": task["id"],
        "revision": task["revision"],
        "store_commit": store_commit,
        "occurred_at": iso(),
        "status": task["status"],
        "observed_sha": (task.get("deployment") or {}).get("observed_sha"),
        "source_repo": (task.get("source") or {}).get("repo"),
    }


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def load_secret(config: NotificationConfig) -> bytes:
    value = os.environ.get(config.secret_env, "")
    secret = value.encode("utf-8")
    file_env = f"{config.secret_env}_FILE"
    secret_file = os.environ.get(file_env, "")
    if not secret and secret_file:
        try:
            # systemd credentials are files. Strip only the line ending commonly
            # introduced while provisioning a text secret; preserve all other bytes.
            secret = Path(secret_file).read_bytes().rstrip(b"\r\n")
        except OSError as exc:
            raise ConfigError(f"无法读取 {file_env} 指定的通知密钥文件: {exc}") from exc
    if not secret:
        # 没有环境变量时，回退到 config/local.yaml 里 secrets.*_file 指定的文件。
        # agent 拿不到面板进程的系统环境，统一从配置文件路径取。
        configured = secret_files.config_secret(getattr(config, "_app_config", None) or config, config.secret_env)
        secret = configured.encode("utf-8")
    if len(secret) < 32:
        raise ConfigError(f"{config.secret_env} 或 {file_env} 指定的密钥缺失或少于 32 字节")
    return secret


def _signature(secret: bytes, timestamp: str, body: bytes) -> str:
    digest = hmac.new(secret, timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _validate_payload(payload: dict) -> None:
    if not isinstance(payload, dict):
        raise SchemaError("通知正文必须是 JSON 对象")
    base = {"schema_version", "event_id", "type", "task_id", "revision", "store_commit", "occurred_at"}
    event_type = payload.get("type")
    if event_type == "task.created":
        expected = base | {"reply_url"}
    elif event_type == "task.completed":
        expected = base | {"status", "observed_sha", "source_repo"}
    else:
        raise SchemaError("通知 type 只允许 task.created/task.completed")
    unknown = set(payload) - expected
    if unknown:
        raise SchemaError(f"通知包含未知字段: {', '.join(sorted(unknown))}")
    if payload.get("schema_version") != 1:
        raise SchemaError("通知 schema_version 必须为 1")
    if not ids.is_ulid(payload.get("event_id")):
        raise SchemaError("通知 event_id 不是合法 ULID")
    if not _TASK_RE.fullmatch(str(payload.get("task_id", ""))):
        raise SchemaError("通知 task_id 格式非法")
    revision = payload.get("revision")
    if type(revision) is not int or revision < 1:
        raise SchemaError("通知 revision 必须为正整数")
    if not _SHA_RE.fullmatch(str(payload.get("store_commit", ""))):
        raise SchemaError("通知 store_commit 必须是完整小写 SHA")
    if not isinstance(payload.get("occurred_at"), str) or not payload["occurred_at"]:
        raise SchemaError("通知 occurred_at 缺失")
    if event_type == "task.created" and payload.get("reply_url"):
        _validate_event_url(payload["reply_url"], COMPLETE_PATH, "reply_url")
    if event_type == "task.completed":
        if payload.get("status") not in ("verified", "failed"):
            raise SchemaError("task.completed status 只允许 verified/failed")
        observed = payload.get("observed_sha")
        if observed is not None and not _SHA_RE.fullmatch(str(observed)):
            raise SchemaError("task.completed observed_sha 不是完整 SHA/null")
        repo = payload.get("source_repo")
        if not isinstance(repo, str) or "/" not in repo or len(repo) > 255:
            raise SchemaError("task.completed source_repo 非法")


def _validate_event_url(url: str, expected_path: str, field: str = "url") -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ConfigError(f"{field} 必须是无账号密码的 http/https URL")
    if parts.path != expected_path or parts.query or parts.fragment:
        raise ConfigError(f"{field} 路径必须精确为 {expected_path}")


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


class Outbox:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        with _connect(self.path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox (
                    event_id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT
                )
                """
            )

    def enqueue(self, payload: dict, url: str) -> bool:
        _validate_payload(payload)
        now = time.time()
        with _connect(self.path) as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO outbox
                    (event_id, url, payload, next_attempt_at, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (payload["event_id"], url, _json_bytes(payload).decode("utf-8"), now, iso()),
            )
        return cur.rowcount == 1

    def due(self, *, limit: int = 20, event_id: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM outbox WHERE delivered_at IS NULL AND next_attempt_at <= ?"
        params: list[object] = [time.time()]
        if event_id:
            sql += " AND event_id = ?"
            params.append(event_id)
        sql += " ORDER BY next_attempt_at, created_at LIMIT ?"
        params.append(limit)
        with _connect(self.path) as conn:
            return list(conn.execute(sql, params))

    def delivered(self, event_id: str) -> None:
        with _connect(self.path) as conn:
            conn.execute(
                "UPDATE outbox SET delivered_at=?, last_error=NULL WHERE event_id=?",
                (iso(), event_id),
            )

    def failed(self, event_id: str, *, error: str, delay: float) -> None:
        with _connect(self.path) as conn:
            conn.execute(
                """
                UPDATE outbox
                SET attempts=attempts+1, next_attempt_at=?, last_error=?
                WHERE event_id=? AND delivered_at IS NULL
                """,
                (time.time() + delay, error[:1000], event_id),
            )

    def status(self, event_id: str) -> dict | None:
        with _connect(self.path) as conn:
            row = conn.execute("SELECT * FROM outbox WHERE event_id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def last_payload(self, task_id: str, event_type: str) -> dict | None:
        """找该任务最近一次某类通知的原始正文与目标 URL。

        重发必须复用当年真正发出去的 revision / store_commit / status：
        前端恢复会话时会核对这些值，现场重新拼很容易拼出对不上的内容。
        """
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT payload, url FROM outbox ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                continue
            if payload.get("task_id") == task_id and payload.get("type") == event_type:
                return {"payload": payload, "url": str(row["url"])}
        return None

    def pending_count(self) -> int:
        with _connect(self.path) as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM outbox WHERE delivered_at IS NULL").fetchone()
        return int(row["n"])

    def delete_tasks(self, task_ids: list[str]) -> int:
        """删除指定任务的本地唤醒记录；Git 任务删除后不能再补发旧通知。"""
        wanted = set(task_ids)
        if not wanted:
            return 0
        with _connect(self.path) as conn:
            rows = conn.execute("SELECT event_id, payload FROM outbox").fetchall()
            event_ids = []
            for row in rows:
                try:
                    payload = json.loads(row["payload"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if payload.get("task_id") in wanted:
                    event_ids.append((row["event_id"],))
            if event_ids:
                conn.executemany("DELETE FROM outbox WHERE event_id=?", event_ids)
        return len(event_ids)


class Notifier:
    def __init__(self, root: str | Path, config: NotificationConfig):
        self.config = config
        self.outbox = Outbox(config.outbox_path(Path(root)))

    def enqueue(self, payload: dict, *, url: str | None = None) -> str:
        target = url or self.config.url
        if not target:
            raise ConfigError("通知目标 URL 未配置")
        expected_path = WAKE_PATH if payload.get("type") == "task.created" else COMPLETE_PATH
        _validate_event_url(target, expected_path, "通知目标 URL")
        self.outbox.enqueue(payload, target)
        return payload["event_id"]

    def send_due(self, *, limit: int = 20, event_id: str | None = None) -> dict:
        secret = load_secret(self.config)
        sent = 0
        failed = 0
        errors: list[dict] = []
        for row in self.outbox.due(limit=limit, event_id=event_id):
            try:
                self._send(row, secret)
                self.outbox.delivered(row["event_id"])
                sent += 1
            except ExecutionError as exc:
                attempts = int(row["attempts"])
                delay = min(
                    self.config.retry_initial_seconds * (2**attempts),
                    self.config.retry_max_seconds,
                )
                self.outbox.failed(row["event_id"], error=exc.message, delay=delay)
                failed += 1
                errors.append({"event_id": row["event_id"], "error": exc.message, "retry_in_seconds": delay})
        return {"sent": sent, "failed": failed, "pending": self.outbox.pending_count(), "errors": errors}

    def run_forever(self, *, interval_seconds: float = 1, logger=print) -> None:
        logger(f"notifier 启动，目标 {self.config.url}")
        while True:
            result = self.send_due()
            if result["sent"] or result["failed"]:
                logger(json.dumps(result, ensure_ascii=False))
            time.sleep(max(0.1, interval_seconds))

    def _send(self, row: sqlite3.Row, secret: bytes) -> None:
        body = row["payload"].encode("utf-8")
        timestamp = str(int(time.time()))
        request = Request(row["url"], data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("X-Handoff-Event-ID", row["event_id"])
        request.add_header("X-Handoff-Timestamp", timestamp)
        request.add_header("X-Handoff-Signature", _signature(secret, timestamp, body))
        # 通知目标是局域网直连地址，绝不能走环境变量里的 HTTP_PROXY：
        # 沙箱/终端注入的代理根本路由不到 192.168.x.x，会伪装成
        # "<urlopen error timed out>" 把真正的连通性问题烧成 5 分钟退避。
        opener = build_opener(ProxyHandler({}), _NoRedirect)
        try:
            with opener.open(request, timeout=self.config.request_timeout_seconds) as response:
                if response.status not in (200, 202):
                    raise ExecutionError(f"通知端返回 HTTP {response.status}")
        except HTTPError as exc:
            raw = exc.read(500).decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
            raise ExecutionError(f"通知端返回 HTTP {exc.code}: {raw}") from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise ExecutionError(f"通知发送失败: {exc}") from exc


class Inbox:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        with _connect(self.path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inbox (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    store_commit TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    client_ip TEXT NOT NULL,
                    received_at TEXT NOT NULL
                    ,event_type TEXT
                    ,reply_url TEXT
                )
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(inbox)")}
            if "event_type" not in columns:
                conn.execute("ALTER TABLE inbox ADD COLUMN event_type TEXT")
            if "reply_url" not in columns:
                conn.execute("ALTER TABLE inbox ADD COLUMN reply_url TEXT")

    def record(self, payload: dict, *, body: bytes, client_ip: str) -> bool:
        digest = hashlib.sha256(body).hexdigest()
        with _connect(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT payload_sha256 FROM inbox WHERE event_id=?", (payload["event_id"],)
            ).fetchone()
            if existing:
                if existing["payload_sha256"] != digest:
                    raise SchemaError("相同 event_id 的通知正文不一致")
                return False
            conn.execute(
                """
                INSERT INTO inbox
                    (event_id, task_id, revision, store_commit, payload_sha256, client_ip, received_at,
                     event_type, reply_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["event_id"],
                    payload["task_id"],
                    payload["revision"],
                    payload["store_commit"],
                    digest,
                    client_ip,
                    iso(),
                    payload["type"],
                    payload.get("reply_url"),
                ),
            )
        return True

    def count(self) -> int:
        with _connect(self.path) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0])

    def completion_url(self, task_id: str) -> str | None:
        with _connect(self.path) as conn:
            row = conn.execute(
                """
                SELECT reply_url FROM inbox
                WHERE task_id=? AND event_type='task.created' AND reply_url IS NOT NULL
                ORDER BY received_at DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        return str(row["reply_url"]) if row else None


@dataclass
class WakeServer:
    root: Path
    config: NotificationConfig
    wake_event: threading.Event | None = None
    on_event: Callable[[dict], None] | None = None

    def __post_init__(self):
        self._secret = load_secret(self.config)
        self._allowed = tuple(ipaddress.ip_network(item, strict=False) for item in self.config.allowed_cidrs)
        self.inbox = Inbox(self.config.inbox_path(self.root))
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self, host: str | None = None, port: int | None = None) -> tuple[str, int]:
        host = host if host is not None else self.config.listen_host
        port = port if port is not None else self.config.listen_port
        if not 0 <= port <= 65535:
            raise ConfigError("通知监听端口必须在 0..65535")
        if not _is_loopback_host(host) and not self._allowed:
            raise ConfigError("局域网监听必须配置 notifications.allowed_cidrs，禁止对所有来源开放")

        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path != "/healthz":
                    self._reply(404, {"ok": False, "error": "not_found"})
                    return
                self._reply(200, {"ok": True})

            def do_POST(self):  # noqa: N802
                if self.path not in (WAKE_PATH, COMPLETE_PATH):
                    self._reply(404, {"ok": False, "error": "not_found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "-1"))
                    if length < 0 or length > MAX_BODY_BYTES:
                        raise SchemaError("通知正文长度非法")
                    body = self.rfile.read(length)
                    payload, fresh = owner._accept(
                        body,
                        path=self.path,
                        event_id=self.headers.get("X-Handoff-Event-ID", ""),
                        timestamp=self.headers.get("X-Handoff-Timestamp", ""),
                        signature=self.headers.get("X-Handoff-Signature", ""),
                        client_ip=self.client_address[0],
                    )
                    if payload["type"] == "task.created" and owner.wake_event is not None:
                        owner.wake_event.set()
                    if owner.on_event is not None:
                        owner.on_event(payload)
                    self._reply(202 if fresh else 200, {"ok": True, "duplicate": not fresh, "task_id": payload["task_id"]})
                except (SchemaError, ExecutionError) as exc:
                    self._reply(401 if isinstance(exc, ExecutionError) else 400, {"ok": False, "error": exc.message})
                except (ValueError, UnicodeError, json.JSONDecodeError):
                    self._reply(400, {"ok": False, "error": "通知正文不是合法 JSON"})

            def _reply(self, status: int, payload: dict) -> None:
                raw = _json_bytes(payload)
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, _format, *_args):
                return

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="handoff-wake-listener", daemon=True)
        self._thread.start()
        actual_host, actual_port = self._server.server_address[:2]
        return str(actual_host), int(actual_port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _accept(
        self,
        body: bytes,
        *,
        path: str,
        event_id: str,
        timestamp: str,
        signature: str,
        client_ip: str,
    ) -> tuple[dict, bool]:
        self._check_source(client_ip)
        try:
            sent_at = int(timestamp)
        except ValueError as exc:
            raise ExecutionError("通知时间戳非法") from exc
        if abs(int(time.time()) - sent_at) > self.config.max_skew_seconds:
            raise ExecutionError("通知时间戳已过期")
        expected = _signature(self._secret, timestamp, body)
        if not hmac.compare_digest(signature, expected):
            raise ExecutionError("通知签名无效")
        payload = json.loads(body.decode("utf-8"))
        _validate_payload(payload)
        expected_path = WAKE_PATH if payload["type"] == "task.created" else COMPLETE_PATH
        if path != expected_path:
            raise SchemaError(f"{payload['type']} 必须发送到 {expected_path}")
        if event_id != payload["event_id"]:
            raise SchemaError("通知头与正文的 event_id 不一致")
        fresh = self.inbox.record(payload, body=body, client_ip=client_ip)
        return payload, fresh

    def _check_source(self, client_ip: str) -> None:
        if not self._allowed:
            return
        ip = ipaddress.ip_address(client_ip)
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not any(ip.version == network.version and ip in network for network in self._allowed):
            raise ExecutionError(f"来源地址 {ip} 不在允许网段")


def _is_loopback_host(host: str) -> bool:
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def emit_task_completed(root: str | Path, config: NotificationConfig, task: dict, store_commit: str) -> dict:
    """Git push 成功后入队完成事件；由后台 notifier 签名和发送。"""
    if task.get("status") not in ("verified", "failed"):
        return {"status": "disabled", "reason": "not_terminal"}
    target = config.completion_url
    if not target:
        target = Inbox(config.inbox_path(Path(root))).completion_url(task["id"])
    if not target:
        return {"status": "disabled", "reason": "no_completion_url"}
    payload = task_completed_payload(task=task, store_commit=store_commit)
    notifier = None
    try:
        notifier = Notifier(root, config)
        event_id = notifier.enqueue(payload, url=target)
        return {
            "status": "pending",
            "delivery": "background_notifier",
            "event_id": event_id,
            "pending": notifier.outbox.pending_count(),
        }
    except (ConfigError, ExecutionError, SchemaError) as exc:
        status = notifier.outbox.status(payload["event_id"]) if notifier else None
        return {
            "status": "pending" if status else "error",
            "event_id": payload["event_id"],
            "error": exc.message,
        }


def replay_task_completed(
    root: str | Path,
    config: NotificationConfig,
    task: dict,
    *,
    dry_run: bool = False,
    store_commit: str | None = None,
) -> dict:
    """手动再发一次 task.completed 到创建任务的前端监听端口。

    目标地址按优先级反查，绝不硬编码 IP：
      1. 该任务原完成通知里记录的 URL（前端 task.created 的 reply_url）
      2. inbox 里登记的 reply_url
      3. 本机 notifications.completion_url

    正文优先复用原通知，只换 event_id / occurred_at。前端 inbox 按 event_id
    幂等，换 ID 才会把它当新事件重新排回原 Codex 会话；revision、store_commit、
    status 保持原值，避免前端核对时判成漂移。

    `store_commit` 用于没有历史通知可复用时现场重建正文。任务的 `_commit` 只是
    写入时的内存回传字段、不落任务单，所以调用方应传交接仓 git 历史里定位到的
    落盘 commit（见 TaskStore.task_commit）。
    """
    task_id = task["id"]
    if task.get("status") not in ("verified", "failed"):
        raise SchemaError("只有 verified / failed 任务才能重发完成通知")

    notifier = Notifier(root, config)
    history = notifier.outbox.last_payload(task_id, "task.completed")
    if history:
        payload = dict(history["payload"])
        source = "outbox"
        target = history["url"]
    else:
        commit = str(task.get("_commit") or "") or str(store_commit or "")
        if not _SHA_RE.fullmatch(commit):
            raise SchemaError(
                f"任务 {task_id} 没有可复用的完成通知，也定位不到它的落盘 commit"
                "（交接仓历史里查不到该任务单）；请确认任务单已提交并推送后再重发"
            )
        payload = task_completed_payload(task=task, store_commit=commit)
        source = "task"
        target = ""

    target = (
        target
        or Inbox(config.inbox_path(Path(root))).completion_url(task_id)
        or config.completion_url
        or ""
    )
    if not target:
        raise ConfigError(
            f"找不到任务 {task_id} 的前端回程地址：该任务不是从前端监听端口创建的"
            "（没有 task.created 记录），也没有配置 notifications.completion_url"
        )
    _validate_event_url(target, COMPLETE_PATH, "前端完成通知地址")

    payload["event_id"] = ids.ulid()
    payload["occurred_at"] = iso()
    common = {
        "task_id": task_id,
        "target": target,
        "event_id": payload["event_id"],
        "revision": payload["revision"],
        "store_commit": payload["store_commit"],
        "payload_source": source,
    }
    if dry_run:
        return {"status": "dry_run", "delivered": False, **common}

    event_id = notifier.enqueue(payload, url=target)
    delivery = notifier.send_due(event_id=event_id)
    delivered = delivery["sent"] == 1 and delivery["failed"] == 0
    return {
        "status": "sent" if delivered else "failed",
        "delivered": delivered,
        **common,
        "delivery": delivery,
    }


def reconcile_completed(
    root: str | Path,
    config: NotificationConfig,
    tasks: list[dict],
    commit_for: Callable[[str], str],
    *,
    dry_run: bool = False,
) -> dict:
    """对账并补发「从未入队过」的 task.completed（完成通知自愈）。

    正常路径由 store._write 在进入 verified/failed 时入队；进程被硬杀、旧版本
    bug 或配置缺失都可能让完成事件静默漏掉（历史上就发生过一次）。这里靠
    outbox 反查：终态任务只要还没有任何 task.completed 记录（含仍在退避重试
    的 pending），就用 replay_task_completed 补发一次。

    已经有通知历史的任务一律跳过——即使它还在 pending 也由后台 notifier 按
    退避重试，绝不在对账里再造一个事件，避免重复唤醒前端会话。
    """
    notifier = Notifier(root, config)
    scanned = 0
    missing: list[str] = []
    skipped_with_history = 0
    replayed: list[dict] = []
    failures: list[dict] = []

    for task in tasks:
        if task.get("status") not in ("verified", "failed"):
            continue
        scanned += 1
        task_id = task["id"]
        if notifier.outbox.last_payload(task_id, "task.completed"):
            skipped_with_history += 1
            continue
        missing.append(task_id)
        try:
            result = replay_task_completed(
                root,
                config,
                task,
                dry_run=dry_run,
                store_commit=commit_for(task_id),
            )
        except (ConfigError, ExecutionError, SchemaError) as exc:
            failures.append({"task_id": task_id, "error": exc.message})
            continue
        replayed.append({"task_id": task_id, **result})

    return {
        "scanned": scanned,
        "missing": missing,
        "skipped_with_history": skipped_with_history,
        "replayed": replayed,
        "failures": failures,
        "dry_run": dry_run,
    }
