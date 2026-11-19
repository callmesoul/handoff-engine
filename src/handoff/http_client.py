"""HTTP 客户端。

用于版本接口轮询和 http 验收检查。带 SSRF 防护：
只允许 http/https，解析出的每个 IP 都不能落在 deny_cidrs，
重定向逐跳重新校验，URL 里不允许带账号密码。
"""

from __future__ import annotations

import ipaddress
import json
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import ExecutionError


@dataclass
class Response:
    status: int
    url: str
    headers: dict[str, str]
    body: str

    def json(self):
        return json.loads(self.body)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class HttpClient:
    def __init__(
        self,
        *,
        timeout: float = 10,
        deny_cidrs: tuple[str, ...] = (),
        allow_private: bool = False,
        retries: int = 2,
        retry_backoff: float = 0.5,
        sleep: Callable[[float], None] | None = None,
    ):
        self.timeout = timeout
        self.allow_private = allow_private
        self._deny = [ipaddress.ip_network(c, strict=False) for c in deny_cidrs]
        self._opener = build_opener(_NoRedirect)
        # 连接类错误（SSL 握手超时、DNS、连接拒绝）是瞬时抖动，重试几次再判失败；
        # HTTP 4xx/5xx 是确定的业务响应，不重试。retries=额外尝试次数。
        self.retries = max(0, int(retries))
        self.retry_backoff = max(0.0, float(retry_backoff))
        self._sleep = sleep or time.sleep

    def _guard(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise ExecutionError(f"不允许的协议: {parts.scheme}")
        if parts.username or parts.password:
            raise ExecutionError("URL 里不允许携带账号密码")
        if self.allow_private:
            return
        host = parts.hostname
        if not host:
            raise ExecutionError(f"URL 缺少主机名: {url}")
        try:
            infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
        except socket.gaierror as exc:
            raise ExecutionError(f"无法解析主机 {host}: {exc}") from exc
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            for net in self._deny:
                if ip.version == net.version and ip in net:
                    raise ExecutionError(f"目标地址 {ip} 命中禁用网段 {net}")

    def _request_once(self, req: Request, current: str) -> Response | str:
        """发一次请求。返回 Response 或重定向的 Location；连接错误向下抛 URLError 类。"""
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return Response(
                    status=resp.status,
                    url=current,
                    headers={k.lower(): v for k, v in resp.headers.items()},
                    body=raw.decode("utf-8", errors="replace"),
                )
        except HTTPError as exc:
            raw = exc.read() if hasattr(exc, "read") else b""
            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location")
                if not location:
                    raise ExecutionError(f"重定向缺少 Location: {current}")
                return location
            return Response(
                status=exc.code,
                url=current,
                headers={k.lower(): v for k, v in (exc.headers or {}).items()},
                body=raw.decode("utf-8", errors="replace"),
            )

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: str | bytes | None = None,
        max_redirects: int = 3,
    ) -> Response:
        current = url
        for _ in range(max_redirects + 1):
            self._guard(current)  # 每次跳转都重新校验 SSRF
            data = body.encode("utf-8") if isinstance(body, str) else body
            req = Request(current, data=data, method=method.upper())
            for key, value in (headers or {}).items():
                req.add_header(key, value)
            # 连接类错误（SSL 握手超时 / DNS / 连接拒绝）是瞬时抖动，重试几次。
            # 重定向跳转本身不消耗重试次数，只有真正拿不到连接才退避重试。
            last_exc: Exception | None = None
            for attempt in range(self.retries + 1):
                try:
                    result = self._request_once(req, current)
                    break
                except (URLError, socket.timeout, TimeoutError) as exc:
                    last_exc = exc
                    if attempt < self.retries:
                        self._sleep(self.retry_backoff * (2 ** attempt))
                        continue
                    raise ExecutionError(f"请求失败 {current}: {exc}（重试 {self.retries} 次后仍失败）") from exc
            if isinstance(result, str):  # 重定向 Location
                current = urljoin(current, result)
                continue
            return result
        raise ExecutionError(f"重定向次数过多: {url}")
