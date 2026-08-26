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
