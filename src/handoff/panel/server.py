"""面板 HTTP 服务：本机回环 API + 单文件前端页面。

约束：
    - 只绑 127.0.0.1（单人桌面工具，无鉴权，禁止改绑局域网地址）；
    - 前端页面全部内联转义渲染，不渲染未清洗的 Markdown/HTML；
    - 日志接口只读 .handoff-run/logs 下的文件，路径不允许逃逸。
"""

from __future__ import annotations

import json
import math
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from ..errors import ConflictError, HandoffError, TaskNotFound
from .service import PanelService

_WEB_DIR = Path(__file__).parent / "web"
_TASK_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)
_PANEL_ROLES = frozenset({"backend", "frontend"})
# The frontend panel is a strict allow-list: any newly added backend operation
# stays unreachable unless it is deliberately added here. The backend panel is
# the full operator console (a superset), so it keeps every route.
_FRONTEND_POST_ROUTES = frozenset({"/api/sync", "/api/tasks", "/api/deliver"})


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """浏览器刷新/切页会掐断在途轮询，这类断连是常态，不该刷一大片栈。"""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001
        exc = sys.exc_info()[1]
        if isinstance(exc, _DISCONNECT_ERRORS):
            return
        super().handle_error(request, client_address)


class PanelServer:
    def __init__(
        self,
        service: PanelService,
        *,
        host: str = "127.0.0.1",
        port: int = 8790,
        role: str | None = None,
    ):
        service_role = getattr(service, "role", "backend")
        effective_role = service_role if role is None else role
        if effective_role not in _PANEL_ROLES:
            raise HandoffError(f"未知面板角色: {effective_role!r}")
        if service_role != effective_role:
            raise HandoffError(
                f"面板服务角色不一致: service={service_role!r}, server={effective_role!r}"
            )
        self.service = service
        self.host = host
        self.port = port
        self.role = effective_role
        self._server: ThreadingHTTPServer | None = None

    def start(self) -> tuple[str, int]:
        if self.host not in ("127.0.0.1", "localhost", "::1"):
            raise HandoffError(f"面板只允许绑定本机回环地址，拒绝 {self.host!r}")
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "handoff-panel/0.1"

            def do_GET(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                path = parts.path
                try:
                    if path == "/" or path == "/index.html":
                        self._send_html(owner._index_html())
                        return
                    if path == "/api/state":
                        state = (
                            owner.service.frontend_state()
                            if owner.role == "frontend"
                            else owner.service.state()
                        )
                        self._send_json({"role": owner.role, **state})
                        return
                    if path == "/api/tasks":
                        query = parse_qs(parts.query)
                        status = query.get("status")
                        status = status[-1] if status else None
                        page = _positive_int(query, "page", default=1)
                        page_size = _positive_int(query, "page_size", default=20, cap=100)
                        self._require_loopback()
                        all_tasks = owner.service.list_tasks(status=status)
                        total = len(all_tasks)
                        pages = max(1, math.ceil(total / page_size))
                        page = min(page, pages)
                        start = (page - 1) * page_size
                        slice_ = all_tasks[start:start + page_size]
                        # 前端会话登记表只查一次，整页共用
                        frontend = owner.service.frontend_sessions()
                        briefs = [_brief(t, frontend) for t in slice_]
                        # tasks 字段保留（老调用方/新建任务后仍按名字取）；分页元数据另给
                        self._send_json({
                            "count": total,
                            "total": total,
                            "tasks": briefs,
                            "items": briefs,
                            "page": page,
                            "page_size": page_size,
                            "pages": pages,
                        })
                        return
                    if path.startswith("/api/tasks/") and path.endswith("/sessions"):
                        if owner.role == "frontend":
                            self._send_json(_role_forbidden(path), status=HTTPStatus.FORBIDDEN)
                            return
                        task_id = _task_id_from(path, suffix="/sessions")
                        executor = (parse_qs(parts.query).get("executor") or ["codex"])[-1]
                        self._send_json({"executor": executor, "sessions": owner.service.sessions(executor, task_id=task_id)})
                        return
                    if path.startswith("/api/sessions/"):
                        if owner.role == "frontend":
                            self._send_json(_role_forbidden(path), status=HTTPStatus.FORBIDDEN)
                            return
                        executor = _task_id_from(path, prefix="/api/sessions/")
                        query = parse_qs(parts.query)
                        task_id = (query.get("task") or [None])[-1]
                        self._send_json({"executor": executor, "sessions": owner.service.sessions(executor, task_id=task_id)})
                        return
                    if path.startswith("/api/tasks/"):
                        task_id = _task_id_from(path)
                        self._send_json(owner.service.get_task(task_id))
                        return
                    if path == "/api/logs":
                        if owner.role == "frontend":
                            self._send_json(_role_forbidden(path), status=HTTPStatus.FORBIDDEN)
                            return
                        query = parse_qs(parts.query)
                        task_id = (query.get("task") or [""])[-1]
                        _validate_task_id(task_id)
                        attempt_raw = (query.get("attempt") or [None])[-1]
                        attempt = int(attempt_raw) if attempt_raw and attempt_raw.isdigit() else None
                        self._send_json(owner.service.logs(task_id, attempt=attempt))
                        return
                    self._send_json({"ok": False, "error": f"未找到路由: {path}"}, status=404)
                except HandoffError as exc:
                    self._send_json({"ok": False, **exc.as_dict()}, status=_status_for(exc))
                except Exception as exc:  # noqa: BLE001 - 服务不能 500 崩溃
                    self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)

            def do_POST(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                path = parts.path
                try:
                    self._require_loopback()
                    if not _post_allowed(owner.role, path):
                        self._send_json(_role_forbidden(path, owner.role), status=HTTPStatus.FORBIDDEN)
                        return
                    payload = self._read_json_body()
                    if path == "/api/sync":
                        self._send_json({"ok": True, **owner.service.sync()})
                        return
                    if path == "/api/tasks":
                        self._send_json({"ok": True, **owner.service.create_task(payload)})
                        return
                    if path == "/api/mode":
                        self._send_json({"ok": True, **owner.service.set_mode(payload)})
                        return
                    if path == "/api/runs/cancel":
                        self._send_json({"ok": True, **owner.service.cancel()})
                        return
                    if path == "/api/runs/steer":
                        self._send_json({"ok": True, **owner.service.steer(str(payload.get("text") or ""))})
                        return
                    if path == "/api/runs/security":
                        self._send_json(
                            {
                                "ok": True,
                                **owner.service.set_security_mode(
                                    payload.get("codex_sandbox"),
                                    apply_now=bool(payload.get("apply_now")),
                                ),
                            }
                        )
                        return
                    if path == "/api/deliver":
                        task_id = str(payload.get("task_id") or "")
                        _validate_task_id(task_id)
                        self._send_json({"ok": True, **owner.service.deliver(task_id)})
                        return
                    if path.startswith("/api/tasks/") and path.endswith("/run"):
                        task_id = _task_id_from(path, suffix="/run")
                        self._send_json({"ok": True, **owner.service.run_task(task_id, **_run_kwargs(payload))})
                        return
                    if path.startswith("/api/tasks/") and path.endswith("/retry"):
                        task_id = _task_id_from(path, suffix="/retry")
                        self._send_json({"ok": True, **owner.service.retry(task_id)})
                        return
                    if path.startswith("/api/tasks/") and path.endswith("/resend-notify"):
                        task_id = _task_id_from(path, suffix="/resend-notify")
                        self._send_json({"ok": True, **owner.service.resend_completed(task_id)})
                        return
                    if path.startswith("/api/tasks/") and path.endswith("/release"):
                        task_id = _task_id_from(path, suffix="/release")
                        reason = str(payload.get("reason") or "面板释放租约")
                        self._send_json({"ok": True, **owner.service.release(task_id, reason)})
                        return
                    if path.startswith("/api/tasks/") and path.endswith("/accept"):
                        task_id = _task_id_from(path, suffix="/accept")
                        reason = str(payload.get("reason") or "面板：前端验收通过")
                        self._send_json({"ok": True, **owner.service.accept(task_id, reason)})
                        return
                    if path.startswith("/api/tasks/") and path.endswith("/advance"):
                        task_id = _task_id_from(path, suffix="/advance")
                        self._send_json({"ok": True, **owner.service.advance(task_id)})
                        return
                    if path.startswith("/api/tasks/") and path.endswith("/reverify"):
                        task_id = _task_id_from(path, suffix="/reverify")
                        reason = str(payload.get("reason") or "面板：重新验收")
                        self._send_json({"ok": True, **owner.service.reverify_task(task_id, reason)})
                        return
                    self._send_json({"ok": False, "error": f"未找到路由: {path}"}, status=404)
                except HandoffError as exc:
                    self._send_json({"ok": False, **exc.as_dict()}, status=_status_for(exc))
                except Exception as exc:  # noqa: BLE001
                    self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)

            # -- 基础设施 ------------------------------------------------------
            def _require_loopback(self) -> None:
                # 服务只绑了回环地址，这里做双保险
                ip = self.client_address[0]
                if ip not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                    raise HandoffError(f"拒绝非本机请求: {ip}")

            def _read_json_body(self) -> dict:
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/json":
                    raise HandoffError("面板 POST 只接受 application/json")
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length <= 0:
                    return {}
                if length > 1_000_000:
                    raise HandoffError("请求体过大")
                raw = self.rfile.read(length)
                try:
                    data = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeError):
                    raise HandoffError("请求体不是合法 JSON") from None
                return data if isinstance(data, dict) else {}

            def _send_json(self, payload: dict, status: int = 200) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    self.close_connection = True
                    return
                self._write(raw)

            def _send_html(self, html: str) -> None:
                raw = html.encode("utf-8")
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    self.close_connection = True
                    return
                self._write(raw)

            def _write(self, raw: bytes) -> None:
                """浏览器切页 / 刷新会掐掉轮询连接，这类断连是常态，不该刷栈。"""
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    self.close_connection = True

            def log_message(self, _format: str, *_args):
                return

            def handle_one_request(self) -> None:
                try:
                    super().handle_one_request()
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    self.close_connection = True

        self._server = _QuietThreadingHTTPServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        actual_host, actual_port = self._server.server_address[:2]
        threading.Thread(target=self._server.serve_forever, name="handoff-panel-http", daemon=True).start()
        return str(actual_host), int(actual_port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # -- 辅助 ----------------------------------------------------------------
    def _index_html(self) -> str:
        filename = "frontend.html" if self.role == "frontend" else "index.html"
        return (_WEB_DIR / filename).read_text(encoding="utf-8")


def _post_allowed(role: str, path: str) -> bool:
    """Frontend uses a strict allow-list; backend keeps the full operator console."""
    if role != "frontend":
        return True
    if path in _FRONTEND_POST_ROUTES:
        return True
    return path.startswith("/api/tasks/") and path.endswith("/accept")


def _role_forbidden(path: str, role: str | None = None) -> dict:
    return {
        "ok": False,
        "error": f"前端面板不提供后端操作: {path}",
        "code": "panel_role_forbidden",
    }


def _positive_int(query: dict, key: str, *, default: int, cap: int | None = None) -> int:
    """从 query string 读正整数；非法值回退默认，超过 cap 截断。"""
    raw = (query.get(key) or [None])[-1]
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value < 1:
        return default
    return min(value, cap) if cap is not None else value


def _status_for(exc: HandoffError) -> int:
    if isinstance(exc, ConflictError):
        return HTTPStatus.CONFLICT
    if isinstance(exc, TaskNotFound):
        return HTTPStatus.NOT_FOUND
    return HTTPStatus.BAD_REQUEST if getattr(exc, "exit_code", 1) in (2, 3) else HTTPStatus.INTERNAL_SERVER_ERROR


def _task_id_from(path: str, *, prefix: str = "/api/tasks/", suffix: str = "") -> str:
    body = path[len(prefix):len(path) - len(suffix)] if suffix else path[len(prefix):]
    task_id = unquote(body).strip("/")
    _validate_task_id(task_id)
    return task_id


def _validate_task_id(task_id: str) -> None:
    if not task_id or not set(task_id) <= _TASK_ID_CHARS:
        raise HandoffError(f"非法任务 ID: {task_id!r}")


def _run_kwargs(payload: dict) -> dict:
    executor = str(payload.get("executor") or "")
    if not executor:
        raise HandoffError("executor 不能为空")
    resume = payload.get("resume_session_id")
    permission = payload.get("permission_mode")
    sandbox = payload.get("codex_sandbox")
    auto_merge = payload.get("auto_merge")
    wait_deployment = payload.get("wait_deployment")
    auto_commit = payload.get("auto_commit")
    work_dir = str(payload.get("work_dir") or "").strip() or None
    model = str(payload.get("model") or "").strip() or None
    effort = str(payload.get("effort") or "").strip() or None
    return {
        "executor": executor,
        "resume_session_id": str(resume) if resume else None,
        "permission_mode": str(permission) if permission else None,
        "codex_sandbox": str(sandbox) if sandbox else None,
        "auto_merge": bool(auto_merge) if auto_merge is not None else None,
        "wait_deployment": bool(wait_deployment) if wait_deployment is not None else None,
        "auto_commit": bool(auto_commit) if auto_commit is not None else None,
        "work_dir": work_dir,
        "model": model,
        "effort": effort,
    }


def _brief(task: dict, frontend: dict[str, dict] | None = None) -> dict:
    """任务列表摘要：面板表格直接用，避免整份任务单。

    source 是前端项目（谁提的单），frontend 是登记在本机的前端会话执行器
    （codex/dsh）——两者都不是后端执行器的 executor，别混为一谈。
    """
    result = task.get("result") or {}
    claim = task.get("claim") or {}
    source = task.get("source") or {}
    session = (frontend or {}).get(task["id"]) or {}
    return {
        "id": task["id"],
        "title": task.get("title", ""),
        "status": task["status"],
        "attempt": task.get("attempt"),
        "revision": task.get("revision"),
        "repo": (task.get("target") or {}).get("repo"),
        "executor": result.get("executor"),
        "frontend_repo": source.get("repo"),
        "frontend_executor": session.get("executor"),
        "pr_url": result.get("pr_url"),
        "merge_sha": result.get("merge_sha"),
        "deployment": (task.get("deployment") or {}).get("status"),
        "worker": claim.get("worker_id"),
        "failure": task.get("failure"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
    }
