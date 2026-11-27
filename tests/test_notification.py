"""局域网主动通知：签名、outbox 重试、inbox 幂等与监听边界。"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from handoff import notification
from handoff.config import NotificationConfig, SecretsConfig
from handoff.errors import ConfigError, ExecutionError, SchemaError


SECRET = "test-secret-that-is-at-least-32-bytes-long"


def config(tmp_path, **overrides):
    base = NotificationConfig(
        secret_env="HANDOFF_TEST_NOTIFY_SECRET",
        outbox_database=str(tmp_path / "outbox.sqlite3"),
        inbox_database=str(tmp_path / "inbox.sqlite3"),
        listen_host="127.0.0.1",
        listen_port=0,
        request_timeout_seconds=2,
        retry_initial_seconds=0.01,
        retry_max_seconds=0.1,
    )
    return replace(base, **overrides)


def _bind_secret_refs(cfg, root, refs, bindings=None):
    object.__setattr__(
        cfg,
        "_app_config",
        SimpleNamespace(
            root=root,
            secrets=SecretsConfig(
                refs={k: str(v) for k, v in refs.items()},
                bindings=dict(bindings or {}),
            ),
            checks=SimpleNamespace(repo_scripts={}, test_suites={}),
        ),
    )
    return cfg


def payload():
    return notification.task_created_payload(
        task_id="ho-20261105-01M6N7G5YSV1FK8AQ7BSX19Y0S",
        revision=1,
        store_commit="a" * 40,
    )


def test_signed_notification_round_trip_and_duplicate(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_TEST_NOTIFY_SECRET", SECRET)
    wake = threading.Event()
    receiver_config = config(tmp_path)
    server = notification.WakeServer(tmp_path, receiver_config, wake)
    host, port = server.start()
    try:
        sender_config = replace(receiver_config, url=f"http://{host}:{port}{notification.WAKE_PATH}")
        notifier = notification.Notifier(tmp_path, sender_config)
        event = payload()
        event_id = notifier.enqueue(event)

        result = notifier.send_due(event_id=event_id)
        assert result == {"sent": 1, "failed": 0, "pending": 0, "errors": []}
        assert wake.wait(1)
        assert server.inbox.count() == 1
        assert notifier.outbox.status(event_id)["delivered_at"] is not None

        # 至少一次投递允许重复；接收端按 event_id 幂等，不会生成第二条 inbox。
        row = notifier.outbox.status(event_id)
        notifier._send(row, SECRET.encode("utf-8"))
        assert server.inbox.count() == 1
    finally:
        server.stop()


def test_secret_preflight_checks_both_sides_and_same_value(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_TEST_NOTIFY_SECRET", SECRET)
    monkeypatch.delenv("SAAS_TEST_SESSION_COOKIE", raising=False)
    frontend_root = tmp_path / "frontend"
    backend_root = tmp_path / "backend"
    frontend_root.mkdir()
    backend_root.mkdir()
    frontend_secret = frontend_root / "session.cookie"
    backend_secret = backend_root / "session.cookie"
    frontend_secret.write_text("shared-cookie\n", encoding="utf-8")
    backend_secret.write_text("shared-cookie\n", encoding="utf-8")

    wake = threading.Event()
    receiver = _bind_secret_refs(
        config(backend_root), backend_root, {"saas-session": backend_secret},
        {"SAAS_TEST_SESSION_COOKIE": "saas-session"},
    )
    server = notification.WakeServer(backend_root, receiver, wake)
    host, port = server.start()
    try:
        sender = replace(
            config(frontend_root),
            url=f"http://{host}:{port}{notification.WAKE_PATH}",
        )
        _bind_secret_refs(sender, frontend_root, {"saas-session": frontend_secret}, {"SAAS_TEST_SESSION_COOKIE": "saas-session"})
        requirement = [{
            "ref": "saas-session", "env": "SAAS_TEST_SESSION_COOKIE", "same_value": True
        }]
        result = notification.preflight_secrets(frontend_root, sender, requirement)

        assert result["status"] == "passed"
        assert result["checked"] == requirement
        assert isinstance(result["request_id"], str)
        assert not wake.is_set()
        assert server.inbox.count() == 0
    finally:
        server.stop()


def test_secret_preflight_rejects_secret_missing_on_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_TEST_NOTIFY_SECRET", SECRET)
    monkeypatch.delenv("SAAS_TEST_SESSION_COOKIE", raising=False)
    frontend_root = tmp_path / "frontend"
    backend_root = tmp_path / "backend"
    frontend_root.mkdir()
    backend_root.mkdir()
    frontend_secret = frontend_root / "session.cookie"
    frontend_secret.write_text("frontend-only", encoding="utf-8")

    receiver = _bind_secret_refs(
        config(backend_root), backend_root, {},
        {"SAAS_TEST_SESSION_COOKIE": "ref"},
    )
    server = notification.WakeServer(backend_root, receiver, threading.Event())
    host, port = server.start()
    try:
        sender = replace(config(frontend_root), url=f"http://{host}:{port}{notification.WAKE_PATH}")
        _bind_secret_refs(sender, frontend_root, {"ref": frontend_secret}, {"SAAS_TEST_SESSION_COOKIE": "ref"})
        with pytest.raises(ExecutionError, match="后端缺少"):
            notification.preflight_secrets(frontend_root, sender, [
                {"ref": "ref", "env": "SAAS_TEST_SESSION_COOKIE", "same_value": True}
            ])
    finally:
        server.stop()


def test_secret_preflight_rejects_remote_mismatch_without_leaking_values(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_TEST_NOTIFY_SECRET", SECRET)
    monkeypatch.delenv("SAAS_TEST_SESSION_COOKIE", raising=False)
    frontend_root = tmp_path / "frontend"
    backend_root = tmp_path / "backend"
    frontend_root.mkdir()
    backend_root.mkdir()
    frontend_secret = frontend_root / "session.cookie"
    backend_secret = backend_root / "session.cookie"
    frontend_secret.write_text("frontend-canary", encoding="utf-8")
    backend_secret.write_text("backend-canary", encoding="utf-8")

    receiver = _bind_secret_refs(
        config(backend_root), backend_root, {"ref": backend_secret},
        {"SAAS_TEST_SESSION_COOKIE": "ref"},
    )
    server = notification.WakeServer(backend_root, receiver, threading.Event())
    host, port = server.start()
    try:
        sender = replace(config(frontend_root), url=f"http://{host}:{port}{notification.WAKE_PATH}")
        _bind_secret_refs(sender, frontend_root, {"ref": frontend_secret}, {"SAAS_TEST_SESSION_COOKIE": "ref"})
        with pytest.raises(ExecutionError) as excinfo:
            notification.preflight_secrets(frontend_root, sender, [
                {"ref": "ref", "env": "SAAS_TEST_SESSION_COOKIE", "same_value": True}
            ])
        message = str(excinfo.value)
        assert "值不一致" in message
        assert "frontend-canary" not in message
        assert "backend-canary" not in message
    finally:
        server.stop()


def _bind_provision_backend(cfg, root, *, enabled=True, refs=(), provision_dir=None):
    """给后端 listener 的应用配置打上 provision_enabled / provision_refs。"""
    app_cfg = getattr(cfg, "_app_config", None)
    if app_cfg is None:
        app_cfg = SimpleNamespace(root=root, secrets=None, checks=SimpleNamespace(repo_scripts={}, test_suites={}))
        object.__setattr__(cfg, "_app_config", app_cfg)
    from handoff.config import SecretsConfig

    sc = SecretsConfig(
        provision_enabled=enabled,
        provision_refs=tuple(refs),
        provision_dir=provision_dir or ".handoff-run/secrets",
    )
    app_cfg.secrets = sc
    return sc


def test_secret_provision_writes_backend_file_then_preflight_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_TEST_NOTIFY_SECRET", SECRET)
    monkeypatch.delenv("SAAS_TEST_SESSION_COOKIE", raising=False)
    frontend_root = tmp_path / "frontend"
    backend_root = tmp_path / "backend"
    frontend_root.mkdir()
    backend_root.mkdir()
    frontend_secret = frontend_root / "session.cookie"
    frontend_secret.write_text("shared-cookie\n", encoding="utf-8")

    wake = threading.Event()
    receiver = config(backend_root)
    _bind_provision_backend(receiver, backend_root, refs=("saas-session",))
    server = notification.WakeServer(backend_root, receiver, wake)
    host, port = server.start()
    try:
        sender = replace(config(frontend_root), url=f"http://{host}:{port}{notification.WAKE_PATH}")
        _bind_secret_refs(sender, frontend_root, {"saas-session": frontend_secret}, {"SAAS_TEST_SESSION_COOKIE": "saas-session"})
        requirement = [{
            "ref": "saas-session", "env": "SAAS_TEST_SESSION_COOKIE", "same_value": True
        }]

        # 后端此刻没有该 secret：provision 把它经签名通道加密推送过去。
        result = notification.provision_secrets(frontend_root, sender, requirement)
        assert result["status"] == "written"
        assert result["written"] == ["saas-session"]
        assert not wake.is_set()
        assert server.inbox.count() == 0

        written = backend_root / ".handoff-run" / "secrets" / "saas-session"
        assert written.is_file()
        # resolver 读取时会去掉末尾换行，写入的是规范化后的值。
        assert written.read_text(encoding="utf-8") == "shared-cookie"
        assert (backend_root / ".handoff-run" / "secrets" / "refs.json").is_file()

        # 后端 resolver 已即时看到新 ref/binding，随后的 preflight 直接通过。
        pre = notification.preflight_secrets(frontend_root, sender, requirement)
        assert pre["status"] == "passed"
    finally:
        server.stop()


def test_secret_provision_rejected_when_backend_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_TEST_NOTIFY_SECRET", SECRET)
    monkeypatch.delenv("SAAS_TEST_SESSION_COOKIE", raising=False)
    frontend_root = tmp_path / "frontend"
    backend_root = tmp_path / "backend"
    frontend_root.mkdir()
    backend_root.mkdir()
    frontend_secret = frontend_root / "session.cookie"
    frontend_secret.write_text("shared-cookie\n", encoding="utf-8")

    receiver = config(backend_root)
    _bind_provision_backend(receiver, backend_root, enabled=False)
    server = notification.WakeServer(backend_root, receiver, threading.Event())
    host, port = server.start()
    try:
        sender = replace(config(frontend_root), url=f"http://{host}:{port}{notification.WAKE_PATH}")
        _bind_secret_refs(sender, frontend_root, {"saas-session": frontend_secret}, {"SAAS_TEST_SESSION_COOKIE": "saas-session"})
        with pytest.raises(ExecutionError, match="未开启密钥自动写入"):
            notification.provision_secrets(frontend_root, sender, [{
                "ref": "saas-session", "env": "SAAS_TEST_SESSION_COOKIE", "same_value": True
            }])
    finally:
        server.stop()


def test_secret_provision_rejects_unauthorized_ref(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_TEST_NOTIFY_SECRET", SECRET)
    monkeypatch.delenv("SAAS_TEST_SESSION_COOKIE", raising=False)
    frontend_root = tmp_path / "frontend"
    backend_root = tmp_path / "backend"
    frontend_root.mkdir()
    backend_root.mkdir()
    frontend_secret = frontend_root / "session.cookie"
    frontend_secret.write_text("shared-cookie\n", encoding="utf-8")

    receiver = config(backend_root)
    _bind_provision_backend(receiver, backend_root, refs=("allowed-ref",))
    server = notification.WakeServer(backend_root, receiver, threading.Event())
    host, port = server.start()
    try:
        sender = replace(config(frontend_root), url=f"http://{host}:{port}{notification.WAKE_PATH}")
        _bind_secret_refs(sender, frontend_root, {"saas-session": frontend_secret}, {"SAAS_TEST_SESSION_COOKIE": "saas-session"})
        with pytest.raises(ExecutionError, match="不在后端"):
            notification.provision_secrets(frontend_root, sender, [{
                "ref": "saas-session", "env": "SAAS_TEST_SESSION_COOKIE", "same_value": True
            }])
    finally:
        server.stop()


def test_secret_preflight_without_requirements_never_connects(tmp_path):
    cfg = config(tmp_path, url="http://127.0.0.1:1/v1/handoff/wake")
    assert notification.preflight_secrets(tmp_path, cfg, []) == {
        "status": "not_required", "checked": []
    }


def test_notification_ignores_env_proxy(tmp_path, monkeypatch):
    """局域网完成通知必须直连，不能被 shell 注入的 HTTP_PROXY 劫持成超时。"""
    monkeypatch.setenv("HANDOFF_TEST_NOTIFY_SECRET", SECRET)
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    server = notification.WakeServer(tmp_path, config(tmp_path), threading.Event())
    host, port = server.start()
    try:
        sender = replace(config(tmp_path), url=f"http://{host}:{port}{notification.WAKE_PATH}")
        notifier = notification.Notifier(tmp_path, sender)
        event_id = notifier.enqueue(payload())
        result = notifier.send_due(event_id=event_id)
        assert result == {"sent": 1, "failed": 0, "pending": 0, "errors": []}
    finally:
        server.stop()


def test_bad_signature_stays_pending_for_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_RECEIVER_SECRET", SECRET)
    receiver = config(tmp_path, secret_env="HANDOFF_RECEIVER_SECRET")
    server = notification.WakeServer(tmp_path, receiver, threading.Event())
    host, port = server.start()
    try:
        monkeypatch.setenv("HANDOFF_SENDER_SECRET", "different-secret-that-is-also-32-bytes")
        sender = replace(
            receiver,
            secret_env="HANDOFF_SENDER_SECRET",
            url=f"http://{host}:{port}{notification.WAKE_PATH}",
        )
        notifier = notification.Notifier(tmp_path, sender)
        event_id = notifier.enqueue(payload())
        result = notifier.send_due(event_id=event_id)
        assert result["sent"] == 0
        assert result["failed"] == 1
        assert result["pending"] == 1
        assert "签名无效" in result["errors"][0]["error"]
        assert server.inbox.count() == 0
    finally:
        server.stop()


def test_lan_bind_requires_source_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_TEST_NOTIFY_SECRET", SECRET)
    server = notification.WakeServer(tmp_path, config(tmp_path), threading.Event())
    with pytest.raises(ConfigError) as excinfo:
        server.start("0.0.0.0", 0)
    assert "allowed_cidrs" in str(excinfo.value)


def test_missing_secret_is_not_silently_accepted(tmp_path, monkeypatch):
    monkeypatch.delenv("HANDOFF_TEST_NOTIFY_SECRET", raising=False)
    monkeypatch.delenv("HANDOFF_TEST_NOTIFY_SECRET_FILE", raising=False)
    with pytest.raises(ConfigError):
        notification.WakeServer(tmp_path, config(tmp_path), threading.Event())


def test_secret_can_be_loaded_from_systemd_credential_file(tmp_path, monkeypatch):
    secret_file = tmp_path / "handoff-notify-secret"
    secret_file.write_text(f"{SECRET}\n", encoding="utf-8")
    monkeypatch.delenv("HANDOFF_TEST_NOTIFY_SECRET", raising=False)
    monkeypatch.setenv("HANDOFF_TEST_NOTIFY_SECRET_FILE", str(secret_file))

    assert notification.load_secret(config(tmp_path)) == SECRET.encode("utf-8")


def test_terminal_event_is_only_enqueued_for_background_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_TEST_NOTIFY_SECRET", SECRET)
    notify_config = config(
        tmp_path,
        completion_url=f"http://127.0.0.1:9{notification.COMPLETE_PATH}",
    )
    task = {
        "id": "ho-20261105-01M6N7G5YSV1FK8AQ7BSX19Y0S",
        "revision": 2,
        "status": "verified",
        "deployment": {"observed_sha": "b" * 40},
        "source": {"repo": "org/web"},
    }

    result = notification.emit_task_completed(tmp_path, notify_config, task, "a" * 40)

    assert result["status"] == "pending"
    assert result["delivery"] == "background_notifier"
    row = notification.Outbox(notify_config.outbox_path(tmp_path)).status(result["event_id"])
    assert row["attempts"] == 0
    assert row["delivered_at"] is None
    assert row["last_error"] is None


TASK_ID = "ho-20261105-01M6N7G5YSV1FK8AQ7BSX19Y0S"


def completed_payload(task_id=TASK_ID, *, store_commit="c" * 40):
    return {
        "schema_version": 1,
        "event_id": notification.ids.ulid(),
        "type": "task.completed",
        "task_id": task_id,
        "revision": 2,
        "store_commit": store_commit,
        "occurred_at": "2026-11-16T10:00:00+08:00",
        "status": "verified",
        "observed_sha": "b" * 40,
        "source_repo": "org/web",
    }


def terminal_task(**overrides):
    task = {
        "id": TASK_ID,
        "status": "verified",
        "revision": 2,
        "_commit": "c" * 40,
        "deployment": {"observed_sha": "b" * 40},
        "source": {"repo": "org/web"},
    }
    task.update(overrides)
    return task


def test_replay_reuses_original_payload_but_gets_new_event_id(tmp_path, monkeypatch):
    """手动重发必须换 event_id，否则前端 inbox 会判重吞掉。"""
    monkeypatch.setenv("HANDOFF_TEST_NOTIFY_SECRET", SECRET)
    seen: list[dict] = []
    receiver_cfg = config(tmp_path)
    server = notification.WakeServer(tmp_path, receiver_cfg, on_event=seen.append)
    host, port = server.start()
    try:
        target = f"http://{host}:{port}{notification.COMPLETE_PATH}"
        sender_cfg = replace(receiver_cfg, completion_url=target)
        notifier = notification.Notifier(tmp_path, sender_cfg)
        original = completed_payload()
        notifier.outbox.enqueue(original, target)
        notifier.outbox.delivered(original["event_id"])

        result = notification.replay_task_completed(tmp_path, sender_cfg, terminal_task())

        assert result["status"] == "sent"
        assert result["delivered"] is True
        assert result["target"] == target
        assert result["payload_source"] == "outbox"
        assert result["event_id"] != original["event_id"]
        assert result["revision"] == 2
        assert result["store_commit"] == "c" * 40
        assert len(seen) == 1
        assert seen[0]["event_id"] == result["event_id"]
        assert seen[0]["status"] == "verified"
        assert server.inbox.count() == 1
        assert notifier.outbox.status(result["event_id"])["delivered_at"] is not None
    finally:
        server.stop()


def test_replay_finds_frontend_target_from_inbox_reply_url(tmp_path, monkeypatch):
    """后端没配 completion_url 时，从前端 hello 事件的 reply_url 反查 192.168.1.3。"""
    monkeypatch.setenv("HANDOFF_TEST_NOTIFY_SECRET", SECRET)
    seen: list[dict] = []
    receiver_cfg = config(tmp_path)
    server = notification.WakeServer(tmp_path, receiver_cfg, on_event=seen.append)
    host, port = server.start()
    try:
        target = f"http://{host}:{port}{notification.COMPLETE_PATH}"
        created = notification.task_created_payload(
            task_id=TASK_ID, revision=1, store_commit="a" * 40, reply_url=target
        )
        notification.Inbox(receiver_cfg.inbox_path(tmp_path)).record(
            created,
            body=notification._json_bytes(created),
            client_ip="192.168.1.3",
        )

        # 没有任何 outbox 历史，只能从 task 现场构造，目标来自 inbox
        result = notification.replay_task_completed(tmp_path, receiver_cfg, terminal_task())

        assert result["status"] == "sent"
        assert result["payload_source"] == "task"
        assert result["target"] == target
        assert len(seen) == 1
        assert seen[0]["revision"] == 2
    finally:
        server.stop()


def test_replay_dry_run_does_not_deliver(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_TEST_NOTIFY_SECRET", SECRET)
    target = f"http://127.0.0.1:9{notification.COMPLETE_PATH}"
    cfg = config(tmp_path, completion_url=target)
    notifier = notification.Notifier(tmp_path, cfg)
    original = completed_payload()
    notifier.outbox.enqueue(original, target)

    result = notification.replay_task_completed(tmp_path, cfg, terminal_task(), dry_run=True)

    assert result["status"] == "dry_run"
    assert result["delivered"] is False
    assert result["target"] == target
    assert notifier.outbox.status(result["event_id"]) is None


def test_replay_rejects_non_terminal_task_and_missing_target(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_TEST_NOTIFY_SECRET", SECRET)
    cfg = config(tmp_path)
    with pytest.raises(SchemaError, match="verified / failed"):
        notification.replay_task_completed(tmp_path, cfg, terminal_task(status="verifying"))

    with pytest.raises(ConfigError, match="前端回程地址"):
        notification.replay_task_completed(tmp_path, cfg, terminal_task())


def test_outbox_can_delete_records_for_selected_tasks(tmp_path):
    outbox = notification.Outbox(tmp_path / "outbox.sqlite3")
    first = payload()
    second = payload()
    second["event_id"] = notification.ids.ulid()
    second["task_id"] = "ho-20261106-01M6N7G5YSV1FK8AQ7BSX19Y0S"
    outbox.enqueue(first, "http://127.0.0.1:8788/v1/handoff/wake")
    outbox.enqueue(second, "http://127.0.0.1:8788/v1/handoff/wake")

    assert outbox.delete_tasks([first["task_id"]]) == 1
    assert outbox.status(first["event_id"]) is None
    assert outbox.status(second["event_id"]) is not None


def test_replay_uses_given_store_commit_when_task_lacks_one(tmp_path, monkeypatch):
    """`_commit` 不落任务单：从磁盘读出的任务必须靠调用方给的落盘 commit 才能重发。"""
    monkeypatch.setenv("HANDOFF_TEST_NOTIFY_SECRET", SECRET)
    seen: list[dict] = []
    cfg = config(tmp_path)
    server = notification.WakeServer(tmp_path, cfg, on_event=seen.append)
    host, port = server.start()
    try:
        target = f"http://{host}:{port}{notification.COMPLETE_PATH}"
        created = notification.task_created_payload(
            task_id=TASK_ID, revision=1, store_commit="a" * 40, reply_url=target
        )
        notification.Inbox(cfg.inbox_path(tmp_path)).record(
            created, body=notification._json_bytes(created), client_ip="192.168.1.3"
        )

        task = terminal_task()
        task.pop("_commit")

        result = notification.replay_task_completed(
            tmp_path, cfg, task, store_commit="d" * 40
        )

        assert result["status"] == "sent"
        assert result["payload_source"] == "task"
        assert result["store_commit"] == "d" * 40
        assert seen[0]["store_commit"] == "d" * 40
    finally:
        server.stop()


def test_replay_without_history_or_commit_is_explicit_error(tmp_path):
    cfg = config(tmp_path, completion_url=f"http://127.0.0.1:9{notification.COMPLETE_PATH}")
    task = terminal_task()
    task.pop("_commit")

    with pytest.raises(SchemaError, match="落盘 commit"):
        notification.replay_task_completed(tmp_path, cfg, task)


def test_reconcile_replays_terminal_tasks_missing_completion(tmp_path, monkeypatch):
    """对账给「终态但从无完成通知历史」的任务补发一次，且能投递成功。"""
    monkeypatch.setenv("HANDOFF_TEST_NOTIFY_SECRET", SECRET)
    seen: list[dict] = []
    cfg = config(tmp_path)
    server = notification.WakeServer(tmp_path, cfg, on_event=seen.append)
    host, port = server.start()
    try:
        target = f"http://{host}:{port}{notification.COMPLETE_PATH}"
        cfg = replace(cfg, completion_url=target)
        missing_task = terminal_task(id="ho-20261105-01M6N7G5YSV1FK8AQ7BSX19Y0S")
        missing_task.pop("_commit")  # 只能靠 commit_for 提供落盘 commit

        result = notification.reconcile_completed(
            tmp_path, cfg, [missing_task], lambda _id: "e" * 40
        )

        assert result["scanned"] == 1
        assert result["missing"] == [missing_task["id"]]
        assert result["failures"] == []
        assert len(result["replayed"]) == 1
        assert result["replayed"][0]["status"] == "sent"
        assert len(seen) == 1
        assert seen[0]["task_id"] == missing_task["id"]
        assert seen[0]["store_commit"] == "e" * 40
    finally:
        server.stop()


def test_reconcile_skips_existing_history_and_nonterminal(tmp_path, monkeypatch):
    """已有通知历史（哪怕 pending）与非终态任务都不应被重复补发。"""
    monkeypatch.setenv("HANDOFF_TEST_NOTIFY_SECRET", SECRET)
    cfg = config(tmp_path, completion_url=f"http://127.0.0.1:9{notification.COMPLETE_PATH}")

    # 这个任务已入队过完成通知（未投递、仍在 pending）——对账必须跳过。
    with_history = terminal_task()
    notifier = notification.Notifier(tmp_path, cfg)
    notifier.enqueue(completed_payload(), url=cfg.completion_url)
    # 非终态任务也应跳过。
    nonterminal = terminal_task(id="ho-20261105-01M6N7G5YSV1FK8AQ7BSX19Y1Z", status="verifying")

    result = notification.reconcile_completed(
        tmp_path, cfg, [with_history, nonterminal], lambda _id: "f" * 40
    )

    assert result["scanned"] == 1  # 只有 with_history 是终态
    assert result["skipped_with_history"] == 1
    assert result["missing"] == []
    assert result["replayed"] == []
    # 没有新增事件：原来那条仍在，且没有第二条 task.completed。
    assert notification.Outbox(cfg.outbox_path(tmp_path)).pending_count() == 1


def test_reconcile_dry_run_does_not_enqueue(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_TEST_NOTIFY_SECRET", SECRET)
    cfg = config(tmp_path, completion_url=f"http://127.0.0.1:9{notification.COMPLETE_PATH}")
    task = terminal_task()
    task.pop("_commit")

    result = notification.reconcile_completed(
        tmp_path, cfg, [task], lambda _id: "a" * 40, dry_run=True
    )

    assert result["dry_run"] is True
    assert result["missing"] == [task["id"]]
    assert result["replayed"][0]["status"] == "dry_run"
    assert notification.Outbox(cfg.outbox_path(tmp_path)).pending_count() == 0
