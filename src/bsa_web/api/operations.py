"""运维操作 API：触发同步/重跑任务、查询任务状态。

操作端点（``POST /api/sync``、``POST /api/rerun``）要求 operator 角色；
任务状态查询（``GET /api/tasks/{id}``）登录即可。CSRF 走请求体 ``_csrf``
字段（与表单隐藏字段同一套签名），任务经 ``TaskRunner`` 入队异步执行，
返回 task_id 供前端轮询状态。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from bsa_web.auth import require_csrf, require_login, require_operator
from bsa_web.runner import QUEUED_WAIT_REASON

router = APIRouter(prefix="/api", tags=["operations"])

_BUSY_MSG = "该目标分支已有任务在运行或排队，请稍后再试"


class SyncBody(BaseModel):
    src: str
    target: str
    sha: str | list[str] | None = None


class RerunBody(BaseModel):
    target: str
    fresh: bool = False


@router.post("/sync", status_code=201, dependencies=[Depends(require_csrf)])
def api_sync(
    request: Request,
    body: SyncBody,
    user: Annotated[dict, Depends(require_operator)],
):
    shas = body.sha if isinstance(body.sha, list) else ([body.sha] if body.sha else None)
    task_id = request.app.state.runner.submit(
        "sync", user["username"], body.target, shas=shas, src=body.src
    )
    if task_id is None:
        raise HTTPException(status_code=409, detail=_BUSY_MSG)
    return {"task_id": task_id, "state": "queued", "wait_reason": QUEUED_WAIT_REASON}


@router.post("/rerun", status_code=201, dependencies=[Depends(require_csrf)])
def api_rerun(
    request: Request,
    body: RerunBody,
    user: Annotated[dict, Depends(require_operator)],
):
    task_id = request.app.state.runner.submit(
        "rerun", user["username"], body.target, fresh=body.fresh
    )
    if task_id is None:
        raise HTTPException(status_code=409, detail=_BUSY_MSG)
    return {"task_id": task_id, "state": "queued", "wait_reason": QUEUED_WAIT_REASON}


@router.get("/tasks/{task_id}")
def api_task_status(
    request: Request,
    task_id: int,
    user: Annotated[dict, Depends(require_login)],
):
    task = request.app.state.runner.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task
