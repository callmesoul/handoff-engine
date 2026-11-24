"""DshAgentRunner：把 DSH 宿主里的 Agent 当成 handoff 的实现执行器。

## 为什么是「插件 + HTTP」两跳

DSH 没有 `codex exec` 那样的无头 CLI：能真正跑一次编码任务的只有宿主进程里的
Agent，而 Agent 只能由宿主进程自己创建（`agentLoop.createAgent`）并投喂 prompt
（`sessionController.prompt`）。所以链路是：

    handoff Worker（Python，跑在独立进程）
        │  POST /handoff-studio/agent/run   (Bearer <token>)
        ▼
    dsh-handoff-studio 插件（DSH 宿主内）
        │  createAgent + prompt + wait
        ▼
    DSH Agent（provider/model/effort 由面板选）

与 `handoff.dsh_bridge` 是同一套「本机插件路由 + 共享令牌」做法，方向相反：
桥接是把完成事件推回会话，这里是把实现任务派给 Agent。

## 与 CodexRunner 的契约

`worker.py` 只要求执行器对象有 `name` / `version()` / `run(...) -> CodexOutcome`。
本类严格满足这三样，且 **run() 返回的 CodexOutcome 与 codex 的语义一致**，因此
worker 的 gate / 提交 / PR / 验收整条链路一行都不用改。

## 为什么不自己解析结果

Agent 的最终回复是一段自然语言 + JSON。这里复用 `codex_runner._extract_json`
和 `schema.errors_for("codex-result", ...)`，与 ZcodeRunner 完全同构——结构化
契约只在 schema 一处定义，不在第二个地方漂移。

## 候选模型链（额度用完自动往下走）

面板的全自动配置可以填一串候选模型。`run()` 按「主模型 → 候选 1 → 候选 2 …」
依次尝试；只有当失败原因**换个模型就可能好转**时才换（额度/限流/鉴权/模型
不可用/服务端故障，见 `can_fall_back`）。依据是宿主回传的结构化失败码
（`turn/end.reason.error.code`，取值来自 `@deepseek-ai/dsh-llm` 的
`LLM_FAILURE_CODES`），不是猜文案。

换模型时不接续上一轮的 DSH 会话：那个会话绑的就是刚失败的那个模型/provider。
整条链的日志落成同一个 attempt 的日志文件，人看到的是一轮里发生了什么。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from handoff import ids, schema
from handoff.codex_runner import CodexOutcome, _extract_json, sanitized_env  # noqa: F401
from handoff.errors import ConfigError, ExecutionError, HandoffError
from handoff.http_client import HttpClient
from handoff.sanitize import clean

from . import session_index

PROMPT_VERSION = "handoff-dsh-v1"

#: 插件注册的前缀路由，与 dsh-plugin 的约定一致。
DEFAULT_ROUTE = "/handoff-studio"

#: 轮询间隔：面板要实时日志，但也不能把宿主打满。
_POLL_INTERVAL = 0.6

#: 连续多少次轮询拿不到响应，才认定这条通路真的断了。
#:
#: 单次轮询超时**不是** run 失败：agent 正在跑长命令时宿主的事件循环会被占住，
#: 一个 `GET .../agent/run?after=N` 卡住十几秒很常见。旧实现把这个瞬时超时当成
#: 致命错误，直接结束整轮并写成「DSH 执行器通信失败」——而此时 agent 明明还在
#: 干活，工作区正在被改，候选模型链还会被这个假失败白白消耗掉一格。
#: 只有连续这么多次都拿不到响应，才按「通路断了」处理。
_POLL_TRANSIENT_FAILURES = 5

#: 单次轮询请求的超时（秒）。它比整轮预算小得多：一次卡住只是重试一次，
#: 不会把这一轮的时间用光。
_POLL_REQUEST_TIMEOUT = 30

#: 单个候选模型最多保留的日志长度（与 zcode 执行器一致）。
_EVENT_TEXT_LIMIT = 400

#: 这些失败码表示「这个模型/provider 当前用不了」，换下一个候选模型有意义。
#:
#: 取值与 `@deepseek-ai/dsh-llm` 的 `LLM_FAILURE_CODES` 逐字一致（那一层是把
#: provider 的错误规范化成稳定机器码的地方）。分三类：
#:   额度/限流  QUOTA / ACCOUNT_QUOTA / RATE_LIMIT
#:   鉴权/凭据  AUTH / ACCOUNT_SIGN_IN_REQUIRED / ACCOUNT_TOKEN_INVALID /
#:              INVALID_CREDENTIAL / MISSING_CREDENTIAL / NO_CREDENTIAL_STORE
#:   模型/路由  UNKNOWN_MODEL / MODEL_UNSUPPORTED / NO_ADAPTER / INVALID_ADAPTER /
#:              INVALID_CATALOG / INVALID_MODEL_INFO / DISCOVERY_FAILED /
#:              DISCOVERY_UNSUPPORTED / UNSUPPORTED_REASONING_EFFORT /
#:              CONTEXT_WINDOW_EXCEEDED
#: 外加 provider 侧 5xx / 超时 / 断流：换一条路由往往就通了。
#:
#: 刻意**不包括** CONTENT_REFUSED / INVALID_REQUEST / ABORTED 这类：任务本身
#: 有问题时换模型只是把一个明确的失败拖长成好几次。
FALLBACK_FAILURE_CODES = frozenset(
    {
        "QUOTA",
        "ACCOUNT_QUOTA",
        "RATE_LIMIT",
        "AUTH",
        "ACCOUNT_SIGN_IN_REQUIRED",
        "ACCOUNT_TOKEN_INVALID",
        "INVALID_CREDENTIAL",
        "MISSING_CREDENTIAL",
        "NO_CREDENTIAL_STORE",
        "UNKNOWN_MODEL",
        "MODEL_UNSUPPORTED",
        "NO_ADAPTER",
        "INVALID_ADAPTER",
        "INVALID_CATALOG",
        "INVALID_MODEL_INFO",
        "DISCOVERY_FAILED",
        "DISCOVERY_UNSUPPORTED",
        "UNSUPPORTED_REASONING_EFFORT",
        "CONTEXT_WINDOW_EXCEEDED",
        "SERVER",
        "TIMEOUT",
        "TRANSPORT",
        "STREAM_CLOSED",
    }
)

#: 失败码是 UNKNOWN 时的兜底判据：宿主把 provider 的原始报错塞进 message，
#: 而「额度用尽」最常见的措辞就是这几个词。只在拿不到稳定码时才用它。
_FALLBACK_MESSAGE_MARKERS = (
    "quota",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
    "insufficient",
    "out of credit",
    "no credit",
    "credits",
    "billing",
    "额度",
    "余额",
    "限流",
    "频繁",
)

#: 这几个 HTTP 状态码说明「这条路当前不通」，换候选模型比原地重试更有意义。
_FALLBACK_STATUSES = frozenset({401, 402, 403, 429})

#: 各失败码翻成中文短句，用于日志与最终失败原因（不用 provider 的英文原文
#: 当主语，否则面板上只看到一行看不懂的英文）。
_FAILURE_LABELS = {
    "QUOTA": "额度用尽",
    "ACCOUNT_QUOTA": "账号额度用尽",
    "RATE_LIMIT": "被限流",
    "AUTH": "鉴权失败",
    "ACCOUNT_SIGN_IN_REQUIRED": "需要重新登录",
    "ACCOUNT_TOKEN_INVALID": "账号令牌失效",
    "INVALID_CREDENTIAL": "凭据无效",
    "MISSING_CREDENTIAL": "缺少凭据",
    "NO_CREDENTIAL_STORE": "没有可用的凭据存储",
    "UNKNOWN_MODEL": "模型不可用",
    "MODEL_UNSUPPORTED": "模型不支持",
    "NO_ADAPTER": "没有对应的 provider 适配器",
    "INVALID_ADAPTER": "provider 适配器配置无效",
    "INVALID_CATALOG": "模型目录无效",
    "INVALID_MODEL_INFO": "模型信息无效",
    "DISCOVERY_FAILED": "无法发现 provider",
    "DISCOVERY_UNSUPPORTED": "provider 不支持发现",
    "UNSUPPORTED_REASONING_EFFORT": "该模型不支持所选思考档位",
    "CONTEXT_WINDOW_EXCEEDED": "超出上下文窗口",
    "SERVER": "provider 服务端错误",
    "TIMEOUT": "请求超时",
    "TRANSPORT": "网络传输失败",
    "STREAM_CLOSED": "流被提前关闭",
}


def describe_failure(failure: dict | None) -> str:
    """把宿主回传的结构化失败翻成一句人能看懂的短句。"""
    if not failure:
        return ""
    code = str(failure.get("code") or "").strip()
    message = str(failure.get("message") or "").strip()
    label = _FAILURE_LABELS.get(code) or code or "未知错误"
    status = failure.get("status")
    if isinstance(status, int):
        label = f"{label}（HTTP {status}）"
    return f"{label}：{message}" if message else label


def can_fall_back(failure: dict | None) -> bool:
    """这个模型级失败值得换下一个候选模型吗？

    只有「换个模型/换个 provider 就可能好转」的失败才算：额度、限流、鉴权、
    模型不可用、服务端故障。任务本身的问题（内容被拒、请求非法、被取消）
    换模型只是把一次明确的失败拖长成好几次。
    """
    if not failure:
        return False
    code = str(failure.get("code") or "").strip()
    if code in FALLBACK_FAILURE_CODES:
        return True
    status = failure.get("status")
    if isinstance(status, int) and status in _FALLBACK_STATUSES:
        return True
    # 只有拿不到稳定码时才看措辞：稳定码永远优先，避免把「工具报错里恰好
    # 提到 rate limit」误判成模型不可用。
    if code and code != "UNKNOWN":
        return False
    message = str(failure.get("message") or "").lower()
    return any(marker in message for marker in _FALLBACK_MESSAGE_MARKERS)


class DshAgentRunner:
    """在 DSH 宿主里用指定模型跑一次实现任务。"""

    name = "dsh"

    def __init__(
        self,
        *,
        bridge_url: str,
        token: str,
        timeout_seconds: float = 3600,
        log_dir: str | Path | None = None,
        model: str | None = None,
        provider: str | None = None,
        effort: str | None = None,
        sandbox: str | None = None,
        permission_mode: str | None = None,
        env_extra: dict[str, str] | None = None,
        resume_session: str | None = None,
        on_event: Callable[[str], None] | None = None,
        fallbacks: list[dict] | None = None,
        session_index: str | Path | None = None,
    ):
        self.bridge_url = bridge_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.log_dir = Path(log_dir) if log_dir else None
        self.model = model
        self.provider = provider
        self.effort = effort
        # 候选模型链：主模型额度用尽/不可用时，按顺序换下一个再跑同一轮。
        # 每个元素是 {provider, model, effort}；空 = 不换。
        self.fallbacks = [dict(item) for item in (fallbacks or []) if item.get("model")]
        # 访问权限隔离：DSH 侧的沙箱档位（read-only / workspace-write /
        # danger-full-access）。它走插件宿主 session 的 sandbox/mode 事件，
        # 不是命令行参数——所以和 codex 的 --sandbox 是两条不同的通路。
        self.sandbox = sandbox
        self.permission_mode = permission_mode
        self.env_extra = env_extra or {}
        # 续跑：接续上一轮的 DSH 会话（sessionController 自己会 resume）
        self.resume_session = resume_session
        # 会话索引：开跑那一刻就把 session id 落盘。父类的「继续上次会话」只认
        # codex 的 thread.started / codex.completed，dsh 两样都没有，见 session_index.py。
        self.session_index = session_index
        self.on_event = on_event
        self._version: str | None = None
        self._client = HttpClient(timeout=30, allow_private=True, retries=1)

    # -- 候选模型链 ----------------------------------------------------------
    def model_chain(self) -> list[dict]:
        """按顺序要尝试的模型：主模型在前，候选模型在后，同 (provider, model) 去重。

        去重是因为自动配置里主模型和候选列表很容易出现同一条（用户先在主下拉
        选 A，又把 A 加进候选），重复尝试只会在额度用尽时白等两轮。
        """
        chain: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for item in [{"provider": self.provider, "model": self.model, "effort": self.effort}, *self.fallbacks]:
            model = str(item.get("model") or "").strip()
            provider = str(item.get("provider") or "").strip()
            effort = str(item.get("effort") or "").strip()
            if not model:
                continue
            key = (provider, model)
            if key in seen:
                continue
            seen.add(key)
            chain.append({"provider": provider or None, "model": model, "effort": effort or None})
        return chain

    @staticmethod
    def _label(item: dict) -> str:
        model = item.get("model") or "?"
        provider = item.get("provider")
        effort = item.get("effort")
        text = f"{provider}/{model}" if provider else str(model)
        return f"{text}（{effort}）" if effort else text

    # -- 与其它执行器同形的接口 ---------------------------------------------
    @property
    def binary(self) -> str:
        return self.bridge_url

    def version(self) -> str | None:
        """执行器健康检查：插件在线且能报出宿主版本才算可用。"""
        if self._version is not None:
            return self._version
        try:
            response = self._client.request(
                "GET",
                f"{self.bridge_url}/health",
                headers={"Accept": "application/json"},
            )
            if response.status != 200:
                self._version = None
                return None
            payload = json.loads(response.body or "{}")
            self._version = clean(str(payload.get("version") or "dsh"), 100) or "dsh"
        except (HandoffError, ValueError):
            self._version = None
        return self._version

    # -- 主流程 --------------------------------------------------------------
    def run(
        self,
        worktree: str | Path,
        prompt: str,
        *,
        task_id: str,
        attempt: int,
        cancel_event=None,
    ) -> CodexOutcome:
        """跑这一轮：主模型不行就按候选链换下一个，直到跑出来或全部失败。

        为什么换模型而不是直接失败：DSH 的模型是按 provider 路由的，一个
        provider 的额度/凭据/限流出问题，同机上往往还有另一条能用的路（另一个
        provider 的同名模型、或另一家的等价模型）。自动模式下没人守着，直接
        失败等于把一单任务扔回到队列里等人发现。

        只在「换模型有意义」的失败上换：额度、限流、鉴权、模型不可用、服务端
        故障（见 {@link can_fall_back}）。任务本身的问题（内容被拒、请求非法、
        被取消、超时）不换——那只会把一次明确的失败拖成好几次。
        """
        worktree = Path(worktree)
        started = time.monotonic()
        deadline = started + float(self.timeout_seconds)
        chain = self.model_chain() or [{"provider": None, "model": None, "effort": None}]
        history: list[str] = []
        reasons: list[str] = []
        outcome: CodexOutcome | None = None
        # 最后一次模型级失败。整条候选链走完仍失败时，这个必须随 outcome 一起交给
        # worker——worker 靠它判断「等额度恢复后自动重试」还是「判死等人处理」。
        last_failure: dict | None = None
        # 第一条记录：让「继续上次会话」在这一轮还没产出任何东西时就有据可查。
        # 父类只在开跑前解析一次会话 id，接不上索引就会直接报「没有可续跑的会话」。
        self._remember_session(
            task_id=task_id,
            attempt=attempt,
            session_id=self.resume_session,
            run_id=None,
            worktree=worktree,
        )
        if self.resume_session:
            line = f"▶ 接着上一轮 DSH 会话 {self.resume_session} 继续"
            history.append(line)
            self._emit(line)

        for index, target in enumerate(chain, start=1):
            if index > 1:
                line = f"▶ 换候选模型 {index}/{len(chain)}：{self._label(target)}"
                history.append(line)
                self._emit(line)
            outcome, failure, logs = self._run_once(
                worktree,
                prompt,
                task_id=task_id,
                attempt=attempt,
                cancel_event=cancel_event,
                target=target,
                deadline=deadline,
                # 只有链上第一个模型能接续上次会话：后面的是换模型重来
                allow_resume=index == 1,
            )
            if failure:
                last_failure = failure
            history.extend(logs)
            if outcome.ok or outcome.status in ("completed", "blocked"):
                break
            # 取消 / 超时是外部决定，换模型没有意义，而且会突破这一轮的时间预算
            if outcome.status == "timed_out" or (cancel_event is not None and cancel_event.is_set()):
                break
            if not can_fall_back(failure):
                break
            reasons.append(f"{self._label(target)} → {describe_failure(failure)}")
            if index < len(chain):
                line = f"⚠ {self._label(target)} 不可用（{describe_failure(failure)}），继续试下一个"
                history.append(line)
                self._emit(line)

        assert outcome is not None
        # 续跑被「会话写锁」挡住时降级重开一次。
        #
        # 为什么必须降级而不是直接失败：DSH 的会话写所有权是内核命名信号量，
        # 只有持有者进程退出才释放。宿主异常退出（崩溃、被强杀、装更新）会留下一个
        # 仍被占用但已经没人干活的会话，此后每次 resume 都拿 `SessionAlreadyOwnedError`，
        # 任务就永久不可续跑——而上一轮的改动好端端躺在工作区里。这种情况下
        # 「开新会话接着干」远好过「什么都不做」，工作区才是事实来源。
        if (
            self.resume_session
            and not outcome.ok
            and _is_session_write_locked(outcome.message)
        ):
            line = (
                "⚠ 上一轮会话仍被 DSH 宿主占用（写入锁未释放），"
                "改为在同一个工作目录开新会话继续：工作区里已有的改动就是上一轮的成果。"
            )
            history.append(line)
            self._emit(line)
            for index, target in enumerate(chain, start=1):
                outcome, failure, logs = self._run_once(
                    worktree,
                    prompt,
                    task_id=task_id,
                    attempt=attempt,
                    cancel_event=cancel_event,
                    target=target,
                    deadline=deadline,
                    # 不再接续：这一轮就是要绕开那把锁
                    allow_resume=False,
                )
                if failure:
                    last_failure = failure
                history.extend(logs)
                if outcome.ok or outcome.status in ("completed", "blocked"):
                    break
                if outcome.status == "timed_out" or (cancel_event is not None and cancel_event.is_set()):
                    break
                if not can_fall_back(failure):
                    break
                reasons.append(f"{self._label(target)} → {describe_failure(failure)}")

        # 收尾也记一条：面板据此显示「这一轮是完成、失败还是被中断」，
        # 而父类派下一轮时会按 attempt 取最新的那条来决定续哪个会话。
        session_index.record(
            self.session_index,
            task_id=task_id,
            attempt=attempt,
            session_id=outcome.thread_id or self.resume_session or "",
            run_id=outcome.run_id,
            cwd=str(worktree),
            status=outcome.status,
        )
        if reasons and not outcome.ok and outcome.status != "blocked":
            # 全部候选都试过了：把每个模型为什么不行写清楚，否则面板上只剩
            # 最后一行的「没有结构化结果」，看不出是额度问题。
            summary = "候选模型全部不可用：" + "；".join(reasons)
            outcome.message = f"{outcome.message}；{summary}" if outcome.message else summary

        # 把结构化的模型级失败交给 worker：它据此决定「等额度恢复自动重试」
        # 还是「判死等人处理」。成功时保持 None。
        if last_failure and not outcome.ok:
            outcome.failure = last_failure
        return self._finish(outcome, task_id=task_id, attempt=attempt, logs=history)

    def _run_once(
        self,
        worktree: Path,
        prompt: str,
        *,
        task_id: str,
        attempt: int,
        cancel_event,
        target: dict,
        deadline: float,
        allow_resume: bool,
    ) -> tuple[CodexOutcome, dict | None, list[str]]:
        """用**一个**指定模型跑一轮，返回 (结果, 模型级失败原因, 本段日志)。"""
        run_id = ids.ulid()
        # 会话 id 预先生成：失败的那一轮也能在面板里被认出来并续跑
        request_id = f"handoff-{task_id}-{attempt}"
        started = time.monotonic()

        try:
            handle = self._start(
                worktree,
                prompt,
                task_id=task_id,
                attempt=attempt,
                request_id=request_id,
                target=target,
                reuse_session=allow_resume,
            )
        except HandoffError as exc:
            return (
                CodexOutcome(
                    status="failed",
                    result=None,
                    message=exc.message,
                    run_id=run_id,
                    cli_version=self.version(),
                    duration_seconds=time.monotonic() - started,
                ),
                None,
                [],
            )

        agent_run = str(handle.get("runId") or "")
        session_id = str(handle.get("sessionId") or "") or (self.resume_session if allow_resume else None)
        if not agent_run:
            return (
                CodexOutcome(
                    status="failed",
                    result=None,
                    message="DSH 插件没有返回 runId",
                    run_id=run_id,
                    cli_version=self.version(),
                    duration_seconds=time.monotonic() - started,
                ),
                None,
                [],
            )

        logs: list[str] = []
        cursor = 0
        timed_out = False
        cancelled = False
        payload: dict = {}
        # 会话 id 一拿到就记进索引：这一轮后面无论成功、失败还是被宿主打断，
        # 「继续上次会话」都能找到它。插件在 resume 时会回填真正的 id，
        # 所以第一次看到 payload 里的 sessionId 也要再记一次（覆盖同一 run_id）。
        self._remember_session(
            task_id=task_id, attempt=attempt, session_id=session_id, run_id=agent_run, worktree=worktree
        )
        # 连续拿不到响应的次数。单次轮询超时只当作「这一拍没读到」，继续轮询：
        # agent 跑长命令时宿主事件循环被占住，卡十几秒很常见，而 run 还在推进。
        # 见 `_POLL_TRANSIENT_FAILURES`。
        missed_polls = 0
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    self._cancel(agent_run)
                if time.monotonic() > deadline:
                    timed_out = True
                    self._cancel(agent_run)
                try:
                    payload = self._poll(agent_run, after=cursor)
                except HandoffError as exc:
                    # 一次轮询失败不等于这一轮失败。只有连续失败到阈值，才认定
                    # 插件那条通路真的断了（宿主重启、端口换了、进程没了）。
                    missed_polls += 1
                    if missed_polls >= _POLL_TRANSIENT_FAILURES:
                        raise
                    if missed_polls == 1:
                        line = f"⚠ 读取执行进度失败（{exc.message}），继续重试"
                        logs.append(line)
                        self._emit(line)
                    if timed_out or cancelled:
                        break
                    time.sleep(_POLL_INTERVAL)
                    continue
                missed_polls = 0
                reported = str(payload.get("sessionId") or "").strip()
                if reported and reported != session_id:
                    session_id = reported
                    self._remember_session(
                        task_id=task_id,
                        attempt=attempt,
                        session_id=session_id,
                        run_id=agent_run,
                        worktree=worktree,
                    )
                for line in payload.get("logs") or []:
                    entry = str(line)
                    logs.append(entry)
                    self._emit(entry)
                cursor = int(payload.get("cursor") or cursor)
                if payload.get("phase") == "finished":
                    break
                if timed_out or cancelled:
                    # 取消/超时后最多再等两轮收尾，不让面板线程一直挂着
                    break
                time.sleep(_POLL_INTERVAL)
        except HandoffError as exc:
            return (
                CodexOutcome(
                    status="failed",
                    result=None,
                    message=f"DSH 执行器通信失败: {exc.message}",
                    run_id=agent_run or run_id,
                    thread_id=session_id,
                    cli_version=self.version(),
                    duration_seconds=time.monotonic() - started,
                ),
                None,
                logs,
            )

        failure = payload.get("failure") if isinstance(payload.get("failure"), dict) else None
        common = {
            "run_id": agent_run or run_id,
            "thread_id": session_id,
            "cli_version": self.version(),
            "duration_seconds": time.monotonic() - started,
        }

        if cancelled:
            return (CodexOutcome(status="failed", result=None, message="DSH 执行已取消", **common), failure, logs)
        if timed_out:
            return (
                CodexOutcome(
                    status="timed_out",
                    result=None,
                    message=f"DSH 执行超时（{int(self.timeout_seconds)}s）",
                    **common,
                ),
                failure,
                logs,
            )
        if payload.get("error"):
            return (
                CodexOutcome(
                    status="failed",
                    result=None,
                    message=f"DSH 执行失败: {clean(str(payload['error']), 800)}",
                    **common,
                ),
                failure,
                logs,
            )

        # 插件回传 Agent 的最终文本；结构化契约与 codex/zcode 共用同一个 schema
        final_text = str(payload.get("text") or "")
        parsed = _extract_json(final_text)
        if parsed is None:
            # 排除已经看到的模型级失败：额度用尽的那一轮根本没有最终文本，
            # 只报「没有结构化结果」会让人以为 agent 没干活。
            message = "DSH Agent 最终消息里没有结构化 JSON 结果"
            if failure:
                message = f"{message}（模型失败：{describe_failure(failure)}）"
            return (CodexOutcome(status="invalid_output", result=None, message=message, **common), failure, logs)
        errs = schema.errors_for("codex-result", parsed)
        if errs:
            return (
                CodexOutcome(
                    status="invalid_output",
                    result=parsed,
                    message="DSH 结果不符合 schema: " + "; ".join(errs[:5]),
                    **common,
                ),
                failure,
                logs,
            )
        return (
            CodexOutcome(
                status=str(parsed.get("status") or "completed"),
                result=parsed,
                message=clean(parsed.get("summary", ""), 500),
                **common,
            ),
            failure,
            logs,
        )

    def _finish(self, outcome: CodexOutcome, *, task_id: str, attempt: int, logs: list[str]) -> CodexOutcome:
        """把整条候选链的日志落成一份文件。

        为什么不在每次尝试里各写一份：一个任务的一次 attempt 有且只有一个日志
        文件，人点「执行日志」时看到的是这一轮**从头到尾**发生了什么（包括换过
        哪个模型、为什么换）。分段写会变成好几个同名文件，反而看不出因果。
        """
        log_path = self._write_log(
            task_id, attempt, outcome.run_id or ids.ulid(), logs, session_id=outcome.thread_id
        )
        if log_path:
            outcome.log_path = log_path
        return outcome

    # -- HTTP ---------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _start(
        self,
        worktree: Path,
        prompt: str,
        *,
        task_id: str,
        attempt: int,
        request_id: str,
        target: dict | None = None,
        reuse_session: bool = True,
    ) -> dict:
        """派一次 run。`target` 是候选链里的一个模型；缺省用实例上的主模型。"""
        chosen = target or {"provider": self.provider, "model": self.model, "effort": self.effort}
        # 换候选模型时**不能**接续上一个模型的会话：那个 DSH 会话绑的正是刚
        # 失败的那个模型/provider，续跑只会把同样的失败再演一遍。所以只有链上
        # 第一次尝试才带 resumeSessionId。
        resume = self.resume_session if reuse_session else None
        body = {
            "cwd": str(worktree),
            "prompt": prompt,
            "taskId": task_id,
            "attempt": attempt,
            "requestId": request_id,
            "provider": chosen.get("provider") or None,
            "model": chosen.get("model") or None,
            "effort": chosen.get("effort") or None,
            "sandbox": self.sandbox or None,
            "resumeSessionId": resume or None,
        }
        response = self._client.request(
            "POST",
            f"{self.bridge_url}/agent/run",
            headers=self._headers(),
            body=json.dumps(body, ensure_ascii=False),
        )
        if response.status == 202:
            try:
                return json.loads(response.body or "{}")
            except ValueError:
                raise ExecutionError("DSH 插件返回了无法解析的 run 句柄", stderr=clean(response.body, 400)) from None
        raise ExecutionError(
            _describe_failure(response.status, response.body),
            stderr=clean(response.body, 400),
        )

    def _poll(self, run: str, *, after: int) -> dict:
        response = self._client.request(
            "GET",
            f"{self.bridge_url}/agent/run/{run}?after={after}",
            headers=self._headers(),
        )
        if response.status != 200:
            raise ExecutionError(
                _describe_failure(response.status, response.body),
                stderr=clean(response.body, 400),
            )
        try:
            return json.loads(response.body or "{}")
        except ValueError:
            raise ExecutionError("DSH 插件返回的 run 状态不是合法 JSON") from None

    def _cancel(self, run: str) -> None:
        try:
            self._client.request(
                "POST",
                f"{self.bridge_url}/agent/run/{run}/cancel",
                headers=self._headers(),
                body="{}",
            )
        except HandoffError:
            # 取消是尽力而为：插件可能已经收尾，不能因此把失败原因盖掉
            pass

    # -- 辅助 ---------------------------------------------------------------
    def _remember_session(
        self,
        *,
        task_id: str,
        attempt: int,
        session_id: str | None,
        run_id: str | None,
        worktree: Path,
    ) -> None:
        """把这一轮的会话 id 记进索引；写不进去不影响执行（见 session_index.record）。"""
        sid = str(session_id or "").strip()
        if not sid:
            return
        session_index.record(
            self.session_index,
            task_id=task_id,
            attempt=attempt,
            session_id=sid,
            run_id=run_id,
            cwd=str(worktree),
        )

    def _emit(self, text: str) -> None:
        if self.on_event is None or not text:
            return
        try:
            self.on_event(text)
        except Exception:  # noqa: BLE001 - 面板回调异常不能中断执行
            pass

    def _write_log(
        self,
        task_id: str,
        attempt: int,
        run_id: str,
        logs: list[str],
        *,
        session_id: str | None = None,
    ) -> str | None:
        if self.log_dir is None:
            return None
        directory = self.log_dir / task_id / f"attempt-{attempt}"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"dsh-{run_id}.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            # 首行必须是会话元数据：父类的 `_log_sessions` 就是靠它把失败的 attempt
            # 也认出来（那条路径原本只服务 codex 的 thread.started）。写成同形
            # 结构，dsh 的会话才能出现在「历史会话」里，而不只在索引里。
            if session_id:
                fh.write(
                    json.dumps(
                        {"type": "thread.started", "thread_id": session_id, "executor": "dsh"},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            for line in logs:
                fh.write(json.dumps({"type": "log", "text": clean(line, _EVENT_TEXT_LIMIT)}, ensure_ascii=False) + "\n")
        return str(path)


def _is_session_write_locked(message: str | None) -> bool:
    """DSH 宿主拒绝 resume 是因为会话的写锁还被别的句柄持有吗？

    只认宿主自己那两处措辞（`SessionAlreadyOwnedError` 的文本、网关的
    `session/writer-held` 码）。别的 resume 失败（会话不存在、格式不支持）
    重开一次也不会好转，反而把一次明确的错误拖成两次。
    """
    text = str(message or "")
    return "is already owned by an active write handle" in text or "session/writer-held" in text


def _describe_failure(status: int, body: str) -> str:
    """把插件的 HTTP 错误翻成用户能照做的句子。"""
    detail = clean(body, 300)
    if status == 401:
        return "DSH 插件拒绝令牌：检查 studio 与 dsh-handoff-studio 是否用同一个令牌文件"
    if status == 403:
        return "DSH 插件只接受本机连接"
    if status == 404:
        return "DSH 插件的 studio 路由不存在：确认 dsh-handoff-studio 已在该 profile 启用并重启 DSH"
    if status == 409:
        return f"DSH 侧已有任务在执行（同一时刻只跑一个 Agent）：{detail}"
    if status == 413:
        return "任务 prompt 超出插件允许的请求体上限"
    return f"DSH 插件返回 HTTP {status}: {detail}"


__all__ = [
    "DshAgentRunner",
    "DEFAULT_ROUTE",
    "FALLBACK_FAILURE_CODES",
    "PROMPT_VERSION",
    "can_fall_back",
    "describe_failure",
]
