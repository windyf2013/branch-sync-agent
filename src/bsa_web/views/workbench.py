"""工作台首页路由：待办/总览/Agent 状态，每次请求实时读投影不缓存。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from bsa_web import projection
from bsa_web.auth import make_csrf, require_login
from bsa_web.branches import rcios_branch_names

router = APIRouter(tags=["workbench"])

_PUSH_STATUSES = ("SUCCESS",)
_RERUN_STATUSES = ("FAILED", "PARTIAL", "MANUAL")


def _branch_options(settings) -> list[str]:
    """RCIOS 仓库分支列表（branch.md），供源/目标分支下拉框；缺失返回空。"""
    if not settings.branch_file:
        return []
    return rcios_branch_names(settings.branch_file)


def _build_todo(payload: dict) -> dict:
    """从投影 payload 推导待办区：可推送 / 待确认 / 待重跑。"""
    branches = payload.get("branch_results") or {}
    return {
        "push_pending": [
            b for b in branches.values() if b.get("status") in _PUSH_STATUSES
        ],
        "review_pending": [
            item
            for item in (payload.get("action_required") or [])
            if item.get("kind") == "ManualReview"
        ],
        "rerun_pending": [
            b for b in branches.values() if b.get("status") in _RERUN_STATUSES
        ],
    }


def _render(request: Request, branch_options: list[str], **extra) -> object:
    return request.app.state.templates.TemplateResponse(
        request, "workbench.html", {"branch_options": branch_options, **extra}
    )


def _op_error(request: Request) -> str | None:
    """快速操作重定向带回来的提示（如 ?error=busy 并发被拒）。"""
    if request.query_params.get("error") == "busy":
        return "该目标分支已有任务在运行或排队，请稍后再试"
    return None


@router.get("/")
def workbench(request: Request, user: Annotated[dict, Depends(require_login)]):
    settings = request.app.state.settings
    log_dir = settings.log_dir
    csrf = make_csrf(settings.secret_key, user["username"])
    records = projection.list_cycles(log_dir)
    op_error = _op_error(request)
    branch_options = _branch_options(settings)

    running = next((r for r in records if r.get("status") == "running"), None)
    if running is not None:
        # 运行中周期：仅显示"进行中"，不渲染详情（实时轮询由前端/后续任务承担）
        return _render(request, branch_options=branch_options,
            user=user,
            csrf=csrf,
            cycles=records,
            agent_status="running",
            running_cycle_id=running.get("cycle_id"),
            op_error=op_error,
        )

    cycle_id = projection.latest_completed_cycle(log_dir)
    if cycle_id is None:
        return _render(request, branch_options=branch_options,
            user=user,
            csrf=csrf,
            cycles=records,
            agent_status=None,
            op_error=op_error,
        )

    payload = projection.load_cycle(log_dir, cycle_id)
    if payload is None:
        # 子进程投影失败（returncode 非 0）→ 平台容错，提示不可用
        return _render(request, branch_options=branch_options,
            user=user,
            csrf=csrf,
            cycles=records,
            agent_status="unavailable",
            current_cycle_id=cycle_id,
            op_error=op_error,
        )

    return _render(request, branch_options=branch_options,
        user=user,
        csrf=csrf,
        cycles=records,
        current_cycle_id=cycle_id,
        agent_status="failed" if payload.get("status") == "FAILED" else "done",
        cycle_status=payload.get("status"),
        payload=payload,
        branches=list((payload.get("branch_results") or {}).values()),
        op_error=op_error,
        **_build_todo(payload),
    )
