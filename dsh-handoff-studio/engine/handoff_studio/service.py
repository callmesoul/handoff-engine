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
import time
from pathlib import Path
from urllib.parse import urlsplit

from handoff.codex_runner import CODEX_SANDBOX_MODES
from handoff.config import Config
from handoff.errors import ConfigError, HandoffError, SchemaError
from handoff.panel import service as panel_service
from handoff.panel.service import PanelService
from handoff.zcode_runner import PERMISSION_MODES as ZCODE_PERMISSION_MODES

from .runner import DEFAULT_ROUTE, DshAgentRunner

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

    # -- 状态文件：studio 独占 ------------------------------------------------
    def _load_state(self) -> dict:
        """父类只认 `mode` / `auto`；studio 的模型选择存在同一个文件的 `studio` 键。

        父类会把不认识的键丢掉，所以这里读一遍原始 JSON 补回来——不覆盖父类的
        校验逻辑（非法档位仍然被清掉）。
        """
        state = super()._load_state()
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return state
        studio = raw.get("studio") if isinstance(raw, dict) else None
        if isinstance(studio, dict):
            state["studio"] = studio
        return state

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
        return state

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
            # 访问权限隔离：面板/自动模式选的档位，留空则跟随部署默认
            sandbox=sandbox,
            permission_mode=permission_mode,
            resume_session=resume_session_id,
            on_event=on_event,
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
        # studio 与原面板共用交接仓但各存各的状态；把这一点明说，免得对着
        # 原面板的 mode 找 studio 的。
        state["state_file"] = str(self._studio_state_path)
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
            # 访问权限隔离：只接受宿主认识的三个值，别的当作「跟随部署默认」
            sandbox = kwargs.pop("sandbox", None)
            kwargs["sandbox"] = sandbox if sandbox in SANDBOX_MODES else None
            self._pending_provider = (provider or "").strip() or studio.get("provider")
        try:
            return super().run_task(task_id, executor=executor, **kwargs)
        finally:
            self._pending_provider = None

    # -- 会话枚举 ------------------------------------------------------------
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
        return [row.as_dict() for row in rows if row.executor == "dsh"][:limit]

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
        # 换模型时：旧档位若不被新模型支持，直接清掉（沿用模型默认），
        # 而不是报错——用户只是换了个模型，不该被上一个模型的档位卡住。
        if patch.get("model") and effort and efforts and effort not in efforts:
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
