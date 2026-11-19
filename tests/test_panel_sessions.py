"""面板会话枚举测试：codex 索引、zcode sqlite、任务历史。"""

from __future__ import annotations

import json
import sqlite3

from handoff.panel import sessions


def test_codex_sessions_from_index(tmp_path, monkeypatch):
    index = tmp_path / "session_index.jsonl"
    rows = [
        {"id": "aaa-1", "thread_name": "订单接口", "updated_at": "2026-11-05T10:00:00Z"},
        {"id": "bbb-2", "thread_name": "版本接口", "updated_at": "2026-11-05T11:00:00Z"},
        {"id": "aaa-1", "thread_name": "订单接口 (2)", "updated_at": "2026-11-05T12:00:00Z"},  # fork 去重
    ]
    index.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    monkeypatch.setattr(sessions, "CODEX_HOME", tmp_path)

    result = sessions.codex_sessions()
    # 反向读（新的在前），重复 id 只保留第一次出现（即最新一次更新）
    assert [s.id for s in result] == ["aaa-1", "bbb-2"]
    assert result[0].title == "订单接口 (2)"
    assert result[0].executor == "codex"


def test_codex_session_cwd_reads_plain_jsonl(tmp_path, monkeypatch):
    day = tmp_path / "sessions" / "2026" / "11" / "05"
    day.mkdir(parents=True)
    first = {"type": "session_meta", "payload": {"session_id": "sid-9", "cwd": "D:\\wt\\attempt-1"}}
    (day / "rollout-2026-11-05T01-02-03-sid-9.jsonl").write_text(
        json.dumps(first) + "\n", encoding="utf-8"
    )
    # zst 存档应被跳过
    (day / "rollout-2026-11-05T01-02-04-sid-8.jsonl.zst").write_bytes(b"\x28\xb5\x2f\xfd")
    monkeypatch.setattr(sessions, "CODEX_HOME", tmp_path)

    assert sessions.codex_session_cwd("sid-9") == "D:\\wt\\attempt-1"
    assert sessions.codex_session_cwd("sid-8") is None
    assert sessions.codex_session_cwd("missing") is None


def test_zcode_sessions_reads_db_readonly(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE session (id text primary key, parent_id text, title text,"
        " directory text not null, time_created integer, time_updated integer)"
    )
    conn.executemany(
        "INSERT INTO session (id, parent_id, title, directory, time_created, time_updated) VALUES (?,?,?,?,?,?)",
        [
            ("sess_main", None, "主会话", "D:/wt/a1", 1792800000000, 1792800900000),
            ("sess_sub", "sess_main", "子代理", "D:/wt/a1", 1792800000000, 1792800500000),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(sessions, "ZCODE_DB", db)

    result = sessions.zcode_sessions()
    assert [s.id for s in result] == ["sess_main"]  # 子代理被过滤
    assert result[0].title == "主会话"
    assert result[0].cwd == "D:/wt/a1"
    assert result[0].updated_at and result[0].updated_at.startswith("2026-")


def test_task_sessions_from_result_and_events():
    task = {
        "id": "ho-1",
        "attempt": 2,
        "result": {
            "executor": "zcode",
            "executor_session_id": "sess_latest",
            "worktree": "D:/wt/attempt-2",
            "codex_thread_id": None,
        },
    }
    events = [
        {"type": "codex.completed", "attempt": 1, "metadata": {"run_id": "sess_old", "executor": "zcode"}},
        {"type": "codex.failed", "attempt": 1, "metadata": {}},  # 非 completed 忽略
        {"type": "codex.completed", "attempt": 2, "metadata": {"run_id": "sess_latest", "executor": "zcode"}},
        {"type": "codex.completed", "attempt": 1, "metadata": {"run_id": "thread_codex_1", "executor": "codex"}},
    ]
    result = sessions.task_sessions(task, events)
    ids = [(s.executor, s.id, s.attempt) for s in result]
    # 按 attempt 倒序，最新在前；同 id 去重
    assert ids[0] == ("zcode", "sess_latest", 2)
    assert ("zcode", "sess_old", 1) in ids
    assert ("codex", "thread_codex_1", 1) in ids
    latest = result[0]
    assert latest.cwd == "D:/wt/attempt-2"
