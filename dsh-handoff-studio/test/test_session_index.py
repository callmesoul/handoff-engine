"""会话索引：「继续上次会话」在 dsh 上到底要不要能点得动。

## 这几条测试防的是什么

面板上有一个按钮发 `resume_session_id="last"`，由父类
`PanelService._last_session_for` 解析。它原本只认 codex 的三处痕迹：

    codex.completed 事件里的 run_id  /  日志首行 thread.started  /  result.executor_session_id

dsh 一条都不占，于是点了就报「这个任务还没有可续跑的会话」——哪怕上一轮刚刚
跑完 40 分钟、41 个文件还躺在工作区里。修之前的表现是：任务做了一半被宿主重启
打断，人想接着做，界面却说「没有可续跑的会话」。

所以这里既验证索引本身，也验证 `StudioService` 真的把它接上了——只测前者会
漏掉「索引写得很好，但没人读」这种最可能的回归。

不起 DSH、不起 HTTP：runner 的 `_start/_poll` 用替身，service 用真实 Config
加载一个临时交接仓。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(os.environ.get("HANDOFF_ROOT", "").strip() or ".")
for candidate in Path(__file__).resolve().parents:
    if (candidate / "config" / "repositories.yaml").is_file():
        REPO_ROOT = candidate
        break

ENGINE_SRC = Path(__file__).resolve().parents[1].parent / "src"
ENGINE_DIR = Path(__file__).resolve().parents[1] / "engine"
for extra in (str(ENGINE_SRC), str(ENGINE_DIR)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from handoff_studio import session_index  # noqa: E402
from handoff_studio.runner import DshAgentRunner  # noqa: E402

COMPLETED = json.dumps(
    {
        "status": "completed",
        "summary": "done",
        "changed_paths": ["a.go"],
        "tests": [],
        "migration_required": False,
        "risks": [],
        "notes": "",
        "commit_message": "feat: a",
    }
)


# -- 索引本身 ----------------------------------------------------------------


def test_index_path_accepts_both_the_store_root_and_the_run_dir(tmp_path):
    # 调用方有的传交接仓根、有的已经拿到 .handoff-run；两种都要落到同一个文件，
    # 否则「写进 A、读 A'」就是永远空的索引。
    assert session_index.index_path(tmp_path) == tmp_path / ".handoff-run" / session_index.INDEX_FILENAME
    assert session_index.index_path(tmp_path / ".handoff-run") == session_index.index_path(tmp_path)


def test_start_and_finish_of_one_round_collapse_into_one_record(tmp_path):
    path = session_index.index_path(tmp_path)
    session_index.record(
        path, task_id="ho-1", attempt=1, session_id="session-a", run_id="run-1", cwd="D:/ws"
    )
    session_index.record(
        path,
        task_id="ho-1",
        attempt=1,
        session_id="session-a",
        run_id="run-1",
        cwd="D:/ws",
        status="completed",
    )
    rows = session_index.task_sessions(path, "ho-1")
    assert len(rows) == 1
    # 后写的那条补齐了状态，前面那条的 cwd 不能被覆盖没
    assert rows[0]["status"] == "completed"
    assert rows[0]["cwd"] == "D:/ws"


def test_the_latest_attempt_wins(tmp_path):
    path = session_index.index_path(tmp_path)
    session_index.record(path, task_id="ho-1", attempt=1, session_id="session-old", cwd="D:/ws")
    session_index.record(path, task_id="ho-1", attempt=2, session_id="session-new", cwd="D:/ws")
    found = session_index.last_session(path, "ho-1")
    assert found["session_id"] == "session-new"
    assert found["attempt"] == 2
    # 列表按 attempt 倒序：最新的在最上面
    assert [row["session_id"] for row in session_index.task_sessions(path, "ho-1")] == [
        "session-new",
        "session-old",
    ]


def test_another_task_never_leaks_in(tmp_path):
    path = session_index.index_path(tmp_path)
    session_index.record(path, task_id="ho-1", attempt=1, session_id="session-a")
    session_index.record(path, task_id="ho-2", attempt=1, session_id="session-b")
    assert session_index.last_session(path, "ho-1")["session_id"] == "session-a"
    assert session_index.last_session(path, "ho-3") is None


def test_broken_lines_do_not_take_the_whole_index_down(tmp_path):
    """索引是追加写的：进程被硬杀时最后一行可能是半截 JSON。

    一条坏行不该让「继续上次会话」整体失效——那等于把断一次电变成永久不可续跑。
    """
    path = session_index.index_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    session_index.record(path, task_id="ho-1", attempt=1, session_id="session-a", cwd="D:/ws")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"task_id": "ho-1", "attempt": 2, "sess')
    found = session_index.last_session(path, "ho-1")
    assert found is not None
    assert found["session_id"] == "session-a"


def test_a_record_without_a_session_id_is_ignored(tmp_path):
    path = session_index.index_path(tmp_path)
    # 空 id 写进去没有意义：拿它 resume 只会得到「会话不存在」
    session_index.record(path, task_id="ho-1", attempt=1, session_id="")
    session_index.record(path, task_id="ho-1", attempt=1, session_id="   ")
    assert session_index.last_session(path, "ho-1") is None


def test_writing_to_an_unwritable_place_does_not_raise(tmp_path):
    """索引写不进去不该把执行搞挂：它只是「去哪儿找会话」的提示。"""
    blocked = tmp_path / "file-not-dir"
    blocked.write_text("x", encoding="utf-8")
    session_index.record(blocked / "nested", task_id="ho-1", attempt=1, session_id="session-a")
    assert session_index.last_session(blocked / "nested", "ho-1") is None


# -- runner 真的会写 ---------------------------------------------------------


class FakeBridge:
    def __init__(self, payload: dict):
        self.payload = payload
        self.starts: list[dict] = []

    def start(self, worktree, prompt, *, task_id, attempt, request_id, target=None, reuse_session=True):
        self.starts.append({"reuse_session": reuse_session})
        return {"runId": "run-1", "sessionId": "session-from-bridge"}

    def poll(self, run: str, after: int) -> dict:
        payload = dict(self.payload)
        payload.setdefault("phase", "finished")
        payload.setdefault("logs", [])
        payload.setdefault("cursor", after)
        return payload


def _runner(bridge: FakeBridge, index: Path) -> DshAgentRunner:
    runner = DshAgentRunner(
        bridge_url="http://127.0.0.1:1/handoff-studio",
        token="t",
        timeout_seconds=60,
        model="m1",
        provider="p1",
        session_index=index,
    )
    runner._start = bridge.start  # noqa: SLF001 - 测试替身
    runner._poll = bridge.poll  # noqa: SLF001
    runner._cancel = lambda run: None  # noqa: SLF001
    runner._version = "test"  # noqa: SLF001 - 别去打 HTTP
    return runner


def test_a_run_records_its_session_even_when_it_fails(tmp_path):
    """失败/被打断的那一轮才是最想续跑的那一轮。"""
    bridge = FakeBridge({"phase": "finished", "text": "", "error": "宿主重启了"})
    index = session_index.index_path(tmp_path)
    runner = _runner(bridge, index)

    outcome = runner.run(tmp_path, "do it", task_id="ho-fail", attempt=1)

    assert outcome.status == "failed"
    found = session_index.last_session(index, "ho-fail")
    assert found is not None
    assert found["session_id"] == "session-from-bridge"
    assert found["cwd"] == str(tmp_path)


def test_a_resumed_run_continues_the_same_session_id(tmp_path):
    bridge = FakeBridge({"phase": "finished", "text": COMPLETED})
    index = session_index.index_path(tmp_path)
    runner = _runner(bridge, index)
    runner.resume_session = "session-previous"

    runner.run(tmp_path, "keep going", task_id="ho-1", attempt=2)

    assert bridge.starts[0]["reuse_session"] is True
    found = session_index.last_session(index, "ho-1")
    assert found["session_id"] == "session-from-bridge"


def test_the_log_file_starts_with_the_session_id(tmp_path):
    """日志首行写成 thread.started：父类靠它把失败的 attempt 也列进历史会话。"""
    bridge = FakeBridge({"phase": "finished", "text": COMPLETED, "logs": ["▶ 干完了"], "cursor": 1})
    runner = _runner(bridge, session_index.index_path(tmp_path))
    runner.log_dir = tmp_path / "logs"

    outcome = runner.run(tmp_path, "do it", task_id="ho-log", attempt=1)

    assert outcome.log_path
    first = json.loads(Path(outcome.log_path).read_text(encoding="utf-8").splitlines()[0])
    assert first["type"] == "thread.started"
    assert first["thread_id"] == "session-from-bridge"


# -- service 真的会读 --------------------------------------------------------


@pytest.fixture()
def store_root(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    root.mkdir()
    shutil.copytree(REPO_ROOT / "config", root / "config")
    if (REPO_ROOT / "schemas").is_dir():
        shutil.copytree(REPO_ROOT / "schemas", root / "schemas")
    for name in ("tasks", "events", "contracts"):
        (root / name).mkdir()
    return root


def _service(store_root: Path, monkeypatch):
    monkeypatch.setenv("HANDOFF_STUDIO_URL", "http://127.0.0.1:59999/handoff-studio")
    from handoff.config import Config
    from handoff_studio.service import StudioService

    return StudioService(store_root, Config.load(store_root))


def test_continue_session_finds_the_dsh_session(store_root, monkeypatch):
    """这条就是那个按钮的回归：修之前它一定返回 None。"""
    service = _service(store_root, monkeypatch)
    index = session_index.index_path(store_root)
    session_index.record(
        index, task_id="ho-1", attempt=1, session_id="session-interrupted", cwd=str(store_root)
    )

    found = service._last_session_for("ho-1", "dsh")  # noqa: SLF001 - 要测的就是这条解析

    assert found == ("session-interrupted", 1)


def test_continue_session_without_any_record_is_still_an_error_not_a_crash(store_root, monkeypatch):
    service = _service(store_root, monkeypatch)
    assert service._last_session_for("ho-missing", "dsh") is None  # noqa: SLF001


def test_resuming_last_lands_in_the_previous_directory(store_root, monkeypatch):
    """续跑必须回到上一轮那个目录。

    换个干净目录去 resume，上下文里说「我改过这些文件」，磁盘上却什么都没有，
    agent 会直接跳过重做——这比报错更难发现。
    """
    service = _service(store_root, monkeypatch)
    work = store_root / "backend-wt"
    work.mkdir()
    session_index.record(
        session_index.index_path(store_root),
        task_id="ho-1",
        attempt=1,
        session_id="session-a",
        cwd=str(work),
    )
    captured: dict = {}

    def fake_super(task_id, *, executor="dsh", **kwargs):
        captured.update(kwargs)
        return {"task_id": task_id}

    monkeypatch.setattr(
        "handoff.panel.service.PanelService.run_task",
        lambda self, task_id, **kwargs: fake_super(task_id, **kwargs),
    )

    service.run_task("ho-1", executor="dsh", resume_session_id="last")

    assert captured["resume_session_id"] == "last"
    assert captured["work_dir"] == str(work)


def test_an_explicit_work_dir_still_wins(store_root, monkeypatch):
    """人显式选了目录就是想在那个目录重来，别自作主张改回去。"""
    service = _service(store_root, monkeypatch)
    previous = store_root / "old-wt"
    previous.mkdir()
    chosen = store_root / "chosen-wt"
    chosen.mkdir()
    session_index.record(
        session_index.index_path(store_root),
        task_id="ho-1",
        attempt=1,
        session_id="session-a",
        cwd=str(previous),
    )
    captured: dict = {}

    monkeypatch.setattr(
        "handoff.panel.service.PanelService.run_task",
        lambda self, task_id, **kwargs: captured.update(kwargs) or {"task_id": task_id},
    )

    service.run_task("ho-1", executor="dsh", resume_session_id="last", work_dir=str(chosen))

    assert captured["work_dir"] == str(chosen)


# -- 会话写锁：为什么「续不上」必须降级而不是失败 ------------------------------


def test_only_the_write_lock_error_counts_as_recoverable():
    from handoff_studio.runner import _is_session_write_locked

    assert _is_session_write_locked('session "s" is already owned by an active write handle')
    assert _is_session_write_locked("session/writer-held")
    # 会话真的不存在 / 格式不支持：重开一次也不会好转，不能当成可降级
    assert not _is_session_write_locked('session "s" not found')
    assert not _is_session_write_locked("")
    assert not _is_session_write_locked(None)


class LockedThenFreeBridge:
    """第一次 resume 被写锁挡住，之后开新会话能跑通。"""

    def __init__(self):
        self.starts: list[dict] = []

    def start(self, worktree, prompt, *, task_id, attempt, request_id, target=None, reuse_session=True):
        self.starts.append({"reuse_session": reuse_session})
        return {"runId": f"run-{len(self.starts)}", "sessionId": "session-owned"}

    def poll(self, run: str, after: int) -> dict:
        first = run == "run-1"
        return {
            "phase": "finished",
            "logs": [],
            "cursor": after,
            "text": COMPLETED,
            "error": (
                'session "session-owned" is already owned by an active write handle' if first else None
            ),
        }


def test_a_locked_session_falls_back_to_a_fresh_one(tmp_path):
    """宿主崩溃留下的写锁不该让任务永久只能看着。

    锁由持有者进程退出才释放；DSH 异常退出后，那个会话会一直被占着。此时
    工作区里的改动才是事实来源，「开新会话接着干」比「直接失败」有用得多。
    """
    bridge = LockedThenFreeBridge()
    index = session_index.index_path(tmp_path)
    runner = DshAgentRunner(
        bridge_url="http://127.0.0.1:1/handoff-studio",
        token="t",
        timeout_seconds=60,
        model="m1",
        provider="p1",
        session_index=index,
        resume_session="session-owned",
    )
    runner._start = bridge.start  # noqa: SLF001
    runner._poll = bridge.poll  # noqa: SLF001
    runner._cancel = lambda run: None  # noqa: SLF001
    runner._version = "test"  # noqa: SLF001

    outcome = runner.run(tmp_path, "keep going", task_id="ho-lock", attempt=2)

    assert outcome.status == "completed"
    # 第一次带着 resume 去撞锁，第二次必须不带 resume 重开
    assert [item["reuse_session"] for item in bridge.starts] == [True, False]


def test_a_missing_session_does_not_retry(tmp_path):
    """会话不存在重开也没用：别把一次明确的错误拖成两次。"""

    class MissingBridge:
        def __init__(self):
            self.starts = 0

        def start(self, worktree, prompt, *, task_id, attempt, request_id, target=None, reuse_session=True):
            self.starts += 1
            return {"runId": f"run-{self.starts}", "sessionId": "session-gone"}

        def poll(self, run, after):
            return {"phase": "finished", "logs": [], "cursor": after, "text": "", "error": 'session "session-gone" not found'}

    bridge = MissingBridge()
    runner = DshAgentRunner(
        bridge_url="http://127.0.0.1:1/handoff-studio",
        token="t",
        timeout_seconds=60,
        model="m1",
        provider="p1",
        session_index=session_index.index_path(tmp_path),
        resume_session="session-gone",
    )
    runner._start = bridge.start  # noqa: SLF001
    runner._poll = bridge.poll  # noqa: SLF001
    runner._cancel = lambda run: None  # noqa: SLF001
    runner._version = "test"  # noqa: SLF001

    outcome = runner.run(tmp_path, "keep going", task_id="ho-gone", attempt=2)

    assert outcome.status == "failed"
    assert bridge.starts == 1
