"""步骤级进度日志：把每个工作流节点的一次执行写成 JSONL，供平台渲染步骤清单。

为什么不用 run.log / cycle.json：那二者只由 cron 周期（``scheduler/cycle.py``）
写入，手动 ``bsa sync`` / ``bsa rerun`` 路径根本不产生，无法统一驱动 UI。
本模块挂在 ``node_wrapper``（唯一 choke point）上，cron 与手动两条路径都经它
包装真实节点，故一处埋点两侧都覆盖。

契约：
- 写失败绝不影响任务本身（``ProgressJournal.write`` 吞掉一切异常）；
- 目录自建（手动同步只 mkdir(log_dir)，cycle 子目录由首次写入创建）；
- 追加式 JSONL，读端容忍读到半行（追加写与并发读并存）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# 节点 → 中文步骤名（平台 UI 展示用）。未列出的节点名原样使用。
NODE_LABELS: dict[str, str] = {
    "detect_commits": "代码迁出",
    "sync_decision": "同步判定",
    "prepare_worktree": "建立 worktree",
    "baseline_build": "基线编译",
    "cherry_pick": "cherry-pick",
    "resolve_conflict": "解决冲突",
    "build": "编译",
    "fix_build": "修复重编译",
    "generate_patch": "生成 patch",
    "report": "生成报告",
    "fail_fast": "失败关联判定",
}


class ProgressJournal:
    """追加写 ``<log_dir>/<cycle_id>/progress.jsonl``；任何写错误都被吞掉。"""

    def __init__(self, log_dir: str | Path) -> None:
        self.log_dir = Path(log_dir)

    def write(self, record: dict[str, Any]) -> None:
        try:
            cycle_id = record.get("cycle_id")
            if not cycle_id:
                return
            path = self.log_dir / str(cycle_id) / "progress.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 — 进度写失败绝不拖累节点执行
            return
