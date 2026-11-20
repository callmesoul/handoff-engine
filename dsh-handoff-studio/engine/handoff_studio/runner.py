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

PROMPT_VERSION = "handoff-dsh-v1"

#: 插件注册的前缀路由，与 dsh-plugin 的约定一致。
DEFAULT_ROUTE = "/handoff-studio"

#: 轮询间隔：面板要实时日志，但也不能把宿主打满。
_POLL_INTERVAL = 0.6

#: 单条日志最多保留长度，防止整段源码刷屏（与 zcode 执行器一致）。
_EVENT_TEXT_LIMIT = 400


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
    ):
        self.bridge_url = bridge_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.log_dir = Path(log_dir) if log_dir else None
        self.model = model
        self.provider = provider
        self.effort = effort
        # 访问权限隔离：DSH 侧的沙箱档位（read-only / workspace-write /
        # danger-full-access）。它走插件宿主 session 的 sandbox/mode 事件，
        # 不是命令行参数——所以和 codex 的 --sandbox 是两条不同的通路。
        self.sandbox = sandbox
        self.permission_mode = permission_mode
        self.env_extra = env_extra or {}
        # 续跑：接续上一轮的 DSH 会话（sessionController 自己会 resume）
        self.resume_session = resume_session
        self.on_event = on_event
        self._version: str | None = None
        self._client = HttpClient(timeout=30, allow_private=True, retries=1)

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
        worktree = Path(worktree)
        run_id = ids.ulid()
        # 会话 id 预先生成：失败的那一轮也能在面板里被认出来并续跑
        request_id = f"handoff-{task_id}-{attempt}"

        started = time.monotonic()
        try:
            handle = self._start(worktree, prompt, task_id=task_id, attempt=attempt, request_id=request_id)
        except HandoffError as exc:
            return CodexOutcome(
                status="failed",
                result=None,
                message=exc.message,
                run_id=run_id,
                cli_version=self.version(),
                duration_seconds=time.monotonic() - started,
            )

        agent_run = str(handle.get("runId") or "")
        session_id = str(handle.get("sessionId") or "") or self.resume_session
        if not agent_run:
            return CodexOutcome(
                status="failed",
                result=None,
                message="DSH 插件没有返回 runId",
                run_id=run_id,
                cli_version=self.version(),
                duration_seconds=time.monotonic() - started,
            )

        logs: list[str] = []
        cursor = 0
        timed_out = False
        cancelled = False
        deadline = started + float(self.timeout_seconds)
        payload: dict = {}
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    self._cancel(agent_run)
                if time.monotonic() > deadline:
                    timed_out = True
                    self._cancel(agent_run)
                payload = self._poll(agent_run, after=cursor)
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
            return CodexOutcome(
                status="failed",
                result=None,
                message=f"DSH 执行器通信失败: {exc.message}",
                run_id=agent_run or run_id,
                thread_id=session_id,
                cli_version=self.version(),
                log_path=self._write_log(task_id, attempt, run_id, logs),
                duration_seconds=time.monotonic() - started,
            )

        duration = time.monotonic() - started
        log_path = self._write_log(task_id, attempt, run_id, logs)
        common = {
            "run_id": agent_run or run_id,
            "thread_id": session_id,
            "cli_version": self.version(),
            "log_path": log_path,
            "duration_seconds": duration,
        }

        if cancelled:
            return CodexOutcome(
                status="failed",
                result=None,
                message="DSH 执行已取消",
                **common,
            )
        if timed_out:
            return CodexOutcome(
                status="timed_out",
                result=None,
                message=f"DSH 执行超时（{int(self.timeout_seconds)}s）",
                **common,
            )
        if payload.get("error"):
            return CodexOutcome(
                status="failed",
                result=None,
                message=f"DSH 执行失败: {clean(str(payload['error']), 800)}",
                **common,
            )

        # 插件回传 Agent 的最终文本；结构化契约与 codex/zcode 共用同一个 schema
        final_text = str(payload.get("text") or "")
        parsed = _extract_json(final_text)
        if parsed is None:
            return CodexOutcome(
                status="invalid_output",
                result=None,
                message="DSH Agent 最终消息里没有结构化 JSON 结果",
                **common,
            )
        errs = schema.errors_for("codex-result", parsed)
        if errs:
            return CodexOutcome(
                status="invalid_output",
                result=parsed,
                message="DSH 结果不符合 schema: " + "; ".join(errs[:5]),
                **common,
            )
        return CodexOutcome(
            status=str(parsed.get("status") or "completed"),
            result=parsed,
            message=clean(parsed.get("summary", ""), 500),
            **common,
        )

    # -- HTTP ---------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _start(self, worktree: Path, prompt: str, *, task_id: str, attempt: int, request_id: str) -> dict:
        body = {
            "cwd": str(worktree),
            "prompt": prompt,
            "taskId": task_id,
            "attempt": attempt,
            "requestId": request_id,
            "provider": self.provider or None,
            "model": self.model or None,
            "effort": self.effort or None,
            "sandbox": self.sandbox or None,
            "resumeSessionId": self.resume_session or None,
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
    def _emit(self, text: str) -> None:
        if self.on_event is None or not text:
            return
        try:
            self.on_event(text)
        except Exception:  # noqa: BLE001 - 面板回调异常不能中断执行
            pass

    def _write_log(self, task_id: str, attempt: int, run_id: str, logs: list[str]) -> str | None:
        if self.log_dir is None:
            return None
        directory = self.log_dir / task_id / f"attempt-{attempt}"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"dsh-{run_id}.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for line in logs:
                fh.write(json.dumps({"type": "log", "text": clean(line, _EVENT_TEXT_LIMIT)}, ensure_ascii=False) + "\n")
        return str(path)


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


__all__ = ["DshAgentRunner", "DEFAULT_ROUTE", "PROMPT_VERSION"]
