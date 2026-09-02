"""工作台首页路由：A 区任务中心（自动/手动分区）+ Agent 状态 + B 区快速操作。

每次请求实时读投影不缓存。自动任务 = 最新周期投影 branch_results 展开；
手动任务 = tasks 表（kind=sync/rerun）合并对应 manual cycle 投影；所有操作
收敛到任务详情页 /task/{cycle_id}/{target}。
"""

import json
import re
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from bsa_web import failure, projection
from bsa_web.auth import make_csrf, require_login
from bsa_web.branches import rcios_branch_names, rcios_branch_sections
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
    "CANCELLED": "已取消",
}

# tasks.state → 展示状态（与投影 branch_results.status 语义对齐）
_TASK_STATE_STATUS = {
    "queued": "QUEUED",
    "running": "RUNNING",
    "succeeded": "SUCCESS",
    "failed": "FAILED",
    "interrupted": "INTERRUPTED",
    "cancelled": "CANCELLED",
}

_ACTIVE_STATES = ("queued", "running")
# interrupted 任务需保留展示（用户据此续跑），窗口过滤时视作活动。
_KEEP_STATES = _ACTIVE_STATES + ("interrupted",)


def _branch_options(settings) -> list[str]:
    """RCIOS 仓库分支列表（branch.md），供源/目标分支下拉框；缺失返回空。"""
    if not settings.branch_file:
        return []
    return rcios_branch_names(settings.branch_file)


def _branch_sections(settings) -> dict[str, str]:
    """分支 → 产品线 映射（branch.md），供 cron 任务按产品线分组/过滤。"""
    if not settings.branch_file:
        return {}
    return rcios_branch_sections(settings.branch_file)


def _section_options(sections: dict) -> list[str]:
    """去重保序的产品线列表（过滤下拉用）。"""
    seen: list[str] = []
    for s in sections.values():
        if s and s not in seen:
            seen.append(s)
    return seen


def _section_short(section: str) -> str:
    """产品线短名：'1.1 4.34 主分支' → '4.34'（剥掉编号与角色后缀）。

    主/业务 section 共属同一产品线，短名取公共前缀（如 4.34），供工作台分组。
    """
    name = re.sub(r"^\d+(\.\d+)*\s*", "", section or "").strip()
    for suffix in ("主分支", "业务分支", "产品分支", "产品主线分支", "分支"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name.strip()


def _active_manual_count(*task_lists: list[dict]) -> int:
    """统计排队/执行中的手动任务数（含关联子行）。"""
    active = 0
    for tasks in task_lists:
        for t in tasks:
            if t.get("state") in ("queued", "running"):
                active += 1
    return active


def _done_count(db) -> int:
    """「已完成」卡 = 历史累计成功任务数（succeeded），非当前周期分支数。

    空周期时当前周期无 SUCCESS 分支，若用当前分支数则「已完成」恒 0，
    让维护者误以为系统从没成功过。改为历史累计成功，形成闭环。
    """
    row = db.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE state='succeeded'"
    ).fetchone()
    return row["n"] if row is not None else 0


def _auto_failed_targets(auto_tasks: list[dict]) -> set[str]:
    """自动周期失败/停批/待处理的目标分支集合，供手动重跑行标注关联关系。"""
    return {
        t["target"]
        for t in auto_tasks
        if t.get("status") in ("FAILED", "PARTIAL", "MANUAL")
    }


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


def _auto_tasks(
    payload: dict, cycle_id: str, abandoned: set, sections: dict | None = None
) -> list[dict]:
    """最新周期投影 branch_results 展开为自动任务面板（分支粒度）。

    分支级放弃（sha=None）标记面板为已放弃（徽章 + 恢复入口），不再提供推送；
    commit 级放弃仅影响待办区过滤，不改面板状态展示。
    sections：branch → 产品线 映射，供按产品线分组/过滤 cron 任务。
    """
    sections = sections or {}
    decisions = payload.get("decisions") or {}
    detected = payload.get("detected_commits") or []
    detected_shas = {c.get("sha") for c in detected if c.get("sha")}
    tasks = []
    for branch in (payload.get("branch_results") or {}).values():
        target = branch.get("target_branch")
        status = branch.get("status") or "UNKNOWN"
        commits = branch.get("commits") or []
        synced = sum(
            1 for cr in commits if cr.get("cherry_pick") in ("OK", "EMPTY")
        )
        skipped = sum(
            1
            for sha in detected_shas
            if (decisions.get(sha) or {}).get(target, {}).get("kind")
            in ("AlreadyIncluded", "OutOfScope")
        )
        reasons = failure.failure_summary(payload, target)
        tasks.append(
            {
                "cycle_id": cycle_id,
                "target": target,
                "status": status,
                "badge": _STATUS_LABELS.get(status, status),
                "reason": reasons[0] if reasons else "",
                "commits": len(commits),
                "synced": synced,
                "skipped": skipped,
                "_applied_shas": [
                    cr.get("sha")
                    for cr in commits
                    if cr.get("sha") and cr.get("cherry_pick") in ("OK", "EMPTY")
                ],
                "worktree_path": branch.get("worktree_path"),
                "patch_path": branch.get("patch_path"),
                "abandoned": (target, None) in abandoned,
                "user": "system",
                "source": "cron",
                "section": sections.get(target, ""),
                "kind": "cycle",
                "created_at": "",
            }
        )
    return tasks


def _cycle_commit_summary(payload: dict, auto_tasks: list[dict]) -> dict:
    """周期级检测/同步/跳过摘要（上下文条展示）——委托共享实现。"""
    return projection.cycle_summary(payload)


def _task_commits(task: dict) -> int | None:
    """从任务行取 commit 数（P2-5）：sync 用 shas 长度；rerun 用引擎落库 commits。"""
    if task["kind"] == "sync" and task.get("shas"):
        try:
            return len(json.loads(task["shas"]))
        except (TypeError, json.JSONDecodeError):
            return None
    return task.get("commits")


def _manual_tasks(
    db, log_dir: str, cutoff: datetime | None, sections: dict | None = None
) -> list[dict]:
    """tasks 表展开为手动任务面板（唯一数据源）。

    解耦架构下所有任务（web/executor 发起的 sync/rerun，以及 executor 对账
    补登记的 CLI 直启周期 source=cli）都统一进 tasks 表；本函数只读 tasks 表，
    不再枚举 checkpoint 线程。终态任务早于 ``cutoff``（最近周期启动时刻，
    已归一化为 UTC-naive）收敛进历史页；进行中/排队/interrupted 始终展示。
    状态唯一来自 tasks.state（投影仅补 commits 等详情字段，不覆盖任务状态）。
    sections：branch → 产品线 映射，供统一任务表按产品线过滤。
    """
    sections = sections or {}
    rows = db.execute(
        "SELECT id, kind, target, src, fresh, state, error, cycle_id, user, source, "
        "created_at, shas, commits FROM tasks WHERE kind IN ('sync','rerun') ORDER BY id DESC"
    ).fetchall()
    start = cutoff
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
        # P2-5：commit 数从任务行取（sync 用 shas 长度 / rerun 用引擎落库的 commits），
        # 不再为每个手动任务 spawn `bsa report` 子进程（威胁首页 <3s）。
        commits = _task_commits(task)
        target = task["target"]
        tasks.append(
            {
                "task_id": task["id"],
                "cycle_id": cycle_id,
                "target": target,
                "kind": task["kind"],
                "state": task["state"],
                "status": status,
                "badge": _STATUS_LABELS.get(status, status),
                "commits": commits,
                "created_at": task["created_at"],
                "src": task["src"],
                "fresh": bool(task["fresh"]),
                "error": task["error"],
                "reason": (task["error"] or "").strip(),
                "user": task["user"],
                "source": task["source"] or "web",
                "section": sections.get(target, ""),
            }
        )
    tasks.sort(key=lambda t: t.get("created_at") or "", reverse=True)
    return tasks


def _shorten_sections(tasks: list[dict]) -> list[dict]:
    """把每行的 section 替换为短名，并附 section_short 供展示。"""
    for t in tasks:
        t["section_short"] = _section_short(t.get("section") or "")
    return tasks


def _link_rerun_children(
    auto_tasks: list[dict], manual_tasks: list[dict]
) -> tuple[list[dict], list[dict]]:
    """把命中自动失败分支的手动重跑任务作为子行挂到对应自动任务下。

    返回 (auto_tasks, standalone_manual)：
    - auto_tasks：每个元素新增 children 列表（关联的手动重跑任务）；
    - standalone_manual：未命中关联的手动任务（同步/重跑）独立成行。
    """
    failed_targets = _auto_failed_targets(auto_tasks)
    by_target = {t["target"]: t for t in auto_tasks}
    standalone = []
    for t in manual_tasks:
        if t.get("kind") == "rerun" and t.get("target") in failed_targets:
            parent = by_target[t["target"]]
            parent.setdefault("children", []).append(t)
        else:
            standalone.append(t)
    return auto_tasks, standalone


def _cycle_task(db, cycle_id: str | None) -> dict | None:
    """最新 cron 周期任务行（kind=cycle），供自动区块展示运行状态。

    解耦后 cron 周期统一进 tasks 表（source=cron）；自动区块据此显示
    queued/running/interrupted 等状态徽章，与手动任务同一生命周期。
    无 cycle 任务行返回 None（历史周期 / 老库未迁移场景回退投影展示）。
    """
    # 优先取指定 cycle_id 的活动/终态任务行；无 cycle_id 时回退最新任意 cycle 行
    # （历史/老库场景），此时 is_current 为 False 仅作展示。
    sql = (
        "SELECT id, kind, cycle_id, state, error, source, created_at "
        "FROM tasks WHERE kind='cycle'"
    )
    params: list[str] = []
    if cycle_id:
        sql += " AND cycle_id=?"
        params.append(cycle_id)
    sql += " ORDER BY id DESC LIMIT 1"
    row = db.execute(sql, params).fetchone()
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
    sections = _branch_sections(settings)
    db = request.app.state.db
    section_options = _section_options(sections)

    running = next((r for r in records if r.get("status") == "running"), None)
    if running is not None:
        # 运行中周期：仅显示"进行中"徽章，不渲染未完成周期详情（手动任务照常展示）
        auto_tasks: list[dict] = []
        manual_rows = _manual_tasks(db, log_dir, projection.latest_cycle_start(log_dir), sections)
        return _render(request, branch_options=branch_options,
            user=user,
            csrf=csrf,
            agent_status="running",
            running_cycle_id=running.get("cycle_id"),
            auto_tasks=_shorten_sections(auto_tasks),
            standalone_manual=_shorten_sections(manual_rows),
            section_options=section_options,
            cycle_task=_cycle_task(db, running.get("cycle_id")),
            active_manual=_active_manual_count(manual_rows),
            done_count=_done_count(db),
            op_error=op_error,
        )

    cycle_id = projection.latest_completed_cycle(log_dir)
    if cycle_id is None:
        auto_tasks = []
        manual_rows = _manual_tasks(db, log_dir, None, sections)
        return _render(request, branch_options=branch_options,
            user=user,
            csrf=csrf,
            agent_status=None,
            auto_tasks=_shorten_sections(auto_tasks),
            standalone_manual=_shorten_sections(manual_rows),
            section_options=section_options,
            cycle_task=_cycle_task(db, cycle_id),
            active_manual=_active_manual_count(manual_rows),
            done_count=_done_count(db),
            op_error=op_error,
        )

    payload = projection.load_cycle(log_dir, cycle_id)
    if payload is None:
        # 子进程投影失败（returncode 非 0）→ 平台容错，提示不可用
        auto_tasks = []
        manual_rows = _manual_tasks(db, log_dir, projection.latest_cycle_start(log_dir), sections)
        return _render(request, branch_options=branch_options,
            user=user,
            csrf=csrf,
            agent_status="unavailable",
            current_cycle_id=cycle_id,
            auto_tasks=_shorten_sections(auto_tasks),
            standalone_manual=_shorten_sections(manual_rows),
            section_options=section_options,
            cycle_task=_cycle_task(db, cycle_id),
            active_manual=_active_manual_count(manual_rows),
            done_count=_done_count(db),
            op_error=op_error,
        )

    abandoned = abandoned_keys(db, cycle_id)
    todo = _build_todo(payload, abandoned)
    auto_tasks = _auto_tasks(payload, cycle_id, abandoned, sections)
    manual_tasks = _manual_tasks(db, log_dir, projection.latest_cycle_start(log_dir), sections)
    auto_tasks, standalone_manual = _link_rerun_children(auto_tasks, manual_tasks)
    active_manual = _active_manual_count(standalone_manual) + sum(
        _active_manual_count(t.get("children") or []) for t in auto_tasks
    )
    return _render(request, branch_options=branch_options,
        user=user,
        csrf=csrf,
        current_cycle_id=cycle_id,
        agent_status="failed" if payload.get("status") in ("FAILED", "PARTIAL") else "done",
        cycle_status=payload.get("status"),
        cycle_status_label=_STATUS_LABELS.get(
            payload.get("status") or "UNKNOWN", "未知"
        ),
        payload=payload,
        auto_tasks=_shorten_sections(auto_tasks),
        standalone_manual=_shorten_sections(standalone_manual),
        section_options=section_options,
        cycle_task=_cycle_task(db, cycle_id),
        abandoned_items=_abandoned_items(cycle_id, abandoned),
        active_manual=active_manual,
        decisions_json=json.dumps(payload.get("decisions") or {}, ensure_ascii=False),
            done_count=_done_count(db),
        **_cycle_commit_summary(payload, auto_tasks),
        op_error=op_error,
        **todo,
    )
