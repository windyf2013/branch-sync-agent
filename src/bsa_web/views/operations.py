"""工作台快速操作表单与任务状态页。

任务 10 的 /sync /rerun 表单（form-encoded + ``_csrf``）在此接线到
``TaskRunner``：提交成功重定向任务状态页 /tasks/{id} 供轮询；同 target 并发
被拒时重定向回工作台并附 ``?error=busy`` 提示。操作端点要求 operator 角色，
任务状态页登录可见。
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from bsa_web.auth import make_csrf, require_csrf, require_login, require_operator
from bsa_web.build_labels import _read_build_log
from bsa_web.executor import task_log_tail
from bsa_web.progress import read_progress
from bsa_web.runner import task_detail_url

router = APIRouter(tags=["operations"])

STATE_LABELS = {
    "queued": "排队中",
    "running": "执行中",
    "succeeded": "成功",
    "failed": "失败",
    "cancelled": "已取消",
    "interrupted": "已中断",
}


def _busy_redirect() -> RedirectResponse:
    return RedirectResponse("/?error=busy", status_code=303)


def _task_redirect(db, task_id: int) -> RedirectResponse:
    """提交后跳转任务状态页：详情页需要已完成投影（state.json），任务可能仍在排队/执行，
    先落状态页（自动刷新，完成后自动跳详情），避免 cycle_id 已回写但投影未就绪时
    详情页误 404 "目标分支不存在"。"""
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/sync", dependencies=[Depends(require_csrf)])
def form_sync(
    request: Request,
    user: Annotated[dict, Depends(require_operator)],
    src: str = Form(...),
    target: str = Form(...),
    sha: str | None = Form(None),
):
    shas = [s.strip() for s in sha.split() if s.strip()] if sha else None
    task_id = request.app.state.enqueue_task(
        request.app.state.db, "sync", user["username"], target, shas=shas, src=src
    )
    if task_id is None:
        return _busy_redirect()
    return _task_redirect(request.app.state.db, task_id)


@router.post("/rerun", dependencies=[Depends(require_csrf)])
def form_rerun(
    request: Request,
    user: Annotated[dict, Depends(require_operator)],
    target: str = Form(...),
    fresh: str | None = Form(None),
    cycle: str | None = Form(None),
):
    fresh = fresh in ("on", "true", "1")
    task_id = request.app.state.enqueue_task(
        request.app.state.db, "rerun", user["username"], target,
        fresh=fresh, cycle_id=cycle,
    )
    if task_id is None:
        return _busy_redirect()
    return _task_redirect(request.app.state.db, task_id)


@router.get("/tasks/{task_id}")
def task_status_page(
    request: Request,
    task_id: int,
    user: Annotated[dict, Depends(require_login)],
):
    task = request.app.state.get_task(request.app.state.db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    csrf = make_csrf(request.app.state.settings.secret_key, user["username"])
    log_tail = (
        task_log_tail(request.app.state.settings.log_dir, task_id, 200)
        if task["state"] in ("queued", "running")
        else ""
    )
    try:
        shas = json.loads(task["shas"]) if task.get("shas") else None
    except (TypeError, ValueError):
        shas = None
    steps = []
    if task.get("cycle_id"):
        steps = read_progress(request.app.state.settings.log_dir, task["cycle_id"])
        for step in steps:
            if step.get("log_path"):
                preview = _read_build_log(
                    request.app.state.settings.log_dir, step["log_path"]
                )
                step["log_preview"] = preview[0] if preview else None
                step["log_truncated"] = preview[1] if preview else False
    return request.app.state.templates.TemplateResponse(
        request,
        "tasks.html",
        {
            "user": user,
            "csrf": csrf,
            "task": task,
            "task_id": task_id,
            "commit_count": len(shas) if shas else (task.get("commits") or None),
            "log_tail": log_tail,
            "steps": steps,
            "state_label": STATE_LABELS.get(task["state"], task["state"]),
            "detail_url": task_detail_url(request.app.state.db, task_id),
        },
    )
