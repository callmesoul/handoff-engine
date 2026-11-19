"""Git 任务仓存储层。

业务代码不得直接读写 tasks/*.json。所有变更走这里，保证：

1. 每次更新先 fetch，校验当前 revision；
2. 原子写临时文件后 rename；
3. 任务快照与审计事件在同一个 commit 里；
4. push 非快进时重新读取并有限重试，绝不 force push。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Callable

from . import ids, schema, state
from .config import Config
from .errors import ConflictError, GitError, HandoffError, LeaseError, SchemaError, TaskNotFound
from .gitcmd import git, git_out, push_rejected
from .sanitize import clean, redact_obj
from .timeutil import iso, now, parse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Actor:
    kind: str
    id: str

    def as_dict(self) -> dict:
        return {"kind": self.kind, "id": self.id}


SYSTEM = Actor("system", "handoffctl")
CLI = Actor("user", "cli")


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    """`ancestor` 是否为 `descendant` 的祖先（含相等）。git 用退出码表达，不是错误。"""
    if not ancestor or not descendant:
        return False
    return git(root, "merge-base", "--is-ancestor", ancestor, descendant).ok


def _serialized(method):
    """同一 checkout 的同步与提交串行执行，防止心跳重置未推送事务。"""
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._transaction_lock:
            return method(self, *args, **kwargs)
    return locked


class TaskStore:
    def __init__(
        self,
        root: str | Path,
        config: Config,
        *,
        push: bool = True,
        actor: Actor = CLI,
        correlation_id: str | None = None,
        max_retries: int = 3,
        allow_dirty: bool = False,
    ):
        self.root = Path(root).resolve()
        self.config = config
        self.push_enabled = push
        self.actor = actor
        self.correlation_id = correlation_id
        self.max_retries = max_retries
        self.allow_dirty = allow_dirty
        self.runtime_dir = self.root / ".handoff-run"
        self._remote_checked: bool | None = None
        self._transaction_lock = threading.RLock()

    # -- 路径 --------------------------------------------------------------
    @property
    def branch(self) -> str:
        return self.config.store.branch

    @property
    def tasks_dir(self) -> Path:
        return self.root / self.config.store.tasks_dir

    @property
    def events_dir(self) -> Path:
        return self.root / self.config.store.events_dir

    def task_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    def event_dir(self, task_id: str) -> Path:
        return self.events_dir / task_id

    def lease_path(self, task_id: str) -> Path:
        return self.runtime_dir / "leases" / f"{task_id}.json"

    # -- Git ---------------------------------------------------------------
    def has_remote(self) -> bool:
        if self._remote_checked is not None:
            return self._remote_checked
        res = git(self.root, "remote", "get-url", self.config.store.remote)
        self._remote_checked = res.ok
        return self._remote_checked

    @_serialized
    def sync(self) -> None:
        """把工作目录对齐到远端主分支。只允许在专用 clone 里执行。

        关键约束：本地已提交但尚未推送的提交不能丢。面板常驻轮询、CLI 长验收
        会并发 sync 同一个 clone，无脑 `reset --hard <remote>/<branch>` 会把对端
        刚写完还没 push 出去的状态回滚掉——表现为「明明写了 verifying，读回来
        还是 deployed」，后续写入就会撞非法状态跳转。
        """
        if not self.has_remote():
            return
        remote = self.config.store.remote
        # 只看已跟踪文件的改动：reset --hard / rebase / checkout 都会毁掉它们；
        # 未跟踪文件本身不受影响。
        dirty = git(self.root, "status", "--porcelain", "-uno").stdout.strip()
        if dirty:
            if not self.allow_dirty:
                raise GitError(
                    "工作区有未提交改动，拒绝 reset --hard。请使用交接仓的专用 clone。",
                    stderr=clean(dirty),
                )
            # allow_dirty=True 只表示"允许在有未提交改动的工作区里读写任务单"，
            # 绝不代表可以重置它。历史上这里只跳过了上面的检查、却继续执行下面的
            # reset --hard，把 24 个未提交的源码改动全部销毁。fetch 是无害的，
            # 照常做；但 reset / rebase / checkout 会改写工作区，必须整体跳过。
            fetch = git(self.root, "fetch", "--prune", remote, timeout=120)
            if not fetch.ok:
                raise GitError("git fetch 失败", stderr=clean(fetch.stderr), returncode=fetch.returncode)
            return
        fetch = git(self.root, "fetch", "--prune", remote, timeout=120)
        if not fetch.ok:
            raise GitError("git fetch 失败", stderr=clean(fetch.stderr), returncode=fetch.returncode)
        target = f"{remote}/{self.branch}"
        verify = git(self.root, "rev-parse", "--verify", target)
        if not verify.ok:
            raise GitError(f"远端分支不存在: {target}", stderr=clean(verify.stderr))

        head = git(self.root, "rev-parse", "HEAD").stdout.strip()
        latest = git(self.root, "rev-parse", target).stdout.strip()
        if head and head == latest:
            return

        # 本地领先（远端是本地 HEAD 的祖先）：必须先推送保住提交，绝不能 reset。
        if head and latest and _is_ancestor(self.root, latest, head):
            self._push()
            return

        # 本地落后或分叉：确保停在目标分支上再快进/变基。
        checkout = git(self.root, "checkout", "-q", self.branch)
        if not checkout.ok:
            raise GitError("切换分支失败", stderr=clean(checkout.stderr))
        if not head or _is_ancestor(self.root, head, latest):
            # 纯快进，reset 不会丢任何提交。
            reset = git(self.root, "reset", "--hard", target)
            if not reset.ok:
                raise GitError("reset --hard 失败", stderr=clean(reset.stderr))
            return

        # 真分叉：两边都有独有提交，用 rebase 合并，冲突则中止并报错（不静默丢数据）。
        rebase = git(self.root, "rebase", target)
        if not rebase.ok:
            git(self.root, "rebase", "--abort")
            raise GitError(
                "本地未推送提交与远端分叉，rebase 失败，请人工处理该 clone",
                stderr=clean(rebase.stderr),
                returncode=rebase.returncode,
            )

    def task_commit(self, task_id: str) -> str:
        """该任务单最后一次落盘的 commit（补发完成通知用）。

        `_commit` 只是 `_write` 的内存回传字段，不落任务单 JSON，所以从磁盘读
        出来的任务永远拿不到它。这里改从交接仓 git 历史反查：优先取已推送
        分支上的最后一次变更（补发时才不会指向一个远端不存在的提交），
        退而取 HEAD。
        """
        rel = f"{self.config.store.tasks_dir}/{task_id}.json"
        refs: list[str] = []
        if self.has_remote():
            refs.append(f"{self.config.store.remote}/{self.branch}")
        refs.append("HEAD")
        for ref in refs:
            res = git(self.root, "log", "-1", "--format=%H", ref, "--", rel)
            sha = res.stdout.strip() if res.ok else ""
            if re.fullmatch(r"[0-9a-f]{40}", sha):
                return sha
        return ""

    def _commit(self, message: str) -> str | None:
        dirs = [
            self.config.store.tasks_dir,
            self.config.store.events_dir,
            self.config.store.contracts_dir,
            self.config.store.artifacts_dir,
        ]
        add_args = ["add", "-A", "--"]
        add_args.extend(d for d in dirs if (self.root / d).exists())
        # _atomic_write 的 .tmp-*.json 可能正躺在这些目录里，-A 会把它们一并加进版本库。
        # 曾因此把某任务的 failed 旧快照提交进 tasks/，列表里凭空多出一条同 id 任务。
        add_args.append(":(exclude)**/.tmp-*")
        added = git(self.root, *add_args)
        if not added.ok:
            raise GitError("git add 失败", stderr=clean(added.stderr))
        staged = git(self.root, "diff", "--cached", "--name-only")
        if not staged.stdout.strip():
            return None
        commit = git(self.root, "commit", "-m", message)
        if not commit.ok:
            raise GitError("git commit 失败", stderr=clean(commit.stderr))
        return git_out(self.root, "rev-parse", "HEAD")

    def _push(self) -> None:
        if not self.push_enabled or not self.has_remote():
            return
        res = git(self.root, "push", self.config.store.remote, f"HEAD:{self.branch}", timeout=120)
        if not res.ok:
            if push_rejected(res):
                raise ConflictError(
                    "任务仓 push 被拒（非快进），重新读取后重试",
                    stderr=clean(res.stderr),
                )
            raise GitError("git push 失败", stderr=clean(res.stderr), returncode=res.returncode)

    def _atomic_write(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # -- 读取 --------------------------------------------------------------
    def _read(self, task_id: str) -> dict:
        path = self.task_path(task_id)
        if not path.is_file():
            raise TaskNotFound(f"任务单不存在: {task_id}", task_id=task_id)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except json.JSONDecodeError as exc:
            raise SchemaError(f"{task_id}: 任务单不是合法 JSON: {exc}", task_id=task_id) from exc

    @_serialized
    def get(self, task_id: str, *, sync: bool = True) -> dict:
        if sync:
            self.sync()
        return self._read(task_id)

    @_serialized
    def list_tasks(self, *, status: str | list[str] | None = None, sync: bool = True) -> list[dict]:
        if sync:
            self.sync()
        return self._scan(status)

    def peek(self, task_id: str) -> dict:
        """只读本地任务单：不加事务锁、不 sync。

        面板每次轮询都 sync（git fetch）要好几秒，会把 HTTP 线程全堵住，
        表现为"点了没反应"。只读视图走这里，快照旧一点没关系。
        """
        return self._read(task_id)

    def peek_tasks(self, *, status: str | list[str] | None = None) -> list[dict]:
        """只读本地任务列表，语义同 peek。"""
        return self._scan(status)

    def _scan(self, status: str | list[str] | None) -> list[dict]:
        wanted = {status} if isinstance(status, str) else set(status) if status else None
        out = []
        if not self.tasks_dir.is_dir():
            return out
        for path in sorted(self.tasks_dir.glob("*.json")):
            # _atomic_write 先写 .tmp-<rand>.json 再 rename；进程被硬杀时 finally 不执行，
            # 临时文件会残留在 tasks 目录。不跳过的话它会被当成一份旧任务读进来，
            # 同一个 id 就凭空多出第二条（且是过期快照）。
            if path.name.startswith("."):
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    task = json.load(fh)
            except (json.JSONDecodeError, OSError):
                continue
            if wanted and task.get("status") not in wanted:
                continue
            out.append(task)
        return out

    @_serialized
    def delete_tasks(self, task_ids: list[str] | None = None, *, delete_all: bool = False) -> dict:
        """原子删除任务及其 Git 内关联材料；提交历史仍可用于恢复。"""
        requested = list(dict.fromkeys(task_ids or []))
        if delete_all == bool(requested):
            raise SchemaError("删除时必须二选一：指定任务 ID，或使用 --all")
        invalid = [task_id for task_id in requested if not ids.is_task_id(task_id)]
        if invalid:
            raise SchemaError("任务 ID 格式非法", errors=invalid)

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                self.sync()
                existing = {task["id"] for task in self.list_tasks(sync=False)}
                targets = sorted(existing) if delete_all else requested
                missing = [task_id for task_id in targets if task_id not in existing]
                if missing:
                    raise TaskNotFound(
                        f"任务单不存在: {', '.join(missing)}",
                        task_ids=missing,
                    )

                for task_id in targets:
                    self.task_path(task_id).unlink()
                    for path in (self.event_dir(task_id), self.contract_dir(task_id), self.artifact_dir(task_id)):
                        if path.is_symlink():
                            path.unlink()
                        elif path.is_dir():
                            shutil.rmtree(path)
                    lease = self.lease_path(task_id)
                    if lease.is_file() or lease.is_symlink():
                        lease.unlink()

                if not targets:
                    return {"deleted": [], "count": 0, "store_commit": None}
                message = (
                    f"handoff: 删除全部 {len(targets)} 个任务"
                    if delete_all
                    else f"handoff: 删除 {len(targets)} 个任务"
                )
                sha = self._commit(message)
                self._push()
                return {"deleted": targets, "count": len(targets), "store_commit": sha}
            except ConflictError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
        raise ConflictError(
            f"删除任务时发生并发冲突，已重试 {self.max_retries} 次",
            detail=str(last_error) if last_error else "",
        )

    # -- 校验 --------------------------------------------------------------
    def _validate(self, task: dict) -> None:
        errs = schema.errors_for("task", task)
        errs.extend(schema.semantic_errors(task, self.config, self.root))
        if errs:
            raise SchemaError(f"{task.get('id', '<no id>')}: 任务单校验失败（{len(errs)} 处）", errors=errs)

    # -- 写入 --------------------------------------------------------------
    @_serialized
    def create(
        self,
        task: dict,
        *,
        summary: str | None = None,
        actor: Actor | None = None,
        before_write: Callable[[dict], None] | None = None,
    ) -> dict:
        actor = actor or self.actor
        task = json.loads(json.dumps(task))  # 深拷贝，别改调用方的对象
        task.setdefault("schema_version", 1)
        task["revision"] = 1
        stamp = iso()
        task.setdefault("created_at", stamp)
        task["updated_at"] = stamp
        if self.task_path(task["id"]).exists():
            raise ConflictError(f"任务已存在: {task['id']}", task_id=task["id"])
        self._validate(task)

        for attempt in range(1, self.max_retries + 1):
            try:
                self.sync()
                if self.task_path(task["id"]).exists():
                    raise ConflictError(f"任务已存在: {task['id']}", task_id=task["id"])
                if before_write is not None:
                    before_write(task)
                self._atomic_write(self.task_path(task["id"]), task)
                event = self._build_event(
                    task,
                    event_type="task.created",
                    actor=actor,
                    summary=summary or f"创建任务：{task['title']}",
                    from_status=None,
                    to_status=task["status"],
                    metadata={"target_repo": task["target"]["repo"]},
                )
                self._write_event(event)
                sha = self._commit(f"handoff({task['id']}): 创建任务单 r1")
                self._push()
                if sha:
                    task["_commit"] = sha
                return task
            except ConflictError:
                if attempt >= self.max_retries:
                    raise
                continue
        raise ConflictError(f"{task['id']}: 创建失败（重试 {self.max_retries} 次）")

    def _build_event(
        self,
        task: dict,
        *,
        event_type: str,
        actor: Actor,
        summary: str,
        from_status: str | None,
        to_status: str | None,
        metadata: dict | None = None,
        links: dict | None = None,
    ) -> dict:
        event = {
            "schema_version": 1,
            "event_id": ids.ulid(),
            "task_id": task["id"],
            "task_revision": task["revision"],
            "attempt": task["attempt"],
            "type": event_type,
            "actor": actor.as_dict(),
            "occurred_at": task["updated_at"],
            "correlation_id": self.correlation_id,
            "from_status": from_status,
            "to_status": to_status,
            "summary": clean(summary, 480),
            "metadata": redact_obj({k: v for k, v in (metadata or {}).items() if v is not None}),
            "links": {k: v for k, v in (links or {}).items() if v},
        }
        errs = schema.errors_for("event", event)
        if errs:
            raise SchemaError("事件校验失败", errors=errs)
        return event

    def _write_event(self, event: dict) -> None:
        path = self.event_dir(event["task_id"]) / f"{event['event_id']}.json"
        if path.exists():
            raise ConflictError(f"事件已存在，禁止覆盖: {path.name}")
        self._atomic_write(path, event)

    @_serialized
    def _write(
        self,
        task_id: str,
        mutate: Callable[[dict], None],
        *,
        event_type: str,
        summary: str,
        actor: Actor | None = None,
        metadata: dict | None = None,
        links: dict | None = None,
        expected_revision: int | None = None,
    ) -> dict:
        actor = actor or self.actor
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                self.sync()
                task = self._read(task_id)
                if expected_revision is not None and task["revision"] != expected_revision:
                    raise ConflictError(
                        f"{task_id}: revision 冲突（本地期望 {expected_revision}，仓库实际 {task['revision']}）",
                        task_id=task_id,
                        expected=expected_revision,
                        actual=task["revision"],
                    )
                prev_status = task["status"]
                base_revision = task["revision"]
                mutate(task)
                task["revision"] = base_revision + 1
                task["updated_at"] = iso()
                self._validate(task)
                changed = task["status"] != prev_status
                event = self._build_event(
                    task,
                    event_type=event_type,
                    actor=actor,
                    summary=summary,
                    from_status=prev_status if changed else None,
                    to_status=task["status"] if changed else None,
                    metadata=metadata,
                    links=links,
                )
                self._atomic_write(self.task_path(task_id), task)
                self._write_event(event)
                sha = self._commit(f"handoff({task_id}): {summary} (r{task['revision']})")
                self._push()
                if sha:
                    task["_commit"] = sha
                if changed and task["status"] in ("verified", "failed"):
                    # 任务进入终态就回程通知前端；不再以 git push 是否开启或
                    # 本次是否有提交为前置条件——「要不要 git push」和「要不要
                    # 告诉前端任务完成」是两件事，通知本就有独立 outbox + 重试。
                    # store_commit 用本次落盘 sha，没有新提交时从 git 历史反查。
                    from . import notification

                    commit = sha or self.task_commit(task_id)
                    notification_result = notification.emit_task_completed(
                        self.root,
                        self.config.notifications,
                        task,
                        commit,
                    )
                    task["_completion_notification"] = notification_result
                    if notification_result.get("status") in ("disabled", "error"):
                        # 不再静默：通知入队失败要留痕，供巡检/人工发现。
                        logger.warning(
                            "任务 %s 完成通知入队失败: %s",
                            task_id,
                            notification_result,
                        )
                return task
            except ConflictError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                continue
        raise ConflictError(
            f"{task_id}: 写入冲突，已重试 {self.max_retries} 次，停止自动覆盖",
            task_id=task_id,
            detail=str(last_error) if last_error else "",
        )

    # -- 语义化操作 --------------------------------------------------------
    def transition(
        self,
        task_id: str,
        to_status: str,
        *,
        expected_revision: int,
        summary: str,
        actor: Actor | None = None,
        mutate: Callable[[dict], None] | None = None,
        event_type: str = "task.status_changed",
        metadata: dict | None = None,
        links: dict | None = None,
    ) -> dict:
        def apply(task: dict) -> None:
            state.assert_transition(task["status"], to_status, task_id)
            task["status"] = to_status
            if mutate:
                mutate(task)

        return self._write(
            task_id,
            apply,
            event_type=event_type,
            summary=summary,
            actor=actor,
            metadata=metadata,
            links=links,
            expected_revision=expected_revision,
        )

    def request_reverify(
        self,
        task_id: str,
        *,
        expected_revision: int,
        reason: str,
        actor: Actor | None = None,
    ) -> dict:
        """保留已合并产物，只清除验收结果并回到 deployed。"""

        def apply(task: dict) -> None:
            state.assert_reverify_transition(task, task_id)
            if not task.get("deployment", {}).get("target_sha") or not task["deployment"].get("observed_sha"):
                raise SchemaError(f"{task_id}: 缺少 target_sha/observed_sha，不能重新验收")
            if task["status"] == "verified":
                state.assert_transition("verified", "deployed", task_id)
            task["status"] = "deployed"
            task["failure"] = None
            task["claim"] = None
            task["result"]["checks"] = []
            task["deployment"]["status"] = "deployed"
            task["deployment"]["error"] = None
            task["deployment"]["updated_at"] = iso()

        return self._write(
            task_id,
            apply,
            event_type="verification.retry_requested",
            summary=f"重新验收：{clean(reason, 320)}",
            actor=actor,
            metadata={"reason": reason},
            expected_revision=expected_revision,
        )

    def update(
        self,
        task_id: str,
        *,
        expected_revision: int,
        mutate: Callable[[dict], None],
        event_type: str,
        summary: str,
        actor: Actor | None = None,
        metadata: dict | None = None,
        links: dict | None = None,
    ) -> dict:
        return self._write(
            task_id,
            mutate,
            event_type=event_type,
            summary=summary,
            actor=actor,
            metadata=metadata,
            links=links,
            expected_revision=expected_revision,
        )

    def claim(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 3600,
        actor: Actor | None = None,
        base_sha: str | None = None,
    ) -> dict:
        task = self.get(task_id)
        if task["claim"] and not self._lease_expired(task):
            raise LeaseError(
                f"{task_id}: 已被 {task['claim']['worker_id']} 持有，租约未过期",
                task_id=task_id,
                worker_id=task["claim"]["worker_id"],
            )

        def apply(t: dict) -> None:
            src = t["status"]
            if src in state.CLAIMED_STATES:
                # 租约已过期：先把上一个 attempt 释放回 open，再认领
                t["status"] = "open"
                src = "open"
            state.assert_transition(src, "claimed", task_id)
            if not t["target"].get("base_sha"):
                if not base_sha:
                    raise SchemaError(f"{task_id}: 认领前必须由 worker 固定 target.base_sha")
                t["target"]["base_sha"] = base_sha
            t["status"] = "claimed"
            t["attempt"] = int(t.get("attempt", 0)) + 1
            t["claim"] = {
                "worker_id": worker_id,
                "claimed_at": iso(),
                "lease_expires_at": _future(lease_seconds),
                "heartbeat_at": iso(),
            }
            t["failure"] = None

        return self._write(
            task_id,
            apply,
            event_type="claim.acquired",
            summary=f"{worker_id} 认领任务（attempt {int(task.get('attempt', 0)) + 1}）",
            actor=actor or Actor("worker", worker_id),
            metadata={
                "worker_id": worker_id,
                "lease_seconds": lease_seconds,
                "base_ref": task["target"]["base_ref"],
                "base_sha": task["target"].get("base_sha") or base_sha,
            },
            expected_revision=task["revision"],
        )

    def _lease_expired(self, task: dict) -> bool:
        claim = task.get("claim") or {}
        lease = claim.get("lease_expires_at")
        if not lease:
            return True
        return parse(lease) <= now()

    def still_owner(self, task_id: str, *, worker_id: str, attempt: int, sync: bool = True) -> bool:
        task = self.get(task_id, sync=sync)
        claim = task.get("claim") or {}
        return (
            claim.get("worker_id") == worker_id
            and task.get("attempt") == attempt
            and task["status"] in state.CLAIMED_STATES
            and not self._lease_expired(task)
        )

    def heartbeat(self, task_id: str, *, worker_id: str, attempt: int, lease_seconds: int = 3600) -> dict:
        """轻量心跳：只写本地运行态，不进 Git。"""
        task = self.get(task_id, sync=False)
        claim = task.get("claim") or {}
        if claim.get("worker_id") != worker_id or task.get("attempt") != attempt:
            raise LeaseError(f"{task_id}: 租约不再属于 {worker_id}（attempt {attempt}）", task_id=task_id)
        payload = {
            "task_id": task_id,
            "worker_id": worker_id,
            "attempt": attempt,
            "heartbeat_at": iso(),
            "lease_expires_at": _future(lease_seconds),
        }
        self._atomic_write(self.lease_path(task_id), payload)
        return payload

    def renew_lease(self, task_id: str, *, worker_id: str, attempt: int, lease_seconds: int = 3600) -> dict:
        """把租约延长持久化进 Git。频率低，走正常事务。"""
        task = self.get(task_id)

        def apply(t: dict) -> None:
            if not t.get("claim") or t["claim"].get("worker_id") != worker_id or t.get("attempt") != attempt:
                raise LeaseError(f"{task_id}: 租约不再属于 {worker_id}", task_id=task_id)
            t["claim"]["heartbeat_at"] = iso()
            t["claim"]["lease_expires_at"] = _future(lease_seconds)

        return self._write(
            task_id,
            apply,
            event_type="claim.heartbeat",
            summary=f"{worker_id} 续租至 {_future(lease_seconds)}",
            actor=Actor("worker", worker_id),
            expected_revision=task["revision"],
        )

    def expire_lease(self, task_id: str, *, actor: Actor = SYSTEM) -> dict:
        task = self.get(task_id)
        if not self._lease_expired(task):
            raise LeaseError(f"{task_id}: 租约尚未过期", task_id=task_id)

        def apply(t: dict) -> None:
            state.assert_transition(t["status"], "open", task_id)
            t["status"] = "open"
            t["claim"] = None

        return self._write(
            task_id,
            apply,
            event_type="claim.expired",
            summary="租约过期，任务重新入队",
            actor=actor,
            expected_revision=task["revision"],
        )

    def expire_stale(self, *, actor: Actor = SYSTEM) -> list[str]:
        """把租约过期的任务重新入队。崩溃的 worker 不会永久占住任务。"""
        expired: list[str] = []
        for task in self.list_tasks(status=list(state.CLAIMED_STATES)):
            if not self._lease_expired(task):
                continue
            try:
                self.expire_lease(task["id"], actor=actor)
                expired.append(task["id"])
            except HandoffError:
                continue
        return expired

    def release_lease(
        self,
        task_id: str,
        *,
        actor: Actor,
        reason: str,
        expected_revision: int | None = None,
        sync: bool = True,
    ) -> dict:
        task = self.get(task_id, sync=sync)

        def apply(t: dict) -> None:
            state.assert_transition(t["status"], "open", task_id)
            t["status"] = "open"
            t["claim"] = None

        return self._write(
            task_id,
            apply,
            event_type="claim.released",
            summary=f"释放租约：{reason}",
            actor=actor,
            expected_revision=expected_revision if expected_revision is not None else task["revision"],
        )

    def fail(
        self,
        task_id: str,
        *,
        stage: str,
        message: str,
        actor: Actor | None = None,
        check_id: str | None = None,
        observed_sha: str | None = None,
        expected_revision: int | None = None,
        notes: str | None = None,
        event_type: str = "task.status_changed",
    ) -> dict:
        task = self.get(task_id, sync=False)
        limit = 4000

        def apply(t: dict) -> None:
            state.assert_transition(t["status"], "failed", task_id)
            t["status"] = "failed"
            t["failure"] = {
                "stage": stage,
                "message": clean(message, limit),
                "check_id": check_id,
                "observed_sha": observed_sha,
                "at": iso(),
            }
            if notes:
                t["result"]["notes"] = clean(notes, limit)

        return self._write(
            task_id,
            apply,
            event_type=event_type,
            summary=f"失败（{stage}）：{clean(message, 120)}",
            actor=actor or self.actor,
            metadata={"stage": stage, "check_id": check_id},
            expected_revision=expected_revision if expected_revision is not None else task["revision"],
        )

    # -- 附件 --------------------------------------------------------------
    def contract_dir(self, task_id: str) -> Path:
        return self.root / self.config.store.contracts_dir / task_id

    def artifact_dir(self, task_id: str) -> Path:
        return self.root / self.config.store.artifacts_dir / task_id


def _future(seconds: float) -> str:
    from datetime import timedelta

    return iso(now() + timedelta(seconds=seconds))
