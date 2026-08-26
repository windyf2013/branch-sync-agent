"""V1 引擎任务登记：向平台 tasks 表（platform.sqlite3）写执行状态。

解耦架构下 tasks 表是唯一任务事实源。引擎执行 sync/rerun/run-cycle 时
主动登记，让平台（web）天然管理所有任务与状态：

- ``BSA_TASK_ID`` 环境变量存在（executor 触发）→ 复用该任务行，执行前回填
  ``cycle_id``（运行期即知周期 id，重启可重挂），终态由 executor 写。
- 不存在（CLI 直启）→ 引擎自建一行 ``source=cli``（running），执行完由引擎
  写终态。这样 CLI 直启也不再是"平台外的执行"，而是统一进 tasks 表被管理。

引擎只依赖 sqlite3，不 import bsa_web；platform.sqlite3 路径 = LOG_DIR 下。
表结构必须与 ``bsa_web.db._SCHEMA`` 的 tasks 一致（幂等建表）。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

_TASKS_DDL = """
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, user TEXT NOT NULL,
  cycle_id TEXT, target TEXT, src TEXT, shas TEXT, fresh INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL,
  started_at TEXT, finished_at TEXT, source TEXT NOT NULL DEFAULT 'web');
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_active_target
  ON tasks(target) WHERE state IN ('queued','running');
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _connect(log_dir: str | Path) -> sqlite3.Connection:
    """打开平台库（幂等建 tasks 表 + busy_timeout，与 web 共享 WAL 环境）。"""
    path = Path(log_dir) / "platform.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), autocommit=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_TASKS_DDL)
    return conn


def task_id_from_env() -> int | None:
    """读取 executor 注入的任务 id（BSA_TASK_ID）；缺失/非法返回 None。"""
    raw = os.environ.get("BSA_TASK_ID")
    if raw:
        try:
            return int(raw)
        except ValueError:
            return None
    return None


def register_start(
    log_dir: str | Path,
    *,
    kind: str,
    target: str | None,
    cycle_id: str,
    src: str | None = None,
    shas: list[str] | None = None,
) -> int | None:
    """执行开始登记。

    - executor 触发（BSA_TASK_ID 存在）：回填 cycle_id，返回该 task_id。
    - CLI 直启：INSERT 一行 ``running``（source=cli），返回新 task_id。
    """
    tid = task_id_from_env()
    if tid is not None:
        conn = _connect(log_dir)
        conn.execute("UPDATE tasks SET cycle_id=? WHERE id=?", (cycle_id, tid))
        return tid
    conn = _connect(log_dir)
    cur = conn.execute(
        "INSERT INTO tasks(kind, user, target, src, shas, fresh, cycle_id, state, "
        "created_at, source) VALUES (?,?,?,?,?,?,?, 'running', ?, 'cli')",
        (
            kind,
            "cli",
            target,
            src,
            json.dumps(shas) if shas else None,
            0,
            cycle_id,
            _now(),
        ),
    )
    return cur.lastrowid


def register_finish(
    log_dir: str | Path,
    task_id: int | None,
    *,
    state: str,
    cycle_id: str,
    error: str | None = None,
) -> None:
    """执行终态登记（succeeded/failed），写入 cycle_id 与 error（可选）。"""
    if task_id is None:
        return
    conn = _connect(log_dir)
    fields = ["state=?", "cycle_id=?", "finished_at=?"]
    params: list = [state, cycle_id, _now()]
    if error is not None:
        fields.append("error=?")
        params.append(error)
    params.append(task_id)
    conn.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id=?", params)
