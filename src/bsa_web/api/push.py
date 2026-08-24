"""推送 API：确认（只读回显）+ 推送执行（四道闸 → 受限 push → 审计）。

- ``POST /api/push/confirm``：operator 读投影回显确认框参数（目标/commit 范围/
  patch 摘要），只读幂等，不执行任何推送。
- ``POST /api/push``：operator + CSRF，四道闸任一不过 → 400+原因；全过 →
  受限执行 ``git push origin HEAD:<target>``（禁 --force）→ 写审计。
  仅此端点触发推送，无任何自动/定时推送路径。

参数解析顺序：先 require_operator（未登录 → 302、viewer → 403）再 require_csrf，
保证未带 CSRF 的未登录请求仍得到登录跳转而非 CSRF 403。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from bsa_web import audit, projection, push
from bsa_web.auth import require_csrf, require_operator

router = APIRouter(prefix="/api", tags=["push"])


class PushConfirmBody(BaseModel):
    target: str | None = None


class PushBody(BaseModel):
    target: str | None = None
    shas: list[str] | None = None


def _load_payload(request: Request) -> tuple[str | None, dict | None]:
    """读最新已完成周期的投影 payload；无完成周期或投影失败返回 (None, None)。"""
    log_dir = request.app.state.settings.log_dir
    cycle_id = projection.latest_completed_cycle(log_dir)
    if cycle_id is None:
        return None, None
    return cycle_id, projection.load_cycle(log_dir, cycle_id)


def _commits_of(branch: dict) -> list[str]:
    """commit 范围 = 投影 branch_results[target].commits[].sha。"""
    return [c.get("sha") for c in (branch.get("commits") or []) if c.get("sha")]


def _require_success_branch(payload: dict, target: str) -> dict:
    """从投影取目标分支并校验存在且 SUCCESS，否则抛 400。"""
    branch = (payload.get("branch_results") or {}).get(target)
    if branch is None:
        raise HTTPException(status_code=400, detail=f"投影中不存在分支 {target}")
    if branch.get("status") != "SUCCESS":
        raise HTTPException(status_code=400, detail=f"分支 {target} 状态非 SUCCESS，不可推送")
    return branch


@router.post("/push/confirm")
def api_push_confirm(
    request: Request,
    body: PushConfirmBody,
    user: Annotated[dict, Depends(require_operator)],
    _csrf: Annotated[None, Depends(require_csrf)] = None,
):
    if not body.target:
        raise HTTPException(status_code=400, detail="缺少 target")
    cycle_id, payload = _load_payload(request)
    if payload is None:
        raise HTTPException(status_code=400, detail="当前周期数据不可用")
    branch = _require_success_branch(payload, body.target)
    shas = _commits_of(branch)
    patch_path = branch.get("patch_path")
    patch_summary = (
        f"补丁 {patch_path}" if patch_path else f"{len(shas)} 个提交"
    )
    return {
        "target": body.target,
        "cycle_id": cycle_id,
        "commits": shas,
        "patch_summary": patch_summary,
        "worktree_path": branch.get("worktree_path"),
    }


@router.post("/push")
def api_push(
    request: Request,
    body: PushBody,
    user: Annotated[dict, Depends(require_operator)],
    _csrf: Annotated[None, Depends(require_csrf)] = None,
):
    if not body.target:
        raise HTTPException(status_code=400, detail="缺少 target")
    cycle_id, payload = _load_payload(request)
    if payload is None:
        raise HTTPException(status_code=400, detail="当前周期数据不可用")
    branch = (payload.get("branch_results") or {}).get(body.target)
    if branch is None:
        raise HTTPException(status_code=400, detail=f"投影中不存在分支 {body.target}")
    worktree = branch.get("worktree_path") or ""

    # 四道闸任一不过 → 400 + 原因
    reasons = push.check_push_gates(
        payload,
        body.target,
        forbidden=push.load_forbidden_branches(),
        status_clean=push.worktree_is_clean(worktree),
    )
    if reasons:
        raise HTTPException(status_code=400, detail={"reasons": reasons})

    # 确认参数校验：commit 范围须与投影一致（防确认后投影变动推送不同内容）
    shas = _commits_of(branch)
    if body.shas is not None and body.shas != shas:
        raise HTTPException(status_code=400, detail="确认信息已过期，请重新确认")

    returncode, message = push.execute_push(
        push.PushExecutor(), worktree, body.target
    )
    audit.record(
        request.app.state.db,
        user["username"],
        "push",
        cycle_id=cycle_id,
        target=body.target,
        sha=",".join(shas) or None,
        detail={
            "worktree": worktree,
            "commits": shas,
            "patch_path": branch.get("patch_path"),
        },
        result="ok" if returncode == 0 else "failed",
    )
    return {
        "target": body.target,
        "message": message,
        "returncode": returncode,
    }
