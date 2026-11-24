"""引擎内的完成通知投递线程：补上 studio 缺的那段 `handoffctl notifier`。

## 为什么需要它（实机 bug）

`handoff` 的完成通知是**两段式**的：

    store._write() 进入 verified/failed
        └─ notification.emit_task_completed() → 只把 task.completed 写进
           本机 outbox（.handoff-run/notify/outbox.sqlite3），状态 "pending"

    handoffctl notifier（独立常驻进程，Notifier.run_forever）
        └─ 定时 drain outbox，签名并 POST 到前端监听端口

原面板由 `run-backend.ps1` / systemd / 计划任务把 notifier 一起拉起来，所以没人
注意到第二段是另一个进程。studio 引擎是 DSH 插件按需 spawn 的**单个** Python
进程，只起 HTTP API——于是：

  * 面板点「恢复推进」→ 后台跑 cmd_deliver → 任务到 verified
  * 事件进了 outbox，`pending` 一直不变
  * 前端会话再也收不到 task.completed

而「重发完成通知」按钮走 `replay_task_completed`，那里自己调了 `send_due(event_id=…)`
同步发一次——所以现象是「重发能用、推进不通知」，很容易被误判成密钥或网段问题。

本模块把缺掉的常驻逻辑搬进引擎：后台线程定期 drain outbox，`kick()` 让推进/验收
结束时立刻发一轮，不必等下一个 tick。

## 只发「本轮之后」的事件

outbox 里可能积着一堆历史 pending（别的进程、别的前端、更早的版本留下的）。一次性
全发出去会给前端灌一串迟到的完成事件，每个都会让前端恢复会话、重新拉交接仓。
所以这里维护一个水位（watermark）：

  * 启动时取 `max(上次持久化的水位, 现在 - grace)`；
  * 只自动投递 `created_at >= 水位` 的事件；
  * 每轮把水位推进到已见过的最新事件时间并落盘——**引擎重启不会漏发**它自己
    刚入队、还没来得及发的事件。

更老的一律不动，只在状态里报数（`stale`），需要时用「重发完成通知」或
`handoffctl notifier --once` 人工处理。

## 不做的事

- **不改** outbox / 签名 / 退避语义：发不发、怎么重试全在 `handoff.notification` 里，
  这里只负责「把该发的发出去」。
- **不把密钥写进任何地方**：`load_secret()` 原来的路径照旧，密钥由 `secrets_bridge`
  在服务构造时补进环境。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import timedelta
from pathlib import Path

from handoff import notification
from handoff.errors import ConfigError, HandoffError, SchemaError
from handoff.timeutil import iso, now, parse

#: 轮询间隔。比原 notifier 的 1s 略宽：面板自己的轮询就是 3s 级。
DEFAULT_INTERVAL_SECONDS = 2.0
#: 单次最多发多少条。
DEFAULT_BATCH = 20
#: 水位回看的宽限：第一次启动时，最近这几分钟内创建的事件仍然算「本轮的」。
DEFAULT_GRACE_SECONDS = 600.0
#: 密钥不可用时不要每 tick 都去读一遍环境/配置文件。
SECRET_RETRY_SECONDS = 30.0
#: 连续出错时的退避上限，避免临时目录被删之类的错误把日志刷满。
MAX_BACKOFF_SECONDS = 30.0
#: 水位落盘位置（相对交接仓根）。只是时间戳，不含密钥与正文。
WATERMARK_FILE = Path(".handoff-run") / "notify" / "studio-pump-watermark.json"


class NotificationPump:
    """后台 drain outbox；`flush()` 同步发一轮，`kick()` 叫醒等待中的线程。

    `notifier_factory` 是给测试的注入点：只要返回一个带 `.outbox` 与 `.send_due()`
    的对象就能替代真的 `notification.Notifier`。
    """

    def __init__(
        self,
        root: str | Path,
        config,
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        batch: int = DEFAULT_BATCH,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        logger=None,
        notifier_factory=None,
        watermark: str | None = None,
        watermark_path: str | Path | None = None,
    ):
        self.root = Path(root)
        self.config = config
        self.interval_seconds = max(0.2, float(interval_seconds))
        self.batch = max(1, int(batch))
        self.grace_seconds = max(0.0, float(grace_seconds))
        self._logger = logger or (lambda _message: None)
        self._factory = notifier_factory
        self._notifier = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        #: 有新事件时置位，让后台线程立刻发一轮而不是等下一个 tick。
        self._dirty = threading.Event()
        self._thread: threading.Thread | None = None
        self._secret_retry_after = 0.0
        self._failures = 0
        self._watermark_path = (
            Path(watermark_path) if watermark_path is not None else self.root / WATERMARK_FILE
        )
        #: 自动投递的下界：比它更老的事件只报数，不擅自重发。
        self.watermark = watermark or self._initial_watermark()
        self._state = {
            "running": False,
            "pending": None,
            "stale": None,
            "last_flush_at": None,
            "last_sent": 0,
            "sent_at": None,
            "last_error": None,
            "secret_ok": None,
            "total_sent": 0,
        }

    def _initial_watermark(self) -> str:
        """水位取「持久化水位」与「现在 - grace」里更晚的那个。

        第一条保证**重启不漏发**：引擎被插件重启（改配置 / 升级 / 崩溃重拉）时，
        outbox 里可能还躺着上一进程刚入队、还没来得及发的事件。
        第二条保证**不翻旧账**：几个月不动的话，持久化水位已经太老，直接按 grace
        重新划一条线，别把历史积压全灌给前端。
        """
        floor = _shift_iso(-self.grace_seconds)
        stored = self._read_watermark()
        if not stored:
            return floor
        try:
            return stored if parse(stored) > parse(floor) else floor
        except (ValueError, TypeError):
            return floor

    def _read_watermark(self) -> str | None:
        try:
            raw = self._watermark_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None
        value = parsed.get("watermark") if isinstance(parsed, dict) else None
        if not isinstance(value, str) or not value:
            return None
        try:
            parse(value)
        except (ValueError, TypeError):
            return None
        return value

    def _save_watermark(self) -> None:
        """只写时间戳；失败只是少一层重启保护，不该让投递停摆。"""
        try:
            self._watermark_path.parent.mkdir(parents=True, exist_ok=True)
            self._watermark_path.write_text(
                json.dumps(
                    {"watermark": self.watermark, "updated_at": iso(), "version": 1},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            self._logger(f"水位落盘失败（不影响投递）：{exc}")

    def _advance_watermark(self, created_at: str | None) -> None:
        """把水位推进到已见过的最新事件时间，只前进不后退。

        比较用「不早于」：已发出去的那条事件的 `created_at` 本身就在水位上，
        下一轮不该再被算成新的。相等时也落一次盘，这样重启后水位文件一定存在。
        """
        if not created_at:
            return
        try:
            moment = parse(str(created_at))
            if moment < parse(self.watermark):
                return
        except (ValueError, TypeError):
            return
        updated = iso(moment)
        if updated == self.watermark and self._watermark_path.exists():
            return
        self.watermark = updated
        self._save_watermark()

    # -- 生命周期 ------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._state["running"] = True
            self._thread = threading.Thread(
                target=self._run, name="studio-notification-pump", daemon=True
            )
            self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        self._dirty.set()  # 立刻叫醒 wait，别让 shutdown 白等一个 interval
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            self._state["running"] = False

    def kick(self) -> None:
        """有新事件入队时喊一声：后台线程立刻发一轮，不等下一个 tick。"""
        self._dirty.set()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.flush()
                except Exception as exc:  # noqa: BLE001 - 后台线程不能崩
                    self._logger(f"通知投递线程异常：{type(exc).__name__}: {exc}")
                    with self._lock:
                        self._state["last_error"] = f"{type(exc).__name__}: {exc}"
                        self._failures += 1
                if self._dirty.wait(self._next_wait()):
                    self._dirty.clear()
        finally:
            with self._lock:
                self._state["running"] = False

    def _next_wait(self) -> float:
        if self._failures <= 0:
            return self.interval_seconds
        return min(self.interval_seconds * (2**self._failures), MAX_BACKOFF_SECONDS)

    # -- 投递 ----------------------------------------------------------------
    def flush(self) -> dict:
        """把「水位之后、已到期」的通知发出去；同步返回本次投递结果。

        结果形状沿用 `Notifier.send_due` 的 `{sent, failed, pending, errors}`，
        外加 `stale`（水位之前积压的条数）与 `idle`（没有可发的事件）。

        outbox 打不开（权限/磁盘）是**环境问题**，不是调用方的错：这里把它变成
        一条可读的 error 结果，而不是让面板按钮收到一个 500。
        """
        with self._lock:
            try:
                return self._flush_locked()
            except sqlite3.Error as exc:
                message = f"本机通知队列不可用：{exc}"
                self._state["last_error"] = message
                self._logger(message)
                return {
                    "sent": 0,
                    "failed": 0,
                    "pending": self._state["pending"],
                    "stale": self._state["stale"],
                    "errors": [{"error": message}],
                    "error": message,
                }

    def _flush_locked(self) -> dict:
        notifier = self._notifier_instance()
        pending = notifier.outbox.pending_count()
        # 多取一些：due 里可能混着水位之前的历史积压，得过滤掉再决定发不发
        due = list(notifier.outbox.due(limit=max(self.batch, 200)))
        fresh = [row for row in due if _is_after(row["created_at"], self.watermark)]
        stale = max(0, len(due) - len(fresh))

        if not fresh:
            self._note(pending=pending, stale=stale, sent=0, error=None)
            return {
                "sent": 0,
                "failed": 0,
                "pending": pending,
                "stale": stale,
                "errors": [],
                "idle": True,
            }

        if time.monotonic() < self._secret_retry_after:
            # 上次因为密钥缺失失败：别每 tick 都去读一遍环境/配置
            return {
                "sent": 0,
                "failed": 0,
                "pending": pending,
                "stale": stale,
                "errors": [],
                "skipped": "secret_unavailable",
                "error": self._state["last_error"],
            }

        sent = 0
        failed = 0
        errors: list[dict] = []
        for row in fresh[: self.batch]:
            try:
                # 逐条按 event_id 发：send_due 只会发「到期」的，过滤不了水位，
                # 必须精确控制「只发这一条」，免得顺手把历史积压带走。
                result = notifier.send_due(event_id=row["event_id"])
            except ConfigError as exc:
                message = getattr(exc, "message", None) or str(exc)
                self._secret_retry_after = time.monotonic() + SECRET_RETRY_SECONDS
                errors.append({"event_id": row["event_id"], "error": message})
                self._note(pending=pending, stale=stale, sent=sent, error=message, secret_ok=False)
                self._logger(f"完成通知暂不可发：{message}")
                break
            except (HandoffError, SchemaError) as exc:
                message = getattr(exc, "message", None) or str(exc)
                errors.append({"event_id": row["event_id"], "error": message})
                self._logger(f"完成通知投递失败：{message}")
                continue
            sent += int(result.get("sent") or 0)
            failed += int(result.get("failed") or 0)
            for item in result.get("errors") or []:
                errors.append(item)
            if result.get("pending") is not None:
                pending = int(result["pending"])

        # 发过（或确认无需再发）就推进水位：这些事件下一轮不该再被当成「新的」
        last_seen = fresh[min(len(fresh), self.batch) - 1]["created_at"]
        self._advance_watermark(last_seen)
        self._note(
            pending=pending,
            stale=stale,
            sent=sent,
            error=(errors[0].get("error") if errors else None),
        )
        if sent:
            # 能发出去就说明密钥可用：把「密钥不可用」的结论翻回来
            self._state["secret_ok"] = True
            self._logger(f"完成通知已送达前端（{sent} 条）")
        return {
            "sent": sent,
            "failed": failed,
            "pending": pending,
            "stale": stale,
            "errors": errors,
        }

    def _note(self, *, pending, stale, sent: int, error, secret_ok=None) -> None:
        self._state["pending"] = pending
        self._state["stale"] = stale
        self._state["last_flush_at"] = iso()
        self._state["last_sent"] = sent
        self._state["last_error"] = error
        if secret_ok is not None:
            self._state["secret_ok"] = secret_ok
        if sent:
            self._state["sent_at"] = iso()
            self._state["total_sent"] = int(self._state.get("total_sent") or 0) + int(sent)
            self._failures = 0

    def _notifier_instance(self):
        if self._notifier is None:
            if self._factory is not None:
                self._notifier = self._factory()
            else:
                self._notifier = notification.Notifier(self.root, self.config)
        return self._notifier

    # -- 状态 ----------------------------------------------------------------
    def status(self) -> dict:
        """给 `/api/state` 用的快照：只有计数与时间，绝不含密钥或正文。"""
        with self._lock:
            out = dict(self._state)
        out["interval_seconds"] = self.interval_seconds
        out["watermark"] = self.watermark
        return out

    def task_event_status(self, task_id: str, event_type: str = "task.completed") -> dict | None:
        """该任务最近一次某类通知的送达情况；没有记录时返回 None。"""
        try:
            notifier = self._notifier_instance()
            found = notifier.outbox.last_payload(task_id, event_type)
            if not found:
                return None
            payload = found["payload"]
            row = notifier.outbox.status(payload["event_id"]) or {}
        except (HandoffError, OSError, ValueError, sqlite3.Error) as exc:
            return {"task_id": task_id, "error": f"{type(exc).__name__}: {exc}"}
        return {
            "task_id": task_id,
            "event_id": payload["event_id"],
            "revision": payload.get("revision"),
            "status": payload.get("status"),
            "target": found.get("url"),
            "created_at": row.get("created_at"),
            "delivered": bool(row.get("delivered_at")),
            "delivered_at": row.get("delivered_at"),
            "attempts": row.get("attempts"),
            "last_error": row.get("last_error"),
            "will_retry": bool(row.get("last_error")) and not row.get("delivered_at"),
        }


def _shift_iso(seconds: float) -> str:
    return iso(now() + timedelta(seconds=seconds))


def _is_after(created_at, watermark: str) -> bool:
    """`created_at` 是否不早于水位；解析失败按「是」处理（宁可多发一次）。"""
    if not created_at:
        return True
    try:
        return parse(str(created_at)) >= parse(watermark)
    except (ValueError, TypeError):
        return True


__all__ = [
    "DEFAULT_BATCH",
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_INTERVAL_SECONDS",
    "MAX_BACKOFF_SECONDS",
    "NotificationPump",
    "SECRET_RETRY_SECONDS",
    "WATERMARK_FILE",
]
