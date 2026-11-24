"""deliver 全链路：合并 → 部署对齐 → 结构化验收 → verified。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from handoff import ids, schema
from handoff.cli import main as cli_main
from handoff.config import Config
from handoff.deploy import CommitAncestry
from handoff.store import Actor, TaskStore
from handoff.worker import Worker, WorkerOptions
from helpers import (
    FakeCodex,
    FakeHost,
    git,
    make_bare_remote,
    make_store_repo,
    make_task,
    repo_entry,
    write_config,
)

MERGE_SHA = "b" * 40
DEPLOYED_SHA = "c" * 40


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/version":
            body = json.dumps({"sha": DEPLOYED_SHA, "build_id": "42", "environment": "dev"}).encode()
        elif self.path.startswith("/api/orders"):
            body = json.dumps({"items": [{"id": 1, "status": "OPEN"}]}).encode()
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_deliver_end_to_end_accepts_newer_stable_dev_version(tmp_path, monkeypatch):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    env_url = f"http://127.0.0.1:{httpd.server_address[1]}"

    try:
        work, _bare = make_store_repo(tmp_path)
        backend = make_bare_remote(tmp_path, "api", {"src/orders/Order.java": "class Order {}\n"})
        write_config(
            work,
            repos=[repo_entry("org/api", "backend", backend), repo_entry("org/web", "frontend", backend)],
            environments=[
                {
                    "name": "dev",
                    "url": env_url,
                    "version_path": "/version",
                    "shared": False,
                    "concurrency_group": "deploy-dev",
                }
            ],
        )
        config = Config.load(work)
        store = TaskStore(work, config, actor=Actor("worker", "w1"))
        checks = [
            {
                "id": "orders",
                "type": "http",
                "method": "GET",
                "url": "{base_url}/api/orders?page=1",
                "expect_status": 200,
                "expect_jsonpath": [{"path": "$.items[0].status", "equals": "OPEN"}],
            }
        ]
        task = make_task(
            ids.task_id(),
            base_sha=git(backend, "rev-parse", "dev"),
            checks=checks,
        )
        store.create(task)

        worker = Worker(
            work,
            config,
            WorkerOptions(worker_id="w1", lease_seconds=900, auto_merge=True, heartbeat_interval_seconds=9999),
            store=store,
            codex=FakeCodex({"src/orders/Status.java": "enum Status { OPEN }\n"}),
            host=FakeHost(merged=True, remotes={"org/api": backend}),
            logger=lambda _m: None,
        )
        worker.run_once()
        merged = store.get(task["id"])
        assert merged["status"] == "merged"
        assert merged["deployment"]["target_sha"] == MERGE_SHA

        monkeypatch.setattr(
            CommitAncestry,
            "contains",
            lambda _self, target, observed: (target == MERGE_SHA and observed == DEPLOYED_SHA, None),
        )

        code = cli_main(["--root", str(work), "deliver", task["id"]])
        assert code == 0

        final = store.get(task["id"])
        assert final["status"] == "verified"
        assert final["deployment"]["target_sha"] == MERGE_SHA
        assert final["deployment"]["observed_sha"] == DEPLOYED_SHA
        assert final["result"]["checks"][0]["id"] == "orders"
        assert final["result"]["checks"][0]["status"] == "passed"
        assert schema.errors_for("task", final) == []

        events = [json.loads(p.read_text(encoding="utf-8")) for p in (work / "events" / task["id"]).glob("*.json")]
        types = {e["type"] for e in events}
        assert "verification.completed" in types
    finally:
        httpd.shutdown()


def test_record_result_unblocks_a_manually_committed_direct_delivery(tmp_path):
    """目录模式人工提交后，record-result 把 head_sha 写回去，deliver 才能对部署。

    回归：目录模式默认不提交，人提交完却没有受支持的入口回填 sha，任务卡在
    delivered，「恢复推进」只会回一句「任务缺少 merge_sha/head_sha」。
    """
    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api2", {"src/orders/Order.java": "class Order {}\n"})
    write_config(
        work,
        repos=[repo_entry("org/api", "backend", backend), repo_entry("org/web", "frontend", backend)],
    )
    config = Config.load(work)
    store = TaskStore(work, config, actor=Actor("worker", "w1"))
    task = make_task(ids.task_id(), base_sha=git(backend, "rev-parse", "dev"), checks=[])
    store.create(task)

    # 目录模式的典型产物：改动在用户工作副本里，任务单没有任何 sha
    head = "a" * 40
    store.update(
        task["id"],
        expected_revision=task["revision"],
        mutate=lambda t: (
            t.__setitem__("status", "delivered"),
            t["result"].update({"worktree": str(tmp_path), "notes": "直接目录模式"}),
        ),
        event_type="task.status_changed",
        summary="直接目录模式交付",
    )
    before = store.get(task["id"])
    assert before["status"] == "delivered"
    assert not before["result"]["head_sha"]

    code = cli_main(
        [
            "--root",
            str(work),
            "record-result",
            task["id"],
            "--head-sha",
            head,
            "--branch",
            "develop",
        ]
    )
    assert code == 0

    after = store.get(task["id"])
    assert after["result"]["head_sha"] == head
    assert after["result"]["branch"] == "develop"
    # 只回填交付事实，不替人推进状态
    assert after["status"] == "delivered"

    events = [json.loads(p.read_text(encoding="utf-8")) for p in (work / "events" / task["id"]).glob("*.json")]
    assert any(e["type"] == "audit.correction_recorded" for e in events)


def test_record_result_rejects_a_malformed_sha(tmp_path):
    """写坏 sha 会让部署永远对不上，宁可在入口就拒绝。"""
    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api3", {"src/orders/Order.java": "class Order {}\n"})
    write_config(
        work,
        repos=[repo_entry("org/api", "backend", backend), repo_entry("org/web", "frontend", backend)],
    )
    config = Config.load(work)
    store = TaskStore(work, config, actor=Actor("worker", "w1"))
    task = make_task(ids.task_id(), base_sha=git(backend, "rev-parse", "dev"), checks=[])
    store.create(task)

    # cli_main 把错误转成返回码（schema_error = 3），不往上抛
    for bad in ("abc", "z" * 40):
        code = cli_main(["--root", str(work), "record-result", task["id"], "--head-sha", bad])
        assert code == 3, f"{bad!r} 应当以 schema_error 拒绝，实际返回 {code}"
    # 拒绝之后任务单不能被写脏
    assert not store.get(task["id"])["result"]["head_sha"]

