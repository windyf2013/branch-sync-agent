"""读取引擎步骤进度（``<log_dir>/<cycle_id>/progress.jsonl``），供任务页渲染。

进度文件由引擎 ``bsa.graph.progress.ProgressJournal`` 追加写入，每条一行 JSON。
本模块容忍读到半行（追加写与并发读并存），把 start/end 配对成已完成步骤并
计算耗时，未配对的 start 即当前进行中步骤。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_progress(log_dir: str | Path, cycle_id: str) -> list[dict[str, Any]]:
    """返回步骤列表：已完成步骤在前，进行中步骤（如有）在末尾。

    每条记录含：``step``（中文名）、``status``、``duration_ms``（进行中为 None）、
    ``running``（bool）、``model`` / ``target`` / ``sha``（若引擎写入）。
    文件缺失/损坏 → 空列表（平台优雅降级，不报错）。
    """
    path = Path(log_dir) / cycle_id / "progress.jsonl"
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []

    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            # 截断的半行（引擎正在写），忽略；下一轮刷新时读完整
            continue

    starts: dict[str, dict[str, Any]] = {}
    steps: list[dict[str, Any]] = []
    running: dict[str, Any] | None = None

    for r in records:
        phase = r.get("phase")
        # 步骤身份：同一 node 连续 start/end 配对；用 node 作键足够（引擎串行执行）
        if phase == "start":
            starts[r["node"]] = r
        elif phase == "end":
            start = starts.pop(r["node"], None)
            entry = {
                "step": r.get("step", r["node"]),
                "status": r.get("status"),
                "model": r.get("model"),
                "target": r.get("target"),
                "sha": r.get("sha"),
                "running": False,
                "duration_ms": _duration_ms(start, r),
            }
            steps.append(entry)

    # 尚未配对的 start = 进行中
    if starts:
        # 取最后一条未闭合的 start（理论上引擎串行只可能有一个）
        r = list(starts.values())[-1]
        running = {
            "step": r.get("step", r["node"]),
            "status": None,
            "model": r.get("model"),
            "target": r.get("target"),
            "sha": r.get("sha"),
            "log_path": r.get("log_path"),
            "running": True,
            "duration_ms": None,
        }
        steps.append(running)

    return steps


def _duration_ms(start: dict[str, Any] | None, end: dict[str, Any]) -> int | None:
    if start is None:
        return end.get("duration_ms")
    ts_s = start.get("ts")
    ts_e = end.get("ts")
    if isinstance(ts_s, (int, float)) and isinstance(ts_e, (int, float)):
        return int((ts_e - ts_s) * 1000)
    return end.get("duration_ms")
