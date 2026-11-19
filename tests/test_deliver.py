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
