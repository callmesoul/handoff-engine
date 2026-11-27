"""JSON Schema 校验 + 字段级语义校验。

原则：schema 是唯一契约。Markdown 只解释字段含义，生产者/消费者都以 schema 为准。
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

from .errors import SchemaError
from . import secrets as secret_files
from .timeutil import parse

SCHEMA_FILES = {
    "task": "task.schema.json",
    "event": "event.schema.json",
    "codex-result": "codex-result.schema.json",
}

_STATUSES = (
    "open",
    "claimed",
    "in_progress",
    "delivered",
    "awaiting_merge",
    "merged",
    "deployed",
    "verifying",
    "verified",
    "frontend_accepted",
    "failed",
    "cancelled",
)


def default_schema_dir() -> Path:
    """Return the schemas bundled with the installed Python package."""
    return Path(__file__).resolve().parent / "schemas"


@lru_cache(maxsize=8)
def _load(path_str: str) -> Draft202012Validator:
    with open(path_str, "r", encoding="utf-8") as fh:
        schema = json.load(fh)
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def validator(name: str, schema_dir: Path | None = None) -> Draft202012Validator:
    if name not in SCHEMA_FILES:
        raise KeyError(f"unknown schema: {name}")
    directory = Path(schema_dir) if schema_dir else default_schema_dir()
    return _load(str(directory / SCHEMA_FILES[name]))


def errors_for(name: str, instance, schema_dir: Path | None = None) -> list[str]:
    out = []
    for err in sorted(validator(name, schema_dir).iter_errors(instance), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in err.absolute_path) or "<root>"
        out.append(f"{where}: {err.message}")
    return out


def validate(name: str, instance, schema_dir: Path | None = None) -> None:
    errs = errors_for(name, instance, schema_dir)
    if errs:
        raise SchemaError(f"{name} 校验失败（{len(errs)} 处）", errors=errs)


def validate_task(task: dict, schema_dir: Path | None = None) -> None:
    validate("task", task, schema_dir)


def validate_event(event: dict, schema_dir: Path | None = None) -> None:
    validate("event", event, schema_dir)


def validate_codex_result(result, schema_dir: Path | None = None) -> None:
    validate("codex-result", result, schema_dir)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# 字段级语义校验：schema 管结构，这里管「这个值在当前仓库/配置下是否成立」
# --------------------------------------------------------------------------


def _glob_is_valid(pattern: str) -> bool:
    from .pathgate import compile_glob

    try:
        compile_glob(pattern)
        return True
    except Exception:  # noqa: BLE001 - 任何异常都视为非法 glob
        return False


def semantic_errors(task: dict, config, root: Path | None = None) -> list[str]:
    """返回不满足业务约束的原因列表。空列表表示通过。"""
    errs: list[str] = []
    root = Path(root) if root else Path(config.root)

    for slot in ("source", "target"):
        block = task.get(slot) or {}
        repo_id = block.get("repo")
        if repo_id and repo_id not in config.repositories:
            errs.append(f"{slot}.repo: 仓库 {repo_id!r} 未在 config/repositories.yaml 注册")
        elif repo_id:
            repo = config.repositories[repo_id]
            if block.get("role") != repo.role:
                errs.append(f"{slot}.role: {block.get('role')!r} 与仓库注册角色 {repo.role!r} 不一致")

    target = task.get("target") or {}
    repo = config.repositories.get(target.get("repo"))
    if repo and target.get("base_ref") not in repo.allowed_base_refs:
        errs.append(
            f"target.base_ref: {target.get('base_ref')!r} 不在仓库 {repo.id} 允许的分支 {list(repo.allowed_base_refs)} 内"
        )

    contract = task.get("contract")
    if contract:
        expected_prefix = f"contracts/{task.get('id')}/"
        if not str(contract.get("path", "")).startswith(expected_prefix):
            errs.append(f"contract.path: 必须以 {expected_prefix} 开头")
        else:
            file_path = root / contract["path"]
            if not file_path.is_file():
                errs.append(f"contract.path: 文件不存在 {contract['path']}")
            elif sha256_file(file_path) != contract.get("sha256"):
                errs.append("contract.sha256: 与仓库中实际文件摘要不一致")

    repro = task.get("repro") or {}
    for idx, att in enumerate(repro.get("attachments") or []):
        kind = att.get("kind")
        where = f"repro.attachments[{idx}]"
        if kind == "store_path":
            rel = att.get("ref") or ""
            if rel.startswith("/") or ".." in Path(rel).parts:
                errs.append(f"{where}.ref: 必须是交接仓内相对路径")
            elif not (root / rel).exists():
                errs.append(f"{where}.ref: 文件不存在 {rel}")
            elif att.get("sha256") and sha256_file(root / rel) != att["sha256"]:
                errs.append(f"{where}.sha256: 与文件实际摘要不一致")
        elif kind == "url":
            if not str(att.get("ref", "")).startswith("https://"):
                errs.append(f"{where}.ref: 只接受 https:// 制品地址")
        elif kind == "repo_path":
            if not (att.get("repo") and att.get("commit_sha") and att.get("path")):
                errs.append(f"{where}: repo_path 必须同时给出 repo/commit_sha/path")
            elif att.get("repo") not in config.repositories:
                errs.append(f"{where}.repo: 仓库未注册")

    for slot in ("allowed_paths", "forbidden_paths"):
        for idx, pattern in enumerate((task.get("scope") or {}).get(slot) or []):
            if not _glob_is_valid(pattern):
                errs.append(f"scope.{slot}[{idx}]: 非法 glob {pattern!r}")

    acceptance = task.get("acceptance") or {}
    env_name = acceptance.get("environment")
    if env_name and env_name not in config.environments:
        errs.append(f"acceptance.environment: 环境 {env_name!r} 未在 config/environments.yaml 注册")

    seen_ids: set[str] = set()
    for idx, check in enumerate(acceptance.get("checks") or []):
        cid = check.get("id")
        if cid in seen_ids:
            errs.append(f"acceptance.checks[{idx}].id: 重复的检查 id {cid!r}")
        seen_ids.add(cid)
        if check.get("type") in ("repo_script", "test_suite"):
            table = config.checks.repo_scripts if check["type"] == "repo_script" else config.checks.test_suites
            spec = table.get(check.get("name"))
            if spec is None:
                errs.append(
                    f"acceptance.checks[{idx}]: {check['type']} {check.get('name')!r} 未在 config/checks.yaml 注册"
                )
            elif spec.repo != check.get("repo"):
                errs.append(
                    f"acceptance.checks[{idx}].repo: 登记表里该检查属于 {spec.repo!r}，任务单写的是 {check.get('repo')!r}"
                )
        if check.get("type") in ("http", "openapi_diff"):
            placeholder_envs, _registered = secret_files.requirement_usage(
                {"acceptance": {"checks": [check]}}, config
            )
            if placeholder_envs:
                url_field = "url" if check.get("type") == "http" else "actual_url"
                raw_url = check.get(url_field)
                suffix = raw_url[len("{base_url}"):] if isinstance(raw_url, str) and raw_url.startswith("{base_url}") else None
                if suffix is None or (suffix and suffix[0] not in "/?#"):
                    errs.append(
                        f"acceptance.checks[{idx}].{url_field}: 含密钥占位时必须使用 {{base_url}} 同源 URL"
                    )
        if check.get("type") == "openapi_diff":
            rel = str(check.get("expected_path") or "")
            where = f"acceptance.checks[{idx}].expected_path"
            if "<id>" in rel:
                errs.append(f"{where}: 仍含 <id> 占位，必须替换为实际任务 id 路径")
            elif rel.startswith("/") or ".." in Path(rel).parts:
                errs.append(f"{where}: 必须是交接仓内相对路径")
            elif not (root / rel).is_file():
                errs.append(f"{where}: 文件不存在 {rel}")

    # 新版任务把公开 requirement 固化，防止任务单漏报、篡改或与 registered check 漂移。
    # 旧任务没有该字段时保持 schema v1 向后兼容。
    if "required_secrets" in task:
        try:
            discovered = secret_files.collect_requirements(task, config)
            declared = task.get("required_secrets") or []
            if discovered != declared:
                errs.append(
                    "required_secrets: 与 acceptance/checks 实际需要的密钥不一致；"
                    f"应为 {discovered!r}"
                )
            placeholder_envs, registered_keys = secret_files.requirement_usage(task, config)
            for index, raw in enumerate(declared):
                item = secret_files.normalize_requirement(raw, where=f"required_secrets[{index}]")
                if item["env"] not in placeholder_envs and (item["ref"], item["env"]) not in registered_keys:
                    errs.append(
                        f"required_secrets[{index}]: 未被 acceptance 占位或 registered check 使用"
                    )
        except SchemaError as exc:
            errs.append(f"required_secrets: {exc.message}")

    status = task.get("status")
    claim = task.get("claim")
    result = task.get("result") or {}
    deployment = task.get("deployment") or {}
    failure = task.get("failure")

    if status not in ("open", "failed", "cancelled") and not target.get("base_sha"):
        errs.append(f"target.base_sha: 状态 {status} 必须已由 worker 固定基线提交")

    if status in ("claimed", "in_progress") and not claim:
        errs.append(f"claim: 状态 {status} 必须带租约信息")
    if status in ("delivered", "awaiting_merge") and not (result.get("branch") and result.get("head_sha")):
        # 直接目录模式：改动留在用户自己的项目里，没有分支；靠 worktree + notes 表达
        direct = bool(result.get("worktree") and result.get("notes"))
        if not direct:
            errs.append(f"result: 状态 {status} 必须有 branch 与 head_sha")
    if status in ("merged", "deployed", "verifying", "verified", "frontend_accepted") and not result.get("merge_sha"):
        # 目录模式（无 PR）没有合并提交，目标对齐用 head_sha；
        # merged 是 PR 模式专属，有 PR 的任务进 deployed 及之后仍必须记录 merge_sha
        if status == "merged" or result.get("pr_number"):
            errs.append(f"result.merge_sha: 状态 {status} 必须已记录合并提交")
    if status in ("deployed", "verifying", "verified", "frontend_accepted"):
        if deployment.get("status") != "deployed":
            errs.append(f"deployment.status: 状态 {status} 要求 deployment.status=deployed")
        if not deployment.get("target_sha") or not deployment.get("observed_sha"):
            errs.append("deployment: 状态已部署时必须记录 target_sha 与 observed_sha")
    if status == "failed" and not failure:
        errs.append("failure: 状态 failed 必须记录失败阶段与原因")

    if claim:
        if parse(claim["lease_expires_at"]) <= parse(claim["claimed_at"]):
            errs.append("claim.lease_expires_at: 必须晚于 claimed_at")
        hb = claim.get("heartbeat_at")
        if hb and parse(hb) < parse(claim["claimed_at"]):
            errs.append("claim.heartbeat_at: 不能早于 claimed_at")

    if parse(task["updated_at"]) < parse(task["created_at"]):
        errs.append("updated_at: 不能早于 created_at")

    if task.get("migration", {}).get("required") and not task["migration"].get("plan", "").strip():
        errs.append("migration.plan: 需要迁移时必须写清计划")

    return errs


def validate_task_full(task: dict, config, root: Path | None = None, schema_dir: Path | None = None) -> None:
    validate_task(task, schema_dir)
    errs = semantic_errors(task, config, root)
    if errs:
        raise SchemaError(f"任务单语义校验失败（{len(errs)} 处）", errors=errs)
