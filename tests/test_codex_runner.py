"""codex 输出解析：额度耗尽等执行器侧失败要透出真实原因，不能被「输出不是合法结构化结果」吞掉。"""

from __future__ import annotations

import json

import pytest

from handoff import codex_runner
from handoff.codex_runner import (
    CodexRunner,
    HeadlessResult,
    _failure_from_events,
    parse_jsonl,
)

# attempt1 of ho-20261117-01M7MBHFTY532B9BP2RC129SDZ 的真实事件流（额度耗尽）
OUT_OF_CREDITS_STREAM = "\n".join(
    [
        '{"type":"thread.started","thread_id":"01a1e956-35af-7002-9e4a-29e582c12986"}',
        '{"type":"item.completed","item":{"id":"item_0","type":"error","message":'
        '"Under-development features enabled: enable_mcp_apps."}}',
        '{"type":"turn.started"}',
        '{"type":"error","message":"Your workspace is out of credits. '
        'Ask your workspace owner to refill in order to continue."}',
        '{"type":"turn.failed","error":{"message":"Your workspace is out of credits. '
        'Ask your workspace owner to refill in order to continue."}}',
    ]
)


def test_failure_from_events_reads_turn_failed():
    msg = _failure_from_events(OUT_OF_CREDITS_STREAM)
    assert msg is not None
    assert "out of credits" in msg


def test_failure_from_events_ignores_dev_warning_noise():
    stream = "\n".join(
        [
            '{"type":"item.completed","item":{"type":"error","message":'
            '"Under-development features enabled: enable_mcp_apps."}}',
        ]
    )
    assert _failure_from_events(stream) is None


def test_failure_from_events_prefers_turn_failed_over_error():
    stream = "\n".join(
        [
            '{"type":"error","message":"transient boom"}',
            '{"type":"turn.failed","error":{"message":"final reason"}}',
        ]
    )
    assert _failure_from_events(stream) == "final reason"


def test_failure_from_events_none_for_clean_stream():
    stream = "\n".join(
        [
            '{"type":"thread.started","thread_id":"t1"}',
            '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}',
        ]
    )
    assert _failure_from_events(stream) is None


def test_failure_from_events_falls_back_to_item_error():
    stream = '{"type":"item.completed","item":{"type":"error","message":"model unavailable"}}'
    assert _failure_from_events(stream) == "model unavailable"


def test_parse_jsonl_returns_none_for_out_of_credits():
    # 没有 agent_message，仍然解析不出结构化结果——这正是必须靠 _failure_from_events 兜底的原因
    result, last_text, thread_id = parse_jsonl(OUT_OF_CREDITS_STREAM)
    assert result is None
    assert last_text is None
    assert thread_id == "01a1e956-35af-7002-9e4a-29e582c12986"


def _fake_headless(monkeypatch, stdout: str, stderr: str = ""):
    def _run(*_args, **_kwargs):
        return HeadlessResult(
            stdout_text=stdout,
            stderr_text=stderr,
            exit_code=1,
            timed_out=False,
            launch_error=None,
            duration_seconds=1.0,
        )

    monkeypatch.setattr(codex_runner, "run_headless", _run)


def test_run_surfaces_out_of_credits_as_failed(tmp_path, monkeypatch):
    _fake_headless(monkeypatch, OUT_OF_CREDITS_STREAM)
    runner = CodexRunner(binary="codex", timeout_seconds=10)
    outcome = runner.run(tmp_path, "do the thing", task_id="t", attempt=1)
    assert outcome.status == "failed"
    assert "out of credits" in (outcome.message or "")


def test_run_keeps_invalid_output_when_no_failure_event(tmp_path, monkeypatch):
    # 只有开发警告、无结构化结果：没有真实失败原因，仍报 invalid_output
    stream = '{"type":"item.completed","item":{"type":"error","message":"Under-development features enabled: x"}}'
    _fake_headless(monkeypatch, stream)
    runner = CodexRunner(binary="codex", timeout_seconds=10)
    outcome = runner.run(tmp_path, "do the thing", task_id="t", attempt=1)
    assert outcome.status == "invalid_output"
    assert "不是合法结构化结果" in (outcome.message or "")
