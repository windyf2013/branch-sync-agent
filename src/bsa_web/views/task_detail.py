"""任务详情页：/task/{cycle_id}/{target} 聚合操作。

复用 bsa_web.views.detail 的分支证据渲染（build 日志预览 + _target_detail.html），
叠加按状态/权限显示的操作入口：
- SUCCESS（最近完成周期自动现场 + 手动/重跑 SUCCESS 现场）→ 推送（四道闸 +
  确认框，复用 _push_confirm.html）；
- FAILED/PARTIAL/MANUAL 且有 worktree → WebSSH 入口 + 重跑；
- 已放弃 → 恢复入口；
- 该分支的 ManualReview 人工项 → 改判定（/api/override）/ 确认继续（/api/confirm）。

操作全部走既有 API，本页只渲染入口。WebSSH 入口指向 /ssh/task/{cycle}/{target}，
由 WebSSH 任务（task 5）实现受限终端。推送对最近完成周期开放；手动同步/重跑
周期（manual-/rerun- 前缀）不写 cycle record、latest_completed_cycle 不可见，
但 SUCCESS 现场同样开放推送入口（实际推送仍经四道闸校验 worktree/干净度）。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from bsa_web import projection
from bsa_web.auth import make_csrf, require_login
from bsa_web.db import abandoned_keys
from bsa_web.views import detail

router = APIRouter(prefix="/task", tags=["task_detail"])

_SSH_STATUSES = ("FAILED", "PARTIAL", "MANUAL")
_ACTIVE_STATES = ("queued", "running")


def _active_task_id(db, cycle_id: str) -> int | None:
    """cycle_id 下是否有活动任务（排队/执行中）。

    详情页投影未就绪（cycle_id 已回写但 state.json 未落盘 / checkpoint 未含该分支）
    时，若有活动任务则重定向到任务状态页，避免误 404 "目标分支不存在"。
    """
    row = db.execute(
        "SELECT id FROM tasks WHERE cycle_id=? AND state IN ('queued','running') "
        "ORDER BY id DESC LIMIT 1",
        (cycle_id,),
    ).fetchone()
    return row["id"] if row is not None else None

# 手动同步（manual-）与 retained 重跑线程（rerun-）的周期 id 前缀：
# 这两类周期不写 cycle record（latest_completed_cycle 读不到），须显式放行推送。
_MANUAL_CYCLE_PREFIXES = ("manual-", "rerun-")


def _is_manual_cycle(cycle_id: str) -> bool:
    """True when cycle_id 是手动同步/重跑周期（manual-/rerun- 前缀）。"""
    return cycle_id.startswith(_MANUAL_CYCLE_PREFIXES)


def _render(request: Request, **extra) -> object:
    return request.app.state.templates.TemplateResponse(request, "task_detail.html", extra)


@router.get("/{cycle_id}/{target:path}")
def task_detail(
    request: Request,
    cycle_id: str,
    target: str,
    user: Annotated[dict, Depends(require_login)],
):
    settings = request.app.state.settings
    csrf = make_csrf(settings.secret_key, user["username"])
    payload = projection.load_cycle(settings.log_dir, cycle_id)
    if payload is None:
        # 周期投影不可用：任务仍在排队/执行（state.json 未落盘且 checkpoint 未就绪）
        active_id = _active_task_id(request.app.state.db, cycle_id)
        if active_id is not None:
            return RedirectResponse(f"/tasks/{active_id}", status_code=303)
        raise HTTPException(status_code=404, detail="周期不存在或数据不可用")
    branch = (payload.get("branch_results") or {}).get(target)
    if branch is None:
        active_id = _active_task_id(request.app.state.db, cycle_id)
        if active_id is not None:
            return RedirectResponse(f"/tasks/{active_id}", status_code=303)
        raise HTTPException(status_code=404, detail="目标分支不存在")

    # 按需读取各 commit build 日志前 N 行（复用 detail.target_detail 的读取逻辑）
    for cr in branch.get("commits") or []:
        for outcome in (cr.get("build") or {}).values():
            preview = detail._read_build_log(settings.log_dir, outcome.get("log_path"))
            outcome["log_preview"] = preview[0] if preview else None
            outcome["log_truncated"] = preview[1] if preview else False

    abandoned = abandoned_keys(request.app.state.db, cycle_id)
    branch_abandoned = (target, None) in abandoned
    status = branch.get("status") or "UNKNOWN"
    # 续跑：该 (cycle_id, target) 存在 interrupted 的 tasks 行（executor 重启对账
    # 打标记）→ 提供保留现场续跑入口（rerun retained + --cycle 来源周期）。
    interrupted_row = request.app.state.db.execute(
        "SELECT id FROM tasks WHERE cycle_id=? AND target=? AND state='interrupted' "
        "LIMIT 1",
        (cycle_id, target),
    ).fetchone()
    show_resume = interrupted_row is not None
    # 推送开放范围：最近完成周期的自动 SUCCESS 现场 + 手动/重跑 SUCCESS 现场
    # （手动/重跑周期不写 cycle record，latest_completed_cycle 不可见，须显式放行）。
    is_current = cycle_id == projection.latest_completed_cycle(settings.log_dir)
    review_items = [
        item
        for item in (payload.get("action_required") or [])
        if item.get("kind") == "ManualReview" and item.get("branch") == target
    ]
    return _render(
        request,
        user=user,
        csrf=csrf,
        payload=payload,
        cycle_id=cycle_id,
        target=target,
        branch=branch,
        status=status,
        is_current=is_current,
        branch_abandoned=branch_abandoned,
        show_resume=show_resume,
        show_webssh=bool(branch.get("worktree_path")) and status in _SSH_STATUSES,
        review_items=review_items,
        show_push=(
            not branch_abandoned
            and status == "SUCCESS"
            and (is_current or _is_manual_cycle(cycle_id))
        ),
    )
