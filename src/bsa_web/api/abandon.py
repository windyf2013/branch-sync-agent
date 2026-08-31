"""放弃/恢复 API：abandons 表持久化标记 + 审计。

- ``POST /api/abandon``：operator 放弃目标（sha=None 表示整分支，指定 sha 为
  commit 级），INSERT OR IGNORE 幂等，审计 action=abandon。
- ``POST /api/restore``：DELETE 对应标记，审计 action=restore。

任务中心/待处理过滤以本表为来源（db.abandoned_keys / db.is_abandoned）。
全部端点 require_operator + CSRF（JSON body ``_csrf``）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from bsa_web import audit
from bsa_web.auth import require_csrf, require_operator
from bsa_web.executor import cancel_task

router = APIRouter(prefix="/api", tags=["abandon"])


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class AbandonBody(BaseModel):
    cycle_id: str | None = None
    target: str | None = None
    sha: str | None = None


@router.post("/abandon", dependencies=[Depends(require_csrf)])
def api_abandon(
    request: Request,
    body: AbandonBody,
    user: Annotated[dict, Depends(require_operator)],
):
    if not body.cycle_id:
        raise HTTPException(status_code=400, detail="缺少 cycle_id")
    if not body.target:
        raise HTTPException(status_code=400, detail="缺少 target")

    db = request.app.state.db
    db.execute(
        "INSERT OR IGNORE INTO abandons(cycle_id, target, sha, user, created_at) "
        "VALUES (?,?,?,?,?)",
        (body.cycle_id, body.target, body.sha, user["username"], _now_iso()),
    )
    db.commit()
    audit.record(
        db,
        user["username"],
        "abandon",
        cycle_id=body.cycle_id,
        target=body.target,
        sha=body.sha,
        result="ok",
    )

    # 分支级放弃（sha=None）且该周期仍有排队/执行中任务 → 取消执行（标 cancelled +
    # 杀进程 + 清 docker + 删 checkpoint/worktree）。commit 级放弃仅过滤待办，不取消。
    cancelled = None
    if body.sha is None:
        row = db.execute(
            "SELECT id FROM tasks WHERE cycle_id=? AND state IN ('queued','running') "
            "ORDER BY id DESC LIMIT 1",
            (body.cycle_id,),
        ).fetchone()
        if row is not None:
            cancelled = cancel_task(
                db, request.app.state.settings.log_dir, row["id"]
            )
            audit.record(
                db,
                user["username"],
                "cancel",
                cycle_id=body.cycle_id,
                target=body.target,
                result="cancelled" if cancelled.get("cancelled") else "noop",
            )

    return {
        "ok": True,
        "cycle_id": body.cycle_id,
        "target": body.target,
        "sha": body.sha,
        "cancelled": bool(cancelled and cancelled.get("cancelled")),
        "task_id": cancelled.get("task_id") if cancelled else None,
    }


@router.post("/restore", dependencies=[Depends(require_csrf)])
def api_restore(
    request: Request,
    body: AbandonBody,
    user: Annotated[dict, Depends(require_operator)],
):
    if not body.cycle_id:
        raise HTTPException(status_code=400, detail="缺少 cycle_id")
    if not body.target:
        raise HTTPException(status_code=400, detail="缺少 target")

    db = request.app.state.db
    db.execute(
        "DELETE FROM abandons WHERE cycle_id=? AND target=? AND sha IS ?",
        (body.cycle_id, body.target, body.sha),
    )
    db.commit()
    audit.record(
        db,
        user["username"],
        "restore",
        cycle_id=body.cycle_id,
        target=body.target,
        sha=body.sha,
        result="ok",
    )
    return {
        "ok": True,
        "cycle_id": body.cycle_id,
        "target": body.target,
        "sha": body.sha,
    }
