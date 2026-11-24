"""把完成事件回推到原 DSH 会话。

DSH 没有 `codex queue --thread` 那样的命令行入口：消息只能由宿主进程内的
`sessionController.prompt()` 入队，而那个方法只存在于 DSH 自身的 Node 进程里。
所以链路是两跳：

1. 本模块用 HTTP 打 DSH 宿主里的 `dsh-handoff-bridge` 插件路由；
2. 插件校验共享令牌后调用 `sessionController.prompt()`。

端口是 DSH 每次启动时由内核分配的，写死配置过一天就失效。所以插件在监听
绑定之后把实际地址写进 `$DSH_HOME/handoff-bridge.json`（见
`dsh-plugin/README.md`），本模块读那个文件自动找端口；文件不在时才退回
`DSH_WEB_URL`，最后才用 `notifications.dsh_bridge_url` 显式配置。

令牌由插件首次启动时生成到 `$DSH_HOME/handoff-bridge.token`，本模块默认读同一个
文件；也可以用 `HANDOFF_DSH_BRIDGE_TOKEN` 显式给。

`requestId` 取事件 id，天然稳定：插件按 requestId 查重，投递成功但响应丢失时
重试不会把同一条完成事件排进会话两次。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .errors import ConfigError, ExecutionError
from .http_client import HttpClient
from .sanitize import clean

#: 插件默认注册的前缀路由。
DEFAULT_BRIDGE_PATH = "/v1/handoff/dsh"

#: 令牌默认存放位置，与插件保持一致。
TOKEN_FILENAME = "handoff-bridge.token"

#: 端点自公布文件，插件绑定端口后写，卸载时删。
ENDPOINT_FILENAME = "handoff-bridge.json"

#: 端点文件格式版本；插件与这里不一致视为不可用。
ENDPOINT_VERSION = 1

TOKEN_ENV = "HANDOFF_DSH_BRIDGE_TOKEN"


def _candidate_homes() -> list[Path]:
    """可能的 DSH 配置根，按优先级排列。

    DSH 是桌面应用时跑在 Windows 上，文件落在 `C:\\Users\\<user>\\.dsh`；
    而 handoff 常常在 WSL 里跑，`Path.home()` 是 `/home/<user>`。两边指的
    不是同一个目录，所以 WSL 下要多列一个 Windows 侧的 home。
    显式 `DSH_HOME` 永远优先，且不再往下找。
    """
    override = os.environ.get("DSH_HOME", "").strip()
    if override:
        return [Path(override).expanduser()]
    return [Path.home() / ".dsh", *_windows_dsh_homes()]


def dsh_home() -> Path:
    """DSH 的配置根（首选那个），与插件里 `dshHome()` 用同一套规则。"""
    return _candidate_homes()[0]


def _locate(filename: str) -> Path:
    """在候选目录里找某个文件，返回第一个存在的；都没有则返回首选目录下的路径。

    用「文件在不在」而不是「目录在不在」来决定，是因为目录可能被别的东西
    建出来（测试、别的工具），只有文件本身才说明这里是真正在用的 DSH home。
    """
    homes = _candidate_homes()
    for home in homes:
        candidate = home / filename
        if candidate.exists():
            return candidate
    return homes[0] / filename


def _windows_dsh_homes() -> list[Path]:
    """WSL 里的 Windows 用户目录候选；非 WSL 返回空列表。"""
    if not Path("/proc/version").exists():
        return []
    try:
        version = Path("/proc/version").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if "microsoft" not in version.lower():
        return []
    out: list[Path] = []
    userprofile = os.environ.get("USERPROFILE", "").strip()
    if userprofile:
        out.append(Path(userprofile) / ".dsh")
    # WSL 里 USERPROFILE 通常不存在，退回到 /mnt/c/Users/<同名的 Windows 用户>
    name = os.environ.get("USERNAME", "").strip() or Path.home().name
    if name:
        out.append(Path("/mnt/c/Users") / name / ".dsh")
    return out


def token_file() -> Path:
    return _locate(TOKEN_FILENAME)


def endpoint_file() -> Path:
    """插件自公布的端点文件。"""
    return _locate(ENDPOINT_FILENAME)


def read_endpoint(path: str | Path | None = None) -> dict | None:
    """读插件公布的端点。

    任何不合用的情形都返回 `None` 而不是抛错：文件可能还没写过（插件没启用）、
    可能被 DSH 崩溃留在那（旧端口），也可能格式比这里新。这些都不该让
    `handoffctl` 直接失败——调用方还有 `DSH_WEB_URL` 和显式配置兜底。
    """
    file = Path(path).expanduser() if path else endpoint_file()
    try:
        raw = file.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("version") != ENDPOINT_VERSION:
        return None
    if not isinstance(parsed.get("url"), str) or not parsed["url"].strip():
        return None
    return parsed


def _loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host in ("localhost", "::1"):
        return True
    return host == "127.0.0.1" or host.startswith("127.")


def _prompt_url(base: str, *, path: str = DEFAULT_BRIDGE_PATH) -> str:
    """把某个 origin 规整成 /prompt 的绝对地址，顺带做来源校验。"""
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https"):
        raise ConfigError(f"DSH 桥接地址必须是 http:// 或 https://: {base}")
    if parts.username or parts.password:
        raise ConfigError("DSH 桥接地址里不允许携带账号密码")
    if not _loopback_host(parts.hostname):
        # 桥接路由只开在本机；指向别处意味着配置错了，早点报出来。
        raise ConfigError(f"DSH 桥接地址必须指向本机 loopback: {base}")
    if not parts.path.rstrip("/").endswith("/prompt"):
        base = base.rstrip("/") + "/prompt"
    return base


@dataclass(frozen=True)
class BridgeOrigin:
    """桥接地址是怎么找到的——doctor 靠它说清楚兜底链走到了哪一步。"""

    url: str
    #: `config`（notifications.dsh_bridge_url）/ `endpoint`（插件自公布）/ `env`（DSH_WEB_URL）
    source: str
    #: 用了端点文件时，这里是它的内容（含 pid、updatedAt）。
    endpoint: dict | None = None


def discover_bridge(explicit: str | None = None, *, path: str = DEFAULT_BRIDGE_PATH) -> BridgeOrigin:
    """按可靠性顺序找桥接地址。

    1. `notifications.dsh_bridge_url` —— 显式配置，压过一切；
    2. `$DSH_HOME/handoff-bridge.json` —— 插件自公布，端口变了也跟得上；
    3. `DSH_WEB_URL` —— 只有从 DSH 会话里启动时才有，且是启动那一刻的端口。
    """
    configured = (explicit or "").strip()
    if configured:
        return BridgeOrigin(_prompt_url(configured, path=path), "config")

    announced = read_endpoint()
    if announced is not None:
        return BridgeOrigin(_prompt_url(announced["url"], path=path), "endpoint", announced)

    web = (os.environ.get("DSH_WEB_URL") or "").strip()
    if web:
        return BridgeOrigin(_prompt_url(web.rstrip("/") + path, path=path), "env")

    raise ConfigError(
        "找不到 DSH 桥接地址：请在 DSH 里启用 dsh-handoff-bridge 插件"
        f"（它会写入 {endpoint_file()}），或设 notifications.dsh_bridge_url，"
        "或在有 DSH_WEB_URL 的环境里启动前端监听器"
    )


def bridge_url(explicit: str | None = None, *, path: str = DEFAULT_BRIDGE_PATH) -> str:
    """桥接地址（只要 URL）。"""
    return discover_bridge(explicit, path=path).url


def bridge_token(explicit_env: str | None = None, *, file: str | Path | None = None) -> str:
    """共享令牌：先环境变量，再令牌文件。"""
    env_name = (explicit_env or "").strip() or TOKEN_ENV
    value = (os.environ.get(env_name) or "").strip()
    if value:
        return value
    path = Path(file).expanduser() if file else token_file()
    try:
        saved = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(
            f"读取 DSH 桥接令牌失败: {path}（先在 DSH 里启用 dsh-handoff-bridge 插件生成令牌，"
            f"或设置环境变量 {env_name}）"
        ) from exc
    if not saved:
        raise ConfigError(f"DSH 桥接令牌文件为空: {path}")
    return saved


def request_id_for(event_id: str) -> str:
    """稳定且可读的 requestId；插件靠它做重复投递查重。"""
    return f"handoff-{event_id}"


#: 插件没启用时两种表现（SPA fallback 的 405 / 空路由的 404）共用的文案。
_MISSING_ROUTE = "DSH 桥接路由不存在：确认 dsh-handoff-bridge 插件已在该 DSH profile 里启用，并已重启 DSH"


def _error_code(body: str) -> str:
    """取插件错误响应里的 `code` 字段；解析不出来就返回空串。"""
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        return ""
    if isinstance(parsed, dict):
        code = parsed.get("code")
        if isinstance(code, str):
            return code
    return ""


class DshBridge:
    """向 DSH 宿主插件投递一条 prompt。"""

    def __init__(self, url: str, token: str, *, timeout: float = 60.0, client: HttpClient | None = None,
                 origin: BridgeOrigin | None = None):
        self.url = url
        self.token = token
        self.timeout = timeout
        self.origin = origin
        self._client = client or HttpClient(timeout=timeout, allow_private=True)

    @classmethod
    def from_config(cls, *, url: str | None = None, token_env: str | None = None,
                    token_file_path: str | None = None, timeout: float = 60.0) -> "DshBridge":
        found = discover_bridge(url)
        return cls(
            found.url,
            bridge_token(token_env, file=token_file_path),
            timeout=timeout,
            origin=found,
        )

    def _stale_hint(self) -> str:
        """端点文件可能是 DSH 上次运行留下的，连不上时把这条线索带出来。"""
        if self.origin is None or self.origin.source != "endpoint":
            return ""
        return (
            f"（地址来自插件公布的 {endpoint_file()}；如果 DSH 重启过而插件没加载，"
            "这个文件会指向已经不存在的端口，重启 DSH 里的插件即可刷新）"
        )

    def post(
        self,
        session_id: str,
        text: str,
        *,
        request_id: str,
        mode: str = "queue",
        provider: str | None = None,
        model: str | None = None,
    ) -> dict:
        if (provider is None) != (model is None):
            raise ConfigError("DSH 恢复模型必须同时提供 provider 和 model")
        body = {
            "sessionId": session_id,
            "requestId": request_id,
            "mode": mode,
            "text": text,
        }
        if provider is not None and model is not None:
            body.update({"provider": provider, "model": model})
        response = self._client.request(
            "POST",
            self.url,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
            body=json.dumps(body, ensure_ascii=False),
        )
        if response.status == 200:
            return response.json() if response.body.strip() else {"accepted": True}
        if response.status == 401:
            raise ExecutionError(
                "DSH 桥接拒绝令牌：检查 handoff 与 dsh-handoff-bridge 是否用同一个令牌文件",
                stderr=clean(response.body, 300),
            )
        if response.status == 403:
            raise ExecutionError("DSH 桥接只接受本机连接", stderr=clean(response.body, 300))
        if response.status == 404:
            # 插件对「会话不存在」也回 404，和「路由不存在」撞车——看 body 里的 code。
            if _error_code(response.body) == "session/not-found":
                raise ExecutionError(
                    f"DSH 会话不存在: {session_id}（会话可能已删除或归档）",
                    stderr=clean(response.body, 300),
                )
            raise ExecutionError(_MISSING_ROUTE + self._stale_hint(), stderr=clean(response.body, 300))
        if response.status == 405:
            # 插件没注册路由时，请求会落到 DSH 的 SPA fallback（只收 GET/HEAD）。
            raise ExecutionError(_MISSING_ROUTE + self._stale_hint(), stderr=clean(response.body, 300))
        raise ExecutionError(
            f"DSH 桥接返回 HTTP {response.status}",
            stderr=clean(response.body, 800),
        )

    def health(self) -> dict:
        base = self.url[: -len("/prompt")] if self.url.endswith("/prompt") else self.url
        response = self._client.request("GET", f"{base}/health", headers={"Accept": "application/json"})
        if response.status != 200:
            raise ExecutionError(f"DSH 桥接健康检查失败: HTTP {response.status}", stderr=clean(response.body, 300))
        return response.json()
