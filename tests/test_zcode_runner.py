"""ZcodeRunner 单元测试：命令构造、输出解析、超时与取消路径。"""

from __future__ import annotations

import json

import pytest

from handoff import zcode_runner
from handoff.codex_runner import HeadlessResult
from handoff.errors import ConfigError
from handoff.zcode_runner import ZcodeRunner, session_uuid


@pytest.fixture(autouse=True)
def _fake_binary(monkeypatch):
    monkeypatch.setattr(ZcodeRunner, "binary_path", lambda self: "zcode-fake")


def _result_json(summary="ok"):
    return json.dumps(
        {"status": "completed", "summary": summary, "changed_paths": ["src/x.py"],
         "tests": [], "migration_required": False, "risks": [], "notes": "",
         "commit_message": "test commit"}
    )


# -- 命令构造 ---------------------------------------------------------------


def test_session_uuid_is_deterministic():
    assert session_uuid("ho-1", 1) == session_uuid("ho-1", 1)
    assert session_uuid("ho-1", 1) != session_uuid("ho-1", 2)
    assert session_uuid("ho-1", 3) != session_uuid("ho-2", 3)


def test_command_new_session_uses_pregenerated_uuid():
    runner = ZcodeRunner(permission_mode="acceptEdits")
    cmd, sid = runner._command("ho-1", 2)
    assert cmd[1:6] == ["-p", "--output-format", "json", "--permission-mode", "acceptEdits"]
    assert "--session-id" in cmd
    assert sid in cmd
    assert sid == session_uuid("ho-1", 2)
    # prompt 走 stdin：没有位置参数
    flagsWithValue = {"--output-format", "--permission-mode", "--model", "--effort", "--session-id", "--resume"}
    tokens = cmd[1:]
    positional = [
        t for i, t in enumerate(tokens)
        if not t.startswith("-") and (i == 0 or tokens[i - 1] not in flagsWithValue)
    ]
    assert not positional


def test_command_resume_and_overrides():
    runner = ZcodeRunner(
        resume_session="sess_abc",
        permission_mode="yolo",
        model="glm-5.3",
        effort="high",
    )
    cmd, sid = runner._command("ho-1", 5)
    assert cmd[1:6] == ["-p", "--output-format", "json", "--permission-mode", "yolo"]
    assert "--model" in cmd and "glm-5.3" in cmd
    assert "--effort" in cmd and "high" in cmd
    assert "--resume" in cmd and "sess_abc" in cmd
    assert "--session-id" not in cmd
    assert sid == "sess_abc"


def test_invalid_permission_mode_rejected():
    with pytest.raises(ConfigError):
        ZcodeRunner(permission_mode="nope")


# -- run() 输出解析 ----------------------------------------------------------


def _fake_headless(monkeypatch, **kwargs):
    result = HeadlessResult(**kwargs)
    monkeypatch.setattr(zcode_runner, "run_headless", lambda *a, **k: result)
    return result


def test_run_completed_extracts_structured_result(monkeypatch, tmp_path):
    envelope = {
        "session_id": "sess_new",
        "result": "最后结果：\n```json\n" + _result_json("加了字段") + "\n```",
        "is_error": False,
    }
    _fake_headless(monkeypatch, stdout_text=json.dumps(envelope), exit_code=0)
    runner = ZcodeRunner(log_dir=tmp_path)
    outcome = runner.run(tmp_path, "prompt", task_id="ho-1", attempt=1)
    assert outcome.status == "completed"
    assert outcome.result["summary"] == "加了字段"
    assert outcome.thread_id == "sess_new"
    assert outcome.run_id == "sess_new"
    assert outcome.log_path and "zcode-" in outcome.log_path


def test_run_reports_error_envelope(monkeypatch, tmp_path):
    envelope = {"session_id": "sess_x", "result": "登录过期", "is_error": True}
    _fake_headless(monkeypatch, stdout_text=json.dumps(envelope), exit_code=1)
    outcome = ZcodeRunner().run(tmp_path, "p", task_id="ho-1", attempt=1)
    assert outcome.status == "failed"
    assert "登录过期" in outcome.message


def test_run_invalid_when_no_json_in_result(monkeypatch, tmp_path):
    envelope = {"session_id": "sess_x", "result": "我做完了，改了三个文件", "is_error": False}
    _fake_headless(monkeypatch, stdout_text=json.dumps(envelope), exit_code=0)
    outcome = ZcodeRunner().run(tmp_path, "p", task_id="ho-1", attempt=1)
    assert outcome.status == "invalid_output"
    assert outcome.thread_id == "sess_x"


def test_run_invalid_when_stdout_not_json(monkeypatch, tmp_path):
    _fake_headless(monkeypatch, stdout_text="随便一段文本", exit_code=0)
    outcome = ZcodeRunner(resume_session="sess_known").run(tmp_path, "p", task_id="ho-1", attempt=1)
    assert outcome.status == "invalid_output"
    # 会话 id 已知（resume 传入），即便输出解析失败也要带回
    assert outcome.thread_id == "sess_known"


def test_run_timeout(monkeypatch, tmp_path):
    _fake_headless(monkeypatch, stdout_text="", timed_out=True, exit_code=1)
    outcome = ZcodeRunner(timeout_seconds=5).run(tmp_path, "p", task_id="ho-1", attempt=1)
    assert outcome.status == "timed_out"
    assert "超时" in outcome.message


def test_run_launch_error(monkeypatch, tmp_path):
    _fake_headless(monkeypatch, stdout_text="", launch_error="找不到可执行文件: zcode")
    outcome = ZcodeRunner().run(tmp_path, "p", task_id="ho-1", attempt=1)
    assert outcome.status == "failed"
    assert "找不到" in outcome.message


def test_run_exit_code_failure_downgrades_completed(monkeypatch, tmp_path):
    envelope = {"session_id": "s", "result": _result_json(), "is_error": False}
    _fake_headless(monkeypatch, stdout_text=json.dumps(envelope), exit_code=2)
    outcome = ZcodeRunner().run(tmp_path, "p", task_id="ho-1", attempt=1)
    assert outcome.status == "failed"
