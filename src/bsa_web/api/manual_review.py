"""人工项处理 API：改判定（override）/ 确认继续（confirm）。

- ``POST /api/override``：operator 人工覆盖 commit 的 is_bug_fix/risk 判定，
  经 V1 CLI ``bsa override``（同解释器子进程，写 judgments.json）同步执行，
  成功后在每次决策执行时生效；审计 action=override 留痕。
- ``POST /api/confirm``：operator 确认允许同步 ManualReview 项，复用
  ``--sha`` 直同步路径（runner 异步跑 ``bsa sync <target> --sha <sha>``），
  返回 task_id；审计 action=confirm_continue。

（放弃人工项已由 abandons 表机制承接：``POST /api/abandon`` / ``POST /api/restore``，
见 ``bsa_web.api.abandon``。）

全部端点 require_operator + CSRF（JSON body ``_csrf``）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from bsa_web import audit
from bsa_web.auth import require_csrf, require_operator
from bsa_web.runner import QUEUED_WAIT_REASON

router = APIRouter(prefix="/api", tags=["manual_review"])

_RISKS: tuple[str, ...] = ("low", "medium", "high")
_OVERRIDE_TIMEOUT_SEC = 60
_BUSY_MSG = "该目标分支已有任务在运行或排队，请稍后再试"


def _run_cli(cmd: list[str], log_dir: str) -> subprocess.CompletedProcess:
    """同步调 V1 CLI：注入 LOG_DIR，其余 env 继承父进程（与 runner 一致）。"""
    env = dict(os.environ)
    env["LOG_DIR"] = str(log_dir)
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=_OVERRIDE_TIMEOUT_SEC, env=env
    )


class OverrideBody(BaseModel):
    sha: str | None = None
    is_bug_fix: bool | None = None
    risk: str | None = None


class ConfirmBody(BaseModel):
    target: str | None = None
    sha: str | None = None


@router.post("/override", dependencies=[Depends(require_csrf)])
def api_override(
    request: Request,
    body: OverrideBody,
    user: Annotated[dict, Depends(require_operator)],
):
    if not body.sha:
        raise HTTPException(status_code=400, detail="缺少 sha")
    if body.risk is not None and body.risk not in _RISKS:
        raise HTTPException(status_code=400, detail="risk 非法（low/medium/high）")
    if body.is_bug_fix is None and body.risk is None:
        raise HTTPException(status_code=400, detail="至少提供 is_bug_fix 或 risk 之一")

    cmd = [sys.executable, "-m", "bsa.cli", "override", body.sha]
    if body.is_bug_fix is not None:
        cmd += ["--is-bug-fix", "true" if body.is_bug_fix else "false"]
    if body.risk is not None:
        cmd += ["--risk", body.risk]
    try:
        proc = _run_cli(cmd, request.app.state.settings.log_dir)
    except subprocess.SubprocessError as exc:
        raise HTTPException(status_code=500, detail=f"覆盖判定执行失败: {exc}") from None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "覆盖判定失败").strip()
        raise HTTPException(status_code=500, detail=detail)

    new_values = {
        "is_bug_fix": body.is_bug_fix,
        "risk": body.risk,
    }
    audit.record(
        request.app.state.db,
        user["username"],
        "override",
        sha=body.sha,
        detail=new_values,
        result="ok",
    )
    return {"message": "覆盖判定已写入，将在下一次决策执行时生效", "sha": body.sha}


@router.post("/confirm", status_code=201, dependencies=[Depends(require_csrf)])
def api_confirm(
    request: Request,
    body: ConfirmBody,
    user: Annotated[dict, Depends(require_operator)],
):
    if not body.target:
        raise HTTPException(status_code=400, detail="缺少 target")
    if not body.sha:
        raise HTTPException(status_code=400, detail="缺少 sha")

    task_id = request.app.state.enqueue_task(
        request.app.state.db, "sync", user["username"], body.target,
        shas=[body.sha], src=None,
    )
    if task_id is None:
        raise HTTPException(status_code=409, detail=_BUSY_MSG)

    audit.record(
        request.app.state.db,
        user["username"],
        "confirm_continue",
        target=body.target,
        sha=body.sha,
        result="queued",
    )
    return {"task_id": task_id, "state": "queued", "wait_reason": QUEUED_WAIT_REASON}
