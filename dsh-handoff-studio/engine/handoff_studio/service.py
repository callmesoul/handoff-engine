"""StudioService：执行器换成 DSH Agent 的面板服务。

## 做法：继承 + 一处模块级常量替换

`PanelService` 把所有执行器合法性判断都写成对模块级常量 `EXECUTORS` 的比较
（`run_task` / `sessions` / `set_mode` / `state` / `_auto_resume_session` …）。
要新增一个执行器而又不改一行父类代码，最可靠的办法就是**在本进程内**把那个常量
换成含 `dsh` 的版本（见 `_install_executor_table`）。

这不是 monkey patch 别人的行为，只是拓宽一张白名单：

    handoff.panel.service.EXECUTORS   ("zcode", "codex")
        →                              ("dsh", "codex", "zcode")

原面板 `handoffctl panel` 跑在**另一个进程**里，它 import 到的仍是原来的常量，
所以这个改动对老面板零影响。两者用同一个交接仓，但执行器表互不干扰。

除此之外只覆盖两件事：

    _build_runner()   dsh 分支返回 DshAgentRunner；codex/zcode 原样交回父类
    _executor_options()  给面板提供 DSH 的可选模型（宿主实时上报）

任务列表、详情、事件、日志、执行横幅、干预续话、自动模式、验收推进……全部沿用
父类实现，所以原面板的每一条行为在 studio 里照旧存在。
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from handoff.codex_runner import CODEX_SANDBOX_MODES
from handoff.config import Config
from handoff.errors import ConfigError, HandoffError, SchemaError
from handoff.panel import service as panel_service
from handoff.panel.service import PanelService
from handoff.zcode_runner import PERMISSION_MODES as ZCODE_PERMISSION_MODES

from .notifier import NotificationPump
from . import session_index
from .runner import DEFAULT_ROUTE, DshAgentRunner
from .secrets_bridge import SecretStatus, discover_docs_key, discover_notify_secret

#: studio 的执行器表。dsh 排第一：默认执行器就是它。
EXECUTORS = ("dsh", "codex", "zcode")

#: 访问权限隔离的三档，与宿主 `@deepseek-ai/dsh-sandbox` 的 SandboxMode 逐字一致。
#: dsh 走 session 的 sandbox/mode 事件；codex 走 `--sandbox`。同一组取值，两条通路。
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")

TOKEN_FILENAME = "handoff-studio.token"
ENDPOINT_FILENAME = "handoff-studio.json"
ENDPOINT_VERSION = 1
TOKEN_ENV = "HANDOFF_STUDIO_TOKEN"
URL_ENV = "HANDOFF_STUDIO_URL"


def _install_executor_table() -> None:
    """把父类模块的白名单拓宽到含 dsh（只影响本进程）。"""
    if tuple(panel_service.EXECUTORS) != EXECUTORS:
        panel_service.EXECUTORS = EXECUTORS


_install_executor_table()


def _candidate_homes() -> list[Path]:
    """DSH 配置根候选。

    handoff 常跑在 WSL、DSH 跑在 Windows 桌面；两边 `$HOME` 不是同一个目录，
    所以 WSL 下要多列一个 Windows 侧的 home。显式 DSH_HOME 永远优先。
    """
    override = (os.environ.get("DSH_HOME") or "").strip()
    if override:
        return [Path(override).expanduser()]
    homes = [Path.home() / ".dsh"]
    if Path("/proc/version").exists():
        try:
            version = Path("/proc/version").read_text(encoding="utf-8", errors="replace")
        except OSError:
            version = ""
        if "microsoft" in version.lower():
            userprofile = (os.environ.get("USERPROFILE") or "").strip()
            if userprofile:
                homes.append(Path(userprofile) / ".dsh")
            name = (os.environ.get("USERNAME") or "").strip() or Path.home().name
            if name:
                homes.append(Path("/mnt/c/Users") / name / ".dsh")
    return homes


def dsh_home() -> Path:
    return _candidate_homes()[0]


def _locate(filename: str) -> Path:
    homes = _candidate_homes()
    for home in homes:
        candidate = home / filename
        if candidate.exists():
            return candidate
    return homes[0] / filename


def discover_route() -> tuple[str, dict | None]:
    """按可靠性顺序找插件地址：显式环境变量 → 插件自公布 → DSH_WEB_URL。"""
    explicit = (os.environ.get(URL_ENV) or "").strip()
    if explicit:
        return _normalize_route(explicit), None

    endpoint_file = _locate(ENDPOINT_FILENAME)
    try:
        parsed = json.loads(endpoint_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        parsed = None
    if isinstance(parsed, dict) and parsed.get("version") == ENDPOINT_VERSION:
        url = parsed.get("url")
        if isinstance(url, str) and url.strip():
            return _normalize_route(url), parsed

    web = (os.environ.get("DSH_WEB_URL") or "").strip()
    if web:
        return _normalize_route(web.rstrip("/") + DEFAULT_ROUTE), None

    raise ConfigError(
        "找不到 DSH Studio 地址：请在 DSH 里启用 dsh-handoff-studio 插件"
        f"（它会写入 {endpoint_file}），或设 {URL_ENV}"
    )


def _normalize_route(url: str) -> str:
    """把地址规整成 `http://127.0.0.1:<port>/handoff-studio`。"""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ConfigError(f"DSH Studio 地址必须是 http(s): {url}")
    host = parts.hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1") and not host.startswith("127."):
        raise ConfigError(f"DSH Studio 地址必须指向本机 loopback: {url}")
    base = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    if not path.endswith(DEFAULT_ROUTE):
        path = DEFAULT_ROUTE
    return base + path


def studio_token() -> str:
    """共享令牌：先环境变量，再令牌文件（插件首次启动时生成）。"""
    value = (os.environ.get(TOKEN_ENV) or "").strip()
    if value:
        return value
    path = _locate(TOKEN_FILENAME)
    try:
        saved = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(
            f"读取 DSH Studio 令牌失败: {path}（先在 DSH 里启用 dsh-handoff-studio 插件生成令牌，"
            f"或设置环境变量 {TOKEN_ENV}）"
        ) from exc
    if not saved:
        raise ConfigError(f"DSH Studio 令牌文件为空: {path}")
    return saved


class StudioService(PanelService):
    """面板服务：默认执行器是 DSH Agent，可选的模型来自 DSH 宿主。"""

    def __init__(self, root, config: Config, **kwargs):
        # 通知密钥要先补进环境再交给父类：父类 __init__ 里可能起唤醒监听，
        # 那条路径第一件事就是读密钥。引擎是插件 spawn 的，不继承
        # run-panel.ps1 注入的 HANDOFF_NOTIFY_SECRET，少这一步就会报
        # 「HANDOFF_NOTIFY_SECRET 或 HANDOFF_NOTIFY_SECRET_FILE 指定的密钥缺失或少于 32 字节」
        # ——而密钥其实好好躺在 secrets/notify-secret.txt 里。
        self._secret_status: SecretStatus = discover_notify_secret(root, config)
        # 验收用的 DOCS_API_KEY 是同一个问题，顺手补上（没有也不算错）
        self._docs_status: SecretStatus = discover_docs_key(root)
        # 地址解析失败不阻止面板启动：插件没装/没启用时，人仍然要能看任务列表和
        # 日志、查状态。真正的阻塞点放在执行那一刻（_build_runner），那里报错
        # 才说得清「为什么点了执行没动」。
        self._route: str | None = None
        self._route_error: str | None = None
        self._endpoint: dict | None = None
        try:
            self._route, self._endpoint = discover_route()
        except ConfigError as exc:
            self._route_error = exc.message
        # provider 不是 run_task 的参数（父类签名固定），用实例状态带着它走一次
        self._pending_provider: str | None = None
        self._catalog_synced_at = 0.0
        self._catalog_error: str | None = None
        self._studio_state_path = Path(root) / ".handoff-run" / "studio-state.json"
        super().__init__(root, config, **kwargs)
        # 父类的 __init__ 会用 panel-state.json 建 self.state_path 并读一次旧状态；
        # 这里改指 studio 自己的文件，再用它的校验逻辑重读一遍。放在 super() 之后
        # 是因为 state_path 由父类赋值，之后的操作（所有 _save_state/_reclaim）都走它。
        self.state_path = self._studio_state_path
        self._panel_state = self._load_state()
        # 完成通知的「第二段」：store 只把 task.completed 写进本机 outbox，真正
        # 签名投递是另一个常驻进程做的事（原面板由 run-backend.ps1 拉起
        # handoffctl notifier）。studio 引擎是插件 spawn 的单个进程，没人起它，
        # 于是「恢复推进」把任务推进到 verified 之后，通知永远停在 pending。
        # 这里把这半段搬进引擎自己。
        self._notifications = NotificationPump(
            self.root,
            config.notifications,
            logger=lambda message: self._pump_log(message),
        )
        self._notifications.start()

    def _pump_log(self, message: str) -> None:
        """通知线程的日志：有执行中的 run 就并进它的实况，否则只留在泵的状态里。"""
        active = self._active
        if active is not None:
            try:
                active.logs.append(f"[{time.strftime('%H:%M:%S')}] {message}")
            except Exception:  # noqa: BLE001 - 记日志失败不能影响投递
                pass

    # -- 状态文件：studio 独占 ------------------------------------------------
    def _require_route(self) -> str:
        if self._route is None:
            # 每次现查一次：插件可能是面板启动之后才装好的
            try:
                self._route, self._endpoint = discover_route()
                self._route_error = None
            except ConfigError as exc:
                self._route_error = exc.message
            if self._route is None:
                raise ConfigError(self._route_error or "DSH Studio 地址未知")
        return self._route

    def sync_catalog(self, *, force: bool = False, min_interval: float = 20.0) -> dict:
        """从 DSH 拉一次实时模型目录。

        `/api/state` 会被面板每 3 秒打一次，不能每次都去问宿主；默认 20 秒一次，
        并且失败只记一段原因、不抛错——面板要能显示「读不到模型」而不是整页报错。
        """
        now = time.monotonic()
        if not force and now - self._catalog_synced_at < min_interval:
            return self.studio_models()
        try:
            route = self._require_route()
            from handoff.http_client import HttpClient

            client = HttpClient(timeout=10, allow_private=True, retries=0)
            response = client.request(
                "GET",
                f"{route}/models",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {studio_token()}",
                },
            )
            if response.status != 200:
                raise ConfigError(
                    f"DSH 模型目录不可用（HTTP {response.status}）：{response.body[:200]}"
                )
            payload = json.loads(response.body or "{}")
            self.remember_catalog(payload.get("catalog") or {})
            self._catalog_error = None
        except (ConfigError, HandoffError, ValueError, OSError) as exc:
            self._catalog_error = getattr(exc, "message", None) or str(exc)
        finally:
            self._catalog_synced_at = now
        return self.studio_models()

    # -- 默认执行器 ----------------------------------------------------------
    def _default_executor(self) -> str:
        """studio 的默认执行器恒为 dsh。

        刻意不跟随 `config/panel.yaml` 的 `default_executor`：那个值属于原面板的
        语义（zcode/codex 子进程），studio 的定位就是「用 DSH 的模型干活」。共用一份
        panel.yaml 时读它，会让人以为配了 codex 却实际在跑 DSH。
        """
        return "dsh"

    def _ensure_executor(self, executor: str | None) -> str:
        name = (executor or "").strip() or self._default_executor()
        if name not in EXECUTORS:
            raise ConfigError(f"未知执行器: {name!r}（可选: {', '.join(EXECUTORS)}）")
        return name

    def _default_state(self) -> dict:
        state = super()._default_state()
        auto = state.setdefault("auto", {})
        auto["executor"] = self._default_executor()
        # 候选模型链：主模型额度用完/不可用时按顺序换下一个（空 = 不换）。
        # 放在默认状态里，父类 set_mode 的「只合并已知键」白名单才会收下它。
        auto["fallback_models"] = []
        # 不继承 config/panel.yaml 的 default_work_dir：那意味着「不建隔离 worktree，
        # 直接改真实项目目录并自动提交」。原面板这么配是它的使用者决定的；
        # studio 默认走隔离 worktree + 分支 + PR，要目录模式必须在 studio 自己的
        # 状态里显式选一次。
        auto["work_dir"] = None
        auto["auto_commit"] = False
        state.setdefault("studio", {})
        return state

    def _load_state(self) -> dict:
        """父类只认 `mode` / `auto`，其它键一律丢掉。

        studio 自己的模型选择额外存在同一个文件的 `studio` 键里，所以这里读一遍
        原始 JSON 再合并回来——不覆盖父类的校验逻辑（非法档位仍然被清掉）。
        """
        state = super()._load_state()
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return state
        studio = raw.get("studio") if isinstance(raw, dict) else None
        if isinstance(studio, dict):
            state["studio"] = studio
        # fallback_models 是 studio 的字段，但它跟着「自动配置」存在 auto 里；
        # 父类只合并它自己认识的 auto 键，不补这一步就会在重启后把候选链清空。
        auto = raw.get("auto") if isinstance(raw, dict) else None
        if isinstance(auto, dict) and "fallback_models" in auto:
            state["auto"]["fallback_models"] = self._clean_fallback_list(auto["fallback_models"])
        return state

    @staticmethod
    def _clean_fallback_list(value) -> list[dict]:
        """把状态文件里的候选链清洗成 [{provider, model, effort}]。

        手改坏的条目（少 model、类型不对）不该让整个服务起不来：丢掉它，其余
        照常。目录层面的校验在 `_fallback_chain()` 里做，那一步需要实时目录。
        """
        if not isinstance(value, list):
            return []
        out = []
        for item in value:
            if not isinstance(item, dict):
                continue
            model = str(item.get("model") or "").strip()
            if not model:
                continue
            out.append(
                {
                    "provider": str(item.get("provider") or "").strip() or None,
                    "model": model,
                    "effort": str(item.get("effort") or "").strip() or None,
                }
            )
        return out

    # -- 模式 ----------------------------------------------------------------
    def set_mode(self, payload: dict) -> dict:
        """切模式 / 存自动配置。多接一个字段：`auto.fallback_models`。

        父类只认识它自己那几个 auto 键，`fallback_models` 会被它的白名单过滤掉
        （合并时 `if k in merged` 才收）。所以这里先校验并把它塞进 auto 补丁，
        再交给父类——父类照旧做 mode / executor / session_strategy / sandbox 的
        校验与落盘，候选链只是多带一个已经清洗过的键。

        校验失败必须**先于**落盘报错：半套配置比没有配置更难排查。
        """
        if isinstance(payload, dict):
            auto_patch = payload.get("auto")
            if isinstance(auto_patch, dict) and "fallback_models" in auto_patch:
                raw = auto_patch["fallback_models"]
                if raw is not None and not isinstance(raw, list):
                    raise SchemaError("auto.fallback_models 只能是数组或 null")
                cleaned = self._clean_fallback_list(raw or [])
                # 显式给了内容却一条都留不下（比如全是空 model），说明写错了；
                # 空数组是有意义的（用户清空了候选链），不能当成错误。
                if raw and not cleaned:
                    raise SchemaError("auto.fallback_models 里的每一条都需要一个非空的 model")
                payload = dict(payload)
                payload["auto"] = {**auto_patch, "fallback_models": cleaned}
        return super().set_mode(payload)

    # -- 执行器构造 ----------------------------------------------------------
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
        if executor != "dsh":
            return super()._build_runner(
                executor,
                resume_session_id=resume_session_id,
                permission_mode=permission_mode,
                codex_sandbox=codex_sandbox,
                on_event=on_event,
                model=model,
                effort=effort,
                sandbox=sandbox,
            )
        studio = self._studio_state()
        return DshAgentRunner(
            bridge_url=self._require_route(),
            token=studio_token(),
            timeout_seconds=self.config.codex.timeout_seconds,
            log_dir=self.root / ".handoff-run" / "logs",
            model=model or studio.get("model"),
            provider=self._pending_provider or studio.get("provider"),
            effort=effort or studio.get("effort"),
            # 候选模型链：主模型额度用尽/不可用时按顺序换下一个。只对 dsh 有意义
            # （codex/zcode 的模型来自各自的配置文件，换模型那条路在父类里）。
            fallbacks=self._fallback_chain(),
            # 访问权限隔离：面板/自动模式选的档位，留空则跟随部署默认
            sandbox=sandbox,
            permission_mode=permission_mode,
            resume_session=resume_session_id,
            on_event=on_event,
            # 会话索引：让「继续上次会话」能找到 dsh 这一轮用过的会话
            session_index=session_index.index_path(self.root),
        )

    # -- 状态 ----------------------------------------------------------------
    def state(self) -> dict:
        # 先把模型目录更新一次（有节流），面板的下拉才有内容可填
        self.sync_catalog()
        state = super().state()
        # 父类的 state() 用的是它自己模块里的 EXECUTORS 常量；本进程已拓宽，
        # 但保险起见按 studio 的表重建一次，保证 dsh 一定出现在健康检查里。
        state["executors"] = {name: self._executor_health(name) for name in EXECUTORS}
        state["studio"] = self.studio_models()
        state["executors_order"] = list(EXECUTORS)
        state["default_executor"] = self._default_executor()
        # 密钥来源只暴露「有没有 / 从哪来 / 多长」，绝不暴露值本身
        state["notify_secret"] = self._secret_status.as_dict()
        state["docs_key"] = self._docs_status.as_dict()
        # studio 与原面板共用交接仓但各存各的状态；把这一点明说，免得对着
        # 原面板的 mode 找 studio 的。
        state["state_file"] = str(self._studio_state_path)
        # 完成通知的投递线程：面板据此显示「还有几条没发出去 / 为什么没发」，
        # 而不是让人对着 outbox 的 pending 数猜。
        state["notify_pump"] = self._notifications.status()
        return state

    def _executor_options(self) -> dict:
        """面板执行对话框的选项。dsh 的模型来自宿主实时上报。"""
        options = super()._executor_options()
        studio = self._studio_state()
        options["dsh"] = {
            "default_model": studio.get("model"),
            "default_effort": studio.get("effort"),
            "provider": studio.get("provider"),
            "models": list(studio.get("models") or []),
            "efforts": list(self._efforts_for(studio.get("model"), studio.get("provider")) or []),
            # 面板据此区分「宿主实时上报」与「当前拿不到」
            "catalog": "live" if studio.get("models") else "unavailable",
            # 访问权限隔离的可选项：三档沙箱，null = 跟随部署默认
            "sandbox_modes": list(SANDBOX_MODES),
        }
        return options

    def _provider_for_model(self, model: str | None, rows: list) -> str | None:
        """模型 id 在目录里对应的 provider；同名多 provider 时返回 None（无法判定）。"""
        if not model:
            return None
        hits = []
        for item in rows or []:
            if not isinstance(item, dict) or item.get("id") != model:
                continue
            p = item.get("provider")
            if p and p not in hits:
                hits.append(str(p))
        return hits[0] if len(hits) == 1 else None

    # -- 执行 ----------------------------------------------------------------
    def run_task(self, task_id: str, *, executor: str = "dsh", **kwargs) -> dict:
        executor = self._ensure_executor(executor)
        provider = kwargs.pop("provider", None)
        if executor == "dsh":
            studio = self._studio_state()
            kwargs.setdefault("model", None)
            kwargs.setdefault("effort", None)
            kwargs["model"] = kwargs["model"] or studio.get("model")
            kwargs["effort"] = kwargs["effort"] or studio.get("effort")
            model = kwargs["model"]
            provider = (provider or "").strip() or studio.get("provider")
            rows = studio.get("models") or []
            # 关键校验：模型必须属于选定的 provider。前端执行弹窗只传 model id、
            # 不传 provider，后端此前 fallback 到全局 provider，导致「gpt-6.2-sol
            # + workbuddy」这类错配被派到宿主，第一轮直接 UNKNOWN_MODEL、拿不到
            # 结构化结果，被误判为 invalid_output。这里按目录纠正：模型在目录里
            # 只对应一个 provider 时自动切过去；同名多 provider（无法判定）时
            # 明确拒绝，让调用方重新选，绝不静默派错。
            if model and rows:
                belongs = any(
                    isinstance(item, dict)
                    and item.get("id") == model
                    and item.get("provider") == provider
                    for item in rows
                )
                if not belongs:
                    resolved = self._provider_for_model(model, rows)
                    if resolved:
                        provider = resolved
                    else:
                        raise ConfigError(
                            f"模型 {model!r} 不属于 provider {provider!r}，"
                            "且在可用模型目录里无法唯一定位其 provider；请重新选择模型"
                        )
            # 换 provider 后旧档位可能不适用，按新 (model, provider) 重算；
            # 新组合不支持时清空，交给执行器用模型默认档位。
            efforts = self._efforts_for(model, provider)
            if kwargs["effort"] and efforts and kwargs["effort"] not in efforts:
                kwargs["effort"] = None
            # 访问权限隔离：只接受宿主认识的三个值，别的当作「跟随部署默认」。
            #
            # 这里必须回落到自动配置里的 `auto.sandbox`：面板的「执行」按钮只发
            # executor（不带 sandbox），若只认请求参数，人在配置里选的「完全权限」
            # 就永远不生效——任务仍按部署默认的 workspace-write 跑，于是每个新建
            # worktree 都要现场给目录补 ACL，补不上就是 SANDBOX_PREPARATION_FAILED，
            # agent 的每个 shell 工具全部失败。配置里写了什么，就该按什么跑。
            sandbox = kwargs.pop("sandbox", None)
            if sandbox not in SANDBOX_MODES:
                sandbox = (self._panel_state.get("auto") or {}).get("sandbox")
            kwargs["sandbox"] = sandbox if sandbox in SANDBOX_MODES else None
            self._pending_provider = provider
            # 续跑必须回到上一轮那个目录。DSH 会话是按 cwd 组织的：换到另一个
            # 工作区去 resume，上下文里说「我改过这些文件」，磁盘上却是干净的一份，
            # agent 会直接跳到下一步，成果等于丢了。调用方显式给了 work_dir 就
            # 尊重调用方（人可能就是想换个目录重来）。
            if str(kwargs.get("resume_session_id") or "") == "last":
                # 续跑用的是同一份工作区、同一套交付语义，所以把自动配置里选的
                # 那几档补上。不补的后果很具体：面板的「继续上次会话」只发
                # executor + resume_session_id，auto_commit 到这里是 None，
                # 父类就退回 config/panel.yaml 的默认（false）——人在 studio 里
                # 明明选了「自动提交」，续跑这一轮却把改动留在工作区不提交，
                # 交付后没有 head_sha，自动验收自然无目标可对齐。
                auto = self._panel_state.get("auto") or {}
                for key in ("auto_commit", "wait_deployment", "auto_merge"):
                    if kwargs.get(key) is None:
                        kwargs[key] = auto.get(key)
                if not kwargs.get("work_dir"):
                    found = session_index.last_session(session_index.index_path(self.root), task_id)
                    cwd = str((found or {}).get("cwd") or "").strip()
                    if cwd and Path(cwd).is_dir():
                        kwargs["work_dir"] = cwd
        try:
            return super().run_task(task_id, executor=executor, **kwargs)
        finally:
            self._pending_provider = None

    # -- 会话枚举 ------------------------------------------------------------
    def _last_session_for(self, task_id: str, executor: str) -> tuple[str, int] | None:
        """dsh：「继续上次会话」到底接着哪个会话。

        父类只认 codex 的痕迹（`codex.completed` 事件里的 run_id、日志首行的
        `thread.started`、`task.result.executor_session_id`）。dsh 这三处要么没有、
        要么是 `run-…` 这种 run 句柄而不是 `session-…`，于是「继续上次会话」
        直接报「还没有可续跑的会话」——上一轮明明刚跑过（哪怕是被宿主打断的那轮）。

        所以 dsh 优先读执行器自己记的会话索引；索引还没有时（比如升级前跑的老
        任务）再退回父类，让 codex 那套路径照旧生效。
        """
        if executor == "dsh":
            found = session_index.last_session(session_index.index_path(self.root), task_id)
            if found:
                return str(found["session_id"]), int(found.get("attempt") or 0)
            return None
        return super()._last_session_for(task_id, executor)

    def sessions(self, executor: str, *, task_id: str | None = None, limit: int = 50):
        if executor not in EXECUTORS:
            raise ConfigError(f"未知执行器: {executor!r}（可选: {', '.join(EXECUTORS)}）")
        if executor != "dsh":
            return super().sessions(executor, task_id=task_id, limit=limit)
        # DSH 会话不落 ~/.codex / ~/.zcode：只用任务历史里那一轮
        if not task_id:
            return []
        from handoff.panel import sessions as session_mod

        try:
            data = self.get_task(task_id)
        except HandoffError:
            return []
        rows = session_mod.task_sessions(data["task"], data.get("events") or [], root=self.root)
        listed = [row.as_dict() for row in rows if row.executor == "dsh"]
        # 索引里的记录更全（含被宿主打断、没来得及写 result 的那轮），
        # 按 id 去重后补在后面。
        seen = {str(item.get("id") or "") for item in listed}
        for row in session_index.task_sessions(session_index.index_path(self.root), task_id):
            sid = str(row.get("session_id") or "")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            listed.append(
                {
                    "executor": "dsh",
                    "id": sid,
                    "title": f"attempt-{row.get('attempt') or '?'}",
                    "cwd": row.get("cwd"),
                    "updated_at": row.get("started_at"),
                    "source": "index",
                    "task_id": task_id,
                    "attempt": row.get("attempt"),
                    "note": str(row.get("status") or ""),
                }
            )
        return listed[:limit]

    def remember_session(self, task_id: str, payload: dict) -> dict:
        """把「这个任务上一轮到底用的哪个 DSH 会话」登记进索引。

        两个用处：

        1. 升级前跑的老任务：那时还没有索引，会话 id 只存在于 `~/.dsh/sessions/`
           的目录名里。人想续跑，面板却找不到——这条路由让人把 id 补进去。
        2. 引擎进程被替换时（插件重启会拉起新的引擎进程），内存里的活动 run 没了，
           但会话还在磁盘上；补登记之后「继续上次会话」仍然接着那个会话，
           而不是开一个新的空白会话重头再来。

        只写运行态索引，不碰任务单，不改 Git。
        """
        session_id = str((payload or {}).get("session_id") or "").strip()
        if not session_id:
            raise SchemaError("session_id 不能为空")
        if not session_id.startswith("session-"):
            # DSH 的会话 id 一定是 session-<uuid>；别的形状多半是把 run 句柄
            # （run-…）当成会话 id 填进来了，那种 id resume 一定失败。
            raise SchemaError(f"看起来不是 DSH 会话 id: {session_id!r}（应形如 session-<uuid>）")
        attempt = (payload or {}).get("attempt")
        try:
            attempt = int(attempt or 1)
        except (TypeError, ValueError):
            raise SchemaError("attempt 必须是整数") from None
        cwd = str((payload or {}).get("cwd") or "").strip() or None
        index = session_index.index_path(self.root)
        session_index.record(
            index,
            task_id=task_id,
            attempt=attempt,
            session_id=session_id,
            run_id=str((payload or {}).get("run_id") or "") or "manual",
            cwd=cwd,
            status=str((payload or {}).get("status") or "registered"),
        )
        stored = session_index.last_session(index, task_id)
        if not stored:
            # 写失败（磁盘/权限）时不能假装成功：面板会拿「已登记」去点续跑。
            raise HandoffError("会话索引写入失败：检查交接仓 .handoff-run 目录是否可写")
        return {"task_id": task_id, "session": stored}

    # -- 面板执行对话框要的选项 ---------------------------------------------
    def run_options(self, model: str | None = None) -> dict:
        """执行弹窗需要的全部选项。

        与原面板 /api/state 里的 executor_options 同形，但 dsh 分支额外给出
        「按模型可选的档位」，这样换模型时档位下拉能立刻跟着变。
        """
        studio = self._studio_state()
        chosen = (model or "").strip() or studio.get("model")
        return {
            "executors": [
                {
                    "id": "dsh",
                    "label": "DSH Agent",
                    "default": self._default_executor() == "dsh",
                    "note": "用 DSH 宿主里的模型跑实现",
                    "models": list(studio.get("models") or []),
                    "selected_model": chosen,
                    "efforts": self._efforts_for(chosen, studio.get("provider")),
                    "selected_effort": studio.get("effort"),
                    # 自动配置里的候选模型链：面板据此画「额度用完接着用」那一列
                    "fallbacks": self.fallback_options(),
                },
                {
                    "id": "codex",
                    "label": "Codex CLI",
                    "default": False,
                    "note": "拉 codex exec 子进程",
                    "models": [{"id": m, "name": m, "provider": "codex"} for m in self.config.codex.models],
                    "efforts": list(self.config.codex.efforts)
                    or ["low", "medium", "high", "xhigh", "max"],
                },
                {
                    "id": "zcode",
                    "label": "Zcode CLI",
                    "default": False,
                    "note": "拉 zcode -p 子进程",
                    "models": [{"id": m, "name": m, "provider": "zcode"} for m in self.config.zcode.models],
                    "efforts": list(self.config.zcode.efforts)
                    or ["low", "medium", "high", "xhigh", "max"],
                },
            ],
            "codex_sandbox_options": list(CODEX_SANDBOX_MODES),
            "codex_sandbox_default": self.config.codex.sandbox,
            "permission_modes": list(ZCODE_PERMISSION_MODES),
            "projects": self._projects(),
            "environments": list(self.config.environments.keys()),
            "work_dirs": self._work_dir_options(),
        }

    def _work_dir_options(self) -> list[dict]:
        """执行弹窗里「工作目录」下拉的候选：已登记仓库的本地路径。"""
        verifier = getattr(self.config.checks, "verifier", None)
        paths = dict(getattr(verifier, "repo_paths", None) or {})
        out = []
        for repo_id, path in paths.items():
            cleaned = str(path or "").strip()
            if cleaned:
                out.append({"repo": repo_id, "path": cleaned})
        return out

    # -- 任务列表（分页） ----------------------------------------------------
    def list_tasks_page(self, *, status: str | None = None, page: int = 1, page_size: int = 20) -> dict:
        """与原面板 /api/tasks 同形的分页结果。"""
        all_tasks = self.list_tasks(status=status)
        total = len(all_tasks)
        pages = max(1, math.ceil(total / page_size)) if total else 1
        page = max(1, min(int(page or 1), pages))
        start = (page - 1) * page_size
        slice_ = all_tasks[start : start + page_size]
        frontend = self.frontend_sessions()
        briefs = [self._brief(task, frontend) for task in slice_]
        return {
            "count": total,
            "total": total,
            "tasks": briefs,
            "items": briefs,
            "page": page,
            "page_size": page_size,
            "pages": pages,
        }

    def _brief(self, task: dict, frontend: dict[str, dict] | None = None) -> dict:
        """任务列表摘要，字段与原面板 _brief 一致。"""
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

    # -- studio 自己的状态 ---------------------------------------------------
    def _studio_state(self) -> dict:
        return dict(self._panel_state.get("studio") or {})

    def _remember_studio(self, patch: dict) -> None:
        with self._lock:
            studio = self._studio_state()
            studio.update({k: v for k, v in patch.items() if v is not None})
            self._panel_state["studio"] = studio
            self._save_state()

    def _executor_health(self, name: str) -> dict:
        """dsh 的健康检查要在这里短路：没配地址时别去连一个不存在的端口。"""
        if name == "dsh" and self._route is None:
            return {"ok": False, "version": None, "error": self._route_error or "DSH Studio 地址未知"}
        return super()._executor_health(name)

    def studio_models(self) -> dict:
        """面板「模型」下拉的数据源：宿主实时上报 + 上一次的选择。"""
        studio = self._studio_state()
        return {
            "route": self._route,
            "error": self._catalog_error or self._route_error,
            "endpoint": self._endpoint,
            "provider": studio.get("provider"),
            "model": studio.get("model"),
            "effort": studio.get("effort"),
            "models": studio.get("models") or [],
            "efforts": self._efforts_for(studio.get("model"), studio.get("provider")),
            "catalog_updated_at": studio.get("catalog_updated_at"),
            # 自动配置的候选模型链（与 auto.fallback_models 同源，带可用性判定）
            "fallbacks": self.fallback_options(),
        }

    def remember_catalog(self, catalog: dict) -> dict:
        """把宿主上报的模型目录记进面板状态（面板刷新后仍能显示上次选择）。"""
        if not isinstance(catalog, dict):
            raise SchemaError("catalog 必须是对象")
        models = []
        for group in catalog.get("groups") or []:
            if not isinstance(group, dict):
                continue
            for model in group.get("models") or []:
                if not isinstance(model, dict) or not model.get("id"):
                    continue
                models.append(
                    {
                        "id": str(model["id"]),
                        "name": str(model.get("name") or model["id"]),
                        "provider": str(group.get("id") or ""),
                        "provider_name": str(group.get("name") or group.get("id") or ""),
                        "reasoning": model.get("reasoning") or {},
                    }
                )
        default = catalog.get("default") or {}
        current = self._studio_state()
        self._remember_studio(
            {
                "models": models[:400],
                # 没选过就用宿主的部署默认，免得下拉一开始是空的
                "provider": current.get("provider") or str(default.get("provider") or "") or None,
                "model": current.get("model") or str(default.get("model") or "") or None,
                "effort": current.get("effort") or str(default.get("reasoningEffort") or "") or None,
                "catalog_updated_at": str(catalog.get("updatedAt") or ""),
            }
        )
        return self.studio_models()

    def select_model(self, payload: dict) -> dict:
        """面板上选定的模型：只改 studio 自己的状态，下一个任务生效。"""
        patch: dict = {}
        for key in ("provider", "model", "effort"):
            if key not in payload:
                continue
            value = payload[key]
            if value is None:
                patch[key] = None
                continue
            if not isinstance(value, str):
                raise SchemaError(f"{key} 只能是字符串或 null")
            patch[key] = value.strip() or None

        model = patch.get("model") or self._studio_state().get("model")
        # provider 一起参与解析：同名模型在不同 provider 下档位不同
        provider = patch.get("provider") or self._studio_state().get("provider")
        efforts = self._efforts_for(model, provider)
        effort = patch.get("effort")
        # 只有「调用方没显式给档位」时才允许静默清理：那是上次选的旧档位，换模型
        # 后不适用，直接清掉沿用新模型默认，不该让人被上一个模型的档位卡住。
        # 调用方**显式**给了档位就必须校验——静默改成别的值会让人以为设置生效了，
        # 实际跑的却是另一档。
        explicit_effort = "effort" in payload and patch.get("effort") is not None
        if not explicit_effort and patch.get("model") and effort and efforts and effort not in efforts:
            patch["effort"] = None
            effort = None
        if effort and efforts and effort not in efforts:
            raise SchemaError(
                f"模型 {model} 不支持思考程度 {effort!r}（可选: {', '.join(efforts)}）"
            )
        self._remember_studio(patch)
        return self.studio_models()

    def _efforts_for(self, model: str | None, provider: str | None = None) -> list[str]:
        """某个模型真正支持的思考档位。

        关键点：同一个 model id 会在多个 provider 下出现，而且档位并不相同——
        deepseek-v4.1-flash 在 trae 是 low/high/xhigh、在 workbuddy 是
        low/high/max。以前只按 id 取第一条匹配，于是「选了 workbuddy 的同名模型，
        面板却列的是 trae 的档位」，提交时被 select_model 拒掉。

        所以带 provider 时必须一起对上；没给 provider 时才退回按 id 匹配。
        """
        if not model:
            return []
        rows = self._studio_state().get("models") or []
        # 第一优先：id + provider 都对上
        if provider:
            for item in rows:
                if item.get("id") == model and item.get("provider") == provider:
                    reasoning = item.get("reasoning") or {}
                    return [str(e.get("id")) for e in (reasoning.get("efforts") or []) if e.get("id")]
        # 退一步：只按 id（调用方没给 provider，或目录里 provider 字段缺失）
        for item in rows:
            if item.get("id") != model:
                continue
            reasoning = item.get("reasoning") or {}
            return [str(e.get("id")) for e in (reasoning.get("efforts") or []) if e.get("id")]
        return []

    # -- 候选模型链 ----------------------------------------------------------
    def _fallback_chain(self) -> list[dict]:
        """自动配置里的候选模型链（已按目录校验、规整成 {provider, model, effort}）。

        数据源是 `auto.fallback_models`：面板上「额度用完后接着用」的那一列。
        这里做三件事：
          1. 丢掉落不到目录里的条目（模型被下架 / 手改错了名字），并在状态里
             留下原因——静默丢弃会让人以为配置没保存；
          2. 按目录把 (model, provider) 对齐：只给了 model 就按目录唯一命中补
             provider，命中不唯一则保留空 provider（宿主会按 model 路由）；
          3. 档位按该模型自己声明的集合校验，不支持就清空用默认（与主模型同一
             条规则：显式给了错的档位才该报错，候选链里的旧档位属于前者之外的
             历史残留）。
        """
        raw = self._panel_state.get("auto", {}).get("fallback_models") or []
        rows = self._studio_state().get("models") or []
        chain: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            model = str(item.get("model") or "").strip()
            if not model:
                continue
            provider = str(item.get("provider") or "").strip()
            if provider and not self._catalog_has(model, provider, rows):
                # provider 对不上这个模型：按目录重新定位，而不是丢掉候选
                provider = self._provider_for_model(model, rows) or ""
            elif not provider:
                provider = self._provider_for_model(model, rows) or ""
            if rows and not self._catalog_has(model, provider, rows):
                # 目录里有内容但找不到这个模型：它已经不可路由了，跳过
                continue
            effort = str(item.get("effort") or "").strip()
            allowed = self._efforts_for(model, provider or None)
            if effort and allowed and effort not in allowed:
                effort = ""
            key = (provider, model)
            if key in seen:
                continue
            seen.add(key)
            chain.append({"provider": provider or None, "model": model, "effort": effort or None})
        return chain

    @staticmethod
    def _catalog_has(model: str, provider: str, rows: list) -> bool:
        for item in rows:
            if not isinstance(item, dict) or item.get("id") != model:
                continue
            if not provider or item.get("provider") == provider:
                return True
        return False

    def fallback_options(self) -> list[dict]:
        """面板画候选链用的行：每条带「现在还在不在目录里」的判定。

        面板不该自己猜模型是否可用——它只负责展示，判定一律以引擎为准。
        """
        rows = self._studio_state().get("models") or []
        chain = self._fallback_chain()
        out = []
        for item in chain:
            model = item["model"]
            provider = item.get("provider") or ""
            out.append(
                {
                    "provider": item.get("provider"),
                    "model": model,
                    "effort": item.get("effort"),
                    "available": self._catalog_has(model, provider, rows) if rows else True,
                    "efforts": self._efforts_for(model, provider or None),
                }
            )
        return out

    # -- 完成通知 ------------------------------------------------------------
    def advance(self, task_id: str) -> dict:
        """恢复推进：父类的后台线程跑完（可能把任务推到 verified）后，立刻发通知。

        父类只在 `_advance_task` 里改任务状态；真正的 task.completed 投递属于
        `handoffctl notifier` 那半段，studio 引擎里没人跑它——所以以前点完
        「恢复推进」，任务变 verified 了，前端却一直收不到完成事件。这里在
        后台线程结束时喊一声，让投递线程立即发一轮，不用等下一个 tick。
        """
        result = super().advance(task_id)
        self._watch_advance(task_id)
        return result

    def _watch_advance(self, task_id: str) -> None:
        def wait_done() -> None:
            while True:
                info = self._advance
                if info is None or info.get("task_id") != task_id or info.get("done"):
                    break
                time.sleep(0.2)
            # 不论成功 / blocked / 出错都踢一脚：出错时 outbox 里也可能有
            # 失败任务刚入队的 task.completed（failed 也是终态，要通知前端）。
            self._notifications.kick()

        threading.Thread(
            target=wait_done, name=f"studio-advance-notify-{task_id}", daemon=True
        ).start()

    def resend_completed(self, task_id: str) -> dict:
        """重发完成通知；发完后把投递线程的计数刷新一次。

        `replay_task_completed` 自己会同步发这一次，但它顺手往 outbox 里留了新行
        （同一任务的下一条记录），投递线程的水位要跟着推进，否则下一轮会把这条
        已送达的记录重新当成待发。所以这里既踢一脚，也顺手回报送达情况。
        """
        result = super().resend_completed(task_id)
        self._notifications.kick()
        if isinstance(result, dict):
            result["delivery_status"] = self._notifications.task_event_status(task_id)
        return result

    def notification_status(self, task_id: str) -> dict:
        """某个任务的完成通知送到哪了（面板「恢复推进」后能看到）。

        没有历史通知时返回 `{"delivered": False, "reason": "no_event"}`，
        而不是 404——面板是轮询调用，缺一条记录不是错误。
        """
        status = self._notifications.task_event_status(task_id)
        if status is None:
            return {"task_id": task_id, "delivered": False, "reason": "no_event"}
        return status

    def flush_notifications(self) -> dict:
        """手动把 outbox 里到期的完成通知发一轮（面板兜底按钮 / 排障用）。"""
        return self._notifications.flush()

    def deliver(self, task_id: str) -> dict:
        """验收（deliver）：同样是后台线程，结束后立刻发通知。

        与 advance 同一个坑：cmd_deliver 把任务推到 verified 只写 outbox，
        没有常驻 notifier 就永远发不出去。
        """
        result = super().deliver(task_id)
        self._watch_deliver(task_id)
        return result

    def _watch_deliver(self, task_id: str) -> None:
        def wait_done() -> None:
            while True:
                info = self._deliver
                if info is None or info.get("task_id") != task_id or info.get("done"):
                    break
                time.sleep(0.2)
            self._notifications.kick()

        threading.Thread(
            target=wait_done, name=f"studio-deliver-notify-{task_id}", daemon=True
        ).start()

    def shutdown(self) -> None:
        # 先停投递线程：它可能在长退避里，别让进程退出时还挂着数据库连接。
        pump = getattr(self, "_notifications", None)
        if pump is not None:
            try:
                pump.stop()
            except Exception:  # noqa: BLE001 - 关停阶段不掩蔽父类的清理
                pass
        super().shutdown()


__all__ = [
    "ENDPOINT_FILENAME",
    "ENDPOINT_VERSION",
    "EXECUTORS",
    "SANDBOX_MODES",
    "StudioService",
    "TOKEN_ENV",
    "TOKEN_FILENAME",
    "URL_ENV",
    "discover_route",
    "dsh_home",
    "studio_token",
]
