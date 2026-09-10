from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bsa.graph.workflow import open_checkpointer, thread_config


def read_cycle_state(settings, cycle_id: str) -> dict[str, Any] | None:
    """从 checkpoint 读该周期最终 state；库/checkpoint 缺失返回 None。

    投影是只读操作：通过 open_checkpointer 打开 sqlite（WAL + busy_timeout，
    任务 3），get_tuple 取当前线程 checkpoint。channel_values 即 TaskState
    顶层 dict（已实测确认非嵌套），内含 pydantic 模型实例。
    """
    db = Path(settings.log_dir) / "state.sqlite3"
    if not db.exists():
        return None
    with open_checkpointer(str(db)) as cp:
        tup = cp.get_tuple(thread_config(cycle_id))
        if tup is None:
            return None
        return tup.checkpoint.get("channel_values") or None


def cycle_failure_text(settings, cycle_id: str, *, limit: int = 2000) -> str:
    """周期失败的一句话归因：节点级 errors 摘要，供任务行 tasks.error 落库。

    平台只读 tasks.error 展示任务失败原因（cycle.json 只存状态、不存原因），
    周期 FAILED/PARTIAL 时不写这里，UI 就只剩一个无因的「失败」。

    刻意不搬 ``bsa_web.failure`` 那套分支感知的散文式归因：分层方向是
    ``bsa_web → bsa``，反过来 import 会成环；且重复实现会让两处文案各自漂移。
    这里只给「哪些节点失败 + 去哪看」的诚实指针。
    """
    state = read_cycle_state(settings, cycle_id)
    errors = (state or {}).get("errors") or {}
    if not errors:
        # 投影缺失（checkpoint 已清 / 异常早退）时不留空，指向可下钻处。
        return "周期失败（未留存节点级原因，详见报告页步骤流）"
    parts = []
    for node, rec in sorted(errors.items()):
        # errors 来自 checkpoint 的 channel_values：这里是 ErrorRecord 实例，
        # 不是 projection_payload 那种已 model_dump 的 dict。两种都要认，
        # 否则会退化成 str(rec) 的 repr 堆（node='x' error='y' ts='z'）。
        if isinstance(rec, dict):
            detail = rec.get("error")
        else:
            detail = getattr(rec, "error", None)
        text = " ".join(str(detail or "").split())
        parts.append(f"{node}: {text}" if text else node)
    return f"周期失败：{'；'.join(parts)}"[:limit]


def _cycle_status(settings, cycle_id: str) -> str:
    # 惰性 import 避免 bsa.report.projection ↔ bsa.scheduler.cycle 循环依赖
    from bsa.scheduler.cycle import list_cycle_records

    record = next(
        (r for r in list_cycle_records(settings.log_dir) if r.get("cycle_id") == cycle_id),
        None,
    )
    return (record or {}).get("status", "running")


def projection_payload(state: dict[str, Any]) -> dict[str, Any]:
    """结构化输出：pydantic 模型 model_dump 为可 JSON 序列化的 dict。"""
    rep = state.get("report")
    return {
        "cycle_id": state.get("cycle_id"),
        "status": state.get("status"),
        "scan_window": state.get("scan_window"),
        "sources": state.get("sources") or [],
        "targets": state.get("targets") or [],
        # 源 → 目标配对。sources/targets 只是两个无配对列表，说不出「哪个业务分支
        # 喂哪个主分支」；报告与平台要呈现同步拓扑只能靠它。旧周期无此键 → 空列表。
        "topology": state.get("topology") or [],
        "detected_commits": [c.model_dump() for c in state.get("detected_commits") or []],
        "decisions": {
            sha: {t: c.model_dump() for t, c in per.items()}
            for sha, per in (state.get("decisions") or {}).items()
        },
        "branch_results": {
            t: b.model_dump() for t, b in (state.get("branch_results") or {}).items()
        },
        "action_required": (rep.action_required if rep is not None else []) or [],
        # 节点级失败原文（含 cherry-pick 的 git 诊断）。不导出它，平台就只剩一串
        # 无因的枚举可显示 —— 「失败但说不出为什么」正是本次复盘的主要痛点。
        "errors": {k: e.model_dump() for k, e in (state.get("errors") or {}).items()},
    }


def cycle_summary(payload: dict[str, Any]) -> dict[str, int]:
    """从投影 payload 算周期级检测/同步/跳过计数（工作台与周期概览共用）。

    - 检测：窗口内扫描到的 commit 总数；
    - 同步：已实际应用到目标分支的 distinct commit 数（cherry_pick OK/EMPTY）；
    - 跳过：未应用且对该分支判定为 AlreadyIncluded/OutOfScope 的 distinct commit 数。
    其余（ManualReview 待确认等）计入差值。
    """
    detected = len(payload.get("detected_commits") or [])
    synced_shas: set[str] = set()
    for branch in (payload.get("branch_results") or {}).values():
        for cr in branch.get("commits") or []:
            if cr.get("sha") and cr.get("cherry_pick") in ("OK", "EMPTY"):
                synced_shas.add(cr["sha"])
    skipped_shas: set[str] = set()
    decisions = payload.get("decisions") or {}
    for c in payload.get("detected_commits") or []:
        sha = c.get("sha")
        if not sha or sha in synced_shas:
            continue
        kinds = {(d.get("kind") or "") for d in (decisions.get(sha) or {}).values()}
        if kinds and kinds <= {"AlreadyIncluded", "OutOfScope"}:
            skipped_shas.add(sha)
    return {
        "cycle_detected": detected,
        "cycle_synced": len(synced_shas),
        "cycle_skipped": len(skipped_shas),
    }


def write_state_json(log_dir: str | Path, cycle_id: str, state: dict[str, Any]) -> Path:
    """周期/同步/重跑结束时把结构化投影写入 state.json（G11）。

    投影数据源改为结构化 state.json，平台只消费 JSON、不再解析 LangGraph
    checkpoint 内部结构（``get_tuple``/``channel_values``）。
    """
    payload = projection_payload(state)
    path = Path(log_dir) / cycle_id / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def read_projection_payload(settings, cycle_id: str) -> dict[str, Any] | None:
    """读投影：优先 state.json（结构化，G11），缺失/损坏回退 checkpoint 投影。

    兼容旧周期（无 state.json）：经 ``read_cycle_state`` 读 checkpoint 后
    ``projection_payload`` 转换。
    """
    path = Path(settings.log_dir) / cycle_id / "state.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            return data
    state = read_cycle_state(settings, cycle_id)
    if state is None:
        return None
    return projection_payload(state)
