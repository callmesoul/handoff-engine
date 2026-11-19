"""后台索引与查询 API。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from handoff import ids, schema
from handoff.admin import indexer, server
from handoff.config import Config
from handoff.store import TaskStore
from helpers import git, make_bare_remote, make_store_repo, make_task, repo_entry, write_config


def build(tmp_path, *, migrate=False):
    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api", {"src/orders/Order.java": "class Order {}\n"})
    write_config(
        work,
        repos=[repo_entry("org/api", "backend", backend), repo_entry("org/web", "frontend", backend)],
    )
    config = Config.load(work)
    store = TaskStore(work, config)
    task = make_task(ids.task_id(), base_sha=git(backend, "rev-parse", "dev"))
    store.create(task)
    store.claim(task["id"], worker_id="w1", lease_seconds=600)
    store.fail(task["id"], stage="codex", message="模型超时")
    return work, config, store, task


def test_index_matches_repo_state(tmp_path):
    work, config, store, task = build(tmp_path)
    idx = indexer.Indexer(config)
    stats = idx.index_incremental()
    assert stats["tasks"] == 1
    assert stats["quarantined"] == 0

    with idx.connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()
        event_count = conn.execute("SELECT COUNT(*) AS n FROM events WHERE task_id=?", (task["id"],)).fetchone()["n"]
        meta = conn.execute("SELECT value FROM meta WHERE key='last_indexed_commit'").fetchone()["value"]
    assert row["status"] == "failed"
    assert row["failure_stage"] == "codex"
    assert event_count == 3
    assert meta == git(work, "rev-parse", "main")


def test_rebuild_is_idempotent(tmp_path):
    work, config, _store, task = build(tmp_path)
    idx = indexer.Indexer(config)
    first = idx.index_incremental()
    with idx.connect() as conn:
        before = (
            conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"],
            conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"],
        )
    idx.rebuild()
    with idx.connect() as conn:
        after = (
            conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"],
            conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"],
        )
    assert before == after == (1, 3)
    assert first["tasks"] == 1


def test_invalid_task_file_is_quarantined(tmp_path):
    work, config, store, task = build(tmp_path)
    broken = work / "tasks" / "ho-20261105-01HZZZZZZZZZZZZZZZZZZZZZZ.json"
    broken.write_text('{"schema_version": 1, "id": "bad"}', encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-m", "broken task")

    idx = indexer.Indexer(config)
    stats = idx.index_incremental()
    assert stats["quarantined"] >= 1
    # 非法记录不阻断正常任务
    assert stats["tasks"] >= 1


def _request(base, path, *, token, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_api_lists_and_actions(tmp_path):
    work, config, store, task = build(tmp_path)
    indexer.Indexer(config).index_incremental()

    handler = type("Bound", (server.Handler,), {"config": config})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, body = _request(base, "/api/tasks", token="viewer-token")
        assert status == 200
        assert body["items"][0]["id"] == task["id"]
        assert body["items"][0]["status"] == "failed"
        assert body["index"]["last_indexed_commit"]

        status, body = _request(base, f"/api/tasks/{task['id']}/events", token="viewer-token")
        assert status == 200
        assert len(body["events"]) == 3

        status, body = _request(base, "/api/tasks", token="bad-token")
        assert status == 401

        with urllib.request.urlopen(f"{base}/?token=viewer-token", timeout=5) as resp:
            page = resp.read().decode("utf-8")
        assert task["id"] in page
        assert "<script" not in page.lower()

        current = store.get(task["id"])
        status, body = _request(
            base,
            f"/api/tasks/{task['id']}/actions/retry",
            token="operator-token",
            method="POST",
            payload={"expected_revision": current["revision"], "reason": "环境修好了", "idempotency_key": "k1"},
        )
        assert status == 200, body
        assert body["status"] == "open"

        # 幂等键重复提交不会二次执行
        status, body = _request(
            base,
            f"/api/tasks/{task['id']}/actions/retry",
            token="operator-token",
            method="POST",
            payload={"expected_revision": current["revision"], "reason": "重复", "idempotency_key": "k1"},
        )
        assert status == 200
        assert body["replayed"] is True

        # viewer 不能执行操作
        status, _body = _request(
            base,
            f"/api/tasks/{task['id']}/actions/cancel",
            token="viewer-token",
            method="POST",
            payload={"expected_revision": current["revision"], "reason": "x", "idempotency_key": "k2"},
        )
        assert status == 403
    finally:
        httpd.shutdown()

    updated = store.get(task["id"])
    assert updated["status"] == "open"
    assert updated["attempt"] == 2
    assert schema.errors_for("task", updated) == []
