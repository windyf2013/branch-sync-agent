"""运维操作 API：候选 commit 查询、触发同步/重跑任务、查询任务状态。

操作端点（``GET /api/commits``、``POST /api/sync``、``POST /api/rerun``）
要求 operator 角色；任务状态查询（``GET /api/tasks/{id}``）登录即可。
API 场景下未登录统一返回 403（不做 302 跳转），CSRF 走请求体 ``_csrf``
字段（与表单隐藏字段同一套签名），任务经 ``TaskRunner`` 入队异步执行，
返回 task_id 供前端轮询状态。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from bsa_web.auth import current_user, require_csrf, require_login, require_operator
from bsa_web.rbac import OPERATOR
from bsa_web.runner import QUEUED_WAIT_REASON

router = APIRouter(prefix="/api", tags=["operations"])

_BUSY_MSG = "该目标分支已有任务在运行或排队，请稍后再试"


def _require_operator_api(request: Request) -> dict:
    """API 权限校验：未登录/非操作者一律 403（JSON 场景不用 302 跳转）。"""
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=403, detail="需要登录")
    if user["role"] != OPERATOR:
        raise HTTPException(status_code=403, detail="需要操作者权限")
    return user


class SyncBody(BaseModel):
    src: str
    target: str
    sha: str | list[str] | None = None
    shas: list[str] | None = None


class RerunBody(BaseModel):
    target: str
    fresh: bool = False


def _load_commits(log_dir: str, src: str, limit: int = 50) -> list[dict]:
    """子进程调 `bsa commits <src> --limit N --refresh`，返回候选 commit 列表。

    ``--refresh`` 使 CLI 先 ``git fetch origin <src>`` 取远端最新（持全局
    flock），保证快速操作选源分支后加载的即最新远端提交。失败返回空列表。

    环境变量注入 ``LOG_DIR``（承 projection.py 既有 subprocess 模式），
    其余 BSA 设置继承进程环境（生产同 env 部署）。
    """
    env = dict(os.environ)
    env["LOG_DIR"] = str(log_dir)
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "bsa.cli",
                "commits",
                src,
                "--limit",
                str(limit),
                "--refresh",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


@router.get("/commits")
def api_commits(
    request: Request,
    src: str,
    limit: int = 50,
    user: Annotated[dict, Depends(_require_operator_api)] = None,
):
    """候选 commit 端点：``bsa commits <src> --limit N``，供 B 区三步表单加载。"""
    return _load_commits(request.app.state.settings.log_dir, src, limit)


@router.post("/sync", status_code=201, dependencies=[Depends(require_csrf)])
def api_sync(
    request: Request,
    body: SyncBody,
    user: Annotated[dict, Depends(_require_operator_api)] = None,
):
    if body.shas is not None:
        shas = body.shas
    else:
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
