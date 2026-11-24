"""schema、状态机、路径 gate、脱敏的单元测试。"""

from __future__ import annotations

import copy

import pytest

from handoff import schema, state
from handoff.errors import TransitionError
from handoff.pathgate import evaluate, matches
from handoff.sanitize import clean, redact, truncate


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


def test_valid_task_passes(task_factory):
    task = task_factory()
    assert schema.errors_for("task", task) == []


def test_missing_required_field(task_factory):
    task = task_factory()
    del task["requirement"]
    errors = schema.errors_for("task", task)
    assert any("requirement" in e for e in errors)


def test_unknown_field_rejected(task_factory):
    task = task_factory()
    task["assignee"] = "backend"
    errors = schema.errors_for("task", task)
    assert any("assignee" in e for e in errors)


def test_short_sha_rejected(task_factory):
    task = task_factory()
    task["target"]["base_sha"] = task["target"]["base_sha"][:7]
    assert schema.errors_for("task", task)


def test_open_task_allows_worker_to_defer_base_sha(task_factory):
    task = task_factory()
    task["target"].pop("base_sha")
    assert schema.errors_for("task", task) == []


def test_bad_timestamp_rejected(task_factory):
    task = task_factory()
    task["created_at"] = "2026-11-05 00:00:00"
    assert schema.errors_for("task", task)


def test_unknown_status_rejected(task_factory):
    task = task_factory()
    task["status"] = "merging"
    assert schema.errors_for("task", task)


def test_shell_string_check_rejected(task_factory):
    task = task_factory()
    task["acceptance"]["checks"] = [{"id": "x", "type": "shell", "command": "curl evil"}]
    assert schema.errors_for("task", task)


def test_free_shell_command_rejected(task_factory):
    task = task_factory()
    task["acceptance"]["checks"] = [{"id": "curl", "type": "http", "method": "GET", "url": "https://x", "expect_status": 200, "shell": "curl x"}]
    assert schema.errors_for("task", task)


def test_state_machine_whitelist():
    assert state.can_transition("open", "claimed")
    assert not state.can_transition("open", "merged")
    # verified → open 是会话续跑入口（2026-11-11 有意放开，delivered 同理）
    assert state.can_transition("verified", "open")
    assert state.can_transition("delivered", "open")
    assert not state.can_transition("awaiting_merge", "open")
    assert state.can_transition("failed", "claimed")
    # 目录模式（无 PR）没有合并环节：delivered 直接部署对齐，
    # 误入 awaiting_merge 的目录模式任务同样从 deployed 救回
    assert state.can_transition("delivered", "deployed")
    assert state.can_transition("awaiting_merge", "deployed")
    assert state.can_transition("awaiting_merge", "merged")
    # 前端已验收：后端验收通过后前端自行验收成功，再落终态记录
    assert state.can_transition("verified", "frontend_accepted")
    assert state.can_transition("frontend_accepted", "open")
    assert not state.can_transition("frontend_accepted", "verified")
    assert not state.can_transition("frontend_accepted", "deployed")
    with pytest.raises(TransitionError):
        state.assert_transition("open", "merged", "ho-x")


def test_reverify_transition_accepts_verification_failure_and_deployed_delivery():
    state.assert_reverify_transition(
        {"status": "failed", "failure": {"stage": "verification"}},
        "ho-x",
    )
    # failed 但交付已部署对齐（执行器被取消、验收没跑完）：重跑检查不重做实现
    state.assert_reverify_transition(
        {
            "status": "failed",
            "failure": {"stage": "codex"},
            "deployment": {"status": "deployed", "target_sha": "a" * 40, "observed_sha": "b" * 40},
        },
        "ho-x",
    )
    with pytest.raises(TransitionError):
        state.assert_reverify_transition(
            {"status": "failed", "failure": {"stage": "worker"}},
            "ho-x",
        )
    # 部署没对齐的 failed 不放行：环境里没有交付物，重验没有意义
    with pytest.raises(TransitionError):
        state.assert_reverify_transition(
            {
                "status": "failed",
                "failure": {"stage": "codex"},
                "deployment": {"status": "timeout", "target_sha": "a" * 40, "observed_sha": "b" * 40},
            },
            "ho-x",
        )


def test_terminal_states():
    assert state.TERMINAL == {"verified", "frontend_accepted", "cancelled"}
    assert state.ALLOWED["cancelled"] == frozenset()


# ---------------------------------------------------------------------------
# path gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,pattern,expected",
    [
        ("src/orders/A.java", "src/orders/**", True),
        ("src/orders/deep/B.java", "src/orders/**", True),
        ("src/other/A.java", "src/orders/**", False),
        ("a/b/migrations/001.sql", "**/migrations/**", True),
        ("migrations/001.sql", "**/migrations/**", True),
        ("infra/prod/main.tf", "infra/prod", True),
        ("infra/prod/x/main.tf", "infra/prod", True),
        ("infra/production/main.tf", "infra/prod", False),
        (".github/workflows/ci.yml", ".github/**", True),
        ("src/a.ts", "src/*.ts", True),
        ("src/nested/a.ts", "src/*.ts", False),
    ],
)
def test_glob_matching(path, pattern, expected):
    assert matches(path, pattern) is expected


def test_gate_rejects_outside_allowed():
    result = evaluate(["src/orders/A.java", "src/payments/B.java"], ["src/orders/**"], [])
    assert not result.ok
    assert any("src/payments/B.java" in v for v in result.violations)


def test_gate_rejects_forbidden_even_if_allowed():
    result = evaluate([".github/workflows/ci.yml"], ["**"], [".github/**"])
    assert not result.ok
    assert any("命中禁止规则" in v for v in result.violations)


def test_gate_rejects_empty_changes():
    assert not evaluate([], ["src/**"], []).ok


def test_gate_rejects_path_traversal():
    assert not evaluate(["../etc/passwd"], ["**"], []).ok


def test_gate_rejects_empty_allowlist():
    assert not evaluate(["src/a.java"], [], []).ok


# ---------------------------------------------------------------------------
# sanitize
# ---------------------------------------------------------------------------


def test_redacts_tokens_and_urls():
    text = (
        "Authorization: Bearer abcdefghijklmnop\n"
        "token=deadbeefdeadbeef\n"
        "https://user:pass@example.com/x\n"
        "https://example.com/cb?token=supersecret&x=1\n"
        "ghp_1234567890abcdefghijklmnopqrstuvwxyz\n"
    )
    out = redact(text)
    assert "abcdefghijklmnop" not in out
    assert "deadbeefdeadbeef" not in out
    assert "user:pass" not in out
    assert "supersecret" not in out
    assert "ghp_1234567890" not in out


def test_truncate_keeps_both_ends():
    text = "a" * 5000
    out = truncate(text, 1000)
    assert len(out) < 1300
    assert "truncated" in out


def test_clean_handles_bytes():
    assert clean(b"hello") == "hello"
    assert clean(None) == ""


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def task_factory(tmp_path):
    from helpers import make_task
    from handoff import ids

    base = make_task(ids.task_id(), base_sha="a" * 40)
    return lambda: copy.deepcopy(base)
