"""通用 secret reference：配置只存本机文件路径，任务只存引用。

密钥值只在运行时从进程环境或受限文件读取，用于双端预检和 verifier 注入；
绝不进入 prompt、任务单、Git、日志或预检响应。预检证明使用每次请求的新 challenge，
因此不会产生可跨请求关联的稳定指纹。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from pathlib import Path

from .errors import ExecutionError, SchemaError

#: 兼容旧配置。键 = 环境变量名；值 = config.secrets 下的 *_FILE 配置项。
LEGACY_SECRETS: dict[str, str] = {
    "HANDOFF_NOTIFY_SECRET": "HANDOFF_NOTIFY_SECRET_FILE",
    "OPENAPI_API_KEY": "OPENAPI_API_KEY_FILE",
}
# 只有业务 API key 仍可按旧行为给执行 agent；通知控制面密钥永不下放。
AGENT_SECRETS = frozenset({"OPENAPI_API_KEY"})

#: verifier 历史占位名与本机规范密钥名的兼容映射。
SECRET_ALIASES: dict[str, str] = {"DOCS_API_KEY": "OPENAPI_API_KEY"}
_ENV_TOKEN_RE = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_PROOF_DOMAIN = b"handoff-secret-preflight-v1\0"
_RESERVED_ENV_NAMES = {
    "PATH", "PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "LD_LIBRARY_PATH",
    "BASH_ENV", "ENV", "SHELLOPTS", "COMSPEC", "PATHEXT", "GIT_CONFIG",
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "HANDOFF_NOTIFY_SECRET",
}
_RESERVED_ENV_PREFIXES = ("DYLD_", "GIT_CONFIG_")


def canonical_name(name: str) -> str:
    return SECRET_ALIASES.get(name, name)


def _config_secret_path(config, env_name: str, *, ref: str | None = None) -> str | None:
    """取通用 reference 或旧 *_FILE 对应的本机路径。"""
    secrets = getattr(config, "secrets", None)
    if secrets is None:
        return None
    if ref:
        value = (getattr(secrets, "refs", None) or {}).get(ref)
        if value:
            return value
    file_key = LEGACY_SECRETS.get(canonical_name(env_name))
    return getattr(secrets, file_key, None) if file_key else None


def read_secret_file(path: str | Path | None, root: Path | None = None) -> str:
    """读取配置指定的密钥文件；未配置或读不到返回空串。"""
    if not path:
        return ""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() and root is not None:
        candidate = Path(root) / candidate
    try:
        # 只剥离文本 secret 常见的行尾；不吞掉有意义的其它空白。
        return candidate.read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeDecodeError):
        return ""


def config_secret(config, env_name: str, *, ref: str | None = None) -> str:
    """从本机配置的文件路径读取密钥。"""
    return read_secret_file(
        _config_secret_path(config, env_name, ref=ref),
        getattr(config, "root", None),
    )


def resolve_secret(config, env_name: str, *, ref: str | None = None) -> str:
    """取值优先级：目标环境变量 → 规范别名变量 → reference 文件 → 旧文件配置。"""
    value = os.environ.get(env_name, "")
    if value:
        return value
    canonical = canonical_name(env_name)
    if canonical != env_name:
        value = os.environ.get(canonical, "")
        if value:
            return value
    return config_secret(config, canonical, ref=ref)


def normalize_requirement(item: object, *, where: str) -> dict:
    if not isinstance(item, dict):
        raise SchemaError(f"{where} 必须是对象")
    unknown = set(item) - {"ref", "env", "same_value"}
    if unknown:
        raise SchemaError(f"{where} 包含未知字段: {', '.join(sorted(unknown))}")
    ref = item.get("ref")
    env = item.get("env")
    same_value = item.get("same_value", True)
    if not isinstance(ref, str) or not _SECRET_REF_RE.fullmatch(ref):
        raise SchemaError(f"{where}.ref 非法")
    if not isinstance(env, str) or not _ENV_NAME_RE.fullmatch(env):
        raise SchemaError(f"{where}.env 不是合法环境变量名")
    if env.upper() in _RESERVED_ENV_NAMES or env.upper().startswith(_RESERVED_ENV_PREFIXES):
        raise SchemaError(f"{where}.env {env!r} 是保留的控制面/进程环境变量，禁止作为验收密钥")
    if type(same_value) is not bool:
        raise SchemaError(f"{where}.same_value 必须是 boolean")
    return {"ref": ref, "env": env, "same_value": same_value}


def binding_allowed(config, ref: str, env: str) -> bool:
    """HTTP/OpenAPI 动态占位只能使用本机明确批准的 env→ref 绑定。"""
    secrets = getattr(config, "secrets", None)
    if secrets is None:
        return False
    if (getattr(secrets, "bindings", None) or {}).get(env) == ref:
        return True
    # 同名并不自动构成授权，否则任务可枚举后端任意进程环境变量。只有已配置
    # public ref 或历史业务 API key 可享受同名简写；纯环境变量需显式 bindings。
    canonical = canonical_name(env)
    if ref != canonical:
        return False
    if ref in (getattr(secrets, "refs", None) or {}):
        return True
    return canonical in AGENT_SECRETS


def preflight_allowed(config, ref: str, env: str) -> bool:
    """预检只回答经本机 HTTP binding 或 registered check 授权的 reference。"""
    if binding_allowed(config, ref, env):
        return True
    checks = getattr(config, "checks", None)
    if checks is None:
        return False
    for table in (checks.repo_scripts, checks.test_suites):
        for spec in table.values():
            if any(item.ref == ref and item.env == env for item in spec.required_secrets):
                return True
    return False


def normalize_requirement_for_config(item: object, config, *, where: str) -> dict:
    normalized = normalize_requirement(item, where=where)
    notifications = getattr(config, "notifications", None)
    control_env = getattr(notifications, "secret_env", None)
    if control_env and normalized["env"] == control_env:
        raise SchemaError(
            f"{where}.env {control_env!r} 是当前通知签名控制面密钥，禁止作为验收密钥"
        )
    return normalized


def requirement_usage(task_or_draft: dict, config) -> tuple[set[str], set[tuple[str, str]]]:
    """返回 acceptance 中真正消费密钥的 placeholder env 与 registered ref/env。"""
    acceptance = task_or_draft.get("acceptance") or {}
    checks = acceptance.get("checks") or []
    registered: set[tuple[str, str]] = set()
    for check in checks:
        if not isinstance(check, dict) or check.get("type") not in ("repo_script", "test_suite"):
            continue
        table = (
            config.checks.repo_scripts
            if check["type"] == "repo_script"
            else config.checks.test_suites
        )
        spec = table.get(check.get("name"))
        if spec is not None:
            registered.update((item.ref, item.env) for item in spec.required_secrets)

    placeholder_names: set[str] = set()

    def scan(value: object) -> None:
        if isinstance(value, str):
            placeholder_names.update(_ENV_TOKEN_RE.findall(value))
        elif isinstance(value, dict):
            for nested in value.values():
                scan(nested)
        elif isinstance(value, list):
            for nested in value:
                scan(nested)

    # 只扫描 verifier 实际会展开的字段；断言文本等位置出现示例不构成密钥消费。
    for check in checks:
        if not isinstance(check, dict):
            continue
        if check.get("type") == "http":
            scan(check.get("url"))
            scan(check.get("headers") or {})
            scan(check.get("body"))
        elif check.get("type") == "openapi_diff":
            scan(check.get("actual_url"))
    return placeholder_names, registered


def collect_requirements(task_or_draft: dict, config) -> list[dict]:
    """物化一项任务需要的全部 secret reference。

    来源按优先级合并：任务显式声明、registered check 声明、验收字段中的
    ``{env:NAME}`` 占位。占位的自定义 reference 必须由本机 secrets.bindings
    明确授权；没有显式绑定时 reference 使用规范环境变量名。
    """
    explicit = task_or_draft.get("required_secrets") or []
    if not isinstance(explicit, list):
        raise SchemaError("required_secrets 必须是数组")
    if len(explicit) > 50:
        raise SchemaError("required_secrets 最多允许 50 项")

    placeholder_names, registered_keys = requirement_usage(task_or_draft, config)
    for index, check in enumerate((task_or_draft.get("acceptance") or {}).get("checks") or []):
        if not isinstance(check, dict) or check.get("type") not in ("http", "openapi_diff"):
            continue
        check_names, _unused = requirement_usage(
            {"acceptance": {"checks": [check]}}, config
        )
        if not check_names:
            continue
        url_field = "url" if check.get("type") == "http" else "actual_url"
        raw_url = check.get(url_field)
        suffix = (
            raw_url[len("{base_url}"):]
            if isinstance(raw_url, str) and raw_url.startswith("{base_url}")
            else None
        )
        if suffix is None or (suffix and suffix[0] not in "/?#"):
            raise SchemaError(
                f"acceptance.checks[{index}].{url_field}: 含密钥占位时必须使用 "
                "{base_url} 同源 URL"
            )
    merged: dict[tuple[str, str], dict] = {}
    env_refs: dict[str, str] = {}

    def add(raw: object, where: str) -> None:
        item = normalize_requirement_for_config(raw, config, where=where)
        previous_ref = env_refs.get(item["env"])
        if previous_ref is not None and previous_ref != item["ref"]:
            raise SchemaError(
                f"环境变量 {item['env']} 同时绑定了不同 secret reference: "
                f"{previous_ref!r} / {item['ref']!r}"
            )
        env_refs[item["env"]] = item["ref"]
        key = (item["ref"], item["env"])
        if key in merged:
            merged[key]["same_value"] = merged[key]["same_value"] or item["same_value"]
        else:
            merged[key] = item

    for index, item in enumerate(explicit):
        normalized = normalize_requirement_for_config(
            item, config, where=f"required_secrets[{index}]"
        )
        key = (normalized["ref"], normalized["env"])
        if normalized["env"] in placeholder_names:
            if not binding_allowed(config, normalized["ref"], normalized["env"]):
                raise SchemaError(
                    f"required_secrets[{index}]: HTTP/OpenAPI 占位绑定 "
                    f"{normalized['env']} → {normalized['ref']} 未在本机 secrets.bindings 授权"
                )
        elif key not in registered_keys:
            raise SchemaError(
                f"required_secrets[{index}]: 未被 acceptance 占位或 registered check 使用"
            )
        add(normalized, f"required_secrets[{index}]")

    acceptance = task_or_draft.get("acceptance") or {}
    checks = acceptance.get("checks") or []
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            continue
        kind = check.get("type")
        if kind in ("repo_script", "test_suite"):
            table = config.checks.repo_scripts if kind == "repo_script" else config.checks.test_suites
            spec = table.get(check.get("name"))
            if spec is not None:
                for req_index, requirement in enumerate(spec.required_secrets):
                    add(
                        requirement.as_dict(),
                        f"acceptance.checks[{index}].required_secrets[{req_index}]",
                    )

    # 只扫描结构化验收字段，不扫描 requirement/repro 等自由文本，避免把示例误判为依赖。
    for name in sorted(placeholder_names):
        if name in env_refs:
            continue
        ref = canonical_name(name)
        if not binding_allowed(config, ref, name):
            raise SchemaError(
                f"acceptance 占位 {{env:{name}}}: {name} → {ref} 未在本机 "
                "secrets.refs/bindings 授权"
            )
        add(
            {"ref": ref, "env": name, "same_value": True},
            f"acceptance 占位 {{env:{name}}}",
        )

    return [merged[key] for key in sorted(merged)]


def secret_env(config, requirements: list[dict] | tuple[dict, ...]) -> dict[str, str]:
    """解析一组需求为 verifier 子进程环境；缺失时一次列全并阻断。"""
    out: dict[str, str] = {}
    missing: list[str] = []
    env_refs: dict[str, str] = {}
    for index, raw in enumerate(requirements):
        if hasattr(raw, "as_dict"):
            raw = raw.as_dict()
        item = normalize_requirement_for_config(
            raw, config, where=f"required_secrets[{index}]"
        )
        previous_ref = env_refs.get(item["env"])
        if previous_ref is not None and previous_ref != item["ref"]:
            raise SchemaError(
                f"环境变量 {item['env']} 同时绑定了不同 secret reference: "
                f"{previous_ref!r} / {item['ref']!r}"
            )
        env_refs[item["env"]] = item["ref"]
        value = resolve_secret(config, item["env"], ref=item["ref"])
        if not value:
            missing.append(f"{item['ref']} → {item['env']}")
        else:
            out[item["env"]] = value
    if missing:
        raise ExecutionError("缺少验收密钥: " + ", ".join(missing))
    return out


def proof(
    value: str,
    *,
    control_secret: bytes,
    request_id: str,
    challenge: str,
    ref: str,
    env: str,
) -> str:
    """会话绑定的同值证明；监听者缺少控制面密钥时不能离线猜业务密钥。"""
    derived_key = hmac.new(
        control_secret,
        _PROOF_DOMAIN + value.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    message = "\0".join((request_id, challenge, ref, env)).encode("utf-8")
    digest = hmac.new(derived_key, message, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def agent_env(config) -> dict[str, str]:
    """兼容注入 codex/zcode 的固定框架密钥；通用验收密钥不会全量暴露给 agent。"""
    out: dict[str, str] = {}
    for env_name in AGENT_SECRETS:
        value = config_secret(config, env_name)
        if value:
            out[env_name] = value
    return out
