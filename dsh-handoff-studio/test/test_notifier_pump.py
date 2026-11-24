"""完成通知投递（NotificationPump）的回归测试。

## 钉住的实机 bug

`handoff` 的完成通知是两段式的：store 进终态时只把 task.completed 写进本机
outbox；真正签名投递由独立的常驻进程 `handoffctl notifier` 做。原面板由
`run-backend.ps1` 把它一起拉起来；studio 引擎是 DSH 插件 spawn 的单个 Python
进程，没人起那半段——于是面板点「恢复推进 / 验收」，任务变成 verified 了，
前端却一直收不到完成事件（outbox 里 pending 数只增不减）。

「重发完成通知」按钮之所以还能用，是因为 `replay_task_completed` 自己同步
调了一次 `send_due`，这让问题看起来像密钥/网段问题，其实是少了一个常驻循环。

这组测试锁住三件事：
  1. 投递循环真的会把到期的通知发出去（不是只写 outbox）；
  2. 存量历史积压不会被一次性灌给前端（水位）；
  3. 推进/验收结束时会主动 `kick()`，不用等下一个 tick。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

ENGINE_SRC = Path(__file__).resolve().parents[1].parent / "src"
ENGINE_DIR = Path(__file__).resolve().parents[1] / "engine"
for extra in (str(ENGINE_SRC), str(ENGINE_DIR)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from handoff.notification import Inbox, Notifier, task_completed_payload  # noqa: E402
from handoff.timeutil import iso, now  # noqa: E402
from handoff_studio import notifier as pump_mod  # noqa: E402
from handoff_studio.notifier import NotificationPump  # noqa: E402

SECRET = "b2" * 32

#: 合法的任务 id：`ho-<8 位日期>-<26 位 Crockford base32>`。
TASK_ID = "ho-20260101-TESTTASK000000000000000001"
assert len(TASK_ID.split("-")[2]) == 26


class FakeOutbox:
    """只实现 pump 用到的那几个查询；行是普通 dict。"""

    def __init__(self, rows):
        self.rows = list(rows)
        self.delivered = set()

    def pending_count(self) -> int:
        return len([r for r in self.rows if r["event_id"] not in self.delivered])

    def due(self, *, limit=20, event_id=None):
        out = [r for r in self.rows if r["event_id"] not in self.delivered]
        if event_id:
            out = [r for r in out if r["event_id"] == event_id]
        return out[:limit]

    def status(self, event_id):
        row = next((r for r in self.rows if r["event_id"] == event_id), None)
        return dict(row) if row else None


class FakeNotifier:
    """记录「应该发哪些」，不真的打 HTTP。"""

    def __init__(self, rows):
        self.outbox = FakeOutbox(rows)
        self.sent_events = []
        self.fail_with = None

    def send_due(self, *, limit=20, event_id=None):
        if self.fail_with is not None:
            raise self.fail_with
        rows = [r for r in self.outbox.due(limit=limit, event_id=event_id)]
        for row in rows:
            self.outbox.delivered.add(row["event_id"])
            self.sent_events.append(row["event_id"])
        return {
            "sent": len(rows),
            "failed": 0,
            "pending": self.outbox.pending_count(),
            "errors": [],
        }


def row(event_id: str, created_at: str, task_id: str = TASK_ID):
    return {
        "event_id": event_id,
        "created_at": created_at,
        "payload": json.dumps({"event_id": event_id, "task_id": task_id, "type": "task.completed"}),
        "url": "http://127.0.0.1:1/v1/handoff/completed",
    }


def make_pump(tmp_path: Path, rows, **kwargs):
    fake = FakeNotifier(rows)
    pump = NotificationPump(
        tmp_path,
        config=None,
        notifier_factory=lambda: fake,
        watermark_path=tmp_path / "watermark.json",
        logger=lambda _m: None,
        **kwargs,
    )
    return pump, fake


# -- 核心回归 ----------------------------------------------------------------

def test_pump_delivers_new_events(tmp_path):
    """新事件必须真的被发出去 —— 这就是「推进后前端收不到通知」那一条。"""
    fresh = iso(now())
    pump, fake = make_pump(tmp_path, [row("01EVENTFRESH00000000000000A", fresh)], watermark=iso(now()))
    result = pump.flush()
    assert result["sent"] == 1, result
    assert fake.sent_events == ["01EVENTFRESH00000000000000A"]
    assert pump.status()["last_sent"] == 1
    assert pump.status()["total_sent"] == 1


def test_pump_ignores_backlog_before_the_watermark(tmp_path):
    """存量积压（更早的历史 pending）不能被顺手灌给前端。"""
    old = "2026-01-01T00:00:00Z"
    pump, fake = make_pump(
        tmp_path,
        [row("01EVENTOLD0000000000000000A", old), row("01EVENTOLD0000000000000000B", old)],
        watermark=iso(now()),
    )
    result = pump.flush()
    assert result["sent"] == 0
    assert result["stale"] == 2, result
    assert result["idle"] is True
    assert fake.sent_events == []


def test_pump_sends_only_the_fresh_ones_when_both_exist(tmp_path):
    old = "2026-01-01T00:00:00Z"
    fresh = iso(now())
    pump, fake = make_pump(
        tmp_path,
        [row("01EVENTOLD0000000000000000A", old), row("01EVENTFRESH00000000000000A", fresh)],
        watermark=iso(now()),
    )
    result = pump.flush()
    assert result["sent"] == 1
    assert fake.sent_events == ["01EVENTFRESH00000000000000A"]


def test_started_pump_sends_without_being_poked(tmp_path):
    """常驻线程：没人手动 flush 也要自己发（原来缺的就是这个循环）。"""
    pump, fake = make_pump(
        tmp_path,
        [row("01EVENTFRESH00000000000000A", iso(now()))],
        watermark=iso(now()),
        interval_seconds=0.2,
    )
    try:
        pump.start()
        deadline = time.time() + 5
        while time.time() < deadline and not fake.sent_events:
            time.sleep(0.05)
        assert fake.sent_events == ["01EVENTFRESH00000000000000A"]
    finally:
        pump.stop()


def test_kick_wakes_the_thread_immediately(tmp_path):
    """推进/验收结束时 kick 一脚，下一轮不用等满 interval。"""
    fake = FakeNotifier([])
    pump = NotificationPump(
        tmp_path,
        config=None,
        notifier_factory=lambda: fake,
        watermark_path=tmp_path / "watermark.json",
        interval_seconds=30.0,
        logger=lambda _m: None,
    )
    try:
        pump.start()
        time.sleep(0.2)
        fake.outbox.rows.append(row("01EVENTLATE000000000000000A", iso(now())))
        started = time.time()
        pump.kick()
        deadline = time.time() + 5
        while time.time() < deadline and not fake.sent_events:
            time.sleep(0.05)
        assert fake.sent_events == ["01EVENTLATE000000000000000A"]
        # 远小于 30s 的 interval，证明是 kick 叫醒的而不是等到了下一个 tick
        assert time.time() - started < 10
    finally:
        pump.stop()


def test_watermark_survives_a_restart(tmp_path):
    """引擎重启（插件重载/崩溃重拉）不能把刚入队的事件当历史积压跳过。"""
    fresh = iso(now())
    first, fake = make_pump(
        tmp_path, [row("01EVENTFIRST00000000000000A", fresh)], watermark=iso(now())
    )
    assert first.flush()["sent"] == 1
    saved = json.loads((tmp_path / "watermark.json").read_text(encoding="utf-8"))
    assert saved["watermark"]

    # 第二个进程：outbox 里又有一条「上一进程刚入队、还没发」的事件
    later = iso(now())
    second, fake2 = make_pump(tmp_path, [row("01EVENTSECOND0000000000000A", later)])
    assert second.watermark >= saved["watermark"]
    assert second.flush()["sent"] == 1
    assert fake2.sent_events == ["01EVENTSECOND0000000000000A"]


def test_watermark_never_moves_backwards(tmp_path):
    pump, _ = make_pump(tmp_path, [], watermark="2026-06-01T00:00:00Z")
    pump._advance_watermark("2026-01-01T00:00:00Z")
    assert pump.watermark == "2026-06-01T00:00:00Z"


def test_missing_secret_is_reported_and_backed_off(tmp_path):
    """密钥缺失要说清楚，而不是每 tick 重试一次刷日志。"""
    from handoff.errors import ConfigError

    fresh = iso(now())
    pump, fake = make_pump(tmp_path, [row("01EVENTFRESH00000000000000A", fresh)], watermark=iso(now()))
    fake.fail_with = ConfigError("HANDOFF_NOTIFY_SECRET 缺失")
    first = pump.flush()
    assert first["sent"] == 0
    assert "缺失" in first["errors"][0]["error"]
    # 退避窗口内不再尝试（否则会每 2 秒撞一次同一个错）
    fake.fail_with = None
    second = pump.flush()
    assert second.get("skipped") == "secret_unavailable"
    assert pump.status()["secret_ok"] is False


def test_status_never_leaks_the_secret(tmp_path):
    pump, _ = make_pump(tmp_path, [row("01EVENTFRESH00000000000000A", iso(now()))], watermark=iso(now()))
    pump.flush()
    text = json.dumps(pump.status(), ensure_ascii=False)
    assert SECRET not in text
    assert "payload" not in text


# -- 端到端：真 outbox + 真签名投递 ------------------------------------------

class _Sink:
    """最小 HTTP 接收端：拦住 Notifier 真正发出的通知体。"""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        received = self.received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or 0)
                received.append(
                    {
                        "path": self.path,
                        "body": json.loads(self.rfile.read(length).decode("utf-8")),
                        "event_id": self.headers.get("X-Handoff-Event-ID"),
                        "signature": self.headers.get("X-Handoff-Signature"),
                    }
                )
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        import threading

        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def _task(task_id: str):
    return {
        "id": task_id,
        "status": "verified",
        "revision": 7,
        "deployment": {"observed_sha": "c" * 40},
        "source": {"repo": "example/api"},
    }


def test_real_outbox_is_drained_and_signed(tmp_path, monkeypatch):
    """走真 Notifier/Outbox：入队 → pump 发出 → 前端真的收到签名请求。"""
    monkeypatch.setenv("HANDOFF_NOTIFY_SECRET", SECRET)
    from handoff.config import NotificationConfig

    root = tmp_path / "store"
    root.mkdir()
    sink = _Sink()
    try:
        config = NotificationConfig(completion_url=f"{sink.url}/v1/handoff/completed")
        task_id = TASK_ID
        notifier = Notifier(root, config)
        payload = task_completed_payload(task=_task(task_id), store_commit="d" * 40)
        event_id = notifier.enqueue(payload, url=f"{sink.url}/v1/handoff/completed")
        assert notifier.outbox.pending_count() == 1

        pump = NotificationPump(
            root,
            config,
            watermark_path=root / "watermark.json",
            interval_seconds=0.2,
            logger=lambda _m: None,
        )
        pump.start()
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not sink.received:
                time.sleep(0.05)
        finally:
            pump.stop()

        assert len(sink.received) == 1, "通知没有被真正投递出去"
        got = sink.received[0]
        assert got["event_id"] == event_id
        assert got["path"] == "/v1/handoff/completed"
        assert got["signature"].startswith("sha256=")
        assert got["body"]["task_id"] == task_id
        assert got["body"]["status"] == "verified"
        # outbox 里应标成已送达，pending 归零
        assert notifier.outbox.pending_count() == 0
    finally:
        sink.stop()


# -- 服务接线 ----------------------------------------------------------------

def test_service_advance_kicks_the_pump(store_root, monkeypatch):
    """面板点「恢复推进」后，投递线程必须被叫醒（否则前端收不到通知）。"""
    monkeypatch.setenv("HANDOFF_NOTIFY_SECRET", SECRET)
    monkeypatch.setenv("HANDOFF_STUDIO_URL", "http://127.0.0.1:59999/handoff-studio")
    from handoff.config import Config
    import handoff_studio.service as studio

    service = studio.StudioService(store_root, Config.load(store_root))
    try:
        kicks = []
        monkeypatch.setattr(service._notifications, "kick", lambda: kicks.append(1))
        # 直接调监视器，避免真的去跑 cmd_deliver（那会打远端仓库）
        service._watch_advance(TASK_ID)
        service._advance = {
            "task_id": TASK_ID,
            "done": True,
            "result": "verified",
        }
        deadline = time.time() + 5
        while time.time() < deadline and not kicks:
            time.sleep(0.05)
        assert kicks, "恢复推进结束后没有 kick 投递线程"
    finally:
        service.shutdown()


def test_service_exposes_pump_state(store_root, monkeypatch):
    monkeypatch.setenv("HANDOFF_NOTIFY_SECRET", SECRET)
    monkeypatch.setenv("HANDOFF_STUDIO_URL", "http://127.0.0.1:59999/handoff-studio")
    from handoff.config import Config
    import handoff_studio.service as studio

    service = studio.StudioService(store_root, Config.load(store_root))
    try:
        pump = service.state()["notify_pump"]
        for key in ("running", "pending", "stale", "last_flush_at", "watermark"):
            assert key in pump, key
        assert pump["running"] is True
    finally:
        service.shutdown()


def test_service_shutdown_stops_the_pump(store_root, monkeypatch):
    monkeypatch.setenv("HANDOFF_NOTIFY_SECRET", SECRET)
    monkeypatch.setenv("HANDOFF_STUDIO_URL", "http://127.0.0.1:59999/handoff-studio")
    from handoff.config import Config
    import handoff_studio.service as studio

    service = studio.StudioService(store_root, Config.load(store_root))
    pump = service._notifications
    service.shutdown()
    assert pump.status()["running"] is False


# -- store_root fixture（与其它 studio 测试同一套布局） ----------------------

@pytest.fixture()
def store_root(tmp_path: Path) -> Path:
    import shutil

    from test_studio_service import REPO_ROOT

    root = tmp_path / "store"
    root.mkdir()
    shutil.copytree(REPO_ROOT / "config", root / "config")
    if (REPO_ROOT / "schemas").is_dir():
        shutil.copytree(REPO_ROOT / "schemas", root / "schemas")
    for name in ("tasks", "events", "contracts"):
        (root / name).mkdir()
    return root


@pytest.fixture(autouse=True)
def _clean_secret_env(monkeypatch):
    for name in ("HANDOFF_NOTIFY_SECRET", "HANDOFF_NOTIFY_SECRET_FILE"):
        monkeypatch.delenv(name, raising=False)
    yield
