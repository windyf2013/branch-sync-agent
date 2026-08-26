"""工作台首页路由：A 区任务中心（自动/手动分区）+ Agent 状态 + B 区快速操作。

每次请求实时读投影不缓存。自动任务 = 最新周期投影 branch_results 展开；
手动任务 = tasks 表（kind=sync/rerun）合并对应 manual cycle 投影；所有操作
收敛到任务详情页 /task/{cycle_id}/{target}。
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from bsa_web import projection
from bsa_web.auth import make_csrf, require_login
from bsa_web.branches import rcios_branch_names
from bsa_web.db import abandoned_keys

router = APIRouter(tags=["workbench"])

_PUSH_STATUSES = ("SUCCESS",)
_RERUN_STATUSES = ("FAILED", "PARTIAL", "MANUAL")

# 任务面板状态徽章中文标签（自动分支状态 / 手动 tasks.state 映射后的状态）
_STATUS_LABELS = {
    "SUCCESS": "成功",
    "FAILED": "失败",
    "PARTIAL": "停批",
    "MANUAL": "待处理",
    "UNKNOWN": "未知",
    "RUNNING": "进行中",
    "QUEUED": "排队中",
    "INTERRUPTED": "已中断",
}

# tasks.state → 展示状态（与投影 branch_results.status 语义对齐）
_TASK_STATE_STATUS = {
    "queued": "QUEUED",
    "running": "RUNNING",
    "succeeded": "SUCCESS",
    "failed": "FAILED",
    "interrupted": "INTERRUPTED",
}

_ACTIVE_STATES = ("queued", "running")
# interrupted 任务需保留展示（用户据此续跑），窗口过滤时视作活动。
_KEEP_STATES = _ACTIVE_STATES + ("interrupted",)


def _branch_options(settings) -> list[str]:
    """RCIOS 仓库分支列表（branch.md），供源/目标分支下拉框；缺失返回空。"""
    if not settings.branch_file:
        return []
    return rcios_branch_names(settings.branch_file)


def _build_todo(payload: dict, abandoned: set) -> dict:
    """从投影 payload 推导待办区：可推送 / 待确认 / 待重跑。

    已放弃项过滤：分支级（sha=None）匹配该分支所有 commit，整个分支从
    可推送/待重跑移除，其 commit 也从待确认移除；commit 级匹配具体 sha。
    """
    branches = payload.get("branch_results") or {}
    push_pending = []
    rerun_pending = []
    for b in branches.values():
        if (b.get("target_branch"), None) in abandoned:
            continue
        if b.get("status") in _PUSH_STATUSES:
            push_pending.append(b)
        if b.get("status") in _RERUN_STATUSES:
            rerun_pending.append(b)

    review_pending = []
    for item in payload.get("action_required") or []:
        if item.get("kind") != "ManualReview":
            continue
        branch = item.get("branch")
        if (branch, None) in abandoned or (branch, item.get("sha")) in abandoned:
            continue
        review_pending.append(item)

    return {
        "push_pending": push_pending,
        "review_pending": review_pending,
        "rerun_pending": rerun_pending,
    }


def _abandoned_items(cycle_id: str, abandoned: set) -> list[dict]:
    """已放弃项展示列表（已放弃徽章 + 恢复入口用）。"""
    return [
        {"cycle_id": cycle_id, "target": target, "sha": sha}
        for (target, sha) in sorted(abandoned)
    ]


def _parse_ts(value: str | None) -> datetime | None:
    """ISO 时间解析并归一化为无时区 UTC（兼容 ±HH:MM 与 Z）；失败返回 None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _auto_tasks(payload: dict, cycle_id: str, abandoned: set) -> list[dict]:
    """最新周期投影 branch_results 展开为自动任务面板（分支粒度）。

    分支级放弃（sha=None）标记面板为已放弃（徽章 + 恢复入口），不再提供推送；
    commit 级放弃仅影响待办区过滤，不改面板状态展示。
    """
    tasks = []
    for branch in (payload.get("branch_results") or {}).values():
        target = branch.get("target_branch")
        status = branch.get("status") or "UNKNOWN"
        tasks.append(
            {
                "cycle_id": cycle_id,
                "target": target,
                "status": status,
                "badge": _STATUS_LABELS.get(status, status),
                "commits": len(branch.get("commits") or []),
                "worktree_path": branch.get("worktree_path"),
                "patch_path": branch.get("patch_path"),
                "abandoned": (target, None) in abandoned,
                "user": "system",
                "source": "cron",
            }
        )
    return tasks


def _manual_tasks(db, log_dir: str, window_start: str | None) -> list[dict]:
    """tasks 表展开为手动任务面板（唯一数据源）。

    解耦架构下所有任务（web/executor 发起的 sync/rerun，以及 executor 对账
    补登记的 CLI 直启周期 source=cli）都统一进 tasks 表；本函数只读 tasks 表，
    不再枚举 checkpoint 线程。终态任务超过最近周期窗口收敛进历史页；
    进行中/排队/interrupted 始终展示。状态唯一来自 tasks.state（投影仅补
    commits 等详情字段，不覆盖任务状态）。
    """
    rows = db.execute(
        "SELECT id, kind, target, src, fresh, state, error, cycle_id, user, source, created_at "
        "FROM tasks WHERE kind IN ('sync','rerun') ORDER BY id DESC"
    ).fetchall()
    start = _parse_ts(window_start)
    tasks = []
    for row in rows:
        task = dict(row)
        created = _parse_ts(task["created_at"])
        if (
            start is not None
            and task["state"] not in _KEEP_STATES
            and created is not None
            and created < start
        ):
            continue
        cycle_id = task.get("cycle_id")
        status = _TASK_STATE_STATUS.get(task["state"], task["state"])
        commits = None
        if cycle_id:
            payload = projection.load_cycle(log_dir, cycle_id)
            branch = ((payload or {}).get("branch_results") or {}).get(task["target"])
            if branch:
                commits = len(branch.get("commits") or [])
        tasks.append(
            {
                "task_id": task["id"],
                "cycle_id": cycle_id,
                "target": task["target"],
                "kind": task["kind"],
                "state": task["state"],
                "status": status,
                "badge": _STATUS_LABELS.get(status, status),
                "commits": commits,
                "created_at": task["created_at"],
                "src": task["src"],
                "fresh": bool(task["fresh"]),
                "error": task["error"],
                "user": task["user"],
                "source": task["source"] or "web",
            }
        )
    tasks.sort(key=lambda t: t.get("created_at") or "", reverse=True)
    return tasks


def _window_start(log_dir: str) -> str | None:
    """最近完成周期扫描窗口起点；无完成周期/投影失败返回 None（不过滤手动任务）。"""
    cycle_id = projection.latest_completed_cycle(log_dir)
    if cycle_id is None:
        return None
    payload = projection.load_cycle(log_dir, cycle_id)
    if payload is None:
        return None
    return (payload.get("scan_window") or [None])[0]


def _cycle_task(db, cycle_id: str | None) -> dict | None:
    """最新 cron 周期任务行（kind=cycle），供自动区块展示运行状态。

    解耦后 cron 周期统一进 tasks 表（source=cron）；自动区块据此显示
    queued/running/interrupted 等状态徽章，与手动任务同一生命周期。
    无 cycle 任务行返回 None（历史周期 / 老库未迁移场景回退投影展示）。
    """
    row = db.execute(
        "SELECT id, kind, cycle_id, state, error, source, created_at "
        "FROM tasks WHERE kind='cycle' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    task = dict(row)
    status = _TASK_STATE_STATUS.get(task["state"], task["state"])
    task["status"] = status
    task["badge"] = _STATUS_LABELS.get(status, status)
    task["is_current"] = task["cycle_id"] == cycle_id
    return task


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
    db = request.app.state.db

    running = next((r for r in records if r.get("status") == "running"), None)
    if running is not None:
        # 运行中周期：仅显示"进行中"徽章，不渲染未完成周期详情（手动任务照常展示）
        return _render(request, branch_options=branch_options,
            user=user,
            csrf=csrf,
            cycles=records,
            agent_status="running",
            running_cycle_id=running.get("cycle_id"),
            auto_tasks=[],
            cycle_task=_cycle_task(db, running.get("cycle_id")),
            manual_tasks=_manual_tasks(db, log_dir, _window_start(log_dir)),
            op_error=op_error,
        )

    cycle_id = projection.latest_completed_cycle(log_dir)
    if cycle_id is None:
        return _render(request, branch_options=branch_options,
            user=user,
            csrf=csrf,
            cycles=records,
            agent_status=None,
            auto_tasks=[],
            cycle_task=_cycle_task(db, cycle_id),
            manual_tasks=_manual_tasks(db, log_dir, None),
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
            auto_tasks=[],
            cycle_task=_cycle_task(db, cycle_id),
            manual_tasks=_manual_tasks(db, log_dir, None),
            op_error=op_error,
        )

    abandoned = abandoned_keys(db, cycle_id)
    window_start = (payload.get("scan_window") or [None])[0]
    return _render(request, branch_options=branch_options,
        user=user,
        csrf=csrf,
        cycles=records,
        current_cycle_id=cycle_id,
        agent_status="failed" if payload.get("status") == "FAILED" else "done",
        cycle_status=payload.get("status"),
        payload=payload,
        auto_tasks=_auto_tasks(payload, cycle_id, abandoned),
        cycle_task=_cycle_task(db, cycle_id),
        manual_tasks=_manual_tasks(db, log_dir, window_start),
        abandoned_items=_abandoned_items(cycle_id, abandoned),
        op_error=op_error,
        **_build_todo(payload, abandoned),
    )
