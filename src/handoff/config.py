"""配置加载。YAML 只描述确定性规则，不描述流程逻辑。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .errors import ConfigError
from .pathgate import compile_glob


class _UniqueKeyLoader(yaml.SafeLoader):
    """重复键直接报错。复制粘贴配置块时最容易踩：后一个会静默覆盖前一个。"""


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConfigError(
                f"重复的键 {key!r}（第 {key_node.start_mark.line + 1} 行）；同一个键只能出现一次"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.construct_mapping = _construct_unique_mapping
_UniqueKeyLoader.add_constructor("tag:yaml.org,2002:map", lambda loader, node: _construct_unique_mapping(loader, node, deep=True))


def _load_auto_provisioned(
    provision_dir: str,
    root: Path,
    explicit_refs: dict[str, str],
    explicit_bindings: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """读取后端自动写入的 refs / bindings；local.yaml 显式配置优先。

    自动写入的密钥值落在 provision_dir，映射落在 provision_dir/refs.json：
    ``{"version":1,"refs":{ref:path},"bindings":{env:ref}}``。这样后端只需在
    local.yaml 打开 provision_enabled，其余 ref/binding 都无需手工抄写。
    """
    candidate = Path(provision_dir).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    mapping_path = candidate / "refs.json"
    try:
        data = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    refs: dict[str, str] = {}
    bindings: dict[str, str] = {}
    raw_refs = data.get("refs", data if "refs" not in data and "bindings" not in data else {})
    if isinstance(raw_refs, dict):
        for ref, path in raw_refs.items():
            if (
                isinstance(ref, str)
                and _SECRET_REF_RE.fullmatch(ref)
                and "/" not in ref
                and "\\" not in ref
                and isinstance(path, str)
                and path.strip()
            ):
                # 只接受位于 provision_dir 内的相对文件名，避免 refs.json 被篡改后
                # 指向任意文件（值仍由服务端解析，不读取内容到这里）。
                if Path(path).name == path:
                    refs[ref] = str(candidate / path)
    raw_bindings = data.get("bindings", {})
    if isinstance(raw_bindings, dict):
        for env, ref in raw_bindings.items():
            if (
                isinstance(env, str)
                and _ENV_NAME_RE.fullmatch(env)
                and env.upper() == env
                and env not in _RESERVED_SECRET_ENVS
                and not env.startswith(_RESERVED_SECRET_ENV_PREFIXES)
                and isinstance(ref, str)
                and _SECRET_REF_RE.fullmatch(ref)
            ):
                bindings[env] = ref
    # local.yaml 显式配置优先，覆盖自动写入的同名项。
    refs.update(explicit_refs)
    bindings.update(explicit_bindings)
    return refs, bindings


def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        raise ConfigError(f"缺少配置文件: {path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.load(fh, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} YAML 解析失败: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} 顶层必须是映射")
    return data


def _resolve(root: Path, value: str | None) -> Path | None:
    if value is None:
        return None
    path = Path(value)
    return path if path.is_absolute() else (root / path)


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_RESERVED_SECRET_ENVS = {
    "PATH", "PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "LD_LIBRARY_PATH",
    "BASH_ENV", "ENV", "SHELLOPTS", "COMSPEC", "PATHEXT", "GIT_CONFIG",
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "HANDOFF_NOTIFY_SECRET",
}
_RESERVED_SECRET_ENV_PREFIXES = ("DYLD_", "GIT_CONFIG_")


def _validate_dsh_bridge_url(value: str) -> None:
    """DSH 桥接只开在本机，地址也必须指向本机。"""
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ConfigError(
            "config/local.yaml: notifications.dsh_bridge_url 必须是无账号密码的 http/https URL"
        )
    host = parts.hostname
    if not (host in ("localhost", "::1") or host.startswith("127.")):
        raise ConfigError(
            "config/local.yaml: notifications.dsh_bridge_url 必须指向本机 loopback"
            "（DSH 桥接路由只在本机 DSH 进程里开）"
        )


def _validate_notification_url(value: str, expected_path: str, name: str) -> None:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ConfigError(f"config/local.yaml: notifications.{name} 必须是无账号密码的 http/https URL")
    if parts.path != expected_path or parts.query or parts.fragment:
        raise ConfigError(f"config/local.yaml: notifications.{name} 路径必须精确为 {expected_path}")


@dataclass(frozen=True)
class SecretsConfig:
    """密钥文件所在的本机路径（值不进配置仓）。

    框架按用途读取：通知密钥只用于控制面签名，通用 refs 只注入声明它的
    verifier check；不会把全部密钥下放给 codex/zcode。*_FILE 与 refs 的值都是
    文件路径，不是密钥本身。路径可相对交接仓根，也可用绝对路径 / ~。
    """

    HANDOFF_NOTIFY_SECRET_FILE: str | None = None
    OPENAPI_API_KEY_FILE: str | None = None
    # 通用 secret reference → 本机密钥文件。reference 可以进入任务单，路径和值不能。
    refs: dict[str, str] = field(default_factory=dict)
    # HTTP/OpenAPI 占位允许的 env → public reference 绑定；防止任务任意换绑窃取密钥。
    bindings: dict[str, str] = field(default_factory=dict)
    # 后端：是否接受前端在 create 时经已认证通道推送密钥值。默认关闭。
    provision_enabled: bool = False
    # 后端：允许被自动写入的 reference 子集；为空表示所有合法 ref 都允许。
    provision_refs: tuple[str, ...] = ()
    # 后端：未在 refs 中显式配置的 reference 写入哪个目录（相对交接仓根或绝对路径）。
    provision_dir: str = ".handoff-run/secrets"
    # 前端：create 时是否主动把本机密钥值加密推送给后端。默认关闭。
    provision_push: bool = False

    def path(self, root: Path, env_name: str, *, ref: str | None = None) -> Path | None:
        """密钥文件的绝对路径；env_name 为注入变量名或 *_FILE 配置项名。

        未配置返回 None。接受两种写法是为了和 secrets.py 的 env 名调用对齐。
        """
        value = self.refs.get(ref) if ref else None
        if not value:
            file_key = env_name if env_name.endswith("_FILE") else env_name + "_FILE"
            value = getattr(self, file_key, None)
        if not value:
            return None
        path = Path(value).expanduser()
        return path if path.is_absolute() else (root / path)


@dataclass(frozen=True)
class StoreConfig:
    branch: str = "main"
    repo: str | None = None
    tasks_dir: str = "tasks"
    events_dir: str = "events"
    contracts_dir: str = "contracts"
    artifacts_dir: str = "artifacts"
    remote: str = "origin"


@dataclass(frozen=True)
class ScopeProfileConfig:
    """仓库拥有者维护的一组语义 scope；任务只保存解析后的路径。"""

    allowed_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepoScopeConfig:
    default_profile: str
    profiles: dict[str, ScopeProfileConfig]
    forbidden_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepoConfig:
    id: str
    role: str
    host: str = "github"
    default_branch: str = "main"
    allowed_base_refs: tuple[str, ...] = ("dev",)
    pr_base_ref: str | None = None
    cache_dir: str | None = None
    remote: str | None = None
    scope: RepoScopeConfig | None = None

    @property
    def remote_url(self) -> str:
        return self.remote or f"https://github.com/{self.id}.git"

    def cache_path(self, root: Path) -> Path:
        return _resolve(root, self.cache_dir) or (root / ".handoff-run" / "repo-cache" / self.id.replace("/", "__"))


_SCOPE_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _scope_globs(value, *, where: str, required: bool = False) -> tuple[str, ...]:
    if value is None:
        value = []
    if not isinstance(value, list):
        raise ConfigError(f"{where} 必须是 glob 字符串数组")
    if required and not value:
        raise ConfigError(f"{where} 至少要有一条路径")
    if len(value) > 200:
        raise ConfigError(f"{where} 最多允许 200 条路径")

    result: list[str] = []
    for index, pattern in enumerate(value):
        if not isinstance(pattern, str) or not pattern:
            raise ConfigError(f"{where}[{index}] 必须是非空字符串")
        if len(pattern) > 500:
            raise ConfigError(f"{where}[{index}] 最长 500 个字符")
        try:
            compile_glob(pattern)
        except ValueError as exc:
            raise ConfigError(f"{where}[{index}] 非法: {exc}") from exc
        if pattern in result:
            raise ConfigError(f"{where} 包含重复规则 {pattern!r}")
        result.append(pattern)
    return tuple(result)


def _repo_scope(raw, *, repo_id: str) -> RepoScopeConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"repositories.yaml: {repo_id}.scope 必须是对象")
    unknown = set(raw) - {"default_profile", "forbidden_paths", "profiles"}
    if unknown:
        raise ConfigError(
            f"repositories.yaml: {repo_id}.scope 有未知键: {', '.join(sorted(unknown))}"
        )

    raw_profiles = raw.get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise ConfigError(f"repositories.yaml: {repo_id}.scope.profiles 至少要配置一个 profile")
    repo_forbidden = _scope_globs(
        raw.get("forbidden_paths"),
        where=f"repositories.yaml: {repo_id}.scope.forbidden_paths",
    )

    profiles: dict[str, ScopeProfileConfig] = {}
    for name, spec in raw_profiles.items():
        if not isinstance(name, str) or not _SCOPE_PROFILE_RE.fullmatch(name):
            raise ConfigError(
                f"repositories.yaml: {repo_id}.scope profile 名 {name!r} 非法；"
                "只允许字母、数字、点、下划线和连字符"
            )
        if not isinstance(spec, dict):
            raise ConfigError(f"repositories.yaml: {repo_id}.scope.profiles.{name} 必须是对象")
        profile_unknown = set(spec) - {"allowed_paths", "forbidden_paths"}
        if profile_unknown:
            raise ConfigError(
                f"repositories.yaml: {repo_id}.scope.profiles.{name} 有未知键: "
                f"{', '.join(sorted(profile_unknown))}"
            )
        profile = ScopeProfileConfig(
            allowed_paths=_scope_globs(
                spec.get("allowed_paths"),
                where=f"repositories.yaml: {repo_id}.scope.profiles.{name}.allowed_paths",
                required=True,
            ),
            forbidden_paths=_scope_globs(
                spec.get("forbidden_paths"),
                where=f"repositories.yaml: {repo_id}.scope.profiles.{name}.forbidden_paths",
            ),
        )
        effective_forbidden = set(repo_forbidden) | set(profile.forbidden_paths)
        if len(effective_forbidden) > 200:
            raise ConfigError(
                f"repositories.yaml: {repo_id}.scope.profiles.{name} 合并仓库禁区后最多 200 条"
            )
        profiles[name] = profile

    default_profile = raw.get("default_profile")
    if not isinstance(default_profile, str) or not default_profile:
        raise ConfigError(f"repositories.yaml: {repo_id}.scope.default_profile 不能为空")
    if default_profile not in profiles:
        raise ConfigError(
            f"repositories.yaml: {repo_id}.scope.default_profile {default_profile!r} 不在 profiles 中"
        )
    return RepoScopeConfig(
        default_profile=default_profile,
        profiles=profiles,
        forbidden_paths=repo_forbidden,
    )


@dataclass(frozen=True)
class EnvConfig:
    name: str
    url: str | None = None
    version_path: str = "/version"
    shared: bool = True
    concurrency_group: str | None = None
    auto_deploy: bool = False

    def version_url(self) -> str | None:
        if not self.url:
            return None
        return self.url.rstrip("/") + "/" + self.version_path.lstrip("/")


@dataclass(frozen=True)
class PollConfig:
    interval_seconds: float = 10
    timeout_seconds: float = 900
    request_timeout_seconds: float = 10
    backoff_factor: float = 1.0
    max_interval_seconds: float = 60


@dataclass(frozen=True)
class SecretRequirement:
    """可公开的密钥需求元数据；绝不包含密钥值或文件路径。"""

    ref: str
    env: str
    same_value: bool = True

    def as_dict(self) -> dict:
        return {"ref": self.ref, "env": self.env, "same_value": self.same_value}


def _secret_requirements(value, *, where: str) -> tuple[SecretRequirement, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{where} 必须是数组")
    if len(value) > 50:
        raise ConfigError(f"{where} 最多允许 50 项")
    result: list[SecretRequirement] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(value):
        path = f"{where}[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{path} 必须是映射")
        unknown = set(item) - {"ref", "env", "same_value"}
        if unknown:
            raise ConfigError(f"{path} 有未知键: {', '.join(sorted(unknown))}")
        ref = item.get("ref")
        env = item.get("env")
        same_value = item.get("same_value", True)
        if not isinstance(ref, str) or not _SECRET_REF_RE.fullmatch(ref):
            raise ConfigError(f"{path}.ref 必须匹配 {_SECRET_REF_RE.pattern}")
        if not isinstance(env, str) or not _ENV_NAME_RE.fullmatch(env):
            raise ConfigError(f"{path}.env 必须是合法的环境变量名")
        upper_env = env.upper()
        if upper_env in _RESERVED_SECRET_ENVS or upper_env.startswith(_RESERVED_SECRET_ENV_PREFIXES):
            raise ConfigError(f"{path}.env {env!r} 是保留的控制面/进程环境变量")
        if type(same_value) is not bool:
            raise ConfigError(f"{path}.same_value 必须是 boolean")
        key = (ref, env)
        if key in seen:
            raise ConfigError(f"{where} 包含重复密钥需求 {ref!r}/{env}")
        seen.add(key)
        result.append(SecretRequirement(ref=ref, env=env, same_value=same_value))
    return tuple(result)


@dataclass(frozen=True)
class SuiteSpec:
    name: str
    repo: str
    argv: tuple[str, ...]
    timeout_seconds: float = 900
    cwd: str = "."
    serial_group: str | None = None
    required_secrets: tuple[SecretRequirement, ...] = ()


@dataclass(frozen=True)
class GateConfig:
    required_checks: tuple[str, ...] = ()
    high_risk_paths: tuple[str, ...] = ()
    # 交接范围只到 develop 分支的代码交付，不包含数据库迁移，
    # 因此默认不因 migration.required 阻断自动合并。
    deny_auto_merge_if_migration_required: bool = False
    max_changed_files: int = 400
    max_file_bytes: int = 2 * 1024 * 1024


@dataclass(frozen=True)
class VerifierConfig:
    output_limit_chars: int = 4000
    max_parallel: int = 1
    deny_cidrs: tuple[str, ...] = ()
    repo_paths: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ChecksConfig:
    test_suites: dict[str, SuiteSpec] = field(default_factory=dict)
    repo_scripts: dict[str, SuiteSpec] = field(default_factory=dict)
    gate: GateConfig = field(default_factory=GateConfig)
    verifier: VerifierConfig = field(default_factory=VerifierConfig)


@dataclass(frozen=True)
class CodexConfig:
    binary: str = "codex"
    sandbox: str = "workspace-write"
    timeout_seconds: float = 3600
    model: str | None = None
    persist_thread: bool = False
    keep_worktree: bool = False
    # 传给执行器子进程的额外环境变量。典型用途：HTTP_PROXY / HTTPS_PROXY。
    # 放在配置里而不是依赖启动终端，否则从快捷方式/计划任务起面板就丢了代理。
    env: dict[str, str] = field(default_factory=dict)
    # 思考程度（codex 的 model_reasoning_effort）。None 表示跟随 codex 全局配置。
    effort: str | None = None
    # 面板下拉候选；留空则只显示「跟随配置」一项
    models: tuple[str, ...] = ()
    efforts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ZcodeConfig:
    """zcode 执行器。会话总是落盘（无 --ephemeral 概念），resume 用 --resume。"""

    binary: str = "zcode"
    timeout_seconds: float = 3600
    model: str | None = None
    effort: str | None = None
    permission_mode: str = "acceptEdits"
    models: tuple[str, ...] = ()
    efforts: tuple[str, ...] = ()
    # 子进程额外环境变量。Windows 上必须给 ZCODE_GIT_BASH_PATH，
    # 否则 zcode 的 Bash 工具直接报 "needs Git Bash on Windows"。
    env: dict[str, str] = field(default_factory=dict)


def _clean_dir(value) -> str | None:
    """配置里的路径常被编辑器塞进 BOM / 零宽字符，先清干净再用。"""
    text = str(value or "").strip().strip("\ufeff\u200b\u200c\u200d\u2060\u00a0").strip()
    return text or None


@dataclass(frozen=True)
class PanelConfig:
    """桌面面板（handoffctl panel）。只绑本机回环，不给局域网用。"""

    host: str = "127.0.0.1"
    # 8789 留给 frontend-listener（后端回推 task.completed 打的就是这个端口），面板避开
    port: int = 8790
    worker_id: str = "handoff-panel"
    default_executor: str = "zcode"
    auto_merge: bool = True
    wait_deployment: bool = True
    # 目录模式执行完自动提交并推送（AI 写提交信息）；默认关，勾了才生效
    auto_commit: bool = False
    # 额度用尽 / 限流 / 服务端故障这类失败，是否排进「到点自动重试」。
    # 默认开：这类失败不是「做不成」，只是「现在不行」，按死在 failed 等人回来
    # 点按钮是浪费。关掉就退回旧行为。
    auto_retry: bool = True
    # 自动重试上限（不含第一次执行）与指数退避的基数秒数
    max_auto_retries: int = 3
    retry_base_seconds: int = 300
    # 自动模式的默认工作目录：填了就直接在这个目录跑，不建隔离 worktree。
    # 留空时按任务的 target.repo 回退到 config/local.yaml 的 repo_paths。
    default_work_dir: str | None = None
    # 自动模式的会话策略：new 每次新会话；continue 接续该任务上次的会话
    session_strategy: str = "new"
    poll_interval_seconds: float = 5
    open_browser: bool = True

    def __post_init__(self) -> None:
        if not 0 <= self.port <= 65535:
            raise ConfigError(f"panel: port 必须在 0..65535，得到 {self.port!r}")


@dataclass(frozen=True)
class DshRestoreModelConfig:
    """恢复 DSH 会话时要选择的完整 provider/model route。"""

    provider: str
    model: str


@dataclass(frozen=True)
class FrontendRestoreModelsConfig:
    """恢复原前端会话时的可选模型覆盖。"""

    codex: str | None = None
    dsh: DshRestoreModelConfig | None = None


@dataclass(frozen=True)
class NotificationConfig:
    """局域网任务唤醒配置；密钥只从环境变量或本机文件读取，不进配置仓。"""

    url: str | None = None
    # 同一后端 listener 的同步密钥预检端点；未配置时从 url 自动派生。
    preflight_url: str | None = None
    # 同一后端 listener 的密钥自动写入端点；未配置时从 url 自动派生。
    provision_url: str | None = None
    secret_env: str = "HANDOFF_NOTIFY_SECRET"
    outbox_database: str = ".handoff-run/notify/outbox.sqlite3"
    inbox_database: str = ".handoff-run/notify/inbox.sqlite3"
    session_database: str = ".handoff-run/notify/frontend-sessions.sqlite3"
    listen_host: str = "127.0.0.1"
    listen_port: int = 8788
    allowed_cidrs: tuple[str, ...] = ()
    completion_url: str | None = None
    queue_remote: str | None = None
    queue_remote_auth_token_env: str | None = None
    dsh_bridge_url: str | None = None
    dsh_bridge_token_env: str | None = None
    restore_models: FrontendRestoreModelsConfig = field(default_factory=FrontendRestoreModelsConfig)
    request_timeout_seconds: float = 5
    max_skew_seconds: int = 300
    retry_initial_seconds: float = 2
    retry_max_seconds: float = 300

    def outbox_path(self, root: Path) -> Path:
        return _resolve(root, self.outbox_database) or (root / ".handoff-run" / "notify" / "outbox.sqlite3")

    def inbox_path(self, root: Path) -> Path:
        return _resolve(root, self.inbox_database) or (root / ".handoff-run" / "notify" / "inbox.sqlite3")

    def session_path(self, root: Path) -> Path:
        return _resolve(root, self.session_database) or (
            root / ".handoff-run" / "notify" / "frontend-sessions.sqlite3"
        )


@dataclass(frozen=True)
class UserConfig:
    token: str
    id: str
    role: str
    repos: tuple[str, ...] = ("*",)


@dataclass(frozen=True)
class AdminConfig:
    database: str = ".handoff-run/index/handoff.sqlite3"
    poll_interval_seconds: float = 30
    host: str = "127.0.0.1"
    port: int = 8787
    read_only: bool = False
    users: tuple[UserConfig, ...] = ()
    codex_logs_days: int | None = 30

    def database_path(self, root: Path) -> Path:
        return _resolve(root, self.database) or (root / ".handoff-run" / "index" / "handoff.sqlite3")


def _restore_model_name(value, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"config/local.yaml: {path} 必须是非空字符串或 null")
    return value.strip()


def _parse_restore_models(value) -> FrontendRestoreModelsConfig:
    path = "notifications.restore_models"
    if value is None:
        return FrontendRestoreModelsConfig()
    if not isinstance(value, dict):
        raise ConfigError(f"config/local.yaml: {path} 必须是映射")
    unknown = set(value) - {"codex", "dsh"}
    if unknown:
        raise ConfigError(f"config/local.yaml: {path} 有未知键: {', '.join(sorted(unknown))}")

    codex = _restore_model_name(value.get("codex"), f"{path}.codex")
    raw_dsh = value.get("dsh")
    if raw_dsh is None:
        dsh = None
    else:
        if not isinstance(raw_dsh, dict):
            raise ConfigError(f"config/local.yaml: {path}.dsh 必须是包含 provider 和 model 的映射")
        unknown_dsh = set(raw_dsh) - {"provider", "model"}
        if unknown_dsh:
            raise ConfigError(
                f"config/local.yaml: {path}.dsh 有未知键: {', '.join(sorted(unknown_dsh))}"
            )
        provider = _restore_model_name(raw_dsh.get("provider"), f"{path}.dsh.provider")
        model = _restore_model_name(raw_dsh.get("model"), f"{path}.dsh.model")
        if provider is None or model is None:
            raise ConfigError(f"config/local.yaml: {path}.dsh 必须同时配置 provider 和 model")
        dsh = DshRestoreModelConfig(provider=provider, model=model)
    return FrontendRestoreModelsConfig(codex=codex, dsh=dsh)


def _apply_local(local: dict, repositories, environments, checks, admin, codex, zcode, panel, notifications, secrets):
    """把 config/local.yaml 合并进来。只认下面几个键，拼错就报错。"""
    known = {
        "repo_paths", "repositories", "environments", "codex", "zcode", "panel", "admin",
        "gate", "notifications", "secrets",
    }
    unknown = set(local) - known
    if unknown:
        raise ConfigError(f"config/local.yaml 里有未知键: {', '.join(sorted(unknown))}")

    if local.get("secrets"):
        raw_secrets = local["secrets"]
        if not isinstance(raw_secrets, dict):
            raise ConfigError("config/local.yaml: secrets 必须是映射")
        patch = dict(raw_secrets)
        if set(patch) - set(SecretsConfig.__dataclass_fields__):
            bad = sorted(set(patch) - set(SecretsConfig.__dataclass_fields__))
            raise ConfigError(f"config/local.yaml: secrets 有未知键: {', '.join(bad)}")
        raw_refs = patch.pop("refs", {}) or {}
        raw_bindings = patch.pop("bindings", {}) or {}
        if not isinstance(raw_refs, dict):
            raise ConfigError("config/local.yaml: secrets.refs 必须是 reference 到文件路径的映射")
        if not isinstance(raw_bindings, dict):
            raise ConfigError("config/local.yaml: secrets.bindings 必须是环境变量到 reference 的映射")
        refs: dict[str, str] = {}
        for ref, value in raw_refs.items():
            if not isinstance(ref, str) or not _SECRET_REF_RE.fullmatch(ref):
                raise ConfigError(f"config/local.yaml: secrets.refs 的 reference {ref!r} 非法")
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"config/local.yaml: secrets.refs.{ref} 必须是非空文件路径字符串")
            refs[ref] = value
        bindings: dict[str, str] = {}
        for env, ref in raw_bindings.items():
            if not isinstance(env, str) or not _ENV_NAME_RE.fullmatch(env):
                raise ConfigError(f"config/local.yaml: secrets.bindings 环境变量 {env!r} 非法")
            upper_env = env.upper()
            if env != upper_env:
                raise ConfigError(f"config/local.yaml: secrets.bindings.{env} 环境变量必须大写")
            if upper_env in _RESERVED_SECRET_ENVS or upper_env.startswith(_RESERVED_SECRET_ENV_PREFIXES):
                raise ConfigError(f"config/local.yaml: secrets.bindings.{env} 是保留环境变量")
            if not isinstance(ref, str) or not _SECRET_REF_RE.fullmatch(ref):
                raise ConfigError(f"config/local.yaml: secrets.bindings.{env} reference 非法")
            bindings[env] = ref
        provision_enabled = patch.pop("provision_enabled", secrets.provision_enabled)
        if type(provision_enabled) is not bool:
            raise ConfigError("config/local.yaml: secrets.provision_enabled 必须是 boolean")
        raw_provision_refs = patch.pop("provision_refs", secrets.provision_refs)
        if raw_provision_refs is None:
            raw_provision_refs = ()
        if not isinstance(raw_provision_refs, (list, tuple)):
            raise ConfigError("config/local.yaml: secrets.provision_refs 必须是 reference 列表")
        provision_refs: tuple[str, ...] = ()
        for ref in raw_provision_refs:
            if not isinstance(ref, str) or not _SECRET_REF_RE.fullmatch(ref):
                raise ConfigError(f"config/local.yaml: secrets.provision_refs 中 {ref!r} 非法")
            if "/" in ref or "\\" in ref or ref in (".", ".."):
                raise ConfigError(f"config/local.yaml: secrets.provision_refs 中 {ref!r} 不能包含路径分隔符")
            provision_refs = provision_refs + (ref,)
        provision_dir = patch.pop("provision_dir", secrets.provision_dir)
        if provision_dir is not None and not isinstance(provision_dir, str):
            raise ConfigError("config/local.yaml: secrets.provision_dir 必须是路径字符串")
        provision_push = patch.pop("provision_push", secrets.provision_push)
        if type(provision_push) is not bool:
            raise ConfigError("config/local.yaml: secrets.provision_push 必须是 boolean")
        for key, value in patch.items():
            if value is not None and not isinstance(value, str):
                raise ConfigError(f"config/local.yaml: secrets.{key} 必须是文件路径字符串")
        secrets = replace(
            secrets,
            refs=refs,
            bindings=bindings,
            provision_enabled=provision_enabled,
            provision_refs=provision_refs,
            provision_dir=provision_dir,
            provision_push=provision_push,
            **patch,
        )

    repos = dict(repositories)
    for repo_id, patch in (local.get("repositories") or {}).items():
        if repo_id not in repos:
            raise ConfigError(f"config/local.yaml: 仓库 {repo_id!r} 未注册")
        repos[repo_id] = replace(repos[repo_id], **patch)

    envs = dict(environments)
    for name, patch in (local.get("environments") or {}).items():
        if name not in envs:
            raise ConfigError(f"config/local.yaml: 环境 {name!r} 未注册")
        envs[name] = replace(envs[name], **patch)

    if "repo_paths" in local:
        paths = dict(checks.verifier.repo_paths)
        for repo_id, path in (local["repo_paths"] or {}).items():
            if str(repo_id) not in repos:
                raise ConfigError(f"config/local.yaml: repo_paths 里的 {repo_id!r} 未注册")
            paths[str(repo_id)] = str(path)
        checks = replace(checks, verifier=replace(checks.verifier, repo_paths=paths))

    if local.get("codex"):
        codex = replace(codex, **local["codex"])

    if local.get("zcode"):
        try:
            zcode = replace(zcode, **local["zcode"])
        except TypeError as exc:
            raise ConfigError(f"config/local.yaml: zcode 配置错误: {exc}") from exc

    if local.get("panel"):
        panel_patch = dict(local["panel"])
        if "default_work_dir" in panel_patch:
            panel_patch["default_work_dir"] = _clean_dir(panel_patch["default_work_dir"])
        try:
            panel = replace(panel, **panel_patch)
        except TypeError as exc:
            raise ConfigError(f"config/local.yaml: panel 配置错误: {exc}") from exc

    if local.get("admin"):
        admin = replace(admin, **local["admin"])

    if local.get("notifications"):
        patch = dict(local["notifications"])
        if "allowed_cidrs" in patch:
            patch["allowed_cidrs"] = tuple(patch["allowed_cidrs"] or ())
        if "restore_models" in patch:
            patch["restore_models"] = _parse_restore_models(patch["restore_models"])
        try:
            notifications = replace(notifications, **patch)
        except TypeError as exc:
            raise ConfigError(f"config/local.yaml: notifications 配置错误: {exc}") from exc
        if not 0 <= notifications.listen_port <= 65535:
            raise ConfigError("config/local.yaml: notifications.listen_port 必须在 0..65535")
        if notifications.queue_remote_auth_token_env and not notifications.queue_remote:
            raise ConfigError("config/local.yaml: queue_remote_auth_token_env 需要同时配置 queue_remote")
        if notifications.dsh_bridge_url:
            _validate_dsh_bridge_url(notifications.dsh_bridge_url)
        if notifications.dsh_bridge_token_env and not _ENV_NAME_RE.fullmatch(
            notifications.dsh_bridge_token_env
        ):
            raise ConfigError(
                "config/local.yaml: notifications.dsh_bridge_token_env 必须是合法的环境变量名"
            )
        if notifications.url:
            _validate_notification_url(notifications.url, "/v1/handoff/wake", "url")
        if notifications.preflight_url:
            _validate_notification_url(
                notifications.preflight_url,
                "/v1/handoff/secret-preflight",
                "preflight_url",
            )
        if notifications.provision_url:
            _validate_notification_url(
                notifications.provision_url,
                "/v1/handoff/secret-provision",
                "provision_url",
            )
        if notifications.completion_url:
            _validate_notification_url(
                notifications.completion_url,
                "/v1/handoff/completed",
                "completion_url",
            )
        for name in (
            "request_timeout_seconds",
            "max_skew_seconds",
            "retry_initial_seconds",
            "retry_max_seconds",
        ):
            if getattr(notifications, name) <= 0:
                raise ConfigError(f"config/local.yaml: notifications.{name} 必须大于 0")

    # 面板和 frontend-listener 抢同一个端口是本机实际踩过的坑，配完就地拦下
    if panel.port == notifications.listen_port:
        raise ConfigError(
            "config/local.yaml: panel.port 不能等于 notifications.listen_port"
            f"（{notifications.listen_port} 已被 frontend-listener 占用）"
        )

    if "gate" in local:
        gate = local["gate"]
        if not isinstance(gate, dict) or set(gate) != {"max_file_bytes"}:
            raise ConfigError("config/local.yaml: gate 仅允许覆盖 max_file_bytes")
        size = gate["max_file_bytes"]
        if type(size) is not int or size <= 0:
            raise ConfigError("config/local.yaml: max_file_bytes 必须为正整数")
        checks = replace(checks, gate=replace(checks.gate, max_file_bytes=size))

    return repos, envs, checks, admin, codex, zcode, panel, notifications, secrets


class Config:
    def __init__(
        self,
        root: Path,
        store: StoreConfig,
        repositories,
        environments,
        deploy_poll,
        verify_poll,
        checks,
        admin,
        codex=None,
        zcode=None,
        panel=None,
        notifications=None,
        secrets=None,
    ):
        self.root = Path(root)
        self.store = store
        self.repositories: dict[str, RepoConfig] = repositories
        self.environments: dict[str, EnvConfig] = environments
        self.deploy_poll = deploy_poll
        self.verify_poll = verify_poll
        self.checks = checks
        self.admin = admin
        self.codex: CodexConfig = codex or CodexConfig()
        self.zcode: ZcodeConfig = zcode or ZcodeConfig()
        self.panel: PanelConfig = panel or PanelConfig()
        self.notifications: NotificationConfig = notifications or NotificationConfig()
        self.secrets: SecretsConfig = secrets or SecretsConfig()
        # 后端打开自动写入时，把 provision_dir/refs.json 中的引用并入 resolver，
        # 使自动写入的密钥在重启后立即可用（local.yaml 显式配置优先）。
        if self.secrets.provision_enabled:
            merged_refs, merged_bindings = _load_auto_provisioned(
                self.secrets.provision_dir,
                self.root,
                self.secrets.refs,
                self.secrets.bindings,
            )
            object.__setattr__(self.secrets, "refs", merged_refs)
            object.__setattr__(self.secrets, "bindings", merged_bindings)
        # 让 notification.load_secret 能从配置文件取密钥（它通常只拿到 NotificationConfig）
        object.__setattr__(self.notifications, "_app_config", self)

    # -- 查找 --------------------------------------------------------------
    def repo(self, repo_id: str) -> RepoConfig:
        try:
            return self.repositories[repo_id]
        except KeyError as exc:
            raise ConfigError(f"仓库未注册: {repo_id}") from exc

    def env(self, name: str) -> EnvConfig:
        try:
            return self.environments[name]
        except KeyError as exc:
            raise ConfigError(f"环境未注册: {name}") from exc

    def repos_by_role(self, role: str) -> list[RepoConfig]:
        return [r for r in self.repositories.values() if r.role == role]

    # -- 加载 --------------------------------------------------------------
    @classmethod
    def load(cls, root: str | Path) -> "Config":
        root = Path(root).resolve()
        conf_dir = root / "config"

        raw_repos = _read_yaml(conf_dir / "repositories.yaml")
        store = StoreConfig(**(raw_repos.get("store") or {}))

        repositories: dict[str, RepoConfig] = {}
        for item in raw_repos.get("repositories") or []:
            if "id" not in item:
                raise ConfigError("repositories.yaml 里每个仓库必须有 id")
            allowed = tuple(item.get("allowed_base_refs") or ["dev"])
            repo = RepoConfig(
                id=item["id"],
                role=item.get("role", "backend"),
                host=item.get("host", "github"),
                default_branch=item.get("default_branch", "main"),
                allowed_base_refs=allowed,
                pr_base_ref=item.get("pr_base_ref") or (allowed[0] if allowed else None),
                cache_dir=item.get("cache_dir"),
                remote=item.get("remote"),
                scope=_repo_scope(item.get("scope"), repo_id=item["id"]),
            )
            if repo.role not in ("backend", "frontend"):
                raise ConfigError(f"{repo.id}: role 必须是 backend/frontend")
            repositories[repo.id] = repo
        if not repositories:
            raise ConfigError("repositories.yaml 里没有任何仓库")

        raw_envs = _read_yaml(conf_dir / "environments.yaml")
        environments: dict[str, EnvConfig] = {}
        for item in raw_envs.get("environments") or []:
            env = EnvConfig(
                name=item["name"],
                url=item.get("url"),
                version_path=item.get("version_path", "/version"),
                shared=bool(item.get("shared", True)),
                concurrency_group=item.get("concurrency_group"),
                auto_deploy=bool(item.get("auto_deploy", False)),
            )
            environments[env.name] = env
        if not environments:
            raise ConfigError("environments.yaml 里没有任何环境")

        deploy_poll = PollConfig(**(raw_envs.get("deploy_poll") or {}))
        verify_poll = PollConfig(**(raw_envs.get("verify_poll") or {}))

        raw_checks = _read_yaml(conf_dir / "checks.yaml")
        suites = {
            name: SuiteSpec(
                name=name,
                repo=spec["repo"],
                argv=tuple(spec["argv"]),
                timeout_seconds=float(spec.get("timeout_seconds", 900)),
                cwd=spec.get("cwd", "."),
                serial_group=spec.get("serial_group"),
                required_secrets=_secret_requirements(
                    spec.get("required_secrets"),
                    where=f"checks.yaml: test_suites.{name}.required_secrets",
                ),
            )
            for name, spec in (raw_checks.get("test_suites") or {}).items()
        }
        scripts = {
            name: SuiteSpec(
                name=name,
                repo=spec["repo"],
                argv=(spec["path"],),
                timeout_seconds=float(spec.get("timeout_seconds", 600)),
                cwd=spec.get("cwd", "."),
                serial_group=spec.get("serial_group"),
                required_secrets=_secret_requirements(
                    spec.get("required_secrets"),
                    where=f"checks.yaml: repo_scripts.{name}.required_secrets",
                ),
            )
            for name, spec in (raw_checks.get("repo_scripts") or {}).items()
        }
        gate_raw = raw_checks.get("gate") or {}
        gate = GateConfig(
            required_checks=tuple(gate_raw.get("required_checks") or ()),
            high_risk_paths=tuple(gate_raw.get("high_risk_paths") or ()),
            deny_auto_merge_if_migration_required=bool(gate_raw.get("deny_auto_merge_if_migration_required", False)),
            max_changed_files=int(gate_raw.get("max_changed_files", 400)),
            max_file_bytes=int(gate_raw.get("max_file_bytes", 2 * 1024 * 1024)),
        )
        ver_raw = raw_checks.get("verifier") or {}
        verifier = VerifierConfig(
            output_limit_chars=int(ver_raw.get("output_limit_chars", 4000)),
            max_parallel=int(ver_raw.get("max_parallel", 1)),
            deny_cidrs=tuple(ver_raw.get("deny_cidrs") or ()),
            repo_paths=dict(ver_raw.get("repo_paths") or {}),
        )
        checks = ChecksConfig(test_suites=suites, repo_scripts=scripts, gate=gate, verifier=verifier)

        admin_raw = _read_yaml(conf_dir / "admin.yaml")
        index_raw = admin_raw.get("index") or {}
        server_raw = admin_raw.get("server") or {}
        retention_raw = admin_raw.get("retention") or {}
        users = tuple(
            UserConfig(
                token=u["token"],
                id=u["id"],
                role=u.get("role", "viewer"),
                repos=tuple(u.get("repos") or ("*",)),
            )
            for u in (admin_raw.get("users") or [])
        )
        for user in users:
            if user.role not in ("viewer", "operator", "admin"):
                raise ConfigError(f"admin.yaml: 未知角色 {user.role!r}")
        admin = AdminConfig(
            database=index_raw.get("database", ".handoff-run/index/handoff.sqlite3"),
            poll_interval_seconds=float(index_raw.get("poll_interval_seconds", 30)),
            host=server_raw.get("host", "127.0.0.1"),
            port=int(server_raw.get("port", 8787)),
            read_only=bool(server_raw.get("read_only", False)),
            users=users,
            codex_logs_days=retention_raw.get("codex_logs_days", 30),
        )

        codex_path = conf_dir / "codex.yaml"
        if codex_path.is_file():
            raw_codex = _read_yaml(codex_path).get("codex") or {}
            codex_env = raw_codex.get("env") or {}
            if not isinstance(codex_env, dict):
                raise ConfigError("codex.env 必须是键值对象，例如 env: {HTTP_PROXY: http://127.0.0.1:10808}")
            codex = CodexConfig(
                binary=raw_codex.get("binary", "codex"),
                sandbox=raw_codex.get("sandbox", "workspace-write"),
                timeout_seconds=float(raw_codex.get("timeout_seconds", 3600)),
                model=raw_codex.get("model"),
                persist_thread=bool(raw_codex.get("persist_thread", False)),
                keep_worktree=bool(raw_codex.get("keep_worktree", False)),
                env={str(k): str(v) for k, v in codex_env.items() if v is not None},
                effort=raw_codex.get("effort"),
                models=tuple(str(m) for m in (raw_codex.get("models") or ())),
                efforts=tuple(str(e) for e in (raw_codex.get("efforts") or ())),
            )
        else:
            codex = CodexConfig()

        zcode_path = conf_dir / "zcode.yaml"
        if zcode_path.is_file():
            raw_zcode = _read_yaml(zcode_path).get("zcode") or {}
            zcode_env = raw_zcode.get("env") or {}
            if not isinstance(zcode_env, dict):
                raise ConfigError("zcode.env 必须是键值对象，例如 env: {ZCODE_GIT_BASH_PATH: C:\\Program Files\\Git\\bin\\bash.exe}")
            zcode = ZcodeConfig(
                binary=raw_zcode.get("binary", "zcode"),
                timeout_seconds=float(raw_zcode.get("timeout_seconds", 3600)),
                model=raw_zcode.get("model"),
                effort=raw_zcode.get("effort"),
                permission_mode=raw_zcode.get("permission_mode", "acceptEdits"),
                models=tuple(str(m) for m in (raw_zcode.get("models") or ())),
                efforts=tuple(str(e) for e in (raw_zcode.get("efforts") or ())),
                env={str(k): str(v) for k, v in zcode_env.items() if v is not None},
            )
        else:
            zcode = ZcodeConfig()

        panel_path = conf_dir / "panel.yaml"
        if panel_path.is_file():
            raw_panel = _read_yaml(panel_path).get("panel") or {}
            panel = PanelConfig(
                host=raw_panel.get("host", "127.0.0.1"),
                port=int(raw_panel.get("port", 8790)),
                worker_id=raw_panel.get("worker_id", "handoff-panel"),
                default_executor=raw_panel.get("default_executor", "zcode"),
                auto_merge=bool(raw_panel.get("auto_merge", True)),
                wait_deployment=bool(raw_panel.get("wait_deployment", True)),
                auto_commit=bool(raw_panel.get("auto_commit", False)),
                default_work_dir=_clean_dir(raw_panel.get("default_work_dir")),
                session_strategy=raw_panel.get("session_strategy", "new"),
                poll_interval_seconds=float(raw_panel.get("poll_interval_seconds", 5)),
                open_browser=bool(raw_panel.get("open_browser", True)),
            )
            if panel.default_executor not in ("zcode", "codex"):
                raise ConfigError(f"panel.yaml: default_executor 只能是 zcode/codex，得到 {panel.default_executor!r}")
            if panel.session_strategy not in ("new", "continue"):
                raise ConfigError(f"panel.yaml: session_strategy 只能是 new/continue，得到 {panel.session_strategy!r}")
        else:
            panel = PanelConfig()

        notifications = NotificationConfig()
        secrets = SecretsConfig()

        # 本机覆盖：config/local.yaml 不进 Git，放机器专属路径（repo_paths、cache_dir、codex 路径…）
        local_path = conf_dir / "local.yaml"
        if local_path.is_file():
            local = _read_yaml(local_path)
            (
                repositories, environments, checks, admin, codex, zcode, panel,
                notifications, secrets,
            ) = _apply_local(
                local, repositories, environments, checks, admin, codex, zcode, panel,
                notifications, secrets,
            )

        return cls(
            root,
            store,
            repositories,
            environments,
            deploy_poll,
            verify_poll,
            checks,
            admin,
            codex,
            zcode,
            panel,
            notifications,
            secrets,
        )


def find_root(start: str | Path | None = None) -> Path:
    """从当前目录向上找 config/repositories.yaml，找不到就报错，不猜。"""
    current = Path(start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "config" / "repositories.yaml").is_file():
            return candidate
    raise ConfigError(f"向上未找到 config/repositories.yaml（起点 {current}）")
