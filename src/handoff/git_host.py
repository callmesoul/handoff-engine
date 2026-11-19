"""PR 平台适配层。

Phase 0 决策：只做 GitHub（走 `gh`），不在核心逻辑里混写两套命令。
接 GitLab 时新增一个实现，接口不变。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import executil
from .errors import ExecutionError
from .gitcmd import run
from .sanitize import clean
from .timeutil import iso


@dataclass
class CheckRun:
    name: str
    conclusion: str | None
    head_sha: str | None = None
    url: str | None = None

    @property
    def green(self) -> bool:
        return (self.conclusion or "").upper() in ("SUCCESS", "NEUTRAL", "SKIPPED")


@dataclass
class PRState:
    number: int
    url: str
    state: str  # OPEN | CLOSED | MERGED
    head_sha: str | None = None
    merge_commit_sha: str | None = None
    mergeable: str | None = None  # MERGEABLE | CONFLICTING | UNKNOWN
    merge_state_status: str | None = None
    checks: list[CheckRun] = field(default_factory=list)

    @property
    def merged(self) -> bool:
        return self.state.upper() == "MERGED" and bool(self.merge_commit_sha)


class GitHost(Protocol):
    def create_pr(self, repo: str, *, head: str, base: str, title: str, body: str) -> PRState: ...
    def get_pr(self, repo: str, number: int) -> PRState: ...
    def enable_auto_merge(self, repo: str, number: int, *, method: str = "squash") -> None: ...
    def close_pr(self, repo: str, number: int) -> None: ...


class GitHubHost:
    def __init__(self, binary: str = "gh", timeout: float = 60):
        self.binary = binary
        self.timeout = timeout
        self._resolved: str | None = None

    def binary_path(self) -> str:
        if self._resolved is None:
            self._resolved = executil.require(
                self.binary,
                env_var="HANDOFF_GH",
                hint="安装 GitHub CLI 并完成 gh auth login",
            )
        return self._resolved

    def _gh(self, args: list[str], *, input_text: str | None = None, check: bool = True) -> str:
        res = run([self.binary_path(), *args], timeout=self.timeout, input_text=input_text, check_env=False)
        if check and not res.ok:
            raise ExecutionError(f"gh {' '.join(args[:2])} 失败", stderr=clean(res.stderr), code_text=res.returncode)
        return res.stdout

    def create_pr(self, repo: str, *, head: str, base: str, title: str, body: str) -> PRState:
        out = self._gh(
            [
                "pr",
                "create",
                "--repo",
                repo,
                "--head",
                head,
                "--base",
                base,
                "--title",
                title,
                "--body-file",
                "-",
            ],
            input_text=body,
        )
        url = out.strip().splitlines()[-1].strip()
        match = re.search(r"/pull/(\d+)", url)
        if not match:
            raise ExecutionError(f"无法从 gh 输出解析 PR 编号: {clean(out, 200)}")
        return self.get_pr(repo, int(match.group(1)))

    def get_pr(self, repo: str, number: int) -> PRState:
        raw = self._gh(
            [
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "number,url,state,headRefOid,mergeCommit,mergeable,mergeStateStatus,statusCheckRollup",
            ]
        )
        data = json.loads(raw)
        merge_commit = data.get("mergeCommit") or {}
        checks = []
        for item in data.get("statusCheckRollup") or []:
            checks.append(
                CheckRun(
                    name=item.get("name") or item.get("context") or "<unknown>",
                    conclusion=item.get("conclusion") or item.get("state"),
                    head_sha=item.get("headOid") or data.get("headRefOid"),
                    url=item.get("targetUrl") or item.get("detailsUrl"),
                )
            )
        return PRState(
            number=int(data["number"]),
            url=data["url"],
            state=data.get("state", "OPEN"),
            head_sha=data.get("headRefOid"),
            merge_commit_sha=merge_commit.get("oid"),
            mergeable=data.get("mergeable"),
            merge_state_status=data.get("mergeStateStatus"),
            checks=checks,
        )

    def enable_auto_merge(self, repo: str, number: int, *, method: str = "squash") -> None:
        self._gh(["pr", "merge", str(number), "--repo", repo, f"--{method}", "--auto"])

    def close_pr(self, repo: str, number: int) -> None:
        self._gh(["pr", "close", str(number), "--repo", repo])


class NullHost:
    """没有平台凭据时的显式降级：创建 PR 直接失败，不静默假装成功。"""

    def create_pr(self, *args, **kwargs) -> PRState:  # noqa: D401
        raise ExecutionError("未配置 Git 托管凭据（gh），无法创建 PR")

    def get_pr(self, *args, **kwargs) -> PRState:
        raise ExecutionError("未配置 Git 托管凭据（gh），无法查询 PR")

    def enable_auto_merge(self, *args, **kwargs) -> None:
        raise ExecutionError("未配置 Git 托管凭据（gh），无法开启自动合并")

    def close_pr(self, *args, **kwargs) -> None:
        raise ExecutionError("未配置 Git 托管凭据（gh），无法关闭 PR")


class LocalHost:
    """沙箱专用：把 PR 记到本地文件，完全不联网。

    不模拟合并 —— enable_auto_merge 直接报错。没有真实平台，就不该产出 merge_sha。
    """

    def __init__(self, root: str | Path):
        self.dir = Path(root) / ".handoff-run" / "local-prs"

    def _repo_dir(self, repo: str) -> Path:
        return self.dir / repo.replace("/", "__")

    def _path(self, repo: str, number: int) -> Path:
        return self._repo_dir(repo) / f"pr-{number}.json"

    def create_pr(self, repo: str, *, head: str, base: str, title: str, body: str) -> PRState:
        directory = self._repo_dir(repo)
        directory.mkdir(parents=True, exist_ok=True)
        existing = sorted(directory.glob("pr-*.json"))
        number = max((int(p.stem.split("-")[1]) for p in existing), default=0) + 1
        payload = {
            "repo": repo,
            "number": number,
            "head": head,
            "base": base,
            "title": title,
            "body": body,
            "state": "OPEN",
            "created_at": iso(),
            "note": "本地沙箱记录，不是真实 PR",
        }
        self._path(repo, number).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return PRState(
            number=number,
            url=f"local://{repo}/pr/{number}",
            state="OPEN",
            mergeable="MERGEABLE",
        )

    def get_pr(self, repo: str, number: int) -> PRState:
        path = self._path(repo, number)
        if not path.is_file():
            raise ExecutionError(f"本地 PR 记录不存在: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        return PRState(
            number=number,
            url=f"local://{repo}/pr/{number}",
            state=payload.get("state", "OPEN"),
            mergeable="MERGEABLE",
        )

    def enable_auto_merge(self, repo: str, number: int, *, method: str = "squash") -> None:
        raise ExecutionError("本地沙箱不模拟合并：没有真实平台，就不会有 merge_sha")

    def close_pr(self, repo: str, number: int) -> None:
        path = self._path(repo, number)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["state"] = "CLOSED"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def get_host(name: str = "github", *, root: str | Path | None = None) -> GitHost:
    if name == "github":
        return GitHubHost()
    if name == "null":
        return NullHost()
    if name == "local":
        if root is None:
            raise ExecutionError("local 托管需要交接仓根目录")
        return LocalHost(root)
    raise ExecutionError(f"未支持的托管平台: {name}")
