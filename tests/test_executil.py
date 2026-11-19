"""可执行文件解析：不依赖调用方 PATH。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from handoff import executil, gitcmd
from handoff.errors import ExecutionError


def test_resolves_git_even_with_empty_path(monkeypatch):
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("HANDOFF_GIT", raising=False)
    gitcmd.reset_binary_cache()
    try:
        resolved = gitcmd.git_binary()
        assert Path(resolved).is_file()
        assert Path(resolved).name.startswith("git")
        # 能真的跑起来，而不是只解析出一个路径
        assert gitcmd.git(".", "--version").ok
    finally:
        gitcmd.reset_binary_cache()


def test_env_override_wins(monkeypatch):
    real = shutil.which("git")
    assert real
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("HANDOFF_GIT", real)
    gitcmd.reset_binary_cache()
    try:
        assert gitcmd.git_binary() == real
    finally:
        gitcmd.reset_binary_cache()


def test_missing_binary_message_is_actionable(monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(ExecutionError) as excinfo:
        executil.require("definitely-not-a-real-binary-xyz", env_var="HANDOFF_XYZ")
    message = str(excinfo.value)
    assert "找不到可执行文件" in message
    assert "HANDOFF_XYZ" in message


def test_with_binary_dir_prepends(monkeypatch):
    env = {"PATH": "/a:/b"}
    executil.with_binary_dir(env, "/usr/bin/git")
    assert env["PATH"].split(":")[0] == "/usr/bin"
    # 不重复插入
    executil.with_binary_dir(env, "/usr/bin/git")
    assert env["PATH"].split(":").count("/usr/bin") == 1


def test_git_env_differs_on_windows():
    from handoff.gitcmd import git_env

    posix = git_env(force_windows=False)
    windows = git_env(force_windows=True)
    assert posix["GIT_ASKPASS"] == "true"
    assert "GIT_ASKPASS" not in windows
    for env in (posix, windows):
        assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_taskkill_argv_targets_process_tree():
    from handoff.codex_runner import taskkill_argv

    assert taskkill_argv(4321) == ["taskkill", "/F", "/T", "/PID", "4321"]

def test_codex_update_dir_falls_back_to_newest_bin(tmp_path, monkeypatch):
    """codex 自更新会换 bin/<hash>/ 目录：写死的老路径必须能自动落到最新版。"""
    import os

    base = tmp_path / "OpenAI" / "Codex" / "bin"
    old = base / "aaaaaaaaaaaaaaaa"
    new = base / "bbbbbbbbbbbbbbbb"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / "codex.exe").write_text("old", encoding="utf-8")
    (new / "codex.exe").write_text("new", encoding="utf-8")
    os.utime(old / "codex.exe", (1_700_000_000, 1_700_000_000))
    os.utime(new / "codex.exe", (1_800_000_000, 1_800_000_000))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("HANDOFF_CODEX", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)

    # 配置里写死的旧目录已被升级换掉 → 自动落到 bin/* 里最新的那个
    resolved = executil.resolve(str(base / "gone" / "codex.exe"), env_var="HANDOFF_CODEX")
    assert Path(resolved) == new / "codex.exe"

    # 写死的路径真实存在时优先用它，不去猜
    assert executil.resolve(str(old / "codex.exe")) == str(old / "codex.exe")
