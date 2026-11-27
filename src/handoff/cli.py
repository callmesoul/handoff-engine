"""handoffctl：唯一入口。

prompt / skill 不允许直接拼 git 命令或改任务单 JSON，只能调这里。
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import threading
import time
import sys
from urllib.parse import urlsplit
from pathlib import Path

from . import doctor, executil, ids, notification, schema, state
from . import secrets as _secret_files
from .codex_runner import CodexRunner
from .config import Config, find_root
from .deploy import CommitAncestry, VersionProbe, wait_for_deployment
from .errors import ConfigError, ConflictError, ExecutionError, HandoffError, SchemaError
from .frontend_session import FrontendDispatcher, FrontendSessions
from .gitcmd import git, resolve_remote_ref, run
from .sanitize import clean
from .scope import resolve_scope
from .store import Actor, TaskStore
from .timeutil import iso
from .verifier import Verifier, summarize


# ---------------------------------------------------------------------------
# 任务单模板
# ---------------------------------------------------------------------------

EMPTY_RESULT = {
    "branch": None,
    "head_sha": None,
    "pr_url": None,
    "pr_number": None,
    "merge_sha": None,
    "executor": None,
    "executor_session_id": None,
    "codex_run_id": None,
    "codex_cli_version": None,
    "prompt_version": None,
    "diff_summary": None,
    "changed_paths": [],
    "worktree": None,
    "codex_thread_id": None,
    "checks": [],
    "notes": None,
}


def empty_deployment(environment: str | None = None) -> dict:
    return {
        "status": "pending",
        "target_sha": None,
        "observed_sha": None,
        "environment": environment,
        "url": None,
        "updated_at": None,
        "error": None,
    }


def resolve_id_placeholders(task_id: str, task: dict) -> None:
    """任务单落库前的规范化（原地修改）：

    1. 把 `contracts/<id>/` 占位写成真实任务 id 路径。任务 id 在 create/amend
       时才生成，草稿里只能写 `<id>` 占位（见 doc/task-draft.example.json）。
    2. openapi_diff 的 `actual_url` 若指向 OpenAPI 却没带 apikey（裸
       `{base_url}/openapi.json` 会 404，`{base_url}/docs/openapi.json` 会 401），
       改成带 apikey 的 `{base_url}/docs/openapi.json?apikey={env:DOCS_API_KEY}`。
       只规范化这两个精确值，不碰调用方显式写的其它地址或已经带 key 的 URL。
    """
    canonical_openapi = "{base_url}/docs/openapi.json?apikey={env:DOCS_API_KEY}"
    # 这两个地址都需要补 apikey：裸路径 404，docs 路径 401。
    openapi_without_key = ("{base_url}/openapi.json", "{base_url}/docs/openapi.json")

    def fix(value):
        return value.replace("<id>", task_id) if isinstance(value, str) else value

    contract = task.get("contract")
    if isinstance(contract, dict) and isinstance(contract.get("path"), str):
        contract["path"] = fix(contract["path"])
    acceptance = task.get("acceptance") or {}
    for check in acceptance.get("checks") or []:
        if not isinstance(check, dict):
            continue
        if isinstance(check.get("expected_path"), str):
            check["expected_path"] = fix(check["expected_path"])
        if (
            check.get("type") == "openapi_diff"
            and check.get("actual_url") in openapi_without_key
        ):
            check["actual_url"] = canonical_openapi
    repro = task.get("repro") or {}
    for att in repro.get("attachments") or []:
        if isinstance(att, dict) and isinstance(att.get("ref"), str):
            att["ref"] = fix(att["ref"])


def new_task(draft: dict) -> dict:
    stamp = iso()
    task = {
        "schema_version": 1,
        "id": draft["id"],
        "revision": 1,
        "attempt": 0,
        "title": draft["title"],
        "source": draft["source"],
        "target": draft["target"],
        "requirement": draft["requirement"],
        "contract": draft.get("contract"),
        "repro": draft.get("repro"),
        "scope": draft["scope"],
        "acceptance": draft["acceptance"],
        # 交接只覆盖 develop 分支的代码交付，不承担数据库迁移。
        # 任务草案可以不写 migration；缺省视为"不需要迁移"。
        "migration": draft.get("migration") or {
            "required": False,
            "backward_compatible": True,
            "plan": "",
            "rollback": "",
            "manual_approval_required": False,
        },
        "status": "open",
        "claim": None,
        "result": dict(EMPTY_RESULT),
        "deployment": empty_deployment(draft["acceptance"].get("environment")),
        "failure": None,
        "created_at": stamp,
        "updated_at": stamp,
    }
    if draft.get("required_secrets"):
        task["required_secrets"] = draft["required_secrets"]
    return task


def slug_from_remote(url: str) -> str | None:
    """git@host:org/repo.git 与 https://host/org/repo.git 都取出 org/repo。"""
    text = url.strip()
    if not text:
        return None
    # 本地路径 / file:// 没有 org/repo 语义，交给 remote 全等匹配
    if text.startswith("/") or text.startswith("file://") or "\\" in text:
        return None
    if len(text) > 2 and text[1] == ":" and text[2] in ("\\", "/"):
        return None
    if text.startswith("git@") and ":" in text:
        path = text.split(":", 1)[1]
    elif "://" in text:
        path = urlsplit(text).path
    else:
        path = text
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[: -4]
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return None
    return "/".join(parts[-2:])


def detect_source_repo(config: Config, path: str | Path) -> str | None:
    """从 --source-path 的 origin 认仓库。认不出就返回 None，交给草稿。"""
    res = git(path, "remote", "get-url", "origin")
    if not res.ok:
        return None
    remote = res.stdout.strip()
    slug = slug_from_remote(remote)
    if slug and slug in config.repositories:
        return slug

    for repo in config.repositories.values():
        if repo.remote and repo.remote_url.rstrip("/") == remote.rstrip("/"):
            return repo.id

    if slug:
        candidates = ", ".join(sorted(r.id for r in config.repos_by_role("frontend"))) or "（无）"
        raise SchemaError(
            f"--source-path 属于 {slug}，但没在 config/repositories.yaml 注册。"
            f"已注册的前端仓：{candidates}"
        )
    return None


def head_sha(repo_path: str | Path) -> str:
    path = Path(repo_path).expanduser()
    if not path.is_dir():
        raise SchemaError(f"--source-path 不是目录: {path}")
    result = git(path, "rev-parse", "HEAD")
    if not result.ok:
        raise SchemaError(f"无法读取 {path} 的 HEAD（是 git 仓库吗？）: {clean(result.stderr, 300)}")
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# 命令实现
# ---------------------------------------------------------------------------


def cmd_create(args, config: Config) -> dict:
    root = config.root
    store = TaskStore(root, config, push=not args.no_push)

    if args.no_session_register:
        raise SchemaError("--no-session-register 已禁用：创建任务必须登记原前端会话")
    detected_executor, detected_session = current_agent_session()
    explicit_thread = (args.source_thread or "").strip()
    thread_id = explicit_thread or detected_session
    if not thread_id:
        raise SchemaError(
            "创建任务必须登记原前端会话：当前环境没有 CODEX_THREAD_ID / DSH_SESSION_ID，"
            "请从 Codex/DSH 会话执行，或显式传 --source-thread"
        )
    workspace = args.source_workspace or args.source_path or str(Path.cwd())
    executor_hint = args.source_executor or (detected_executor if not explicit_thread else None)
    executor = _frontend_executor(executor_hint, thread_id)

    draft_path = Path(args.from_file).expanduser()
    if not draft_path.is_absolute():
        draft_path = Path.cwd() / draft_path
    with open(draft_path, "r", encoding="utf-8") as fh:
        draft = json.load(fh)

    task_id = draft.get("id") or ids.task_id()
    if not task_id.startswith("ho-"):
        raise SchemaError(f"任务 id 必须以 ho- 开头: {task_id}")

    # 先把本地输入一次查完，别让人改一处跑一次
    problems: list[str] = []

    contract_src: Path | None = None
    if args.contract:
        contract_src = Path(args.contract).expanduser()
        if not contract_src.is_absolute():
            contract_src = Path.cwd() / contract_src
        if not contract_src.is_file():
            problems.append(
                f"契约文件不存在: {contract_src}"
                "（--contract 可省略；仓库自带示例 doc/openapi.example.yaml）"
            )

    attachment_srcs: list[Path] = []
    for item in args.attachment or []:
        src = Path(item).expanduser()
        if not src.is_absolute():
            src = Path.cwd() / src
        if not src.is_file():
            problems.append(f"附件不存在: {src}")
            continue
        attachment_srcs.append(src)

    if args.source_path:
        source_dir = Path(args.source_path).expanduser()
        if not source_dir.is_dir():
            problems.append(f"--source-path 不是目录: {source_dir}")

    if problems:
        raise SchemaError(f"创建前的输入检查未通过（{len(problems)} 处）", errors=problems)

    # 显式 flag > 从 --source-path 自动识别 > 草稿
    if args.target:
        draft.setdefault("target", {})["repo"] = args.target
    if args.environment:
        draft.setdefault("acceptance", {})["environment"] = args.environment

    if args.source_path:
        draft.setdefault("source", {})
        draft["source"]["commit_sha"] = head_sha(args.source_path)
        detected = detect_source_repo(config, args.source_path)
        chosen = args.source_repo or detected or draft["source"].get("repo")
        if chosen:
            draft["source"]["repo"] = chosen
    if not draft.get("source", {}).get("commit_sha"):
        raise SchemaError("source.commit_sha 缺失：用 --source-path 指定前端仓库，或在草稿里写完整 SHA")

    # 前端无权选择后端基线；兼容旧草稿，但由 worker 认领时重新固定。
    target = draft.get("target") or {}
    target.pop("base_sha", None)

    # 路径知识属于目标仓库。旧草稿中的 raw glob 不再生效，只保留语义边界；
    # 创建时把后端配置的 profile 解析并固化到任务单，后续配置变化不影响该任务。
    target_repo_id = str(target.get("repo") or "")
    target_repo = config.repositories.get(target_repo_id)
    if target_repo is None:
        raise SchemaError(f"目标项目未注册: {target_repo_id!r}")
    draft_scope = draft.get("scope") or {}
    if not isinstance(draft_scope, dict):
        raise SchemaError("scope 必须是对象")
    profile_name = draft.pop("scope_profile", None)
    draft["scope"] = resolve_scope(
        target_repo,
        profile_name=profile_name,
        out_of_scope=draft_scope.get("out_of_scope") or [],
    )

    draft["id"] = task_id
    draft.setdefault("repro", None)
    # 草稿里的 contracts/<id>/ 是占位（含 openapi_diff 的 expected_path），落库前换真实路径
    resolve_id_placeholders(task_id, draft)

    # 密钥是硬前置：使用与 verifier 相同的 resolver 检查本机，再向后端 listener
    # 发起有签名响应的一次性 challenge。失败时尚未复制附件、登记会话或写任务。
    required_secrets = _secret_files.collect_requirements(draft, config)
    if required_secrets:
        draft["required_secrets"] = required_secrets
    else:
        draft.pop("required_secrets", None)
    # 先拒绝所有无需文件系统即可发现的结构错误，再访问远端 presence/proof。
    schema.validate_task(new_task(draft))
    # 前端可选：把本机密钥值经已签名通道加密推给后端，让后端自动写入本机文件。
    # 只在显式打开 secrets.provision_push 时进行；失败时任务尚未写入。
    secret_provision = {"status": "disabled"}
    if required_secrets and (getattr(args, "provision_secrets", False) or getattr(config.secrets, "provision_push", False)):
        secret_provision = notification.provision_secrets(
            root, config.notifications, required_secrets
        )
    secret_preflight = notification.preflight_secrets(root, config.notifications, required_secrets)

    # 契约附件：复制进交接仓，记录相对路径与摘要
    if contract_src is not None:
        src = contract_src
        dest_dir = store.contract_dir(task_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        shutil.copyfile(src, dest)
        draft["contract"] = {
            "kind": args.contract_kind,
            "path": str(dest.relative_to(root)).replace(os.sep, "/"),
            "sha256": schema.sha256_file(dest),
        }
    elif draft.get("contract"):
        contract = draft["contract"]
        if contract.get("path") and (root / contract["path"]).is_file() and not contract.get("sha256"):
            contract["sha256"] = schema.sha256_file(root / contract["path"])

    if attachment_srcs:
        repro = draft.get("repro") or {"error": "", "logs": "", "attachments": []}
        for src in attachment_srcs:
            dest_dir = store.artifact_dir(task_id)
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / src.name
            shutil.copyfile(src, dest)
            repro.setdefault("attachments", []).append(
                {
                    "kind": "store_path",
                    "ref": str(dest.relative_to(root)).replace(os.sep, "/"),
                    "sha256": schema.sha256_file(dest),
                    "repo": None,
                    "commit_sha": None,
                    "path": None,
                }
            )
        draft["repro"] = repro

    task = new_task(draft)
    dispatcher = FrontendDispatcher(root, config.notifications, codex_binary=config.codex.binary)

    def register_frontend_session(created_task: dict) -> None:
        dispatcher.register(
            created_task["id"],
            thread_id=thread_id,
            workspace=workspace,
            executor=executor,
        )

    # 会话登记是任务创建的硬前置条件。登记失败时 store 还没有写任务快照、事件或 commit。
    created = store.create(
        task,
        summary=f"创建任务：{task['title']}",
        before_write=register_frontend_session,
    )
    session_result = {
        "status": "registered",
        "executor": executor,
        "thread_id": thread_id,
        "workspace": str(Path(workspace).expanduser().resolve()),
    }
    notify_result = {"status": "disabled"}
    if config.notifications.url and not args.no_push:
        payload = notification.task_created_payload(
            task_id=created["id"],
            revision=created["revision"],
            store_commit=created.get("_commit") or "",
            reply_url=config.notifications.completion_url,
        )
        notifier = None
        try:
            notifier = notification.Notifier(root, config.notifications)
            event_id = notifier.enqueue(payload)
            notify_result = {
                "status": "pending",
                "delivery": "background_notifier",
                "event_id": event_id,
                "pending": notifier.outbox.pending_count(),
            }
        except HandoffError as exc:
            # 任务已经成功写入交接仓。入队失败不能把已创建说成失败。
            status = notifier.outbox.status(payload["event_id"]) if notifier else None
            notify_result = {
                "status": "pending" if status else "error",
                "event_id": payload["event_id"],
                "error": exc.message,
            }
        except Exception as exc:  # noqa: BLE001 - Git 任务已经创建，通知层不能反向改写事实
            status = notifier.outbox.status(payload["event_id"]) if notifier else None
            notify_result = {
                "status": "pending" if status else "error",
                "event_id": payload["event_id"],
                "error": clean(f"{type(exc).__name__}: {exc}", 500),
            }
    out = {
        "id": created["id"],
        "revision": created["revision"],
        "status": created["status"],
        "store_commit": created.get("_commit"),
        "notification": notify_result,
        "secret_preflight": secret_preflight,
        "secret_provision": secret_provision,
        "frontend_session": session_result,
        "next": f"handoffctl show {created['id']}",
    }
    return out


def cmd_show(args, config: Config) -> dict:
    store = TaskStore(config.root, config, push=False)
    task = store.get(args.task_id)
    if getattr(args, "json", False):
        return task
    return _task_brief(task)


def _task_brief(task: dict) -> dict:
    return {
        "id": task["id"],
        "title": task["title"],
        "status": task["status"],
        "revision": task["revision"],
        "attempt": task["attempt"],
        "target": f"{task['target']['repo']}@{task['target']['base_ref']}",
        "worker": (task.get("claim") or {}).get("worker_id"),
        "lease_expires_at": (task.get("claim") or {}).get("lease_expires_at"),
        "branch": task["result"].get("branch"),
        "head_sha": task["result"].get("head_sha"),
        "pr": task["result"].get("pr_url"),
        "merge_sha": task["result"].get("merge_sha"),
        "deployment": task["deployment"].get("status"),
        "observed_sha": task["deployment"].get("observed_sha"),
        "failure": task["failure"],
        "updated_at": task["updated_at"],
    }


def cmd_list(args, config: Config) -> dict:
    store = TaskStore(config.root, config, push=False)
    tasks = store.list_tasks(status=args.status)
    return {"count": len(tasks), "tasks": [_task_brief(t) for t in tasks]}


def cmd_delete(args, config: Config) -> dict:
    if not args.yes:
        raise SchemaError("删除任务是不可逆工作区操作；确认范围后重新执行并加 --yes")
    if args.all and args.task_ids:
        raise SchemaError("--all 不能和任务 ID 同时使用")
    if not args.all and not args.task_ids:
        raise SchemaError("请指定至少一个任务 ID，或使用 --all")

    store = TaskStore(config.root, config, push=not args.no_push)
    result = store.delete_tasks(args.task_ids, delete_all=args.all)
    outbox = notification.Outbox(config.notifications.outbox_path(config.root))
    result["notifications_deleted"] = outbox.delete_tasks(result["deleted"])
    sessions = FrontendSessions(config.notifications.session_path(config.root))
    result["frontend_sessions_deleted"] = sessions.delete_tasks(result["deleted"])

    # 管理索引是派生数据；只在本机已有索引时增量应用删除提交。
    index_path = config.admin.database_path(config.root)
    if index_path.is_file():
        from .admin.indexer import Indexer

        stats = Indexer(config).index_incremental()
        result["index_head"] = stats["head"]
    return result


def cmd_validate(args, config: Config) -> dict:
    store = TaskStore(config.root, config, push=False)
    tasks = store.list_tasks()
    bad = []
    for task in tasks:
        errs = schema.errors_for("task", task)
        errs.extend(schema.semantic_errors(task, config, config.root))
        if errs:
            bad.append({"id": task.get("id"), "errors": errs})
    return {"checked": len(tasks), "invalid": len(bad), "problems": bad}


def cmd_transition(args, config: Config) -> dict:
    store = TaskStore(config.root, config, push=not args.no_push)
    task = store.get(args.task_id)
    expected = args.expected_revision if args.expected_revision is not None else task["revision"]
    updated = store.transition(
        args.task_id,
        args.to,
        expected_revision=expected,
        summary=args.reason or f"手动转为 {args.to}",
        actor=Actor("user", args.actor or "cli"),
    )
    return _task_brief(updated)


def cmd_record_result(args, config: Config) -> dict:
    """回填目录模式下人工提交后的交付结果（head_sha / branch）。

    为什么需要这条命令：目录模式（`work_dir`）默认不提交、不推送，改动留在用户
    自己的工作副本里，由人 review 后自行提交。但 `result.head_sha` 是后续所有
    环节的前置条件——`cmd_deliver` 要拿它对部署版本，「恢复推进」也要它才肯往下
    走（没有就报「任务缺少 merge_sha/head_sha，无法对齐部署」）。人提交完却没有
    受支持的入口把 sha 写回任务单，任务就永久卡在 delivered。

    这条命令就是那个入口：只写 result 里的交付事实，不动 requirement /
    acceptance / 状态；状态推进仍然由 transition / deliver / advance 决定。
    """
    store = TaskStore(config.root, config, push=not args.no_push)
    task = store.get(args.task_id)
    expected = args.expected_revision if args.expected_revision is not None else task["revision"]

    head_sha = (args.head_sha or "").strip()
    if head_sha and (len(head_sha) != 40 or any(c not in "0123456789abcdef" for c in head_sha.lower())):
        raise SchemaError(f"--head-sha 必须是 40 位十六进制提交，得到 {head_sha!r}")
    if not head_sha and not args.branch:
        raise SchemaError("至少要给 --head-sha 或 --branch")

    status = task["status"]
    allowed = ("delivered", "awaiting_merge", "merged", "deployed", "verifying", "failed")
    if status not in allowed:
        raise SchemaError(
            f"状态 {status} 不接受交付结果回填（仅 {', '.join(allowed)}）", errors=[f"当前状态: {status}"]
        )

    def mutate(t: dict) -> None:
        result = t.setdefault("result", dict(EMPTY_RESULT))
        if head_sha:
            result["head_sha"] = head_sha
        if args.branch:
            result["branch"] = args.branch.strip()
        notes = clean(args.note or "", 3600)
        if notes:
            result["notes"] = notes
        elif not result.get("notes"):
            result["notes"] = "目录模式：人工提交后回填交付结果"

    updated = store.update(
        args.task_id,
        expected_revision=expected,
        mutate=mutate,
        # 复用既有的审计修正类型：这就是「人把任务单里缺的事实补上」，
        # 没必要为它新增一个 schema 枚举值去扩大契约面。
        event_type="audit.correction_recorded",
        summary=args.reason
        or f"回填交付结果：head_sha={head_sha[:12] or '-'} branch={args.branch or '-'}",
        actor=Actor("user", args.actor or "cli"),
        metadata={"head_sha": head_sha, "branch": args.branch},
    )
    return {
        "id": updated["id"],
        "status": updated["status"],
        "revision": updated["revision"],
        "result": {
            "branch": (updated.get("result") or {}).get("branch"),
            "head_sha": (updated.get("result") or {}).get("head_sha"),
        },
    }


def cmd_amend(args, config: Config) -> dict:
    """受控修订失败任务的 requirement、acceptance 与附带契约。"""
    store = TaskStore(config.root, config, push=not args.no_push)
    amendment_path = Path(args.from_file).expanduser().resolve()
    if not amendment_path.is_file():
        raise SchemaError(f"修订文件不存在: {amendment_path}")
    with open(amendment_path, "r", encoding="utf-8") as fh:
        amendment = json.load(fh)
    allowed = {"acceptance", "requirement_replacements", "required_secrets"}
    extras = sorted(set(amendment) - allowed)
    if extras:
        raise SchemaError("修订文件包含未允许字段", errors=extras)
    if "acceptance" not in amendment:
        raise SchemaError("修订文件必须包含 acceptance")
    replacements = amendment.get("requirement_replacements") or []
    if not isinstance(replacements, list):
        raise SchemaError("requirement_replacements 必须是数组")

    contract_src = Path(args.contract).expanduser().resolve() if args.contract else None
    if contract_src is not None and not contract_src.is_file():
        raise SchemaError(f"契约文件不存在: {contract_src}")

    current = store.get(args.task_id)
    expected = args.expected_revision if args.expected_revision is not None else current["revision"]
    if current["revision"] != expected:
        raise ConflictError(
            f"{args.task_id}: revision 冲突（本地期望 {expected}，仓库实际 {current['revision']}）",
            expected=expected,
            actual=current["revision"],
        )

    def apply_content(task: dict) -> None:
        failure = task.get("failure") or {}
        if task.get("status") != "failed" or failure.get("stage") != "verification":
            raise SchemaError("只有 verification 失败的任务允许修订验收契约")
        requirement = task["requirement"]
        for idx, replacement in enumerate(replacements):
            if not isinstance(replacement, dict) or set(replacement) != {"old", "new"}:
                raise SchemaError(f"requirement_replacements[{idx}] 必须且只能包含 old/new")
            old = replacement["old"]
            new = replacement["new"]
            if not isinstance(old, str) or not old or not isinstance(new, str) or not new:
                raise SchemaError(f"requirement_replacements[{idx}] 的 old/new 必须是非空字符串")
            if requirement.count(old) != 1:
                raise SchemaError(f"requirement_replacements[{idx}].old 必须在 requirement 中恰好出现一次")
            requirement = requirement.replace(old, new, 1)
        task["requirement"] = requirement
        previous_requirements = task.pop("required_secrets", []) or []
        task["acceptance"] = amendment["acceptance"]
        # 修订文件同样可能是草稿占位（contracts/<id>/...），落库前换真实路径。
        resolve_id_placeholders(task["id"], task)
        placeholder_names, _registered = _secret_files.requirement_usage(task, config)
        if "required_secrets" in amendment:
            explicit_requirements = amendment.get("required_secrets") or []
        else:
            # 仅保留新 acceptance 仍使用的显式 placeholder 绑定；registered requirements
            # 必须按当前 checks.yaml 从零物化，不能把旧 suite 的需求带进新修订。
            explicit_requirements = [
                item for item in previous_requirements
                if isinstance(item, dict) and item.get("env") in placeholder_names
            ]
        if explicit_requirements:
            task["required_secrets"] = explicit_requirements
        required_secrets = _secret_files.collect_requirements(task, config)
        if required_secrets:
            task["required_secrets"] = required_secrets
        else:
            task.pop("required_secrets", None)

    # 先构造完整候选。新契约暂存到最终路径并保留旧内容；任何校验、预检或
    # store 更新失败都会原样回滚，避免失败修订遗留/覆盖契约文件。
    candidate = copy.deepcopy(current)
    apply_content(candidate)
    contract_dest: Path | None = None
    contract_backup: bytes | None = None
    contract_existed = False
    if contract_src is not None:
        contract_dest = store.contract_dir(candidate["id"]) / contract_src.name
        contract_existed = contract_dest.is_file()
        if contract_existed:
            contract_backup = contract_dest.read_bytes()
        contract_dest.parent.mkdir(parents=True, exist_ok=True)
        if contract_src.resolve() != contract_dest.resolve():
            staging = contract_dest.with_name(f".{contract_dest.name}.{ids.ulid()}.tmp")
            try:
                shutil.copyfile(contract_src, staging)
                os.replace(staging, contract_dest)
            finally:
                if staging.exists():
                    staging.unlink()
        candidate["contract"] = {
            "kind": (candidate.get("contract") or {}).get("kind", "text"),
            "path": str(contract_dest.relative_to(config.root)).replace(os.sep, "/"),
            "sha256": schema.sha256_file(contract_dest),
        }

    try:
        schema.validate_task_full(candidate, config, config.root)
        secret_preflight = notification.preflight_secrets(
            config.root, config.notifications, candidate.get("required_secrets") or []
        )

        def mutate(task: dict) -> None:
            apply_content(task)
            # 清理早期 worker 写入、但当前 schema 已由标准字段替代的别名。
            task["result"].pop("executor", None)
            task["result"].pop("executor_session_id", None)
            if contract_dest is not None:
                task["contract"] = copy.deepcopy(candidate["contract"])

        updated = store.update(
            args.task_id,
            expected_revision=expected,
            mutate=mutate,
            event_type="task.acceptance_amended",
            summary=f"修订验收契约：{args.reason}",
            actor=Actor("user", args.actor or "cli"),
            metadata={"reason": args.reason},
        )
    except Exception:
        if contract_dest is not None:
            if contract_existed and contract_backup is not None:
                contract_dest.write_bytes(contract_backup)
            elif contract_dest.exists():
                contract_dest.unlink()
        raise
    return {
        **_task_brief(updated),
        "contract": updated.get("contract"),
        "checks": [check["id"] for check in updated["acceptance"]["checks"]],
        "secret_preflight": secret_preflight,
    }


def cmd_reverify(args, config: Config) -> dict:
    store = TaskStore(config.root, config, push=not args.no_push)
    task = store.get(args.task_id)
    expected = args.expected_revision if args.expected_revision is not None else task["revision"]
    if task["revision"] != expected:
        raise ConflictError(
            f"{args.task_id}: revision 冲突（本地期望 {expected}，仓库实际 {task['revision']}）",
            expected=expected,
            actual=task["revision"],
        )
    # 所有本地 state/schema 检查先于远端密钥查询，避免非法操作形成 presence oracle。
    state.assert_reverify_transition(task, args.task_id)
    if not task.get("deployment", {}).get("target_sha") or not task["deployment"].get("observed_sha"):
        raise SchemaError(f"{args.task_id}: 缺少 target_sha/observed_sha，不能重新验收")
    candidate = copy.deepcopy(task)
    previous_requirements = candidate.pop("required_secrets", []) or []
    placeholder_names, _registered = _secret_files.requirement_usage(candidate, config)
    retained_bindings = [
        item for item in previous_requirements
        if isinstance(item, dict) and item.get("env") in placeholder_names
    ]
    if retained_bindings:
        candidate["required_secrets"] = retained_bindings
    requirements = _secret_files.collect_requirements(candidate, config)
    if requirements:
        candidate["required_secrets"] = requirements
    else:
        candidate.pop("required_secrets", None)
    schema.validate_task_full(candidate, config, config.root)
    secret_preflight = notification.preflight_secrets(config.root, config.notifications, requirements)
    updated = store.request_reverify(
        args.task_id,
        expected_revision=expected,
        reason=args.reason,
        actor=Actor("user", args.actor or "cli"),
        required_secrets=requirements,
    )
    return {**_task_brief(updated), "secret_preflight": secret_preflight}


def cmd_accept(args, config: Config) -> dict:
    """前端验收成功：verified → frontend_accepted（终态记录，不重发完成通知）。"""
    store = TaskStore(config.root, config, push=not args.no_push)
    task = store.get(args.task_id)
    expected = args.expected_revision if args.expected_revision is not None else task["revision"]
    updated = store.transition(
        args.task_id,
        "frontend_accepted",
        expected_revision=expected,
        summary=args.reason or "前端验收通过",
        actor=Actor("user", args.actor or "cli"),
        event_type="frontend.accepted",
    )
    return _task_brief(updated)


def cmd_claim(args, config: Config) -> dict:
    store = TaskStore(config.root, config, push=not args.no_push)
    task = store.get(args.task_id)
    target = task["target"]
    base_sha = target.get("base_sha")
    if not base_sha:
        base_sha = resolve_remote_ref(config.repo(target["repo"]), target["base_ref"])
    updated = store.claim(
        args.task_id,
        worker_id=args.worker,
        lease_seconds=args.lease_seconds,
        actor=Actor("worker", args.worker),
        base_sha=base_sha,
    )
    return {"id": updated["id"], "attempt": updated["attempt"], "revision": updated["revision"], "previous": task["status"]}


def cmd_release(args, config: Config) -> dict:
    store = TaskStore(config.root, config, push=not args.no_push)
    updated = store.release_lease(
        args.task_id,
        actor=Actor("user", args.actor or "cli"),
        reason=args.reason,
    )
    return _task_brief(updated)


def cmd_expire(args, config: Config) -> dict:
    store = TaskStore(config.root, config, push=not args.no_push)
    return {"expired": store.expire_stale(actor=Actor("user", args.actor or "cli"))}


def cmd_deliver(args, config: Config) -> dict:
    root = config.root
    store = TaskStore(root, config, push=not args.no_push)
    task = store.get(args.task_id)
    task_id = task["id"]

    if task["status"] in ("verified",):
        return {"id": task_id, "result": "already_verified", "checks": task["result"]["checks"]}
    if task["status"] not in ("merged", "deployed", "verifying", "delivered", "awaiting_merge"):
        return {
            "id": task_id,
            "result": "not_ready",
            "status": task["status"],
            "message": "还没交付到可验收状态，等状态到 delivered/merged/deployed 再跑",
        }
    if task["status"] in ("delivered", "awaiting_merge") and (task.get("result") or {}).get("pr_number"):
        return {
            "id": task_id,
            "result": "not_ready",
            "status": task["status"],
            "message": "任务有 PR 尚未合并，先走合并流程（面板「恢复推进」会自动处理）",
        }

    env_name = task["acceptance"]["environment"]
    env = config.env(env_name)
    probe = VersionProbe(_probe_client(config))
    ancestry = CommitAncestry(root, config.repo(task["target"]["repo"]))

    if task["status"] in ("merged", "delivered", "awaiting_merge"):
        # merged：PR 已合并，等环境部署；delivered/awaiting_merge：目录模式没有合并环节，
        # 改动已直接提交在目标分支（head_sha 即目标），同样先对齐部署
        target = (
            task["deployment"].get("target_sha")
            or task["result"].get("merge_sha")
            or task["result"].get("head_sha")
        )
        if not target:
            raise HandoffError("任务缺少 merge_sha/head_sha，无法对齐部署")
        outcome = wait_for_deployment(
            env,
            target,
            config.deploy_poll,
            probe=probe,
            sleep=_sleep,
            logger=_log,
            contains=ancestry.contains,
        )
        task = store.get(task_id)
        if outcome.status != "deployed":
            store.update(
                task_id,
                expected_revision=task["revision"],
                mutate=lambda t: _deployment(t, "timeout" if outcome.status == "timeout" else "failed", outcome, target),
                event_type="deployment.failed",
                summary=f"部署未对齐：{clean(outcome.error or '', 120)}",
                metadata={"target_sha": target, "observed_sha": outcome.observed_sha},
            )
            return {
                "id": task_id,
                "result": "deployment_not_ready",
                "target_sha": target,
                "observed_sha": outcome.observed_sha,
                "error": outcome.error,
                "message": "部署没完成，不是验收通过",
            }
        task = store.get(task_id)
        store.transition(
            task_id,
            "deployed",
            expected_revision=task["revision"],
            summary=f"环境已包含目标 {target[:12]}（当前 {outcome.observed_sha[:12]}）",
            mutate=lambda t: _deployment(t, "deployed", outcome, target),
            metadata={"target_sha": target, "observed_sha": outcome.observed_sha},
        )
        task = store.get(task_id)

    target_sha = (
        task["deployment"].get("target_sha")
        or task["result"].get("merge_sha")
        or task["result"].get("head_sha")
    )
    observed, error = probe.fetch(env)
    previously_proven = (
        task["deployment"].get("status") == "deployed"
        and observed == task["deployment"].get("observed_sha")
    )
    contains_target, relation_error = (False, None)
    if observed:
        contains_target = previously_proven or observed == target_sha
        if not contains_target:
            contains_target, relation_error = ancestry.contains(target_sha, observed)
    if not contains_target:
        task = store.get(task_id)
        detail = error or relation_error or "环境提交不包含目标提交"
        store.update(
            task_id,
            expected_revision=task["revision"],
            mutate=lambda t: _deployment(t, "failed", _Outcome(observed, detail), target_sha),
            event_type="deployment.failed",
            summary="环境版本未包含目标任务，拒绝自动验收",
            metadata={"target_sha": target_sha, "observed_sha": observed},
        )
        return {
            "id": task_id,
            "result": "environment_drift",
            "target_sha": target_sha,
            "observed_sha": observed,
            "message": "环境版本落后、分叉或无法证明包含目标提交；请检查部署或仓库只读权限",
        }

    if task["status"] == "deployed":
        task = store.get(task_id)
        store.transition(
            task_id,
            "verifying",
            expected_revision=task["revision"],
            summary="开始验收",
            metadata={"observed_sha": observed},
        )
        task = store.get(task_id)

    verifier = Verifier(root, config, client=_probe_client(config))
    results = verifier.run(task, observed_sha=observed)
    final_observed, _err = probe.fetch(env)
    drifted = final_observed != observed

    checks = [r.as_dict() for r in results]
    failed = [r for r in results if r.status != "passed"]

    def mutate(t: dict) -> None:
        t["result"]["checks"] = checks
        t["deployment"]["observed_sha"] = final_observed
        t["deployment"]["updated_at"] = iso()

    if drifted:

        def mutate_fail(t: dict) -> None:
            mutate(t)
            t["status"] = "failed"
            t["failure"] = {
                "stage": "verification",
                "message": f"验收期间环境 SHA 变化（{(observed or 'unknown')[:12]} → {(final_observed or 'unknown')[:12]}），结果不可信",
                "check_id": None,
                "observed_sha": final_observed,
                "at": iso(),
            }

        store._write(  # 显式绕过状态白名单：verifying → failed 是合法转换，这里要一并写多个字段
            task_id,
            lambda t: (state.assert_transition(t["status"], "failed", task_id), mutate_fail(t)),
            event_type="verification.completed",
            summary="环境漂移，验收结果不采信",
            metadata={"target_sha": target_sha, "observed_sha": final_observed},
        )
        return {
            "id": task_id,
            "result": "environment_drift",
            "summary": summarize(results),
            "checks": checks,
            "message": "验收过程中环境前后不一致，不写 verified",
        }

    if failed:
        store._write(
            task_id,
            lambda t: (
                state.assert_transition(t["status"], "failed", task_id),
                mutate(t),
                t.__setitem__(
                    "failure",
                    {
                        "stage": "verification",
                        "message": clean(failed[0].output or failed[0].id, 2000),
                        "check_id": failed[0].id,
                        "observed_sha": final_observed,
                        "at": iso(),
                    },
                ),
                t.__setitem__("status", "failed"),
            ),
            event_type="verification.completed",
            summary=f"验收失败：{failed[0].id}",
            metadata={"failed_checks": ",".join(r.id for r in failed)},
        )
        return {
            "id": task_id,
            "result": "failed",
            "summary": summarize(results),
            "checks": checks,
            "observed_sha": final_observed,
        }

    written = store._write(
        task_id,
        lambda t: (
            state.assert_transition(t["status"], "verified", task_id),
            mutate(t),
            t.__setitem__("status", "verified"),
        ),
        event_type="verification.completed",
        summary=f"{len(results)} 项检查全部通过",
        metadata={"observed_sha": final_observed},
    )
    # 把完成通知入队结果暴露给调用方（面板/CLI），不再静默丢弃。
    notification_result = written.get("_completion_notification")
    result = {"id": task_id, "result": "verified", "summary": summarize(results), "checks": checks}
    if notification_result is not None:
        result["completion_notification"] = notification_result
    return result


class _Outcome:
    def __init__(self, observed, error):
        self.observed_sha = observed
        self.error = error


def _deployment(task: dict, status: str, outcome, target_sha: str | None = None) -> None:
    deployment = task["deployment"]
    deployment["status"] = status
    # schema 要求 deployed 及之后必须记录 target_sha 与 observed_sha；
    # 只写 observed 会让 deployed transition 在校验处挂掉
    if target_sha:
        deployment["target_sha"] = target_sha
    deployment["observed_sha"] = outcome.observed_sha
    deployment["updated_at"] = iso()
    deployment["error"] = clean(outcome.error or "", 2000) if status != "deployed" else None


def _probe_client(config: Config):
    from .http_client import HttpClient

    return HttpClient(
        timeout=config.deploy_poll.request_timeout_seconds,
        deny_cidrs=config.checks.verifier.deny_cidrs,
        allow_private=getattr(config, "allow_private_http", False),
    )


def current_agent_session() -> tuple[str, str]:
    """当前前端会话的 (执行器, 会话 id)，用于把完成事件排回原会话。

    codex 设 CODEX_THREAD_ID；dsh 设 DSH_SESSION_ID（形如 session-<uuid>）。
    都没有就返回 ("", "")——调用方自己决定是否要报错。
    """
    codex = (os.environ.get("CODEX_THREAD_ID") or "").strip()
    if codex:
        return "codex", codex
    dsh = (os.environ.get("DSH_SESSION_ID") or "").strip()
    if dsh:
        return "dsh", dsh
    return "", ""


def _frontend_executor(explicit: str | None, session_id: str) -> str:
    """--executor 明确给了就用，否则按会话 id 的形状推断。"""
    if explicit:
        return explicit
    return "dsh" if session_id.startswith("session-") else "codex"


_sleep = time.sleep
_log = lambda msg: print(msg, file=sys.stderr, flush=True)  # noqa: E731


def cmd_worker(args, config: Config) -> dict:
    from .worker import Worker, WorkerOptions

    listen = _parse_listen(args.listen, config) if args.listen else None
    if args.once and listen:
        raise ConfigError("--once 与 --listen 不能同时使用")
    poll_interval = args.poll_interval
    if poll_interval is None:
        poll_interval = 0 if listen else 15
    if poll_interval < 0:
        raise ConfigError("--poll-interval 不能为负数")

    options = WorkerOptions(
        worker_id=args.worker,
        lease_seconds=args.lease_seconds,
        codex_timeout_seconds=args.codex_timeout,
        auto_merge=args.auto_merge,
        auto_commit=args.auto_commit,
        wait_deployment=args.wait_deployment,
        poll_interval_seconds=poll_interval,
        target_role=args.role,
        keep_worktree=args.keep_worktree,
    )
    codex = CodexRunner(
        binary=args.codex_bin,
        timeout_seconds=args.codex_timeout,
        log_dir=config.root / ".handoff-run" / "logs",
        model=args.codex_model or config.codex.model,
        persist=config.codex.persist_thread,
        sandbox=config.codex.sandbox,
        # 密钥从配置的文件读入注入 agent 进程；不依赖启动 worker 的终端环境
        env_extra={**(config.codex.env or {}), **_secret_files.agent_env(config)},
    )
    # host 不在这里定：由 worker 按目标仓库的 config host 现场构造（github / local / null）
    wake_event = threading.Event() if listen else None
    worker = Worker(config.root, config, options, codex=codex, wake_event=wake_event)
    if args.once:
        processed = worker.run_once()
        return {"processed": processed, "worker": args.worker}
    wake_server = None
    if listen:
        wake_server = notification.WakeServer(config.root, config.notifications, wake_event)
        host, port = wake_server.start(*listen)
        print(f"局域网通知监听 http://{host}:{port}{notification.WAKE_PATH}", file=sys.stderr, flush=True)
    try:
        worker.run_forever()
    finally:
        if wake_server is not None:
            wake_server.stop()
    return {"worker": args.worker, "status": "stopped"}


def _parse_listen(value: str, config: Config) -> tuple[str, int]:
    text = value.strip()
    if text == "config":
        return config.notifications.listen_host, config.notifications.listen_port
    if ":" not in text:
        raise ConfigError("--listen 格式应为 HOST:PORT，或写 config")
    host, raw_port = text.rsplit(":", 1)
    host = host.strip("[]")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ConfigError("--listen 端口不是整数") from exc
    return host, port


def cmd_notifier(args, config: Config) -> dict:
    notifier = notification.Notifier(config.root, config.notifications)
    if args.once:
        return notifier.send_due(limit=args.limit)
    notifier.run_forever(interval_seconds=args.interval, logger=lambda msg: print(msg, file=sys.stderr, flush=True))
    return {"status": "stopped"}


def cmd_resend_completed(args, config: Config) -> dict:
    """手动把某个任务的完成通知再发一次到前端监听端口。"""
    store = TaskStore(config.root, config, push=False)
    task = store.get(args.task_id)
    return notification.replay_task_completed(
        config.root,
        config.notifications,
        task,
        dry_run=args.dry_run,
        store_commit=store.task_commit(args.task_id),
    )


def cmd_reconcile_completed(args, config: Config) -> dict:
    """对账：给漏发完成通知的终态任务补发 task.completed。

    默认只报告不投递——历史积压的任务可能有一堆，一次性把它们翻出来重发会
    给前端灌一串迟到的完成事件。确认清单无误后再加 `--apply` 真正补发。

    `--task-id` 只查一个任务；否则扫描全部 verified/failed 任务。
    """
    store = TaskStore(config.root, config, push=False)
    if args.task_id:
        # 只读对账不走 sync：它会 reset --hard，工作区有未提交改动时直接报错，
        # 而「补发漏掉的通知」不该被本地的无关改动挡住。
        tasks = [store.peek(args.task_id)]
    else:
        tasks = store.peek_tasks(status=["verified", "failed"])
    return notification.reconcile_completed(
        config.root,
        config.notifications,
        tasks,
        store.task_commit,
        dry_run=not args.apply,
    )


def cmd_register_session(args, config: Config) -> dict:
    """显式登记任务应恢复到哪个前端会话。"""
    TaskStore(config.root, config, push=False).get(args.task_id)
    detected_executor, detected_session = current_agent_session()
    thread_id = (args.thread or detected_session).strip()
    if not thread_id:
        raise SchemaError("缺少 --thread，且当前环境没有 CODEX_THREAD_ID / DSH_SESSION_ID")
    executor = _frontend_executor(args.executor or detected_executor, thread_id)
    workspace = args.workspace or str(Path.cwd())
    FrontendDispatcher(
        config.root,
        config.notifications,
        codex_binary=config.codex.binary,
    ).register(
        args.task_id,
        thread_id=thread_id,
        workspace=workspace,
        executor=executor,
    )
    return {
        "task_id": args.task_id,
        "executor": executor,
        "thread_id": thread_id,
        "workspace": str(Path(workspace).expanduser().resolve()),
        "status": "registered",
    }


def cmd_frontend_listener(args, config: Config) -> dict:
    """接收完成事件并把后续工作排回创建任务的原前端会话。"""
    dispatcher = FrontendDispatcher(
        config.root,
        config.notifications,
        codex_binary=config.codex.binary,
    )
    if args.once:
        return dispatcher.send_due(limit=args.limit)

    listen = _parse_listen(args.listen, config)
    server = notification.WakeServer(
        config.root,
        config.notifications,
        on_event=dispatcher.enqueue,
    )
    host, port = server.start(*listen)
    print(
        f"前端完成通知监听 http://{host}:{port}{notification.COMPLETE_PATH}",
        file=sys.stderr,
        flush=True,
    )
    try:
        while True:
            result = dispatcher.send_due(limit=args.limit)
            if result["sent"] or result["failed"]:
                print(json.dumps(result, ensure_ascii=False), file=sys.stderr, flush=True)
            time.sleep(max(0.1, args.interval))
    finally:
        server.stop()
    return {"status": "stopped"}


def cmd_frontend_completions(args, config: Config) -> dict:
    """查询 task.completed 的本地接收与派发历史。"""
    sessions = FrontendSessions(config.notifications.session_path(config.root))
    return sessions.completion_history(
        task_id=args.task_id,
        dispatch_state=args.state,
        limit=args.limit,
    )


def cmd_admin(args, config: Config) -> dict:
    from .admin import indexer, server

    if args.action == "index":
        idx = indexer.Indexer(config)
        if args.rebuild:
            return idx.rebuild()
        return idx.index_incremental()
    if args.action == "serve":
        server.serve(config, host=args.host, port=args.port)
        return {"status": "stopped"}
    raise ConfigError(f"未知 admin 动作: {args.action}")


_EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def _open_panel_window(url: str) -> bool:
    """用 Edge 应用窗口打开面板（无地址栏独立窗口）；失败退回默认浏览器。"""
    if os.name == "nt":
        for candidate in _EDGE_PATHS:
            if Path(candidate).is_file():
                subprocess.Popen([candidate, f"--app={url}"], close_fds=True)
                return True
    import webbrowser

    return webbrowser.open(url)


def cmd_panel(args, config: Config) -> dict:
    from .panel.server import PanelServer
    from .panel.service import PanelService

    role = args.role
    if role == "frontend" and args.mode:
        raise ConfigError("前端面板不支持 --mode；执行模式只属于后端面板")
    service = PanelService(config.root, config, role=role)
    if args.mode:
        service.set_mode({"mode": args.mode})
    host = args.host or config.panel.host
    port = args.port if args.port is not None else config.panel.port
    server = PanelServer(service, host=host, port=port, role=role)
    actual_host, actual_port = server.start()
    url = f"http://{actual_host}:{actual_port}/"
    if args.mode == "auto":
        service.set_mode({"mode": "auto"})  # 立即启动自动循环与唤醒监听
    opened = False
    if not args.no_open and config.panel.open_browser:
        opened = _open_panel_window(url)
    print(f"面板已启动: {url}" + (f"（{'已开窗口' if opened else '请自行打开浏览器'}）" if not args.no_open else ""), file=sys.stderr, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("正在停止面板…", file=sys.stderr, flush=True)
    finally:
        service.shutdown()
        server.stop()
    return {"status": "stopped"}


def cmd_events(args, config: Config) -> dict:
    directory = config.root / config.store.events_dir / args.task_id
    events = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            # 跳过 _atomic_write 被硬杀时残留的 .tmp-*.json，避免同一事件重复出现。
            if path.name.startswith("."):
                continue
            with open(path, "r", encoding="utf-8") as fh:
                events.append(json.load(fh))
    return {"task_id": args.task_id, "count": len(events), "events": events}


def cmd_state_machine(args, config: Config) -> dict:
    return state.describe_all()

def cmd_session(args, config: Config) -> dict:
    """把任务背后的执行器会话交出来：session id、worktree、resume 命令。"""
    store = TaskStore(config.root, config, push=False)
    task = store.get(args.task_id)
    result = task["result"]
    executor = result.get("executor") or "codex"
    thread = result.get("executor_session_id") or result.get("codex_thread_id")
    worktree = result.get("worktree")
    worktree_exists = bool(worktree) and Path(worktree).is_dir()

    resume = None
    resume_exec = None
    if thread and worktree_exists:
        if executor == "zcode":
            resume = f"cd {worktree} && zcode -r {thread}"
            resume_exec = f"zcode -p --resume {thread} --output-format json"
        else:
            resume = f"cd {worktree} && codex resume {thread}"
            resume_exec = f"codex exec resume {thread} -C {worktree} -"
    elif thread:
        if executor == "zcode":
            resume = f"zcode -r {thread}"
        else:
            # cwd 不在了也能按 id 找，但 Codex 会回到记录的 cwd
            resume = f"codex resume --all {thread}"

    return {
        "id": task["id"],
        "status": task["status"],
        "attempt": task["attempt"],
        "executor": executor,
        "thread_id": thread,
        "worktree": worktree,
        "worktree_exists": worktree_exists,
        "resume": resume,
        "resume_exec": resume_exec,
        "log_dir": str(config.root / ".handoff-run" / "logs" / task["id"]),
        "pr": result.get("pr_url"),
        "merge_sha": result.get("merge_sha"),
    }


def cmd_notify(args, config: Config) -> dict:
    """把一句话推进已有的 Codex 会话（codex queue）。

    需要目标会话跑在同一台机器的 app-server 上；跨机器用 codex queue --remote。
    """
    message = args.message.strip()
    if not message:
        raise SchemaError("--message 不能为空")
    codex = executil.require("codex", env_var="HANDOFF_CODEX")
    argv = [codex, "queue", "--thread", args.thread, "--message", message]
    if args.dry_run:
        return {"thread": args.thread, "command": " ".join(argv), "sent": False}
    result = run(argv, timeout=60, check_env=False)
    if not result.ok:
        raise ExecutionError(
            "推送消息失败（会话可能不在本机 app-server 上，或已结束）",
            stderr=clean(result.stderr, 800),
        )
    return {"thread": args.thread, "sent": True}


def cmd_clean_worktrees(args, config: Config) -> dict:
    """清理已经不需要 resume 的 worktree。默认只列不动手。"""
    store = TaskStore(config.root, config, push=False)
    statuses = {t["id"]: t["status"] for t in store.list_tasks()}
    root = config.root / ".handoff-run" / "worktrees"
    dead_status = {"verified", "frontend_accepted", "cancelled"}
    candidates: list[Path] = []
    if not root.is_dir():
        return {"candidates": [], "removed": [], "applied": False}
    for task_dir in sorted(root.iterdir()):
        if not task_dir.is_dir():
            continue
        status = statuses.get(task_dir.name)
        forced = args.task == task_dir.name
        if forced or status is None or status in dead_status:
            for attempt in sorted(task_dir.iterdir()):
                if attempt.is_dir():
                    candidates.append(attempt)

    if args.task:
        candidates = [c for c in candidates if c.parent.name == args.task]

    removed: list[str] = []
    if args.apply:
        for path in candidates:
            caches = sorted((config.root / ".handoff-run" / "repo-cache").glob("*"))
            done = False
            for cache in caches:
                if not (cache / ".git").exists():
                    continue
                res = git(cache, "worktree", "remove", "--force", str(path))
                if res.ok:
                    removed.append(str(path))
                    done = True
                    break
                git(cache, "worktree", "prune")
                if not path.exists():
                    removed.append(str(path))
                    done = True
                    break
            if not done:
                removed.append(f"{path}（未找到所属 clone，需手工处理）")

    return {
        "candidates": [str(c) for c in candidates],
        "removed": removed,
        "applied": bool(args.apply),
        "hint": "加 --apply 才真的删除；这些目录里有 codex 会话的 cwd",
    }


def cmd_doctor(args, config: Config) -> dict:
    role = None if args.role == "all" else args.role
    checks = doctor.run(config, role=role)
    summary = doctor.summarize(checks)
    code = 0 if summary["ok"] else 1
    if getattr(args, "json", False):
        return {**summary, "_exit": code}
    print(doctor.render(checks))
    return {"_exit": code, "_silent": True}


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    # default=SUPPRESS：子命令未显式给出时不要覆盖主命令已解析的值
    common.add_argument("--root", default=argparse.SUPPRESS, help="交接仓根目录（默认向上查找 config/repositories.yaml）")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="输出 JSON")

    parser = argparse.ArgumentParser(prog="handoffctl", description="Handoff Engine CLI", parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    def _sub(name: str, **kwargs):
        # 让 --root/--json 在子命令前后都能用
        return sub.add_parser(name, parents=[common], **kwargs)

    p = _sub("create", help="创建任务单")
    p.add_argument("--from-file", required=True, help="任务草稿 JSON")
    p.add_argument("--source-path", help="前端仓库本地路径，用于读取 HEAD 并识别仓库")
    p.add_argument("--source-repo", help="显式指定 source.repo（默认从 --source-path 的 origin 识别）")
    p.add_argument("--target", help="显式指定目标后端仓库 id")
    p.add_argument("--environment", help="显式指定验收环境名")
    p.add_argument("--contract")
    p.add_argument("--contract-kind", default="openapi", choices=["openapi", "jsonschema", "graphql", "typescript", "text"])
    p.add_argument("--attachment", action="append")
    p.add_argument(
        "--source-thread",
        help="完成后要恢复的前端会话 id（默认 CODEX_THREAD_ID 或 DSH_SESSION_ID）",
    )
    p.add_argument(
        "--source-executor",
        choices=["codex", "dsh"],
        help="前端会话类型（默认从环境变量或 id 形状推断）",
    )
    p.add_argument("--source-workspace", help="恢复原会话时使用的前端工作区（默认 --source-path 或当前目录）")
    p.add_argument(
        "--no-session-register",
        action="store_true",
        help="已禁用；创建任务必须登记原前端会话",
    )
    p.add_argument("--no-push", action="store_true")
    p.add_argument(
        "--provision-secrets",
        action="store_true",
        help="创建前把本机验收密钥经已签名通道加密推给后端自动写入（需后端开启 provision_enabled）",
    )
    p.set_defaults(func=cmd_create)

    p = _sub("show", help="查看任务")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_show)

    p = _sub("list", help="列出任务")
    p.add_argument("--status", action="append")
    p.set_defaults(func=cmd_list)

    p = _sub("delete", help="永久删除任务及其事件、契约和附件")
    p.add_argument("task_ids", nargs="*")
    p.add_argument("--all", action="store_true", help="删除当前交接仓中的全部任务")
    p.add_argument("--yes", action="store_true", help="确认执行删除")
    p.add_argument("--no-push", action="store_true")
    p.set_defaults(func=cmd_delete)

    p = _sub("validate", help="校验所有任务单")
    p.set_defaults(func=cmd_validate)

    p = _sub("transition", help="手动状态转换（受白名单约束）")
    p.add_argument("task_id")
    p.add_argument("--to", required=True, choices=list(state.STATUSES))
    p.add_argument("--expected-revision", type=int)
    p.add_argument("--reason")
    p.add_argument("--actor", default="cli")
    p.add_argument("--no-push", action="store_true")
    p.set_defaults(func=cmd_transition)

    p = _sub("record-result", help="回填目录模式人工提交后的 head_sha / branch")
    p.add_argument("task_id")
    p.add_argument("--head-sha", help="已推送的提交（40 位十六进制）")
    p.add_argument("--branch", help="提交所在分支（如 develop）")
    p.add_argument("--note", help="写进 result.notes 的说明")
    p.add_argument("--reason", help="事件摘要")
    p.add_argument("--expected-revision", type=int)
    p.add_argument("--actor", default="cli")
    p.add_argument("--no-push", action="store_true")
    p.set_defaults(func=cmd_record_result)

    p = _sub("amend", help="受控修订 verification 失败任务的验收契约")
    p.add_argument("task_id")
    p.add_argument("--from-file", required=True, help="包含 acceptance 和 requirement_replacements 的 JSON")
    p.add_argument("--contract", help="替换后的契约文件")
    p.add_argument("--reason", required=True)
    p.add_argument("--expected-revision", type=int)
    p.add_argument("--actor", default="cli")
    p.add_argument("--no-push", action="store_true")
    p.set_defaults(func=cmd_amend)

    p = _sub("reverify", help="修正验收契约后重新验收，不增加 attempt")
    p.add_argument("task_id")
    p.add_argument("--reason", required=True)
    p.add_argument("--expected-revision", type=int)
    p.add_argument("--actor", default="cli")
    p.add_argument("--no-push", action="store_true")
    p.set_defaults(func=cmd_reverify)

    p = _sub("accept", help="前端验收通过：标记为 frontend_accepted（终态）")
    p.add_argument("task_id")
    p.add_argument("--reason", default="前端验收通过")
    p.add_argument("--expected-revision", type=int)
    p.add_argument("--actor", default="cli")
    p.add_argument("--no-push", action="store_true")
    p.set_defaults(func=cmd_accept)

    p = _sub("claim", help="认领任务（调试用）")
    p.add_argument("task_id")
    p.add_argument("--worker", required=True)
    p.add_argument("--lease-seconds", type=int, default=3600)
    p.add_argument("--no-push", action="store_true")
    p.set_defaults(func=cmd_claim)

    p = _sub("release", help="释放租约")
    p.add_argument("task_id")
    p.add_argument("--reason", required=True)
    p.add_argument("--actor", default="cli")
    p.add_argument("--no-push", action="store_true")
    p.set_defaults(func=cmd_release)

    p = _sub("expire-leases", help="把过期租约的任务重新入队")
    p.add_argument("--actor", default="cli")
    p.add_argument("--no-push", action="store_true")
    p.set_defaults(func=cmd_expire)

    p = _sub("deliver", help="等部署对齐并跑验收")
    p.add_argument("task_id")
    p.add_argument("--no-push", action="store_true")
    p.set_defaults(func=cmd_deliver)

    p = _sub("worker", help="启动 worker")
    p.add_argument("--worker", required=True)
    p.add_argument("--role", default="backend", choices=["backend", "frontend"])
    p.add_argument("--lease-seconds", type=int, default=3600)
    p.add_argument("--codex-timeout", type=float, default=3600)
    p.add_argument("--codex-bin", default="codex")
    p.add_argument("--codex-model")
    p.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        help="无监听时默认 15 秒；启用 --listen 时默认 0（只由通知唤醒）",
    )
    p.add_argument("--listen", help="监听局域网唤醒，格式 HOST:PORT；写 config 使用 local.yaml 配置")
    p.add_argument("--auto-merge", action="store_true")
    p.add_argument("--wait-deployment", action="store_true")
    p.add_argument(
        "--auto-commit",
        action="store_true",
        help="direct 模式执行完自动提交并推送当前分支（提交信息由执行器的 commit_message 决定）",
    )
    p.add_argument("--keep-worktree", action="store_true")
    p.add_argument("--once", action="store_true")
    p.set_defaults(func=cmd_worker)

    p = _sub("notifier", help="重试发送本机 outbox 中的正向或回程局域网通知")
    p.add_argument("--once", action="store_true", help="只发送一轮后退出")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--interval", type=float, default=1)
    p.set_defaults(func=cmd_notifier)

    p = _sub("resend-completed", help="手动再发一次 task.completed 到创建任务的前端监听端口")
    p.add_argument("task_id")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只显示反查到的目标地址与事件内容，不实际投递",
    )
    p.set_defaults(func=cmd_resend_completed)

    p = _sub("reconcile-completed", help="对账并补发漏发的 task.completed（终态任务自愈）")
    p.add_argument("--task-id", help="只检查指定任务（默认扫描全部 verified/failed）")
    p.add_argument(
        "--apply",
        action="store_true",
        help="真正补发；默认只列出缺通知的任务与目标地址，不投递",
    )
    p.set_defaults(func=cmd_reconcile_completed)

    p = _sub("register-session", help="登记任务完成后应恢复的原前端会话")
    p.add_argument("task_id")
    p.add_argument("--thread", help="前端会话 id（默认 CODEX_THREAD_ID 或 DSH_SESSION_ID）")
    p.add_argument("--executor", choices=["codex", "dsh"], help="前端类型（默认从环境变量或 id 形状推断）")
    p.add_argument("--workspace", help="会话恢复后的工作区（默认当前目录）")
    p.set_defaults(func=cmd_register_session)

    p = _sub("frontend-listener", help="监听后端完成事件并恢复原前端会话（Codex 或 DSH）")
    p.add_argument("--listen", default="config", help="HOST:PORT；默认使用 local.yaml notifications 配置")
    p.add_argument("--once", action="store_true", help="只重试本地待派发事件，不启动监听")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--interval", type=float, default=1)
    p.set_defaults(func=cmd_frontend_listener)

    p = _sub("frontend-completions", help="查看 task.completed 的本地接收与派发历史")
    p.add_argument("--task-id", help="只查看指定任务")
    p.add_argument(
        "--state",
        default="all",
        choices=["all", "pending", "failed", "dispatched"],
        help="按派发状态过滤（默认 all）",
    )
    p.add_argument("--limit", type=int, default=50, help="最多返回多少条记录（默认 50）")
    p.set_defaults(func=cmd_frontend_completions)

    p = _sub("events", help="查看任务事件时间线")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_events)

    p = _sub("state-machine", help="打印状态转换白名单")
    p.set_defaults(func=cmd_state_machine)

    p = _sub("doctor", help="检查配置与工具链，列出还缺什么")
    p.add_argument("--role", default="all", choices=["all", "frontend", "backend"], help="只检查指定角色需要访问的仓库与检查命令")
    p.set_defaults(func=cmd_doctor)

    p = _sub("session", help="查看任务背后的 Codex 会话与 resume 命令")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_session)

    p = _sub("notify", help="把一句话推进已有的 Codex 会话（codex queue）")
    p.add_argument("--thread", required=True)
    p.add_argument("--message", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_notify)

    p = _sub("clean-worktrees", help="清理已结束任务的 worktree（默认只列）")
    p.add_argument("--task")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_clean_worktrees)

    p = _sub("admin", help="后台索引与查询 API")
    p.add_argument("action", choices=["index", "serve"])
    p.add_argument("--rebuild", action="store_true")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_admin)

    p = _sub("panel", help="启动桌面面板（可选前端发单/验收或后端执行面板）")
    p.add_argument("--host", help="默认 127.0.0.1；只允许回环地址")
    p.add_argument("--port", type=int, help="默认 8790（8789 留给 frontend-listener）")
    p.add_argument("--no-open", action="store_true", help="不自动开窗口，自己用浏览器打开")
    p.add_argument("--role", choices=["frontend", "backend"], default="backend", help="面板职责；DSH 插件固定使用 frontend")
    p.add_argument("--mode", choices=["manual", "auto"], help="后端面板启动模式（默认沿用持久化状态）")
    p.set_defaults(func=cmd_panel)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        root_arg = getattr(args, "root", None)
        root = Path(root_arg).resolve() if root_arg else find_root()
        config = Config.load(root)
        result = args.func(args, config)
    except HandoffError as exc:
        payload = {"ok": False, **exc.as_dict()}
        if getattr(args, "json", False):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"错误[{exc.code}]: {exc.message}", file=sys.stderr)
            for err in getattr(exc, "errors", []) or []:
                print(f"  - {err}", file=sys.stderr)
        return exc.exit_code
    out = dict(result) if isinstance(result, dict) else {"result": result}
    code = int(out.pop("_exit", 0))
    silent = bool(out.pop("_silent", False))
    payload = {"ok": code == 0, **out}
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif not silent:
        _print_human(out)
    return code


def _print_human(result) -> None:
    if isinstance(result, dict):
        for key, value in result.items():
            if isinstance(value, (dict, list)):
                print(f"{key}: {json.dumps(value, ensure_ascii=False, indent=2)}")
            else:
                print(f"{key}: {value}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
