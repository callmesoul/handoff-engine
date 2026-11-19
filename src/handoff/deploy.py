"""部署对齐。

版本接口至少返回 {"sha": "<full sha>"}。比较用完整 SHA，不比较前七位。
超时不写进 SHA 字段，写 deployment.status。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import EnvConfig, PollConfig, RepoConfig
from .errors import ExecutionError, GitError
from .gitcmd import git
from .http_client import HttpClient
from .sanitize import clean

_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_SHA_KEYS = ("sha", "commit", "commit_sha", "git_sha", "revision", "version")


@dataclass
class DeploymentResult:
    status: str  # deployed | timeout | failed
    observed_sha: str | None
    attempts: int
    error: str | None = None


class CommitAncestry:
    """用 Git 提交图判断 observed 是否包含 target。

    SHA 本身没有可比较的先后顺序；只有 target 是 observed 的祖先时，才能
    证明共享 dev 已经部署了目标改动。优先复用 worker 的仓库缓存，避免重复
    clone；前端机没有缓存时会建立只读 bare 缓存。
    """

    def __init__(self, root: str | Path, repo: RepoConfig):
        self.root = Path(root)
        self.repo = repo
        self.cache = repo.cache_path(self.root)
        self._refreshed = False

    def contains(self, target_sha: str, observed_sha: str) -> tuple[bool, str | None]:
        if target_sha == observed_sha:
            return True, None
        try:
            self._ensure_cache()
            self._ensure_commit(target_sha)
            self._ensure_commit(observed_sha)
        except (ExecutionError, GitError) as exc:
            return False, exc.message

        result = git(self.cache, "merge-base", "--is-ancestor", target_sha, observed_sha, timeout=60)
        if result.returncode == 0:
            return True, None
        if result.returncode == 1:
            return False, None
        return False, f"无法比较部署提交关系: {clean(result.stderr, 500)}"

    def _ensure_cache(self) -> None:
        if (self.cache / ".git").exists() or (self.cache / "HEAD").is_file():
            if not self._refreshed:
                ref = self.repo.pr_base_ref or self.repo.default_branch
                result = git(
                    self.cache,
                    "fetch",
                    "--no-tags",
                    "origin",
                    f"+refs/heads/{ref}:refs/remotes/origin/{ref}",
                    timeout=300,
                )
                if not result.ok:
                    raise ExecutionError("更新仓库提交图失败", stderr=clean(result.stderr, 500))
                self._refreshed = True
            return

        self.cache.parent.mkdir(parents=True, exist_ok=True)
        result = git(
            self.root,
            "clone",
            "--bare",
            "--filter=blob:none",
            "--no-tags",
            "--quiet",
            self.repo.remote_url,
            str(self.cache),
            timeout=900,
        )
        if not result.ok:
            raise ExecutionError(
                f"无法读取仓库 {self.repo.id} 的提交图",
                stderr=clean(result.stderr, 500),
            )
        self._refreshed = True

    def _ensure_commit(self, sha: str) -> None:
        if git(self.cache, "cat-file", "-e", f"{sha}^{{commit}}", timeout=30).ok:
            return
        result = git(self.cache, "fetch", "--no-tags", "origin", sha, timeout=300)
        if not result.ok or not git(self.cache, "cat-file", "-e", f"{sha}^{{commit}}", timeout=30).ok:
            raise ExecutionError(
                f"仓库 {self.repo.id} 中找不到部署提交 {sha[:12]}",
                stderr=clean(result.stderr, 500),
            )


class VersionProbe:
    def __init__(self, client: HttpClient | None = None):
        self.client = client or HttpClient()

    def fetch(self, env: EnvConfig) -> tuple[str | None, str | None]:
        url = env.version_url()
        if not url:
            return None, f"环境 {env.name} 未配置 url，无法确认部署版本"
        try:
            resp = self.client.request("GET", url, headers={"Accept": "application/json"})
        except ExecutionError as exc:
            return None, exc.message
        if resp.status >= 400:
            return None, f"{url} 返回 HTTP {resp.status}"
        try:
            payload = resp.json()
        except ValueError:
            return None, f"{url} 返回的不是 JSON"
        if not isinstance(payload, dict):
            return None, f"{url} 返回的 JSON 不是对象"
        for key in _SHA_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and _SHA_RE.match(value.strip()):
                return value.strip(), None
        return None, f"{url} 未返回可识别的完整 SHA（接受字段：{', '.join(_SHA_KEYS)}）"


def wait_for_deployment(
    env: EnvConfig,
    target_sha: str,
    poll: PollConfig,
    *,
    probe: VersionProbe | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    logger: Callable[[str], None] | None = None,
    contains: Callable[[str, str], tuple[bool, str | None]] | None = None,
) -> DeploymentResult:
    probe = probe or VersionProbe()
    started = clock()
    interval = max(1.0, poll.interval_seconds)
    attempts = 0
    last_error: str | None = None
    observed: str | None = None

    while True:
        attempts += 1
        observed, error = probe.fetch(env)
        if error:
            last_error = error
            if logger:
                logger(f"版本接口未就绪: {error}")
        elif observed:
            deployed = observed == target_sha
            relation_error = None
            if not deployed and contains is not None:
                deployed, relation_error = contains(target_sha, observed)
            if deployed:
                return DeploymentResult("deployed", observed, attempts)
            if relation_error:
                last_error = f"无法确认 {observed[:12]} 是否包含目标 {target_sha[:12]}: {relation_error}"
            else:
                last_error = f"版本接口当前是 {observed[:12]}，尚未包含目标 {target_sha[:12]}"
            if logger:
                logger(last_error)

        elapsed = clock() - started
        if elapsed + interval > poll.timeout_seconds:
            return DeploymentResult("timeout", observed, attempts, clean(last_error, 500))
        sleep(interval)
        interval = min(interval * max(1.0, poll.backoff_factor), poll.max_interval_seconds)
