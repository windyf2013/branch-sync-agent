from __future__ import annotations

from pathlib import Path
from typing import Any

from bsa.graph.workflow import open_checkpointer, thread_config
from bsa.scheduler.cycle import list_cycle_records


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


def _cycle_status(settings, cycle_id: str) -> str:
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
        "detected_commits": [c.model_dump() for c in state.get("detected_commits") or []],
        "decisions": {
            sha: {t: c.model_dump() for t, c in per.items()}
            for sha, per in (state.get("decisions") or {}).items()
        },
        "branch_results": {
            t: b.model_dump() for t, b in (state.get("branch_results") or {}).items()
        },
        "action_required": (rep.action_required if rep is not None else []) or [],
    }
