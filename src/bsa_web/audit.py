"""审计日志：INSERT audit_log 记录平台操作留痕。

任务 13 建最小 record 供人工项处理（override/confirm/abandon）写审计；
任务 14 在此基础上补全审计列表页（查询复用本表）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def record(
    db: sqlite3.Connection,
    user: str,
    action: str,
    *,
    cycle_id: str | None = None,
    target: str | None = None,
    sha: str | None = None,
    detail: dict | None = None,
    result: str | None = None,
) -> int:
    """写入一条审计记录，返回自增 id。``detail`` 以 JSON 存入 detail_json。"""
    detail_json = json.dumps(detail, ensure_ascii=False) if detail is not None else None
    cur = db.execute(
        "INSERT INTO audit_log(ts, user, action, cycle_id, target, sha, detail_json, result) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (_now_iso(), user, action, cycle_id, target, sha, detail_json, result),
    )
    db.commit()
    return cur.lastrowid


def list_records(
    db: sqlite3.Connection, limit: int = 500
) -> list[dict]:
    """按时间倒序返回最近 ``limit`` 条审计记录（同秒记录以 id 倒序稳定）。"""
    rows = db.execute(
        "SELECT id, ts, user, action, cycle_id, target, sha, detail_json, result "
        "FROM audit_log ORDER BY ts DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]
