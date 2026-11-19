"""store.sync() 的并发安全：不能丢弃本地已提交但未推送的状态。

面板常驻轮询和 CLI 长验收共用同一个 clone，历史上 `reset --hard origin/main`
会把对端刚写完、还没 push 出去的任务状态回滚掉，表现为重新验收时读回旧状态。
"""

from __future__ import annotations

import subprocess

import pytest

from handoff.config import Config
from handoff.errors import GitError
from handoff.store import TaskStore
from helpers import git, make_bare_remote, make_store_repo, repo_entry, write_config


def _build(tmp_path):
    work, bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api", {"src/orders/Order.java": "class Order {}\n"})
    write_config(work, repos=[repo_entry("org/api", "backend", backend)])
    return work, bare, Config.load(work)


def _head(repo) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def _remote_head(bare) -> str:
    return git(bare, "rev-parse", "main").strip()


def _is_ancestor(repo, ancestor: str, descendant: str) -> bool:
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=str(repo), capture_output=True,
    ).returncode == 0


def _local_commit(work, rel: str, content: str, message: str) -> str:
    path = work / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-m", message)
    return _head(work)


def test_sync_pushes_instead_of_dropping_local_commit(tmp_path):
    """本地领先远端时，sync 必须保住本地提交（而不是 reset 掉）。"""
    work, bare, config = _build(tmp_path)
    local = _local_commit(work, "tasks/local-only.json", '{"id": "local"}\n', "local work")
    assert _remote_head(bare) != local

    TaskStore(work, config).sync()

    assert _head(work) == local, "本地未推送提交被 sync 丢弃了"
    assert _remote_head(bare) == local, "sync 应把本地领先的提交推上去"


def test_sync_fast_forwards_when_local_is_behind(tmp_path):
    """本地落后且无独有提交时，正常快进到远端。"""
    work, bare, config = _build(tmp_path)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.name", "t"], check=True)
    remote_new = _local_commit(other, "tasks/from-other.json", '{"id": "other"}\n', "other work")
    subprocess.run(["git", "-C", str(other), "push", "-q", "origin", "main"], check=True)

    TaskStore(work, config).sync()

    assert _head(work) == remote_new
    assert (work / "tasks" / "from-other.json").exists()


def test_sync_rebases_diverged_history(tmp_path):
    """两边各有独有提交时用 rebase 合并，两边的提交都要保留。"""
    work, bare, config = _build(tmp_path)
    local = _local_commit(work, "tasks/mine.json", '{"id": "mine"}\n', "mine")

    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.name", "t"], check=True)
    theirs = _local_commit(other, "tasks/theirs.json", '{"id": "theirs"}\n', "theirs")
    subprocess.run(["git", "-C", str(other), "push", "-q", "origin", "main"], check=True)

    TaskStore(work, config).sync()

    head = _head(work)
    assert head != local and head != theirs
    assert _is_ancestor(work, theirs, head), "远端提交丢失"
    assert (work / "tasks" / "theirs.json").exists()
    assert (work / "tasks" / "mine.json").exists()


def test_task_commit_points_at_pushed_state(tmp_path):
    """补发完成通知要的 store_commit：取已推送分支上该任务单的最后一次变更。"""
    work, _bare, config = _build(tmp_path)
    store = TaskStore(work, config)
    commit = _local_commit(work, "tasks/ho-20261117-01TEST.json", '{"id": "x"}\n', "state")
    store.sync()

    assert store.task_commit("ho-20261117-01TEST") == commit


def test_task_commit_is_empty_for_unknown_task(tmp_path):
    work, _bare, config = _build(tmp_path)
    assert TaskStore(work, config).task_commit("ho-20261117-01NOSUCH") == ""


DIRTY_PATH = "tasks/.gitkeep"  # 已跟踪文件：git status -uno 能看到它


def _uncommitted_change(work) -> None:
    """改动一个已跟踪文件，令 git status --porcelain -uno 报告 dirty。"""
    (work / DIRTY_PATH).write_text("LOCAL_UNCOMMITTED_EDIT\n", encoding="utf-8")


def test_sync_default_rejects_dirty_worktree(tmp_path):
    """默认行为不变：dirty 工作区不得 sync。"""
    work, _bare, config = _build(tmp_path)
    _uncommitted_change(work)

    with pytest.raises(GitError, match="工作区有未提交改动"):
        TaskStore(work, config).sync()
    assert (work / DIRTY_PATH).read_text(encoding="utf-8") == "LOCAL_UNCOMMITTED_EDIT\n"


def test_sync_allow_dirty_never_resets_worktree(tmp_path):
    """allow_dirty=True 只允许读写任务，绝不能 reset --hard 销毁未提交源码。"""
    work, bare, config = _build(tmp_path)
    # 让远端领先一个提交；旧实现会 checkout + reset --hard 到该提交，把本地改动冲掉。
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.name", "t"], check=True)
    _local_commit(other, "tasks/from-other.json", '{"id": "other"}\n', "other work")
    subprocess.run(["git", "-C", str(other), "push", "-q", "origin", "main"], check=True)

    _uncommitted_change(work)
    TaskStore(work, config, allow_dirty=True).sync()

    # 旧实现在这里会 reset --hard 到远端，把未提交的改动清空。
    assert (work / DIRTY_PATH).read_text(encoding="utf-8") == "LOCAL_UNCOMMITTED_EDIT\n"
