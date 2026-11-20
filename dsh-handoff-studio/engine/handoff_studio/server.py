"""StudioServer：只提供 JSON API 的本机服务（不提供 HTML 页面）。

为什么要单独一份：原面板 `handoff.panel.server.PanelServer` 把 HTML 与路由写在
同一个 Handler 里，而 studio 的界面是 DSH 自己的 React 面板，不需要也不该再发一份
HTML。但 **API 形状必须与原来完全一致**，这样新界面就是原面板的等价重写，而不是
一套平行方言。

路由因此刻意与 `src/handoff/panel/server.py` 对齐，差别只有：
    多了 /api/studio/models、/api/studio/select —— DSH 模型选择
    少了 /             —— 没有 HTML
"""

from __future__ import annotations

import json
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from handoff.errors import ConflictError, HandoffError, SchemaError, TaskNotFound

from .service import StudioService

_TASK_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)


class _QuietServer(ThreadingHTTPServer):
    """浏览器刷新/切页会掐断在途轮询，这类断连是常态，不该刷一大片栈。"""

    daemon_threads = True

    # Windows 上 SO_REUSEADDR 允许**两个进程绑定同一个端口**，于是几个引擎
    # 同时"活着"、请求随机落到旧实例上，状态怎么看都不对（原面板踩过同一个坑）。
    # 关掉它：端口被占就直接报错，而不是静默叠一层。
    allow_reuse_address = False

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001
        exc = sys.exc_info()[1]
        if isinstance(exc, _DISCONNECT_ERRORS):
            return
        super().handle_error(request, client_address)


class StudioServer:
    def __init__(self, service: StudioService, *, host: str = "127.0.0.1", port: int = 8791):
        self.service = service
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None

    def start(self) -> tuple[str, int]:
        if self.host not in ("127.0.0.1", "localhost", "::1"):
            raise HandoffError(f"studio 只允许绑定本机回环地址，拒绝 {self.host!r}")
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "handoff-studio/0.1"

            def _send_error(self, exc: HandoffError) -> None:
                """与原面板同形：把 as_dict() 摊平到顶层（code / message / 上下文）。"""
                self._send_json({"ok": False, **exc.as_dict()}, status=_status_for(exc))

            # -- GET --------------------------------------------------------
            def do_GET(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                path = parts.path
                try:
                    if path == "/api/state":
                        self._send_json(owner.service.state())
                        return
                    if path == "/api/studio/models":
                        self._send_json(owner.service.studio_models())
                        return
                    if path == "/api/tasks":
                        query = parse_qs(parts.query)
                        status = (query.get("status") or [None])[-1]
                        page = _positive_int(query, "page", default=1)
                        page_size = _positive_int(query, "page_size", default=20, cap=100)
                        self._send_json(owner.service.list_tasks_page(status=status, page=page, page_size=page_size))
                        return
                    if path.startswith("/api/tasks/") and path.endswith("/sessions"):
                        task_id = _task_id(path, suffix="/sessions")
                        executor = (parse_qs(parts.query).get("executor") or ["dsh"])[-1]
                        self._send_json(
                            {"executor": executor, "sessions": owner.service.sessions(executor, task_id=task_id)}
                        )
                        return
                    if path.startswith("/api/sessions/"):
                        executor = _task_id(path, prefix="/api/sessions/")
                        task_id = (parse_qs(parts.query).get("task") or [None])[-1]
                        self._send_json(
                            {"executor": executor, "sessions": owner.service.sessions(executor, task_id=task_id)}
                        )
                        return
                    if path.startswith("/api/tasks/"):
                        self._send_json(owner.service.get_task(_task_id(path)))
                        return
                    if path == "/api/logs":
                        query = parse_qs(parts.query)
                        task_id = (query.get("task") or [""])[-1]
                        _validate_task_id(task_id)
                        raw = (query.get("attempt") or [None])[-1]
                        attempt = int(raw) if raw and raw.isdigit() else None
                        tail = _positive_int(query, "tail", default=400, cap=5000)
                        self._send_json(owner.service.logs(task_id, attempt=attempt, tail=tail))
                        return
                    self._send_json({"ok": False, "error": f"未找到路由: {path}"}, status=404)
                except HandoffError as exc:
                    self._send_error(exc)
                except (OSError, ValueError) as exc:
                    # 路径穿越 / 坏 JSON / 文件系统错误：客户端问题，不是 500
                    self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)
                except Exception as exc:  # noqa: BLE001 - 服务不能 500 崩溃
                    self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)

            # -- POST -------------------------------------------------------
            def do_POST(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                try:
                    self._require_loopback()
                    payload = self._read_json_body()
                    if path == "/api/sync":
                        self._send_json({"ok": True, **owner.service.sync()})
                        return
                    if path == "/api/mode":
                        self._send_json({"ok": True, **owner.service.set_mode(payload)})
                        return
                    if path == "/api/studio/select":
                        self._send_json({"ok": True, **owner.service.select_model(payload)})
                        return
                    if path == "/api/studio/options":
                        # 执行弹窗要的全部选项：模型 + 每个模型支持的档位 + 执行器表
                        self._send_json({"ok": True, **owner.service.run_options(payload.get("model"))})
                        return
                    if path == "/api/studio/catalog":
                        self._send_json({"ok": True, **owner.service.remember_catalog(payload.get("catalog") or {})})
                        return
                    if path == "/api/tasks/create":
                        self._send_json({"ok": True, **owner.service.create_task(payload)})
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
                        task_id = _task_id(path, suffix="/run")
                        self._send_json({"ok": True, **owner.service.run_task(task_id, **_run_kwargs(payload))})
                        return
                    if path.startswith("/api/tasks/") and path.endswith("/retry"):
                        self._send_json({"ok": True, **owner.service.retry(_task_id(path, suffix="/retry"))})
                        return
                    if path.startswith("/api/tasks/") and path.endswith("/release"):
                        task_id = _task_id(path, suffix="/release")
                        reason = str(payload.get("reason") or "studio 释放租约")
                        self._send_json({"ok": True, **owner.service.release(task_id, reason)})
                        return
                    if path.startswith("/api/tasks/") and path.endswith("/accept"):
                        task_id = _task_id(path, suffix="/accept")
                        reason = str(payload.get("reason") or "studio：前端验收通过")
                        self._send_json({"ok": True, **owner.service.accept(task_id, reason)})
                        return
                    if path.startswith("/api/tasks/") and path.endswith("/advance"):
                        self._send_json({"ok": True, **owner.service.advance(_task_id(path, suffix="/advance"))})
                        return
                    if path.startswith("/api/tasks/") and path.endswith("/resend-notify"):
                        task_id = _task_id(path, suffix="/resend-notify")
                        self._send_json({"ok": True, **owner.service.resend_completed(task_id)})
                        return
                    self._send_json({"ok": False, "error": f"未找到路由: {path}"}, status=404)
                except HandoffError as exc:
                    self._send_error(exc)
                except (OSError, ValueError) as exc:
                    self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)
                except Exception as exc:  # noqa: BLE001
                    self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)

            # -- 基础设施 ----------------------------------------------------
            def _require_loopback(self) -> None:
                ip = self.client_address[0]
                if ip not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                    raise HandoffError(f"拒绝非本机请求: {ip}")

            def _read_json_body(self) -> dict:
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length <= 0:
                    return {}
                if length > 2_000_000:
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
                except _DISCONNECT_ERRORS:
                    self.close_connection = True
                    return
                try:
                    self.wfile.write(raw)
                except _DISCONNECT_ERRORS:
                    self.close_connection = True

            def log_message(self, _format: str, *_args) -> None:
                return

            def handle_one_request(self) -> None:
                try:
                    super().handle_one_request()
                except _DISCONNECT_ERRORS:
                    self.close_connection = True

        self._server = _QuietServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        actual_host, actual_port = self._server.server_address[:2]
        threading.Thread(target=self._server.serve_forever, name="studio-http", daemon=True).start()
        return str(actual_host), int(actual_port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


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


def _task_id(path: str, *, prefix: str = "/api/tasks/", suffix: str = "") -> str:
    body = path[len(prefix) : len(path) - len(suffix)] if suffix else path[len(prefix) :]
    task_id = unquote(body).strip("/")
    _validate_task_id(task_id)
    return task_id


def _validate_task_id(task_id: str) -> None:
    # SchemaError 的 exit_code 是 3，_status_for 会把它映射成 400：
    # 非法 id 是调用方的问题，不该报成 500 服务器错误。
    if not task_id or not set(task_id) <= _TASK_ID_CHARS:
        raise SchemaError(f"非法任务 ID: {task_id!r}")


def _run_kwargs(payload: dict) -> dict:
    """把面板执行请求翻成 run_task 的参数。

    executor 缺省为 dsh：新面板的默认执行器就是 DSH Agent。
    """
    executor = str(payload.get("executor") or "").strip() or "dsh"
    resume = payload.get("resume_session_id")
    model = str(payload.get("model") or "").strip() or None
    effort = str(payload.get("effort") or "").strip() or None
    provider = str(payload.get("provider") or "").strip() or None
    work_dir = str(payload.get("work_dir") or "").strip() or None
    return {
        "executor": executor,
        "resume_session_id": str(resume) if resume else None,
        "permission_mode": payload.get("permission_mode") or None,
        "codex_sandbox": payload.get("codex_sandbox") or None,
        # 访问权限隔离（dsh）：三档沙箱，走 session 的 sandbox/mode
        "sandbox": str(payload.get("sandbox") or "").strip() or None,
        "auto_merge": payload.get("auto_merge"),
        "wait_deployment": payload.get("wait_deployment"),
        "auto_commit": payload.get("auto_commit"),
        "work_dir": work_dir,
        "model": model,
        "effort": effort,
        "provider": provider,
    }


__all__ = ["StudioServer"]
