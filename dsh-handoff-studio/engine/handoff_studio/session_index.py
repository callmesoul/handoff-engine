"""dsh 会话索引：把每一轮真正用过的 DSH 会话 id 落成一行 JSONL。

## 为什么需要这个文件

面板上的「继续上次会话」按钮发的是 `resume_session_id="last"`，父类
（`handoff.panel.service.PanelService._last_session_for`）的解析顺序是：

    1. 任务事件里的 `codex.completed` → metadata.run_id
    2. 执行日志首行的 `{"type":"thread.started","thread_id":…}`
    3. `task.result.executor_session_id` / `codex_thread_id`

这三条 dsh 一条都不占：

    - 事件里记的 `run_id` 是**本轮 run 的句柄**（`run-…`），不是会话 id
      （`session-…`），拿它去 resume 只会得到「会话不存在」；
    - 日志首行是我们自己写的 `{"type":"log","text":…}`；
    - 失败/中断的那一轮根本不会写 `result`。

而那个会话其实好好躺在 `~/.dsh/sessions/<cwd>/<session-id>/` 里。于是表现就是：
点「继续上次会话」→「这个任务还没有可续跑的会话」，明明上一轮刚跑过。

所以 dsh 执行器在**开跑的那一刻**就把会话 id 记下来，父类再从这份索引里取。

## 为什么另存一份而不是改父类

引擎仓（`handoff` 包）是只读复用的：studio 只允许在交接仓里加运行态文件，
不去改 `handoff.*` 的任何一行（见 `__init__.py` 的模块说明）。索引是运行态，
落在 `<交接仓>/.handoff-run/` 下，与日志同级，不进 Git。

## 记录格式

一行一条，同一轮（run_id 相同）先写 `started` 再写 `finished`，读的时候后者覆盖
前者的状态字段。字段：

    task_id / attempt / executor / session_id / run_id / cwd /
    started_at / finished_at / status

`cwd` 很关键：DSH 会话是按工作目录组织的，续跑必须回到同一个目录，否则 agent
以为改过、磁盘上却是干净的另一份。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

#: 索引文件名（相对 `.handoff-run/`）。
INDEX_FILENAME = "studio-sessions.jsonl"

#: 只保留最近这么多条，避免索引无限长（一次执行一行，几十万行没有意义）。
MAX_RECORDS = 4000


def index_path(base: str | Path) -> Path:
    """`base` 可以是交接仓根，也可以是 `.handoff-run/` 本身。"""
    root = Path(base)
    if root.name == ".handoff-run":
        return root / INDEX_FILENAME
    return root / ".handoff-run" / INDEX_FILENAME


def _stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def record(
    path: str | Path | None,
    *,
    task_id: str,
    attempt: int,
    session_id: str,
    run_id: str | None = None,
    cwd: str | None = None,
    status: str | None = None,
) -> None:
    """追加一条记录。

    索引只是「续跑时去哪儿找会话」的提示，写不进去不该把执行本身搞挂：
    磁盘满、权限错、路径为空都只跳过，不抛异常。
    """
    if not path:
        return
    session_id = str(session_id or "").strip()
    if not session_id:
        return
    payload = {
        "task_id": str(task_id or ""),
        "attempt": int(attempt or 0),
        "executor": "dsh",
        "session_id": session_id,
        "run_id": str(run_id or "") or None,
        "cwd": str(cwd or "") or None,
        "started_at": _stamp(),
    }
    if status:
        payload["status"] = str(status)
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        return
    _trim(target)


def _trim(path: Path) -> None:
    """超过上限就重写末尾一段。

    重写而不是删行：Windows 上并发追加 + 原地截断很容易把最后一行切成半截，
    换来的是下一次读到一条坏记录。整文件替换是原子的（`os.replace`）。
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    if len(lines) <= MAX_RECORDS:
        return
    kept = lines[-MAX_RECORDS:]
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        temp.write_text("\n".join(kept) + "\n", encoding="utf-8")
        temp.replace(path)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass


def _load(path: str | Path | None) -> list[dict]:
    if not path:
        return []
    target = Path(path)
    if not target.is_file():
        return []
    rows: list[dict] = []
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("session_id"):
            rows.append(item)
    return rows


def _merged(rows: list[dict]) -> list[dict]:
    """同一轮（task/attempt/run_id）的多条记录合并成一条，后写的字段覆盖先写的。"""
    merged: dict[tuple, dict] = {}
    order: list[tuple] = []
    for index, row in enumerate(rows):
        key = (
            str(row.get("task_id") or ""),
            int(row.get("attempt") or 0),
            str(row.get("run_id") or f"#{index}"),
        )
        if key not in merged:
            order.append(key)
            merged[key] = {}
        merged[key].update({k: v for k, v in row.items() if v is not None})
    return [merged[key] for key in order]


def last_session(path: str | Path | None, task_id: str, *, executor: str = "dsh") -> dict | None:
    """本任务最近一轮的会话记录；没有就返回 None。

    排序按 (attempt, started_at)：续跑要看的是**最新**那一轮，不是第一轮。
    """
    rows = [
        row
        for row in _merged(_load(path))
        if str(row.get("task_id") or "") == str(task_id)
        and str(row.get("executor") or "dsh") == executor
    ]
    if not rows:
        return None
    rows.sort(key=lambda row: (int(row.get("attempt") or 0), str(row.get("started_at") or "")))
    return rows[-1]


def task_sessions(path: str | Path | None, task_id: str, *, executor: str = "dsh") -> list[dict]:
    """本任务跑过的全部会话，attempt 倒序（面板列表要最新的在最上面）。"""
    rows = [
        row
        for row in _merged(_load(path))
        if str(row.get("task_id") or "") == str(task_id)
        and str(row.get("executor") or "dsh") == executor
    ]
    rows.sort(key=lambda row: (int(row.get("attempt") or 0), str(row.get("started_at") or "")), reverse=True)
    return rows


__all__ = ["INDEX_FILENAME", "MAX_RECORDS", "index_path", "last_session", "record", "task_sessions"]
