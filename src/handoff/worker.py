"""worker：认领 → 隔离 worktree → codex → 确定性 gate → 提交 → 推送 → PR。

worker 不部署、不写 dev/main、不合并主干（除非显式打开低风险自动合并）。
"""

from __future__ import annotations

import threading
import time
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import merge_gate, secrets as secret_files, state
from .codex_runner import CodexRunner, build_prompt
from .config import Config
from .deploy import CommitAncestry, wait_for_deployment
from .errors import ConflictError, GateError, HandoffError, LeaseError, SchemaError, TaskNotFound
from .git_host import get_host
from .gitcmd import resolve_remote_ref
from .pathgate import evaluate as path_evaluate
from .sanitize import clean, redact
from .store import Actor, TaskStore
from .timeutil import iso

# 契约原文进 prompt 的硬上限：正常契约几万字符，这里只是防止病态大文件
_CONTRACT_HARD_LIMIT = 400_000
from .workspace import Workspace


@dataclass
class WorkerOptions:
    worker_id: str
    lease_seconds: int = 3600
    codex_timeout_seconds: float = 3600
    auto_merge: bool = False
    wait_deployment: bool = False
    merge_timeout_seconds: float = 1800
    heartbeat_interval_seconds: float = 60
    lease_renew_interval_seconds: float = 900
    poll_interval_seconds: float = 15
    target_role: str = "backend"
    keep_worktree: bool = False
    # 面板指定的「项目目录」：给了就跳过隔离 worktree，直接在该目录干活
    workdir: str | None = None
    # 续跑时复用上一轮的 worktree，避免"agent 记得改过、文件却是原始的"
    reuse_worktree: str | None = None
    # direct 模式执行完自动提交并推送到当前分支；提交信息用执行器写的 commit_message
    auto_commit: bool = False


class Worker:
    def __init__(
        self,
        root: str | Path,
        config: Config,
        options: WorkerOptions,
        *,
        store: TaskStore | None = None,
        codex: CodexRunner | None = None,
        host=None,
        logger: Callable[[str], None] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        wake_event: threading.Event | None = None,
        cancel_event: threading.Event | None = None,
        prompt_note: str | None = None,
    ):
        self.root = Path(root)
        self.config = config
        self.options = options
        self.log = logger or (lambda msg: print(msg, flush=True))
        self.sleeper = sleeper
        self.wake_event = wake_event
        # 面板等外部调用方的取消信号：置位后传导给执行器子进程（杀进程树）
        self.external_cancel = cancel_event
        # 面板干预：追加到 prompt 末尾的额外指令（打断后带话续跑用）
        self.prompt_note = prompt_note
        self.store = store or TaskStore(
            self.root,
            config,
            actor=Actor("worker", options.worker_id),
            correlation_id=None,
        )
        self.codex = codex or CodexRunner(
            timeout_seconds=options.codex_timeout_seconds,
            log_dir=self.root / ".handoff-run" / "logs",
            # 密钥从配置的文件读入注入 agent 进程；默认 worker 用 codex 配置里的 env
            env_extra={**(config.codex.env or {}), **secret_files.agent_env(config)},
        )
        self.host = host  # None 时按仓库配置的 host 现场构造
        self._invalid_tasks: dict[str, str] = {}

    @property
    def executor_name(self) -> str:
        """执行器名（codex / zcode / …），决定分支前缀与审计元数据。"""
        return getattr(self.codex, "name", "codex")

    # -- 主循环 ------------------------------------------------------------
    def run_forever(self) -> None:
        if self.wake_event is not None and self.options.poll_interval_seconds <= 0:
            mode = "局域网通知唤醒（不轮询）"
        elif self.wake_event is not None:
            mode = f"局域网通知唤醒 + {self.options.poll_interval_seconds}s 兜底轮询"
        else:
            mode = f"轮询间隔 {self.options.poll_interval_seconds}s"
        self.log(f"worker {self.options.worker_id} 启动，{mode}")
        while True:
            # 先清旧信号再同步任务仓。同步期间到达的新信号会保持 set，避免在
            # run_once 返回空之后、进入 wait 之前被清掉而永久丢失。
            if self.wake_event is not None:
                self.wake_event.clear()
            processed = None
            try:
                processed = self.run_once()
            except HandoffError as exc:
                self.log(f"本轮出错：{exc.code}: {exc.message}")
            except Exception as exc:  # noqa: BLE001 - 常驻进程不能因为单次异常退出
                self.log(f"未预期错误：{type(exc).__name__}: {exc}")
            if processed is None:
                if self.wake_event is not None:
                    timeout = self.options.poll_interval_seconds if self.options.poll_interval_seconds > 0 else None
                    self.wake_event.wait(timeout=timeout)
                else:
                    self.sleeper(self.options.poll_interval_seconds)

    def run_once(self) -> str | None:
        self.store.sync()
        # 崩溃的 worker 留下的过期租约先回收，否则任务永远卡在 claimed/in_progress
        expired = self.store.expire_stale(actor=Actor("system", f"{self.options.worker_id}-sweep"))
        if expired:
            self.log(f"回收过期租约：{', '.join(expired)}")
        candidates = [
            t
            for t in self.store.list_tasks(status="open", sync=False)
            if self._is_for_me(t)
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda t: t["created_at"])
        for task in candidates:
            task_id = task["id"]
            try:
                # 上游漏替换的 contracts/<id>/ 占位先自愈，再校验（幂等，无占位不写）
                task = self._self_heal_placeholders(task)
                errs = self._validation_errors(task)
                if errs:
                    # 非法任务无法通过写入层的同一套校验；不能尝试写失败再把本轮
                    # 当作成功处理，否则通知模式会一直重入，且阻塞后续合法任务。
                    signature = hashlib.sha256(json.dumps(
                        [task, errs], ensure_ascii=False, sort_keys=True,
                    ).encode("utf-8")).hexdigest()
                    if self._invalid_tasks.get(task_id) != signature:
                        self.log(f"{task_id}: 任务校验失败，跳过并等待修正：" + clean("; ".join(errs), 4000))
                        self._invalid_tasks[task_id] = signature
                    continue
                self._invalid_tasks.pop(task_id, None)
                target = task["target"]
                base_sha = target.get("base_sha")
                if not base_sha:
                    try:
                        base_sha = resolve_remote_ref(
                            self.config.repo(target["repo"]),
                            target["base_ref"],
                        )
                    except HandoffError as exc:
                        self._fail(task_id, "workspace", exc.message)
                        return task_id
                claimed = self.store.claim(
                    task_id,
                    worker_id=self.options.worker_id,
                    lease_seconds=self.options.lease_seconds,
                    base_sha=base_sha,
                )
            except (ConflictError, LeaseError, SchemaError, TaskNotFound) as exc:
                self.log(f"{task_id}: 认领失败，跳过（{exc.message}）")
                continue
            self.log(f"{task_id}: 已认领（attempt {claimed['attempt']}）")
            self._process(claimed)
            return task_id
        return None

    def claim_and_process(self, task_id: str) -> dict:
        """认领指定任务并执行完整流程。面板手动模式的入口。

        与 run_once 的差别：只认领指定任务；校验失败直接抛错（不写 failed），
        让调用方决定怎么处理。
        """
        self.store.sync()
        task = next((t for t in self.store.list_tasks(sync=False) if t["id"] == task_id), None)
        if task is None:
            raise TaskNotFound(f"任务不存在: {task_id}", task_id=task_id)
        if not self._is_for_me(task):
            raise SchemaError(
                f"任务目标仓库的角色不是本 worker 的 target_role={self.options.target_role}: {task_id}"
            )
        # 上游漏替换的 contracts/<id>/ 占位先自愈，再校验（幂等，无占位不写）
        task = self._self_heal_placeholders(task)
        errs = self._validation_errors(task)
        if errs:
            raise SchemaError("任务单校验失败: " + "; ".join(errs[:10]), errors=errs)
        target = task["target"]
        base_sha = target.get("base_sha") or resolve_remote_ref(
            self.config.repo(target["repo"]),
            target["base_ref"],
        )
        claimed = self.store.claim(
            task_id,
            worker_id=self.options.worker_id,
            lease_seconds=self.options.lease_seconds,
            base_sha=base_sha,
        )
        self.log(f"{task_id}: 已认领（attempt {claimed['attempt']}，executor={self.executor_name}）")
        self._process(claimed)
        return claimed

    def _is_for_me(self, task: dict) -> bool:
        repo = task.get("target", {}).get("repo")
        registered = self.config.repositories.get(repo)
        if registered is None:
            return False
        return registered.role == self.options.target_role

    def _self_heal_placeholders(self, task: dict) -> dict:
        """任务单落库前规范化的兜底自愈并落库。

        上游（前端 handoff）生成任务单时两类遗漏：
        1. 契约路径只换了顶层 contract.path、openapi_diff 的 expected_path 仍是
           `contracts/<id>/...` → worker 认领时 schema 校验报「仍含 <id> 占位」。
        2. openapi_diff 的 actual_url 写成裸 `{base_url}/openapi.json`（404），
           应为带 apikey 的 `/docs/openapi.json?apikey={env:DOCS_API_KEY}`。
        这里在校验前用现成的 resolve_id_placeholders 兜底：幂等，无需修正则不写
        不产生事件；有则写回任务单，后续校验与验收都能按真实地址/路径通过。
        """
        from .cli import resolve_id_placeholders

        probe = json.loads(json.dumps(task))
        resolve_id_placeholders(task["id"], probe)
        if probe == task:
            return task
        try:
            updated = self.store.update(
                task["id"],
                expected_revision=task["revision"],
                mutate=lambda t: resolve_id_placeholders(t["id"], t),
                event_type="audit.correction_recorded",
                summary=f"自愈任务单占位符：{task['id'][:12]}",
                actor=Actor("worker", self.options.worker_id),
                metadata={"task_id": task["id"]},
            )
            self.log(f"{task['id']}: 已自愈任务单（<id> 占位符 / openapi 验收地址已规范化）")
            return updated
        except (ConflictError, SchemaError) as exc:
            # 写回失败不阻断本轮：校验仍会暴露问题，由人处理，不假装成功
            self.log(f"{task['id']}: 占位符自愈写回失败（{exc.message}），继续走校验")
            return task

    def _validation_errors(self, task: dict) -> list[str]:
        from . import schema

        errs = schema.errors_for("task", task)
        if errs:
            # 结构不合法时不再读取缺失字段做语义校验，保留原始字段错误。
            return errs
        return schema.semantic_errors(task, self.config, self.root)

    # -- 单任务流程 --------------------------------------------------------
    def _process(self, task: dict) -> None:
        task_id = task["id"]
        attempt = task["attempt"]
        executor = self.executor_name
        repo = self.config.repo(task["target"]["repo"])
        host = self.host or get_host(repo.host, root=self.root)
        workspace = Workspace(
            repo,
            self.root,
            task_id,
            attempt,
            prefix=executor,
            direct=self.options.workdir,
            reuse=self.options.reuse_worktree,
        )
        heartbeat = _Heartbeat(self, task_id, attempt)
        heartbeat.start()
        cancel_watcher = _start_external_cancel_watcher(self, heartbeat)
        try:
            self.log(f"{task_id}: 准备 worktree（attempt {attempt}）…")
            worktree = workspace.prepare(task["target"]["base_sha"])
            self.log(f"{task_id}: worktree 就绪 {worktree}")
            self._heartbeat_or_raise(task_id, attempt)
            # 首次克隆可能跨过多次心跳，切换状态前重新读取当前任务版本。
            task = self.store.get(task_id)
            self.store.transition(
                task_id,
                "in_progress",
                expected_revision=task["revision"],
                summary="开始执行（worktree 已就绪）",
                actor=Actor("worker", self.options.worker_id),
                metadata={"branch": workspace.branch, "executor": executor},
            )
            task = self.store.get(task_id)
            attempt = task["attempt"]

            contract_text = self._contract_text(task)
            prompt = build_prompt(task, worktree, contract_text)
            if self.prompt_note:
                prompt += (
                    "\n\n# 人工干预指令（最高优先级，先照这个改）\n\n"
                    f"{self.prompt_note.strip()}\n"
                )
                self.log(f"{task_id}: 已把人工干预指令附到本轮 prompt")
            self.store.update(
                task_id,
                expected_revision=task["revision"],
                mutate=lambda t: None,
                event_type="codex.started",
                summary=f"启动 {executor}",
                actor=Actor("worker", self.options.worker_id),
                metadata={
                    "executor": executor,
                    "cli_version": self.codex.version() if hasattr(self.codex, "version") else None,
                },
            )
            task = self.store.get(task_id)
            self.log(f"{task_id}: prompt 就绪（{len(prompt)} 字符），启动 {executor} 子进程…")
            outcome = self.codex.run(
                worktree,
                prompt,
                task_id=task_id,
                attempt=attempt,
                cancel_event=heartbeat.cancel,
            )
            self.log(
                f"{task_id}: {executor} {outcome.status}"
                + (f"（{outcome.message}）" if outcome.message else "")
            )
            if outcome.status == "timed_out":
                # 先停续租再写失败状态：心跳线程一直改 revision，_fail 的
                # expected_revision 会过期并撞 revision_conflict，重试 3 次后放弃，
                # 任务就永久卡在 in_progress（见 out-of-credits 启动即失败事故）。
                heartbeat.stop()
                self._fail(
                    task_id,
                    "codex",
                    outcome.message or f"{executor} 超时",
                    notes=outcome.stderr_tail,
                    event_type="codex.timed_out",
                )
                return
            if outcome.status != "completed" or outcome.result is None:
                detail = outcome.message or f"{executor} 未产出结构化结果"
                if outcome.result:
                    detail += f"；{executor} 自述：{clean(outcome.result.get('notes') or '', 800)}"
                heartbeat.stop()
                self._fail(task_id, "codex", detail, notes=outcome.stderr_tail, event_type="codex.failed")
                return

            # blocked 意味着"做不完就停手"，不是"做错了"。它手上的改动是有价值的，
            # 直接 _fail 会把 593 行成果扔在 worktree 里烂掉——按部分交付走完提交 + PR。
            partial = outcome.result.get("status") == "blocked"
            if partial:
                self.log(
                    f"{task_id}: {executor} 报告 blocked——按「部分交付」处理，"
                    "把已改好的部分提交上来，剩余项写进 PR 说明"
                )

            task = self.store.get(task_id)
            self.store.update(
                task_id,
                expected_revision=task["revision"],
                mutate=lambda t: None,
                event_type="codex.completed",
                summary=f"{executor} 完成（{outcome.run_id}）",
                actor=Actor("worker", self.options.worker_id),
                metadata={
                    "executor": executor,
                    "run_id": outcome.run_id,
                    "cli_version": outcome.cli_version,
                    "duration_seconds": round(outcome.duration_seconds, 1),
                },
                links={"logs": outcome.log_path},
            )

            self._heartbeat_or_raise(task_id, attempt)
            changes = workspace.collect_changes(workspace.diff_base(task["target"]["base_sha"]))
            paths = [c.path for c in changes]
            if not paths:
                self._fail(task_id, "gate", f"{executor} 报告完成但没有产生任何改动", event_type="gate.failed")
                return

            gate_cfg = self.config.checks.gate
            if len(paths) > gate_cfg.max_changed_files:
                self._fail(task_id, "gate", f"改动文件过多：{len(paths)} > {gate_cfg.max_changed_files}", event_type="gate.failed")
                return

            decision = path_evaluate(paths, task["scope"]["allowed_paths"], task["scope"]["forbidden_paths"])
            if not decision.ok:
                self._fail(task_id, "gate", "路径 gate 拒绝：\n" + "\n".join(decision.violations[:20]), event_type="gate.failed")
                return

            problems = workspace.inspect(changes, max_file_bytes=gate_cfg.max_file_bytes)
            submodules = workspace.submodules()
            if submodules:
                problems.append("出现子模块改动：" + ", ".join(submodules[:10]))
            if problems:
                self._fail(task_id, "gate", "改动检查未通过：\n" + "\n".join(problems[:20]), event_type="gate.failed")
                return

            if workspace.direct is not None:
                self._deliver_direct(task, workspace, paths, worktree, outcome)
                return

            message = (
                f"{executor}: {task['title']}\n\n"
                f"task: {task_id}\n"
                f"attempt: {attempt}\n"
                f"{executor}-run: {outcome.run_id}\n\n"
                f"{clean(outcome.result.get('summary', ''), 1000)}\n"
            )
            head_sha = workspace.stage_and_commit(message)
            diff_stat = workspace.diff_stat(task["target"]["base_sha"])

            self._heartbeat_or_raise(task_id, attempt)
            try:
                workspace.push()
            except HandoffError as exc:
                self._fail(task_id, "push", f"推送分支失败：{exc.message}")
                return

            pr_body = self._pr_body(task, outcome.result, diff_stat, partial=partial)
            pr = host.create_pr(
                repo.id,
                head=workspace.branch,
                base=repo.pr_base_ref or task["target"]["base_ref"],
                title=(
                    f"[{task_id}][部分交付] {task['title']}"
                    if partial
                    else f"[{task_id}] {task['title']}"
                ),
                body=pr_body,
            )
            self.log(f"{task_id}: PR #{pr.number} {pr.url}")

            result_block = {
                "branch": workspace.branch,
                "head_sha": head_sha,
                "pr_url": pr.url,
                "pr_number": pr.number,
                "merge_sha": None,
                "executor": executor,
                "executor_session_id": clean(outcome.thread_id or "", 200) or None,
                "codex_run_id": clean(outcome.run_id, 200) or None,
                "codex_cli_version": clean(outcome.cli_version or "", 100) or None,
                "prompt_version": outcome.result.get("prompt_version") or ("handoff-zcode-v1" if executor == "zcode" else "handoff-codex-v1"),
                "diff_summary": diff_stat,
                "changed_paths": paths,
                "worktree": str(worktree),
                "codex_thread_id": clean(outcome.thread_id or "", 200) or None if executor == "codex" else None,
                "checks": [],
                "notes": ("[部分交付] " if partial else "")
                + clean(outcome.result.get("notes") or "", 3600),
            }

            task = self.store.get(task_id)
            self.store.transition(
                task_id,
                "delivered",
                expected_revision=task["revision"],
                summary=(
                    f"分支已推送并创建 PR #{pr.number}（部分交付，剩余项见 PR）"
                    if partial
                    else f"分支已推送并创建 PR #{pr.number}"
                ),
                actor=Actor("worker", self.options.worker_id),
                mutate=lambda t: t.__setitem__("result", result_block),
                metadata={"head_sha": head_sha, "pr": pr.url},
                links={"pr": pr.url},
            )
            task = self.store.get(task_id)
            self.store.transition(
                task_id,
                "awaiting_merge",
                expected_revision=task["revision"],
                summary="等待合并 gate",
                actor=Actor("worker", self.options.worker_id),
            )
            if self.options.auto_merge:
                self._try_auto_merge(task_id, repo)

        except LeaseError as exc:
            heartbeat.cancel.set()
            heartbeat.stop()
            self.log(f"{task_id}: 租约丢失，停止写入（{exc.message}）")
        except HandoffError as exc:
            # 先停续租再写失败状态：心跳线程一直改 revision，_fail 会连撞 3 次写不进去，
            # 任务就永久卡在 in_progress。
            heartbeat.stop()
            detail = "; ".join(getattr(exc, "errors", []) or [])
            self.log(f"{task_id}: {exc.code}: {exc.message}" + (f" · {detail}" if detail else ""))
            self._fail(
                task_id,
                self._stage_for(exc),
                exc.message + (f"（{clean(detail, 1500)}）" if detail else ""),
            )
        except Exception as exc:  # noqa: BLE001 - 单任务失败不能拖垮 worker
            heartbeat.stop()
            self.log(f"{task_id}: 未预期错误 {type(exc).__name__}: {exc}")
            self._fail(task_id, "internal", f"{type(exc).__name__}: {exc}")
        finally:
            heartbeat.stop()
            cancel_watcher.set()
            preserve = self.options.keep_worktree or (
                self.config.codex.persist_thread and self.config.codex.keep_worktree
            )
            if preserve:
                self.log(f"{task_id}: 保留 worktree {workspace.path}（{executor} resume 需要这个 cwd）")
            else:
                workspace.remove(force=True)

    # -- 子流程 ------------------------------------------------------------
    def _try_auto_merge(self, task_id: str, repo, host=None) -> None:
        host = host or self.host or get_host(repo.host, root=self.root)
        task = self.store.get(task_id)
        pr_number = task["result"].get("pr_number")
        if not pr_number:
            return
        pr = host.get_pr(repo.id, pr_number)
        decision = merge_gate.evaluate(task, pr, self.config.checks)
        if not decision.ok:
            self.log(f"{task_id}: 自动合并 gate 未通过，留给人：{'; '.join(decision.reasons[:5])}")
            return
        host.enable_auto_merge(repo.id, pr_number, method="squash")
        self.log(f"{task_id}: 已开启自动合并，轮询 PR 状态")
        pr = merge_gate.wait_for_merge(
            host,
            repo.id,
            pr_number,
            timeout_seconds=self.options.merge_timeout_seconds,
            logger=self.log,
            sleep=self.sleeper,
        )
        if not pr.merged or not pr.merge_commit_sha:
            self.log(f"{task_id}: PR 未确认合并（state={pr.state}），不写 merged")
            return
        task = self.store.get(task_id)
        merge_sha = pr.merge_commit_sha
        self.store.transition(
            task_id,
            "merged",
            expected_revision=task["revision"],
            summary=f"PR #{pr_number} 已合并（{merge_sha[:12]}）",
            actor=Actor("worker", self.options.worker_id),
            mutate=lambda t: (
                t["result"].__setitem__("merge_sha", merge_sha),
                t.__setitem__(
                    "deployment",
                    {
                        "status": "pending",
                        "target_sha": merge_sha,
                        "observed_sha": None,
                        "environment": t["acceptance"]["environment"],
                        "url": self.config.env(t["acceptance"]["environment"]).url,
                        "updated_at": iso(),
                        "error": None,
                    },
                ),
            ),
            metadata={"merge_sha": merge_sha},
        )
        if self.options.wait_deployment:
            self._wait_deployment(task_id, merge_sha)

    def _wait_deployment(self, task_id: str, merge_sha: str) -> None:
        task = self.store.get(task_id)
        env_name = task["deployment"]["environment"] or task["acceptance"]["environment"]
        env = self.config.env(env_name)
        repo = self.config.repo(task["target"]["repo"])
        ancestry = CommitAncestry(self.root, repo)
        outcome = wait_for_deployment(
            env,
            merge_sha,
            self.config.deploy_poll,
            logger=self.log,
            contains=ancestry.contains,
        )
        if outcome.status != "deployed":
            task = self.store.get(task_id)
            self.store.update(
                task_id,
                expected_revision=task["revision"],
                mutate=lambda t: _set_deployment(t, "timeout" if outcome.status == "timeout" else "failed", outcome, merge_sha),
                event_type="deployment.failed",
                summary=f"部署未对齐（{outcome.status}）：{clean(outcome.error or '', 120)}",
                actor=Actor("worker", self.options.worker_id),
                metadata={"observed_sha": outcome.observed_sha, "target_sha": merge_sha},
            )
            return
        task = self.store.get(task_id)
        self.store.transition(
            task_id,
            "deployed",
            expected_revision=task["revision"],
            summary=f"环境已包含目标 {merge_sha[:12]}（当前 {outcome.observed_sha[:12]}）",
            actor=Actor("worker", self.options.worker_id),
            mutate=lambda t: _set_deployment(t, "deployed", outcome, merge_sha),
            metadata={"target_sha": merge_sha, "observed_sha": outcome.observed_sha},
        )

    def _deliver_direct(self, task: dict, workspace, paths: list[str], worktree, outcome) -> None:
        """直接目录模式：代码已经在用户的项目里，默认不提交、不推送、不建 PR。

        这条路径的定位是「让 agent 在我的工作副本上动手」，所以默认不碰 git 历史：
        用户的未提交改动也在里面，替人提交等于埋雷。
        勾选 auto_commit 后例外：只提交任务改动清单里的文件（不扫用户其他改动），
        提交信息用执行器写的 commit_message，推送到当前分支。
        """
        task_id = task["id"]
        attempt = task.get("attempt") or 0
        diff_stat = workspace.diff_stat(workspace.diff_base(task["target"]["base_sha"]))
        branch = None
        head_sha = None
        extra_meta: dict = {}
        if self.options.auto_commit:
            self.log(
                f"{task_id}: 直接目录模式——{len(paths)} 个文件已改动在 {worktree}，自动提交并推送…"
            )
            message = clean(outcome.result.get("commit_message") or "", 2000).strip()
            if not message:
                # 执行器没给 commit_message（旧输出/被截断）：退化成 GitHub 链路同款格式
                message = (
                    f"{self.executor_name}: {task['title']}\n\n"
                    f"task: {task_id}\nattempt: {attempt}\n\n"
                    f"{clean(outcome.result.get('summary', ''), 500)}"
                )
            extra_meta["commit_message"] = clean(message, 500)
            self._heartbeat_or_raise(task_id, attempt)
            try:
                head_sha = workspace.stage_paths_and_commit(paths, message)
                extra_meta["commit_sha"] = head_sha
                self.log(f"{task_id}: 已提交 {head_sha[:12]}（提交信息由 AI 生成）")
            except HandoffError as exc:
                extra_meta["commit_error"] = clean(exc.message, 300)
                self.log(f"{task_id}: 自动提交失败：{exc.message}")
            if head_sha:
                try:
                    branch = workspace.push_current()
                    extra_meta["pushed"] = True
                    self.log(f"{task_id}: 已推送至 {branch}")
                except HandoffError as exc:
                    extra_meta["push_error"] = clean(exc.message, 300)
                    self.log(f"{task_id}: 自动推送失败：{exc.message}")
        else:
            self.log(
                f"{task_id}: 直接目录模式——{len(paths)} 个文件已改动在 {worktree}，未提交未推送"
            )

        if head_sha and extra_meta.get("pushed"):
            notes = (
                f"直接目录模式：改动已提交 {head_sha[:12]} 并推送至 {branch}"
                "（提交信息由 AI 生成）。"
            )
            summary_suffix = f"（已提交并推送 {head_sha[:12]}）"
        elif head_sha:
            notes = (
                f"直接目录模式：改动已提交 {head_sha[:12]}，但推送失败"
                f"（{extra_meta.get('push_error', '')}），需人工推送。"
            )
            summary_suffix = f"（已提交 {head_sha[:12]}，推送失败）"
        elif self.options.auto_commit:
            notes = (
                "直接目录模式：改动落在工作目录，自动提交失败"
                f"（{extra_meta.get('commit_error', '')}），未推送，需人工处理。"
            )
            summary_suffix = "（自动提交失败）"
        else:
            notes = "直接目录模式：改动落在工作目录，未提交、未推送、未建 PR，需人工 review 后自行提交。"
            summary_suffix = "（未提交）"

        result_block = {
            "branch": branch,
            "head_sha": head_sha,
            "pr_url": None,
            "pr_number": None,
            "merge_sha": None,
            "executor": self.executor_name,
            "executor_session_id": clean(getattr(outcome, "thread_id", "") or "", 200) or None,
            "codex_run_id": clean(getattr(outcome, "run_id", "") or "", 200) or None,
            "codex_cli_version": clean(getattr(outcome, "cli_version", "") or "", 100) or None,
            "prompt_version": None,
            "diff_summary": diff_stat,
            "changed_paths": paths,
            "worktree": str(worktree),
            "codex_thread_id": (
                clean(getattr(outcome, "thread_id", "") or "", 200) or None
                if self.executor_name == "codex"
                else None
            ),
            "checks": [],
            "notes": notes,
        }
        current = self.store.get(task_id)
        self.store.transition(
            task_id,
            "delivered",
            expected_revision=current["revision"],
            summary=f"直接目录 {worktree} 改动 {len(paths)} 个文件{summary_suffix}",
            actor=Actor("worker", self.options.worker_id),
            mutate=lambda t: t.__setitem__("result", result_block),
            metadata={"workdir": str(worktree), "direct": True, **extra_meta},
            links={},
        )

        # 目录模式与 PR 模式一样支持 --wait-deployment：提交推送到目标分支后
        # GitHub Actions 会自动部署，这里轮询 /version 对齐 head_sha，成功后
        # 转 deployed（后续验收由 worker 的 --auto-merge 链路或面板 deliver 完成）。
        # 没有 head_sha（未提交/未推送）时 nothing to wait，保持 delivered 等人工。
        if self.options.wait_deployment and head_sha:
            self.log(f"{task_id}: 目录模式已推送，等待 dev 部署对齐 {head_sha[:12]} …")
            self._wait_deployment(task_id, head_sha)

    def _heartbeat_or_raise(self, task_id: str, attempt: int) -> None:
        if not self.store.still_owner(task_id, worker_id=self.options.worker_id, attempt=attempt):
            raise LeaseError(f"{task_id}: 租约已不属于 {self.options.worker_id}")

    def _fail(
        self,
        task_id: str,
        stage: str,
        message: str,
        *,
        notes: str | None = None,
        event_type: str = "task.status_changed",
    ) -> None:
        try:
            task = self.store.get(task_id)
            if task["status"] in state.TERMINAL:
                return
            self.store.fail(
                task_id,
                stage=stage,
                message=message,
                actor=Actor("worker", self.options.worker_id),
                notes=notes,
                expected_revision=task["revision"],
                event_type=event_type,
            )
        except HandoffError as exc:
            detail = "; ".join(getattr(exc, "errors", []) or [])
            self.log(f"{task_id}: 写失败状态也失败：{exc.code}: {exc.message}"
                     + ("；" + clean(detail, 4000) if detail else ""))

    @staticmethod
    def _stage_for(exc: HandoffError) -> str:
        if isinstance(exc, GateError):
            return "gate"
        if isinstance(exc, SchemaError):
            return "validation"
        code = getattr(exc, "code", "")
        return {
            "git_error": "workspace",
            "execution_error": "workspace",
            "revision_conflict": "internal",
        }.get(code, "internal")

    # -- 文本 --------------------------------------------------------------
    def _contract_text(self, task: dict) -> str | None:
        """读任务契约原文。

        契约是施工图，不能像普通日志那样截断——之前 clean(text, 20000) 把一份
        41KB 的契约砍成首尾两段，agent 看到"中间没了"就直接报 blocked 不干活。
        这里只脱敏（可能含凭据样例），保留全文；真遇到超大文件才兜底截断。
        """
        contract = task.get("contract")
        if not contract:
            return None
        path = self.root / contract["path"]
        if not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        if len(text) > _CONTRACT_HARD_LIMIT:
            self.log(
                f"{task['id']}: 契约文件过大（{len(text)} 字符），"
                f"只取前 {_CONTRACT_HARD_LIMIT} 字符"
            )
            text = text[:_CONTRACT_HARD_LIMIT] + "\n\n...[契约文件过长，已截断]..."
        return redact(text)

    def _pr_body(self, task: dict, codex_result: dict, diff_stat: str, *, partial: bool = False) -> str:
        scope = task["scope"]
        executor = self.executor_name
        partial_note = (
            "## ⚠ 部分交付\n\n"
            f"{executor} 报告本轮没能全部完成，**已改好的部分先交上来**；"
            "未完成项见下方「执行器自述」。合并前请按那张清单确认要不要继续。\n\n"
            if partial
            else ""
        )
        migration = task.get("migration") or {}
        migration_line = (
            f"需要。兼容旧代码：{'是' if migration.get('backward_compatible') else '否'}；"
            f"执行位置：{migration.get('order') or 'manual'}；回滚：{migration.get('rollback') or '见任务单'}"
            if migration.get("required")
            else "不需要"
        )
        tests = codex_result.get("tests") or []
        test_lines = "\n".join(f"- {t['name']}: {t['status']}" for t in tests) or f"- （{executor} 未报告）"
        return f"""{partial_note}## 任务

- ID：`{task['id']}`
- 标题：{task['title']}
- 基线：`{task['target']['repo']}@{task['target']['base_sha'][:12]}`（{task['target']['base_ref']}）
- 条目来源：`{self.config.store.tasks_dir}/{task['id']}.json`

## 需求

{clean(task['requirement'], 4000)}

## 范围

允许修改：
{chr(10).join('- `' + p + '`' for p in scope['allowed_paths'])}

不在范围：
{chr(10).join('- ' + s for s in (scope.get('out_of_scope') or ['（无）'])) or '- （无）'}

## 数据库迁移

{migration_line}

## 风险

{chr(10).join('- ' + r for r in (codex_result.get('risks') or ['（未报告）']))}

## {executor} 自测

{test_lines}

## 改动规模

```
{diff_stat or '(no diff)'}
```

---
由 handoff worker 自动创建。合并前请确认范围与迁移说明。
"""


def _set_deployment(task: dict, status: str, outcome, target_sha: str | None = None) -> None:
    deployment = task["deployment"]
    deployment["status"] = status
    # schema 要求 deployed 及之后必须记录 target_sha 与 observed_sha
    if target_sha:
        deployment["target_sha"] = target_sha
    deployment["observed_sha"] = outcome.observed_sha
    deployment["updated_at"] = iso()
    deployment["error"] = clean(outcome.error or "", 2000) if status != "deployed" else None


def _start_external_cancel_watcher(worker: "Worker", heartbeat: "_Heartbeat") -> threading.Event:
    """面板等外部取消信号快速传导到执行器子进程（不等心跳周期）。"""
    done = threading.Event()

    def _watch() -> None:
        while not done.wait(0.5):
            external = worker.external_cancel
            if external is not None and external.is_set():
                heartbeat.cancel.set()
                return

    threading.Thread(target=_watch, name="external-cancel-watcher", daemon=True).start()
    return done


class _Heartbeat:
    """独立线程续租。租约丢失或外部取消时 set cancel，让执行器子进程被杀掉。"""

    def __init__(self, worker: Worker, task_id: str, attempt: int):
        self.worker = worker
        self.task_id = task_id
        self.attempt = attempt
        # Reuse the caller's event when present so a panel cancel reaches the
        # executor immediately; a polling bridge can otherwise lose a race with
        # a process that finishes between the request and the next poll.
        self.cancel = worker.external_cancel if worker.external_cancel is not None else threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_renew = 0.0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=f"heartbeat-{self.task_id}", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        opts = self.worker.options
        while not self._stop.wait(max(1.0, opts.heartbeat_interval_seconds)):
            if self.cancel.is_set():
                return
            # 面板等外部调用方请求取消：传导给执行器子进程后停止续租
            external = getattr(self.worker, "external_cancel", None)
            if external is not None and external.is_set():
                self.cancel.set()
                return
            try:
                if not self.worker.store.still_owner(self.task_id, worker_id=opts.worker_id, attempt=self.attempt):
                    self.cancel.set()
                    return
                self.worker.store.heartbeat(
                    self.task_id,
                    worker_id=opts.worker_id,
                    attempt=self.attempt,
                    lease_seconds=int(opts.lease_seconds),
                )
                now_ts = time.monotonic()
                if now_ts - self._last_renew >= opts.lease_renew_interval_seconds:
                    self.worker.store.renew_lease(
                        self.task_id,
                        worker_id=opts.worker_id,
                        attempt=self.attempt,
                        lease_seconds=int(opts.lease_seconds),
                    )
                    self._last_renew = now_ts
            except LeaseError:
                self.cancel.set()
                return
            except Exception:  # noqa: BLE001 - 心跳失败不能让线程崩溃
                continue

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
