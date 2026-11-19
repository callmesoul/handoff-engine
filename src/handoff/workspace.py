"""隔离工作区。

worker 只用专用 clone + 每次 attempt 独立 worktree。
永不复用开发者的日常 clone，永不覆盖已推送分支。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .config import RepoConfig
from .errors import ExecutionError, GitError
from .gitcmd import git, git_out
from .sanitize import clean

_SECRET_MARKERS = [
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(rb"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
]


@dataclass
class Change:
    status: str
    path: str

    @property
    def is_delete(self) -> bool:
        return self.status.startswith("D")


def branch_name(task_id: str, attempt: int, prefix: str = "codex") -> str:
    return f"{prefix}/{task_id}/attempt-{attempt}"


class Workspace:
    def __init__(
        self,
        repo: RepoConfig,
        root: Path,
        task_id: str,
        attempt: int,
        *,
        prefix: str = "codex",
        direct: str | Path | None = None,
        reuse: str | Path | None = None,
    ):
        self.repo = repo
        self.root = Path(root)
        self.task_id = task_id
        self.attempt = attempt
        self.prefix = prefix
        self.branch = branch_name(task_id, attempt, prefix)
        self.cache = repo.cache_path(self.root)
        self.path = self.root / ".handoff-run" / "worktrees" / task_id / f"attempt-{attempt}"
        # 面板指定的「项目目录」：跳过隔离 worktree，直接在这个目录干活
        self.direct = Path(direct).expanduser().resolve() if direct else None
        # 复用上一轮的隔离 worktree：续跑时不能开新副本，否则 agent 记得改过、
        # 文件却全是原始的。与 direct 互斥，direct 优先。
        self.reuse = Path(reuse).expanduser().resolve() if reuse else None

    # -- 准备 --------------------------------------------------------------
    def _ensure_cache(self) -> None:
        if (self.cache / ".git").exists() or (self.cache / "HEAD").exists():
            fetch = git(self.cache, "fetch", "--prune", "origin", timeout=600)
            if not fetch.ok:
                raise GitError("仓库缓存 fetch 失败", stderr=clean(fetch.stderr), returncode=fetch.returncode)
            return
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        clone = git(self.root, "clone", "--no-checkout", "--quiet", self.repo.remote_url, str(self.cache), timeout=1800)
        if not clone.ok:
            raise GitError(
                f"clone 仓库失败: {self.repo.remote_url}",
                stderr=clean(clone.stderr),
                returncode=clone.returncode,
            )

    def _ensure_commit(self, sha: str) -> None:
        if git(self.cache, "cat-file", "-e", f"{sha}^{{commit}}").ok:
            return
        # 基线可能是较新的提交，单独取一次
        git(self.cache, "fetch", "origin", sha, timeout=300)
        if not git(self.cache, "cat-file", "-e", f"{sha}^{{commit}}").ok:
            raise ExecutionError(f"基线提交 {sha} 在仓库 {self.repo.id} 中不可达")

    def prepare(self, base_sha: str) -> Path:
        if self.direct is not None:
            return self._prepare_direct()
        if self.reuse is not None:
            return self._prepare_reuse()
        self._ensure_cache()
        self._ensure_commit(base_sha)
        if git(self.cache, "rev-parse", "--verify", f"refs/heads/{self.branch}").ok:
            raise ExecutionError(f"分支已存在，拒绝覆盖: {self.branch}")
        remote_branch = git(self.cache, "rev-parse", "--verify", f"refs/remotes/origin/{self.branch}")
        if remote_branch.ok:
            raise ExecutionError(f"远端分支已存在，拒绝覆盖: {self.branch}")
        if self.path.exists():
            # 上一轮残留，先尝试正常移除
            self.remove(force=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        add = git(
            self.cache,
            "worktree",
            "add",
            "--no-track",
            "-b",
            self.branch,
            str(self.path),
            base_sha,
            timeout=600,
        )
        if not add.ok:
            raise GitError("创建 worktree 失败", stderr=clean(add.stderr), returncode=add.returncode)
        return self.path

    def _prepare_direct(self) -> Path:
        """直接使用面板指定的项目目录：不克隆、不建分支、不建 worktree。"""
        path = self.direct
        if path is None:
            raise ExecutionError("未指定项目目录")
        if not path.exists():
            raise ExecutionError(f"项目目录不存在: {path}")
        if not path.is_dir():
            raise ExecutionError(f"项目目录不是文件夹: {path}")
        if not git(path, "rev-parse", "--git-dir").ok:
            raise ExecutionError(f"项目目录不是 git 仓库: {path}")
        self.path = path
        return path

    def _prepare_reuse(self) -> Path:
        """接着用上一轮的 worktree：保留未提交改动，只把分支名改成当前 attempt。"""
        path = self.reuse
        if path is None or not path.is_dir():
            raise ExecutionError(f"要复用的 worktree 不存在: {path}")
        if not git(path, "rev-parse", "--git-dir").ok:
            raise ExecutionError(f"要复用的目录不是 git worktree: {path}")
        self.path = path
        current = git_out(path, "rev-parse", "--abbrev-ref", "HEAD")
        # 旧实现只认 codex/ 前缀，zcode 等其他执行器的复用分支不会被改名，
        # 推送时 self.branch 是新名字、本地 HEAD 还挂在旧 attempt 分支上，push 必挂
        if current and current != self.branch and current.startswith(f"{self.prefix}/"):
            # branch -m 只改名不切换，工作区里的改动原样保留
            git(path, "branch", "-m", self.branch)
        return path

    def diff_base(self, base_sha: str) -> str:
        """对比基准。直接目录模式用当前 HEAD，避免拿远端 sha 去 diff 本地不存在的提交。"""
        if self.direct is None:
            return base_sha
        head = git_out(self.path, "rev-parse", "HEAD")
        return head or base_sha

    def remove(self, *, force: bool = False) -> None:
        if self.path.exists():
            args = ["worktree", "remove"]
            if force:
                args.append("--force")
            args.append(str(self.path))
            git(self.cache, *args)
            if self.path.exists() and force:
                git(self.cache, "worktree", "prune")
        else:
            git(self.cache, "worktree", "prune")

    # -- 变更检查 ----------------------------------------------------------
    def collect_changes(self, base_sha: str) -> list[Change]:
        changes: dict[str, Change] = {}

        # codex 明确被禁止提交，但它可能仍然提交了 —— 一并纳入检查
        diff = git(self.path, "diff", "--name-status", "-z", base_sha, "--")
        if diff.ok:
            for status, path in _parse_name_status(diff.stdout):
                changes[path] = Change(status, path)

        status_out = git(self.path, "status", "--porcelain", "-z", "-uall").stdout
        for entry in status_out.split("\0"):
            if len(entry) < 4:
                continue
            code = entry[:2].strip() or "M"
            path = entry[3:]
            changes.setdefault(path, Change(code, path))
        return sorted(changes.values(), key=lambda c: c.path)

    def inspect(self, changes: list[Change], *, max_file_bytes: int) -> list[str]:
        """确定性检查：路径逃逸、符号链接、超大文件、敏感特征。"""
        problems: list[str] = []
        root = self.path.resolve()
        for change in changes:
            path = change.path
            parts = Path(path).parts
            if path.startswith("/") or ".." in parts:
                problems.append(f"{path}: 非法路径（绝对路径或包含 ..）")
                continue
            target = root / path
            if change.is_delete:
                continue
            if os.path.islink(target):
                try:
                    resolved = target.resolve()
                except OSError:
                    problems.append(f"{path}: 符号链接无法解析")
                    continue
                if not os.path.normcase(str(resolved)).startswith(os.path.normcase(str(root))):
                    problems.append(f"{path}: 符号链接逃逸出工作区（→ {resolved}）")
                continue
            if not target.exists():
                continue
            try:
                size = target.stat().st_size
            except OSError:
                continue
            if size > max_file_bytes:
                problems.append(f"{path}: 文件过大（{size} 字节 > {max_file_bytes}）")
                continue
            if _looks_binary(target):
                continue
            try:
                data = target.read_bytes()
            except OSError:
                continue
            for marker in _SECRET_MARKERS:
                if marker.search(data):
                    problems.append(f"{path}: 命中凭据特征 {marker.pattern.decode('utf-8', 'replace')}")
                    break
        return problems

    def submodules(self) -> list[str]:
        out = git(self.path, "ls-files", "-s").stdout
        return [line.split("\t", 1)[1] for line in out.splitlines() if line.startswith("160000")]

    def stage_and_commit(self, message: str) -> str:
        add = git(self.path, "add", "-A")
        if not add.ok:
            raise GitError("git add 失败", stderr=clean(add.stderr))
        diff = git(self.path, "diff", "--cached", "--name-only")
        if not diff.stdout.strip():
            raise ExecutionError("没有可提交的改动")
        commit = git(self.path, "commit", "-m", message)
        if not commit.ok:
            raise GitError("git commit 失败", stderr=clean(commit.stderr))
        return git_out(self.path, "rev-parse", "HEAD")

    def push(self, *, timeout: float = 600) -> None:
        res = git(self.path, "push", "--set-upstream", "origin", self.branch, timeout=timeout)
        if not res.ok:
            # 非快进说明有并发写者；停止并报警，绝不 force push
            raise GitError(
                f"推送分支失败（未使用 --force）: {self.branch}",
                stderr=clean(res.stderr),
                returncode=res.returncode,
            )

    def stage_paths_and_commit(self, paths: list[str], message: str) -> str:
        """只暂存指定路径并提交（direct 模式自动提交用）。

        与 stage_and_commit 的 `add -A` 不同：工作区里用户自己的未提交改动
        不在任务改动清单里，不能替人一起提交，否则等于埋雷。
        """
        if not paths:
            raise ExecutionError("没有可提交的文件列表")
        # 分批 add：Windows 命令行长度有限，改动多时一次性传路径会炸
        for start in range(0, len(paths), 100):
            add = git(self.path, "add", "-A", "--", *paths[start : start + 100])
            if not add.ok:
                raise GitError("git add 失败", stderr=clean(add.stderr))
        diff = git(self.path, "diff", "--cached", "--name-only")
        if not diff.stdout.strip():
            raise ExecutionError("没有可提交的改动")
        commit = git(self.path, "commit", "-m", message)
        if not commit.ok:
            raise GitError("git commit 失败", stderr=clean(commit.stderr))
        return git_out(self.path, "rev-parse", "HEAD")

    def push_current(self, *, timeout: float = 600) -> str:
        """推送当前检出分支（direct 模式自动提交用），返回分支名。"""
        branch = git_out(self.path, "rev-parse", "--abbrev-ref", "HEAD")
        if not branch or branch == "HEAD":
            raise GitError("当前处于 detached HEAD，无法确定推送分支")
        res = git(self.path, "push", "--set-upstream", "origin", branch, timeout=timeout)
        if not res.ok:
            # 与 push() 同策略：非快进绝不 force
            raise GitError(
                f"推送分支失败（未使用 --force）: {branch}",
                stderr=clean(res.stderr),
                returncode=res.returncode,
            )
        return branch

    def diff_stat(self, base_sha: str) -> str:
        out = git(self.path, "diff", "--stat", f"{base_sha}..HEAD").stdout.strip()
        return clean(out, 4000)


def _parse_name_status(raw: str) -> list[tuple[str, str]]:
    parts = [p for p in raw.split("\0")]
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(parts):
        status = parts[i].strip()
        if not status:
            i += 1
            continue
        if status.startswith("R") or status.startswith("C"):
            if i + 2 >= len(parts):
                break
            out.append((status[0], parts[i + 2]))
            i += 3
        else:
            if i + 1 >= len(parts):
                break
            out.append((status[0], parts[i + 1]))
            i += 2
    return out


def _looks_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(8000)
    except OSError:
        return True
    if b"\0" in chunk:
        return True
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False
