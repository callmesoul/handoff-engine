"""调用 `zcode -p` 作为实现执行器（与 CodexRunner 同构的鸭子接口）。

边界与 CodexRunner 一致：
    - 只给实现所需的材料，不给推送凭据；
    - commit / push / PR 由 worker 做；
    - 结果从最终文本里提取 JSON——zcode 没有 --output-schema，
      依赖 prompt 里的输出契约 + `_extract_json` 兜底。

会话语义（zcode 0.1.82 实测）：
    - `zcode -p` 无位置参数时从 stdin 读 prompt；
    - `--output-format json` 输出单对象，含 session_id / result / is_error；
    - 新会话用 `--session-id <uuid>` 预指定 id（不需要解析输出就能 resume）；
    - 续会话用 `--resume <session-id>`，保留原会话上下文。
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import Callable

from . import executil, ids, schema
from .codex_runner import CodexOutcome, _extract_json, run_headless, sanitized_env
from .errors import ConfigError, HandoffError
from .sanitize import clean, compact, repair_mojibake

PROMPT_VERSION = "handoff-zcode-v1"

PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan", "yolo")

# 面板实时展示时，单条文本最多保留这么长（防止整段代码刷屏）
_EVENT_TEXT_LIMIT = 400


def session_uuid(task_id: str, attempt: int) -> str:
    """每个 (task, attempt) 一个确定性的新会话 id，跑完即可用于 resume。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"handoff-zcode/{task_id}/attempt-{attempt}"))


class ZcodeRunner:
    name = "zcode"

    def __init__(
        self,
        *,
        binary: str = "zcode",
        timeout_seconds: float = 3600,
        log_dir: str | Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        permission_mode: str = "acceptEdits",
        env_extra: dict[str, str] | None = None,
        resume_session: str | None = None,
        stream: bool = False,
        on_event: Callable[[str], None] | None = None,
    ):
        if permission_mode not in PERMISSION_MODES:
            raise ConfigError(
                f"zcode permission_mode 非法: {permission_mode!r}（允许: {', '.join(PERMISSION_MODES)}）"
            )
        self.binary = binary
        self._resolved_binary: str | None = None
        self.timeout_seconds = timeout_seconds
        self.log_dir = Path(log_dir) if log_dir else None
        self.model = model
        self.effort = effort
        self.permission_mode = permission_mode
        self.env_extra = env_extra or {}
        # 指定时用 `--resume <session-id>` 接续已有会话上下文
        self.resume_session = resume_session
        # 流式模式：--output-format stream-json，逐事件回调给面板
        self.stream = stream
        self.on_event = on_event
        self._version: str | None = None
        self.session_seen: str | None = None

    def binary_path(self) -> str:
        if self._resolved_binary is None:
            self._resolved_binary = executil.require(
                self.binary,
                env_var="HANDOFF_ZCODE",
                hint="安装 zcode CLI，或在 config/local.yaml 的 zcode.binary 指定绝对路径",
            )
        return self._resolved_binary

    def version(self) -> str | None:
        if self._version is not None:
            return self._version
        try:
            proc = subprocess.run(
                [self.binary_path(), "--version"],
                capture_output=True,
                text=True,
                timeout=30,
                env=sanitized_env(),
            )
            raw = (proc.stdout or proc.stderr).strip().splitlines()[0] if (proc.stdout or proc.stderr) else None
            # 防御：--version 输出异常时（比如 stub 把正文打出来）不要把超长文本
            # 塞进任务单的 codex_cli_version（schema 上限 100 字符）
            self._version = clean(raw, 100) or None if raw else None
        except (OSError, subprocess.SubprocessError, IndexError, HandoffError):
            self._version = None
        return self._version

    def _command(self, task_id: str, attempt: int) -> tuple[list[str], str]:
        """返回 (argv, 本轮会话 id)。新会话预指定 uuid，resume 用传入 id。"""
        cmd = [
            self.binary_path(),
            "-p",
            "--output-format",
            "stream-json" if self.stream else "json",
            "--permission-mode",
            self.permission_mode,
        ]
        if self.stream:
            # stream-json 在 -p 下必须配 --verbose，否则 zcode 直接报错退出
            cmd.append("--verbose")
        if self.model:
            cmd.extend(["--model", self.model])
        if self.effort:
            cmd.extend(["--effort", self.effort])
        if self.resume_session:
            cmd.extend(["--resume", self.resume_session])
            session_id = self.resume_session
        else:
            session_id = session_uuid(task_id, attempt)
            cmd.extend(["--session-id", session_id])
        return cmd, session_id

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
        try:
            cmd, known_session = self._command(task_id, attempt)
        except HandoffError as exc:
            return CodexOutcome(
                status="failed",
                result=None,
                message=exc.message,
                run_id=run_id,
                cli_version=None,
                duration_seconds=0.0,
            )

        headless = run_headless(
            cmd,
            worktree,
            prompt,
            timeout_seconds=self.timeout_seconds,
            env=sanitized_env(self.env_extra),
            cancel_event=cancel_event,
            on_line=self._on_line if self.stream else None,
        )
        log_path = self._write_log(
            task_id, attempt, run_id, headless.stdout_text, headless.stderr_text, headless.launch_error
        )

        common = {
            "run_id": run_id,
            "exit_code": headless.exit_code,
            "cli_version": self.version(),
            "log_path": log_path,
            "stderr_tail": clean(headless.stderr_text, 2000),
            "duration_seconds": headless.duration_seconds,
        }

        if headless.launch_error:
            return CodexOutcome(status="failed", result=None, message=headless.launch_error, **common)
        if headless.timed_out:
            return CodexOutcome(
                status="timed_out",
                result=None,
                message=f"zcode 超时（{int(self.timeout_seconds)}s）",
                **common,
            )

        # zcode -p --output-format json 输出单个 JSON 对象
        envelope = _parse_envelope(headless.stdout_text)
        if envelope is None:
            return CodexOutcome(
                status="invalid_output",
                result=None,
                message="zcode 输出不是合法 JSON 结果对象",
                thread_id=known_session,
                **common,
            )

        session_id = envelope.get("session_id") or known_session or self.session_seen
        common["run_id"] = session_id or run_id
        final_text = envelope.get("result")
        if envelope.get("is_error"):
            return CodexOutcome(
                status="failed",
                result=None,
                message=clean(final_text if isinstance(final_text, str) else "", 500) or "zcode 会话报错",
                thread_id=session_id,
                **common,
            )

        parsed = _extract_json(final_text) if isinstance(final_text, str) else None
        if parsed is None:
            return CodexOutcome(
                status="invalid_output",
                result=None,
                message="zcode 最终消息里没有结构化 JSON 结果",
                thread_id=session_id,
                **common,
            )

        errs = schema.errors_for("codex-result", parsed)
        if errs:
            return CodexOutcome(
                status="invalid_output",
                result=parsed,
                message="zcode 结果不符合 schema: " + "; ".join(errs[:5]),
                thread_id=session_id,
                **common,
            )

        status = parsed.get("status", "completed")
        if headless.exit_code not in (0, None) and status == "completed":
            status = "failed"

        return CodexOutcome(
            status=status,
            result=parsed,
            message=clean(parsed.get("summary", ""), 500),
            thread_id=session_id,
            **common,
        )

    def _on_line(self, kind: str, line: str) -> None:
        """stream-json 逐行回调：把事件压成一行人话，喂给面板。"""
        text = line.strip()
        if not text:
            return
        if kind != "stdout":
            self._emit(f"[stderr] {clean(text, _EVENT_TEXT_LIMIT)}")
            return
        event = _loads(text)
        if event is None:
            self._emit(clean(text, _EVENT_TEXT_LIMIT))
            return
        if not self.session_seen:
            session = event.get("session_id")
            if isinstance(session, str) and session:
                self.session_seen = session
        desc = _describe_event(event)
        if desc:
            self._emit(desc)

    def _emit(self, text: str) -> None:
        if self.on_event is not None and text:
            try:
                self.on_event(text)
            except Exception:  # noqa: BLE001 - 面板回调异常不能中断执行
                pass

    def _write_log(self, task_id: str, attempt: int, run_id: str, stdout: str, stderr: str, error: str | None) -> str | None:
        if self.log_dir is None:
            return None
        directory = self.log_dir / task_id / f"attempt-{attempt}"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"zcode-{run_id}.json"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(stdout)
            if stderr:
                fh.write("\n--- stderr ---\n")
                fh.write(stderr)
            if error:
                fh.write("\n--- launcher error ---\n")
                fh.write(error)
        return str(path)


def _loads(line: str) -> dict | None:
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _brief_tool_input(name: str, data) -> str:
    """把工具入参压成一行，面板里看得懂就行。"""
    if not isinstance(data, dict):
        return compact(repair_mojibake(str(data)), 120)
    for key in ("command", "file_path", "path", "pattern", "url", "description", "prompt"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return compact(repair_mojibake(value), 160)
    try:
        return compact(json.dumps(data, ensure_ascii=False), 120)
    except (TypeError, ValueError):
        return ""


def _describe_event(event: dict) -> str | None:
    """把 zcode stream-json 事件翻成一行人话；无关事件返回 None。"""
    kind = event.get("type")
    if kind == "system":
        if event.get("subtype") == "init":
            model = event.get("model") or "?"
            mode = event.get("permissionMode") or "?"
            tools = len(event.get("tools") or [])
            return f"▶ 会话启动：模型 {model} · 权限 {mode} · 工具 {tools} 个"
        return None
    if kind in ("assistant", "user"):
        message = event.get("message") or {}
        blocks = message.get("content")
        if isinstance(blocks, str):
            return clean(" ".join(blocks.split()), _EVENT_TEXT_LIMIT) or None
        if not isinstance(blocks, list):
            return None
        lines: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = clean(" ".join(str(block.get("text") or "").split()), _EVENT_TEXT_LIMIT)
                if text:
                    lines.append(text)
            elif block_type == "tool_use":
                name = str(block.get("name") or "tool")
                brief = _brief_tool_input(name, block.get("input"))
                lines.append(f"🔧 {name}" + (f"  {brief}" if brief else ""))
            elif block_type == "tool_result":
                if block.get("is_error"):
                    content = block.get("content")
                    if isinstance(content, list):
                        content = " ".join(
                            str(c.get("text") or "") for c in content if isinstance(c, dict)
                        )
                    lines.append("⚠ 工具返回错误: " + clean(str(content or ""), 200))
        return "\n".join(lines) if lines else None
    if kind == "result":
        subtype = event.get("subtype") or "?"
        turns = event.get("num_turns")
        cost = event.get("total_cost_usd")
        duration = event.get("duration_ms")
        parts = [f"■ 结束：{subtype}"]
        if turns is not None:
            parts.append(f"{turns} 轮")
        if isinstance(duration, (int, float)):
            parts.append(f"{duration / 1000:.1f}s")
        if isinstance(cost, (int, float)) and cost:
            parts.append(f"${cost:.4f}")
        if event.get("is_error"):
            parts.append("is_error=true")
        return " · ".join(parts)
    return None


def _parse_envelope(stdout: str) -> dict | None:
    """取执行结果对象：json 模式是单个对象，stream-json 模式是多行事件流。"""
    stripped = stdout.strip()
    if not stripped:
        return None
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            return obj
    for line in reversed(stripped.splitlines()):
        obj = _loads(line.strip())
        if obj is None:
            continue
        if obj.get("type") == "result" or ("result" in obj and "is_error" in obj):
            return obj
    return None
