"""历史报告页：周期列表（按结论筛选）+ 入口跳转详情页。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from bsa_web import projection
from bsa_web.auth import make_csrf, require_login

router = APIRouter(prefix="/history", tags=["history"])

# (查询参数值, 展示标签)：前四项对应 Conclusion4.kind，failed/stopped 为周期/分支状态
KIND_FILTERS = (
    ("NeedSync", "待同步"),
    ("AlreadyIncluded", "已包含"),
    ("ManualReview", "人工确认"),
    ("OutOfScope", "范围外"),
    ("failed", "失败"),
    ("stopped", "停批"),
)

_STOPPED_STATUSES = ("PARTIAL", "MANUAL")


def _kind_counts(payload: dict) -> dict[str, int]:
    """统计周期投影内各结论 kind / 失败 / 停批 出现次数，供筛选展示。"""
    counts: dict[str, int] = {}
    for per_target in (payload.get("decisions") or {}).values():
        for conclusion in per_target.values():
            kind = (conclusion or {}).get("kind")
            if kind:
                counts[kind] = counts.get(kind, 0) + 1
    branches = payload.get("branch_results") or {}
    n_failed = sum(1 for b in branches.values() if b.get("status") == "FAILED")
    if n_failed or payload.get("status") == "FAILED":
        counts["failed"] = max(n_failed, 1)
    n_stopped = sum(
        1 for b in branches.values() if b.get("status") in _STOPPED_STATUSES
    )
    if n_stopped:
        counts["stopped"] = n_stopped
    return counts


def _render(request: Request, **extra) -> object:
    return request.app.state.templates.TemplateResponse(request, "history.html", extra)


@router.get("")
def history(
    request: Request,
    user: Annotated[dict, Depends(require_login)],
    kind: str | None = None,
):
    settings = request.app.state.settings
    csrf = make_csrf(settings.secret_key, user["username"])
    active = kind if kind in dict(KIND_FILTERS) else None
    cycles = []
    for rec in projection.list_cycles(settings.log_dir):
        entry = {
            "cycle_id": rec.get("cycle_id"),
            "status": rec.get("status"),
            "started_at": rec.get("started_at"),
        }
        if active:
            # 筛选需按周期读投影统计对应 kind 计数；无法读取（子进程失败）则跳过
            payload = projection.load_cycle(settings.log_dir, rec.get("cycle_id"))
            if payload is None:
                continue
            count = _kind_counts(payload).get(active, 0)
            if not count:
                continue
            entry["count"] = count
        cycles.append(entry)
    return _render(
        request,
        user=user,
        csrf=csrf,
        cycles=cycles,
        kind=active,
        kind_filters=KIND_FILTERS,
    )
