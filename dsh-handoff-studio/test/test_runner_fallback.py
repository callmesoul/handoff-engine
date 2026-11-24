"""候选模型链：主模型额度用尽时按顺序换下一个。

为什么这组测试值得存在：换模型这件事很容易写成「失败就换下一个」，但那样
会把「任务本身做错了」也拖成好几次重跑，还把 agent 的产物丢掉。真正的规则
窄得多——只有**换个模型就可能好转**的失败才换。规则写错的表现不在运行器里，
而是任务列表上一条本该失败的记录变成了成功/或者一单任务跑了两小时。

不起 DSH：`_start/_poll` 用假实现替掉，只验证「按什么顺序、带什么参数、
什么情况下停」。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


def _repo_root() -> Path:
    override = os.environ.get("HANDOFF_ROOT", "").strip()
    if override:
        return Path(override)
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "config" / "repositories.yaml").is_file():
            return candidate
    return Path(__file__).resolve().parents[2]


ENGINE_SRC = Path(__file__).resolve().parents[1].parent / "src"
ENGINE_DIR = Path(__file__).resolve().parents[1] / "engine"
for extra in (str(ENGINE_SRC), str(ENGINE_DIR)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from handoff_studio.runner import (  # noqa: E402
    DshAgentRunner,
    FALLBACK_FAILURE_CODES,
    can_fall_back,
    describe_failure,
)

COMPLETED = json.dumps(
    {
        "status": "completed",
        "summary": "done",
        "changed_paths": ["a.py"],
        "tests": [],
        "migration_required": False,
        "risks": [],
        "notes": "",
        "commit_message": "feat: a",
    }
)
BLOCKED = json.dumps(
    {
        "status": "blocked",
        "summary": "cannot finish",
        "changed_paths": [],
        "tests": [],
        "migration_required": False,
        "risks": [],
        "notes": "blocked on missing spec",
        "commit_message": "wip: partial",
    }
)


class FakeBridge:
    """替掉 HTTP 那一层：按脚本回答每次 run，并记下每次派单的请求体。"""

    def __init__(self, script: list[dict]):
        # 每个元素是「这一次 run 的最终 payload」，按顺序消费
        self.script = list(script)
        self.starts: list[dict] = []
        self.polls: list[str] = []

    def start(self, worktree, prompt, *, task_id, attempt, request_id, target=None, reuse_session=True):
        # 这层只关心「派给谁」：模型/provider 与是否允许接续上次会话
        self.starts.append(
            {
                "model": target.get("model") if target else None,
                "provider": target.get("provider") if target else None,
                "effort": target.get("effort") if target else None,
                "reuse_session": reuse_session,
            }
        )
        return {"runId": f"run-{len(self.starts)}", "sessionId": f"session-{len(self.starts)}"}

    def poll(self, run: str, after: int) -> dict:
        self.polls.append(run)
        payload = dict(self.script.pop(0)) if self.script else {"phase": "finished"}
        payload.setdefault("phase", "finished")
        payload.setdefault("logs", [])
        payload.setdefault("cursor", after)
        return payload


def make_runner(bridge: FakeBridge, *, model="m1", provider="p1", effort=None, fallbacks=None):
    runner = DshAgentRunner(
        bridge_url="http://127.0.0.1:1/handoff-studio",
        token="t",
        timeout_seconds=60,
        model=model,
        provider=provider,
        effort=effort,
        fallbacks=fallbacks,
    )
    runner._start = bridge.start  # noqa: SLF001 - 测试替身
    runner._poll = bridge.poll  # noqa: SLF001
    runner._cancel = lambda run: None  # noqa: SLF001
    runner._version = "test"  # noqa: SLF001 - 别去打 HTTP
    return runner


# -- 失败码分类 --------------------------------------------------------------


def test_quota_and_rate_limit_are_fallback_reasons():
    for code in ("QUOTA", "ACCOUNT_QUOTA", "RATE_LIMIT"):
        assert can_fall_back({"code": code, "message": ""}) is True, code


def test_a_task_level_problem_does_not_trigger_a_fallback():
    # 内容被拒 / 请求非法 / 被取消：换个模型只会把一次明确的失败拖成好几次
    for code in ("CONTENT_REFUSED", "INVALID_REQUEST", "ABORTED", "INVALID_ADAPTER_IRRELEVANT"):
        assert can_fall_back({"code": code, "message": ""}) is False, code


def test_unknown_code_falls_back_on_message_markers_only():
    assert can_fall_back({"code": "UNKNOWN", "message": "429 Too Many Requests"}) is True
    assert can_fall_back({"code": "UNKNOWN", "message": "当前模型额度已用尽"}) is True
    assert can_fall_back({"code": "UNKNOWN", "message": "tool read failed"}) is False
    # 拿不到 code 也没有可识别措辞：宁可不换
    assert can_fall_back({"code": "UNKNOWN", "message": ""}) is False


def test_http_statuses_that_mean_this_route_is_closed():
    assert can_fall_back({"code": "UNKNOWN", "message": "", "status": 429}) is True
    assert can_fall_back({"code": "UNKNOWN", "message": "", "status": 402}) is True
    assert can_fall_back({"code": "UNKNOWN", "message": "", "status": 500}) is False


def test_no_failure_never_falls_back():
    assert can_fall_back(None) is False
    assert can_fall_back({}) is False


def test_describe_failure_is_readable():
    assert describe_failure({"code": "QUOTA", "message": "no balance"}) == "额度用尽：no balance"
    assert describe_failure({"code": "RATE_LIMIT", "message": "", "status": 429}) == "被限流（HTTP 429）"
    assert describe_failure({"code": "SOMETHING_NEW", "message": ""}) == "SOMETHING_NEW"
    assert describe_failure(None) == ""


def test_the_code_list_covers_what_dsh_actually_raises():
    # 这几个是 dsh-llm 的 LLM_FAILURE_CODES 里与「模型当前用不了」相关的条目；
    # 它们从清单里掉出去，额度耗尽就不再触发换模型了。
    for code in ("QUOTA", "ACCOUNT_QUOTA", "RATE_LIMIT", "AUTH", "UNKNOWN_MODEL", "MODEL_UNSUPPORTED"):
        assert code in FALLBACK_FAILURE_CODES, code


# -- 链的构造 ----------------------------------------------------------------


def test_model_chain_dedupes_and_keeps_order():
    runner = DshAgentRunner(
        bridge_url="http://127.0.0.1:1/handoff-studio",
        token="t",
        model="m1",
        provider="p1",
        effort="high",
        fallbacks=[
            # 与主模型重复：不能因此多跑一遍
            {"provider": "p1", "model": "m1", "effort": "high"},
            {"provider": "p2", "model": "m2", "effort": None},
            # 同 model 不同 provider 是**两条**路，必须都保留
            {"provider": "p3", "model": "m2", "effort": "max"},
            {"provider": None, "model": "", "effort": None},
        ],
    )
    assert runner.model_chain() == [
        {"provider": "p1", "model": "m1", "effort": "high"},
        {"provider": "p2", "model": "m2", "effort": None},
        {"provider": "p3", "model": "m2", "effort": "max"},
    ]


# -- 执行 --------------------------------------------------------------------


def test_a_quota_failure_moves_to_the_next_candidate():
    bridge = FakeBridge(
        [
            {
                "phase": "finished",
                "text": "",
                "failure": {"code": "QUOTA", "message": "quota exhausted", "status": 402},
            },
            {"phase": "finished", "text": COMPLETED},
        ]
    )
    runner = make_runner(bridge, model="m1", fallbacks=[{"provider": "p2", "model": "m2"}])
    outcome = runner.run(Path("."), "do it", task_id="ho-1", attempt=1)

    assert outcome.status == "completed"
    assert outcome.result is not None
    # 两次派单：先主模型，再候选
    assert [body["model"] for body in bridge.starts] == ["m1", "m2"]
    assert [body["provider"] for body in bridge.starts] == ["p1", "p2"]


def test_a_fallback_does_not_resume_the_failed_models_session():
    """换模型绝不能接续上一个模型的会话：那个会话绑的就是刚失败的模型。"""
    bridge = FakeBridge(
        [
            {"phase": "finished", "text": "", "failure": {"code": "ACCOUNT_QUOTA", "message": ""}},
            {"phase": "finished", "text": COMPLETED},
        ]
    )
    runner = make_runner(bridge, model="m1", fallbacks=[{"provider": "p2", "model": "m2"}])
    runner.resume_session = "session-old"
    runner.run(Path("."), "do it", task_id="ho-1", attempt=2)

    # 第一次允许接续（这才是「续跑」的本意），换模型那次必须新开
    assert bridge.starts[0]["reuse_session"] is True
    assert bridge.starts[1]["reuse_session"] is False


def test_all_candidates_exhausted_reports_every_reason():
    bridge = FakeBridge(
        [
            {"phase": "finished", "text": "", "failure": {"code": "QUOTA", "message": "no balance"}},
            {"phase": "finished", "text": "", "failure": {"code": "RATE_LIMIT", "message": "too fast", "status": 429}},
        ]
    )
    runner = make_runner(bridge, model="m1", fallbacks=[{"provider": "p2", "model": "m2"}])
    outcome = runner.run(Path("."), "do it", task_id="ho-1", attempt=1)

    assert outcome.status == "invalid_output"
    # 每个模型为什么不行都要写出来，否则面板上只剩「没有结构化结果」
    assert "额度用尽" in outcome.message
    assert "被限流" in outcome.message
    assert "p1/m1" in outcome.message
    assert "p2/m2" in outcome.message


def test_a_task_level_failure_stops_immediately():
    """agent 自己报 blocked 是有效结果：不能当成模型失败再换一个。"""
    bridge = FakeBridge([{"phase": "finished", "text": BLOCKED}])
    runner = make_runner(bridge, model="m1", fallbacks=[{"provider": "p2", "model": "m2"}])
    outcome = runner.run(Path("."), "do it", task_id="ho-1", attempt=1)

    assert outcome.status == "blocked"
    assert len(bridge.starts) == 1


def test_content_refusal_does_not_switch_models():
    bridge = FakeBridge(
        [
            {
                "phase": "finished",
                "text": "",
                "failure": {"code": "CONTENT_REFUSED", "message": "refused", "status": 400},
            }
        ]
    )
    runner = make_runner(bridge, model="m1", fallbacks=[{"provider": "p2", "model": "m2"}])
    outcome = runner.run(Path("."), "do it", task_id="ho-1", attempt=1)

    assert outcome.status == "invalid_output"
    assert len(bridge.starts) == 1, "a refused request must not be retried on another model"


def test_a_timeout_does_not_switch_models():
    """超时是时间预算问题：换模型只会再花一倍时间。"""
    bridge = FakeBridge([{"phase": "finished", "text": "", "failure": None}])
    runner = make_runner(bridge, model="m1", fallbacks=[{"provider": "p2", "model": "m2"}])
    # 负数让「时间预算已用完」在第一轮轮询前就成立，测试不依赖时钟精度
    runner.timeout_seconds = -1
    outcome = runner.run(Path("."), "do it", task_id="ho-1", attempt=1)

    assert outcome.status == "timed_out"
    assert len(bridge.starts) == 1


# -- 轮询瞬时失败 ------------------------------------------------------------


class FlakyBridge(FakeBridge):
    """按脚本在指定次序上抛通信错误，模拟宿主事件循环被占住的那一拍。"""

    def __init__(self, script, *, fail_at, error="请求失败 ...: timed out"):
        super().__init__(script)
        self.fail_at = set(fail_at)
        self.error = error
        self.attempts = 0

    def poll(self, run: str, after: int) -> dict:
        self.attempts += 1
        if self.attempts in self.fail_at:
            from handoff.errors import ExecutionError

            raise ExecutionError(self.error)
        return super().poll(run, after)


def test_a_single_poll_timeout_does_not_kill_the_run():
    """单次轮询超时不是 run 失败。

    这是真机踩到的坑：agent 跑长命令时宿主事件循环被占住，一个轮询请求卡住十
    几秒，旧实现立刻把整轮判成「DSH 执行器通信失败」——而 agent 还在改工作区，
    候选链还被这个假失败白白消耗一格。
    """
    bridge = FlakyBridge(
        [
            {"phase": "running", "logs": ["▶ 第 1 轮开始"], "cursor": 1},
            {"phase": "finished", "text": COMPLETED},
        ],
        fail_at={1},
    )
    runner = make_runner(bridge, model="m1")
    outcome = runner.run(Path("."), "do it", task_id="ho-1", attempt=1)

    assert outcome.status == "completed", outcome.message
    # 这一轮没有被当成失败，也没有被换模型
    assert len(bridge.starts) == 1


def test_a_poll_timeout_is_retried_not_treated_as_a_model_failure():
    """瞬时轮询失败不能触发换模型：那不是模型的问题。"""
    bridge = FlakyBridge(
        [{"phase": "finished", "text": COMPLETED}],
        fail_at={1, 2},
    )
    runner = make_runner(bridge, model="m1", fallbacks=[{"provider": "p2", "model": "m2"}])
    outcome = runner.run(Path("."), "do it", task_id="ho-1", attempt=1)

    assert outcome.status == "completed"
    assert len(bridge.starts) == 1, "a transport hiccup must not consume a fallback slot"


def test_a_persistent_transport_failure_still_fails_the_run():
    """通路真的断了（宿主重启/端口换了）时，不能无限重试。"""
    bridge = FlakyBridge(
        [{"phase": "finished", "text": COMPLETED}],
        fail_at=set(range(1, 100)),
    )
    runner = make_runner(bridge, model="m1", fallbacks=[{"provider": "p2", "model": "m2"}])
    outcome = runner.run(Path("."), "do it", task_id="ho-1", attempt=1)

    assert outcome.status == "failed"
    assert "通信失败" in outcome.message
    # 断的是通路，不是模型：不该去换候选
    assert len(bridge.starts) == 1


def test_no_fallbacks_keeps_the_old_single_attempt_behaviour():
    bridge = FakeBridge([{"phase": "finished", "text": COMPLETED}])
    runner = make_runner(bridge, model="m1")
    outcome = runner.run(Path("."), "do it", task_id="ho-1", attempt=1)

    assert outcome.status == "completed"
    assert len(bridge.starts) == 1
    assert bridge.starts[0]["model"] == "m1"


def test_a_completed_run_without_a_fallback_still_returns_its_logs(tmp_path):
    """日志不能因为「这次没换模型」就丢掉：面板的「执行日志」靠它。

    以前每轮自己写日志文件；改成整条链共用一个文件后，成功路径也必须把这一轮
    的日志一起交回去，否则日志文件会变成空的。
    """
    bridge = FakeBridge(
        [
            {
                "phase": "finished",
                "text": COMPLETED,
                "logs": ["▶ 第 1 轮开始", "🔧 bash ls"],
                "cursor": 2,
            }
        ]
    )
    runner = make_runner(bridge, model="m1")
    runner.log_dir = tmp_path
    outcome = runner.run(tmp_path, "do it", task_id="ho-log", attempt=1)

    assert outcome.status == "completed"
    assert outcome.log_path is not None
    written = Path(outcome.log_path).read_text(encoding="utf-8").strip().splitlines()
    assert any("第 1 轮开始" in line for line in written)
    assert any("bash" in line for line in written)


def test_fallback_logs_say_which_model_was_skipped():
    bridge = FakeBridge(
        [
            {"phase": "finished", "text": "", "failure": {"code": "QUOTA", "message": "no balance"}},
            {"phase": "finished", "text": COMPLETED},
        ]
    )
    runner = make_runner(bridge, model="m1", fallbacks=[{"provider": "p2", "model": "m2"}])
    lines: list[str] = []
    runner.on_event = lines.append
    runner.run(Path("."), "do it", task_id="ho-1", attempt=1)

    joined = "\n".join(lines)
    assert "p1/m1 不可用" in joined
    assert "换候选模型 2/2" in joined
