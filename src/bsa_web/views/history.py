"""历史报告页：所有任务（手动 sync/rerun + cron 周期）的超期归档列表。

工作台显示「超期前」（活动 + 最近周期窗口内）的任务，历史报告只负责归档显示
超期后的终态任务。数据源是 tasks 表（所有任务的唯一真相源），不按结论筛选——
「待同步/已包含」等操作型结论对归档没有意义，已移除。

「主任务」定义（用户定夺：cycle 去重 + rerun 归并，主任务取最终结果）：
- 手动 sync：每次手动发起是一个主任务，独立成行；
- cron cycle：同一 cycle_id 的多次失败尝试折叠成一行（取最后一次尝试的终态）；
- rerun：不是单独任务，归并到同 target 最近的主任务，主任务状态取 rerun 的最终结果。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from bsa_web import projection
from bsa_web.auth import make_csrf, require_login

router = APIRouter(prefix="/history", tags=["history"])

# 归档只显示终态；活动/中断任务保留在工作台（可续跑/可操作），不进历史。
_ARCHIVED_STATES = ("succeeded", "failed", "cancelled")

_STATE_LABELS = {
    "succeeded": "成功",
    "failed": "失败",
    "cancelled": "已取消",
}

_KIND_LABELS = {
    "cycle": "周期",
    "sync": "同步",
    "rerun": "重跑",
}


def _parse_ts(value: str | None) -> datetime | None:
    """ISO 时间解析并归一化为无时区 UTC；失败返回 None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _shas_count(task: dict) -> int | None:
    """sync 任务 shas JSON 的 commit 数；无/损坏返回 None。"""
    shas = task.get("shas")
    if not shas:
        return None
    try:
        return len(json.loads(shas))
    except (TypeError, json.JSONDecodeError):
        return None


def _cycle_detected_info(log_dir: str, cycle_id: str) -> tuple[list[str], int]:
    """从 cycle 的 state.json 提取源分支列表与检测 commit 数（无文件返回空）。"""
    path = Path(log_dir) / cycle_id / "state.json"
    if not path.is_file():
        return [], 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], 0
    detected = data.get("detected_commits") or []
    sources = data.get("sources") or sorted(
        {c.get("source_branch") for c in detected if c.get("source_branch")}
    )
    return list(sources), len(detected)


def _archived_tasks(db, log_dir: str) -> list[dict]:
    """读 tasks 表，构建「主任务」归档列表（cycle 去重 + rerun 归并 + sync 独立）。"""
    start = _parse_ts(projection.window_start(log_dir))
    if start is None:
        return []
    rows = db.execute(
        "SELECT id, kind, target, src, state, cycle_id, user, source, "
        "created_at, finished_at, shas "
        "FROM tasks WHERE state IN ('succeeded','failed','cancelled') ORDER BY id"
    ).fetchall()

    # 分桶：cycle 按 cycle_id 折叠；sync 独立；rerun 归并到同 target 最近 sync。
    cycles: dict[str, dict] = {}   # cycle_id -> 最后一次尝试
    syncs: list[dict] = []         # 独立主任务（按 id 升序）
    reruns: list[dict] = []
    for row in rows:
        task = dict(row)
        created = _parse_ts(task["created_at"])
        if created is None or created >= start:
            continue
        task["created_dt"] = created
        if task["kind"] == "cycle":
            cid = task.get("cycle_id")
            if cid:
                # 同一 cycle 的多次尝试折叠，取 id 最大（最后尝试）的终态
                if cid not in cycles or task["id"] > cycles[cid]["id"]:
                    cycles[cid] = task
        elif task["kind"] == "sync":
            syncs.append(task)
        elif task["kind"] == "rerun":
            reruns.append(task)

    # rerun 归并：找到同 target 且在该 rerun 之前发起的最近 sync，覆盖其最终结果。
    for rerun in reruns:
        target = rerun.get("target")
        candidates = [s for s in syncs if s.get("target") == target and s["id"] < rerun["id"]]
        if not candidates:
            continue
        parent = candidates[-1]  # 最近的 sync 主任务
        parent["state"] = rerun["state"]
        parent["finished_at"] = rerun["finished_at"]
        parent["cycle_id"] = rerun.get("cycle_id")  # 跳转到最终结果的详情
        parent["rerun_of"] = True

    tasks: list[dict] = []

    # cycle 主任务：源分支/commit 从 state.json 提取。
    for cid, task in sorted(cycles.items(), key=lambda kv: kv[1]["id"]):
        sources, detected = _cycle_detected_info(log_dir, cid)
        tasks.append(
            {
                "task_id": task["id"],
                "kind": "cycle",
                "kind_label": _KIND_LABELS["cycle"],
                "cycle_id": cid,
                "target": task.get("target"),
                "src": "、".join(sources) if sources else "—",
                "commits": detected if detected else None,
                "state": task["state"],
                "badge": _STATE_LABELS.get(task["state"], task["state"]),
                "user": task["user"],
                "created_at": task["created_at"],
                "finished_at": task["finished_at"],
                "detail_url": f"/cycle/{cid}",
            }
        )

    # sync 主任务（含被 rerun 覆盖最终结果的）：源分支=src，commit=shas 数。
    for task in syncs:
        cycle_id = task.get("cycle_id")
        target = task.get("target")
        detail_url = (
            f"/task/{cycle_id}/{target}" if cycle_id and target else f"/tasks/{task['id']}"
        )
        tasks.append(
            {
                "task_id": task["id"],
                "kind": "sync",
                "kind_label": _KIND_LABELS["sync"],
                "cycle_id": cycle_id,
                "target": target,
                "src": task.get("src") or "—",
                "commits": _shas_count(task),
                "state": task["state"],
                "badge": _STATE_LABELS.get(task["state"], task["state"]),
                "user": task["user"],
                "created_at": task["created_at"],
                "finished_at": task["finished_at"],
                "detail_url": detail_url,
            }
        )

    tasks.sort(key=lambda t: t["created_at"] or "", reverse=True)
    return tasks


def _render(request: Request, **extra) -> object:
    return request.app.state.templates.TemplateResponse(request, "history.html", extra)


@router.get("")
def history(request: Request, user: Annotated[dict, Depends(require_login)]):
    settings = request.app.state.settings
    csrf = make_csrf(settings.secret_key, user["username"])
    tasks = _archived_tasks(request.app.state.db, settings.log_dir)
    return _render(request, user=user, csrf=csrf, tasks=tasks)
