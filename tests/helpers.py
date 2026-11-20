"""测试脚手架：临时交接仓、本地 bare 远端、假 Codex / 假托管平台。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(Path.home()),
        },
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失败:\n{proc.stderr}")
    return proc.stdout.strip()


def make_bare_remote(tmp_path: Path, name: str, files: dict[str, str], branch: str = "dev") -> Path:
    """建一个 bare 仓库，并往指定分支塞一次提交。返回 bare 路径。"""
    bare = tmp_path / f"{name}.git"
    bare.mkdir(parents=True)
    git(bare, "init", "--bare", "-b", "main")
    seed = tmp_path / f"{name}-seed"
    seed.mkdir()
    git(seed, "init", "-b", branch)
    for rel, content in files.items():
        path = seed / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "seed")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "origin", f"{branch}:{branch}")
    if branch != "main":
        git(seed, "push", "origin", f"{branch}:main")
    return bare


def make_store_repo(tmp_path: Path, name: str = "handoff") -> tuple[Path, Path]:
    """交接仓工作副本 + bare 远端。"""
    bare = tmp_path / f"{name}-remote.git"
    bare.mkdir(parents=True)
    git(bare, "init", "--bare", "-b", "main")
    work = tmp_path / name
    work.mkdir()
    git(work, "init", "-b", "main")
    for rel in ("tasks/.gitkeep", "events/.gitkeep", "contracts/.gitkeep", "artifacts/.gitkeep"):
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-m", "init")
    git(work, "remote", "add", "origin", str(bare))
    git(work, "push", "-u", "origin", "main")
    return work, bare


def write_config(
    root: Path,
    *,
    repos: list[dict],
    environments: list[dict] | None = None,
    checks: dict | None = None,
    admin: dict | None = None,
    codex: dict | None = None,
) -> None:
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    (config_dir / "repositories.yaml").write_text(
        yaml.safe_dump(
            {
                "store": {"repo": None, "branch": "main", "remote": "origin"},
                "repositories": repos,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (config_dir / "environments.yaml").write_text(
        yaml.safe_dump(
            {
                "environments": environments
                or [
                    {
                        "name": "dev",
                        "url": "https://dev.example.com",
                        "version_path": "/version",
                        "shared": True,
                        "concurrency_group": "deploy-dev",
                    }
                ],
                "deploy_poll": {
                    "interval_seconds": 1,
                    "timeout_seconds": 5,
                    "request_timeout_seconds": 2,
                },
                "verify_poll": {"interval_seconds": 1, "timeout_seconds": 5, "request_timeout_seconds": 2},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (config_dir / "checks.yaml").write_text(
        yaml.safe_dump(
            checks
            or {
                "test_suites": {},
                "repo_scripts": {},
                "gate": {
                    "required_checks": [],
                    "high_risk_paths": [".github/**", "infra/prod/**", "**/migrations/**"],
                    "deny_auto_merge_if_migration_required": True,
                    "max_changed_files": 50,
                    "max_file_bytes": 65536,
                },
                "verifier": {"output_limit_chars": 2000, "max_parallel": 1},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    if codex:
        (config_dir / "codex.yaml").write_text(
            yaml.safe_dump({"codex": codex}, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

    (config_dir / "admin.yaml").write_text(
        yaml.safe_dump(
            admin
            or {
                "index": {"database": ".handoff-run/index/handoff.sqlite3"},
                "server": {"host": "127.0.0.1", "port": 0, "read_only": False},
                "users": [
                    {"token": "viewer-token", "id": "viewer@test", "role": "viewer", "repos": ["*"]},
                    {"token": "operator-token", "id": "operator@test", "role": "operator", "repos": ["org/api"]},
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def repo_entry(repo_id: str, role: str, remote: Path, cache_dir: str | None = None, host: str = "github") -> dict:
    entry = {
        "id": repo_id,
        "role": role,
        "host": host,
        "default_branch": "main",
        "allowed_base_refs": ["dev"],
        "pr_base_ref": "dev",
        "remote": str(remote),
        "cache_dir": cache_dir or f".handoff-run/repo-cache/{repo_id.replace('/', '__')}",
    }
    if role == "backend":
        entry["scope"] = {
            "default_profile": "application",
            "forbidden_paths": [".github/**", "infra/prod/**"],
            "profiles": {
                "application": {
                    "allowed_paths": ["src/**", "tests/**"],
                }
            },
        }
    return entry


def make_task(
    task_id: str,
    *,
    base_sha: str | None,
    allowed_paths: list[str] | None = None,
    forbidden_paths: list[str] | None = None,
    target_repo: str = "org/api",
    source_repo: str = "org/web",
    environment: str = "dev",
    checks: list[dict] | None = None,
    migration: dict | None = None,
    requirement: str = "为订单接口增加 status 字段",
) -> dict:
    target = {"role": "backend", "repo": target_repo, "base_ref": "dev"}
    if base_sha is not None:
        target["base_sha"] = base_sha
    return {
        "schema_version": 1,
        "id": task_id,
        "revision": 1,
        "attempt": 0,
        "title": "订单接口增加状态字段",
        "source": {"role": "frontend", "repo": source_repo, "commit_sha": "a" * 40},
        "target": target,
        "requirement": requirement,
        "contract": None,
        "repro": None,
        "scope": {
            "out_of_scope": ["不要动鉴权模块"],
            "allowed_paths": allowed_paths or ["src/orders/**", "tests/orders/**"],
            "forbidden_paths": forbidden_paths or [".github/**", "infra/prod/**"],
        },
        "acceptance": {"environment": environment, "checks": checks or []},
        "migration": migration
        or {
            "required": False,
            "backward_compatible": True,
            "plan": "",
            "rollback": "",
            "manual_approval_required": False,
        },
        "status": "open",
        "claim": None,
        "result": {
            "branch": None,
            "head_sha": None,
            "pr_url": None,
            "pr_number": None,
            "merge_sha": None,
            "codex_run_id": None,
            "codex_cli_version": None,
            "prompt_version": None,
            "diff_summary": None,
            "changed_paths": [],
            "worktree": None,
            "codex_thread_id": None,
            "checks": [],
            "notes": None,
        },
        "deployment": {
            "status": "pending",
            "target_sha": None,
            "observed_sha": None,
            "environment": environment,
            "url": None,
            "updated_at": None,
            "error": None,
        },
        "failure": None,
        "created_at": "2026-11-05T00:00:00Z",
        "updated_at": "2026-11-05T00:00:00Z",
    }


class FakeCodex:
    """假 codex：按脚本写文件，返回结构化结果。"""

    def __init__(self, files: dict[str, str] | None = None, *, status: str = "completed", message: str = "ok"):
        self.files = files if files is not None else {"src/orders/Status.java": "class Status {}\n"}
        self.status = status
        self.message = message
        self.calls: list[dict] = []
        self.cli_version = "codex-cli 0.0.0-fake"

    def run(self, worktree, prompt, *, task_id, attempt, cancel_event=None):
        from handoff.codex_runner import CodexOutcome

        self.calls.append({"task_id": task_id, "attempt": attempt, "prompt": prompt, "worktree": str(worktree)})
        if self.status == "completed":
            for rel, content in self.files.items():
                path = Path(worktree) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
        result = {
            "status": "blocked" if self.status == "blocked" else self.status,
            "summary": self.message,
            "changed_paths": list(self.files),
            "tests": [{"name": "unit", "status": "passed" if self.status == "completed" else "failed", "output": ""}],
            "migration_required": False,
            "risks": [],
            "notes": "",
        }
        return CodexOutcome(
            status=self.status,
            result=result if self.status in ("completed", "blocked") else None,
            message=self.message,
            run_id="run-fake",
            thread_id="thread-fake",
            exit_code=0,
            cli_version=self.cli_version,
            duration_seconds=0.1,
        )


class FakeHost:
    def __init__(self, *, merged: bool = False, remotes: dict[str, Path] | None = None):
        self.prs: dict[int, dict] = {}
        self.auto_merge_calls: list[int] = []
        self._next = 1
        self.should_merge = merged
        self.merge_done = False
        self.remotes = remotes or {}

    def create_pr(self, repo, *, head, base, title, body):
        from handoff.git_host import PRState

        number = self._next
        self._next += 1
        state = PRState(
            number=number,
            url=f"https://github.com/{repo}/pull/{number}",
            state="OPEN",
            head_sha="0" * 40,
            mergeable="MERGEABLE",
        )
        self.prs[number] = {"repo": repo, "head": head, "base": base, "title": title, "body": body}
        return state

    def _head_sha(self, info: dict) -> str | None:
        remote = self.remotes.get(info["repo"])
        if not remote:
            return info.get("head_sha")
        out = git(Path(remote), "rev-parse", f"refs/heads/{info['head']}")
        return out or None

    def get_pr(self, repo, number):
        from handoff.git_host import PRState

        info = self.prs[number]
        return PRState(
            number=number,
            url=f"https://github.com/{repo}/pull/{number}",
            state="MERGED" if self.merge_done else "OPEN",
            head_sha=self._head_sha(info),
            merge_commit_sha="b" * 40 if self.merge_done else None,
            mergeable="MERGEABLE",
        )

    def enable_auto_merge(self, repo, number, *, method="squash"):
        self.auto_merge_calls.append(number)
        self.merge_done = self.should_merge

    def close_pr(self, repo, number):
        pass


def load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
