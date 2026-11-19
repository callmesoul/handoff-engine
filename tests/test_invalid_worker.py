"""非法任务不能让通知 worker 空转或阻塞后续任务。"""

from types import SimpleNamespace

from handoff.errors import SchemaError
from handoff.worker import Worker, WorkerOptions


def make_worker(tmp_path):
    task = {"id": "invalid", "created_at": "2026-01-01", "revision": 1, "target": {"base_sha": "a" * 40}}
    claims, logs = [], []
    def claim(task_id, **kwargs):
        claims.append(task_id)
        return {"id": task_id, "attempt": 1}
    store = SimpleNamespace(sync=lambda: None, expire_stale=lambda **kw: [],
                            list_tasks=lambda **kw: [task], claim=claim)
    worker = Worker(tmp_path, SimpleNamespace(), WorkerOptions(worker_id="test"),
                    store=store, codex=object(), logger=logs.append)
    if hasattr(worker.options, "auto_claim"):
        worker.options.auto_claim = True
    worker._is_for_me = lambda task: True
    worker._validation_errors = lambda t: ["target/base_sha: 缺少必填字段"] if t["id"] == "invalid" else []
    worker._fail = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("非法任务不应尝试写失败状态"))
    worker._process = lambda t: None
    return worker, task, claims, logs


def test_invalid_task_waits_and_reports_details_once(tmp_path):
    worker, task, claims, logs = make_worker(tmp_path)
    assert worker.run_once() is None
    assert worker.run_once() is None
    assert claims == []
    assert len(logs) == 1
    assert "target/base_sha" in logs[0]
    task["revision"] += 1
    assert worker.run_once() is None
    assert len(logs) == 2


def test_invalid_task_does_not_block_valid_task(tmp_path):
    worker, task, claims, logs = make_worker(tmp_path)
    worker.store.list_tasks = lambda **kw: [task, {"id": "valid", "created_at": "2026-01-02", "target": {"base_sha": "a" * 40}}]
    assert worker.run_once() == "valid"
    assert claims == ["valid"]
    # 修正后的同一任务可被再次领取，不会被永久隔离。
    worker._validation_errors = lambda t: []
    assert worker.run_once() == "invalid"
    assert claims == ["valid", "invalid"]


def test_failure_write_logs_field_errors(tmp_path):
    worker, task, claims, logs = make_worker(tmp_path)
    worker.store.get = lambda task_id: {"status": "open", "revision": 1}
    def fail(*a, **kw):
        raise SchemaError("任务单校验失败", errors=["updated_at: 不能早于 created_at"])
    worker.store.fail = fail
    Worker._fail(worker, "invalid", "validation", "校验失败")
    assert "updated_at" in logs[0]


def test_malformed_task_reports_schema_before_semantics(tmp_path):
    worker, task, claims, logs = make_worker(tmp_path)
    errors = Worker._validation_errors(worker, {"id": "invalid"})
    assert errors
    assert any("required" in error for error in errors)
