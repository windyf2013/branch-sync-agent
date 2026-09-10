from __future__ import annotations

from typing import Any, TypedDict

from bsa.domain.models import (
    BranchResult,
    CommitInfo,
    Conclusion4,
    ErrorRecord,
    Report,
    SyncDecision,
)


class TaskState(TypedDict):
    """LangGraph state contract (decision 31). Outer TypedDict, inner pydantic."""

    cycle_id: str
    scan_window: tuple[str, str]
    branch_md_version: str
    detected_commits: list[CommitInfo]
    classifications: dict[str, SyncDecision]
    decisions: dict[str, dict[str, Conclusion4]]
    sources: list[str]
    targets: list[str]
    # 源 → 目标配对（``[{"section", "sources", "targets"}]``）。sources/targets 是
    # 两个扁平的无配对列表，光有它们报告说不出「哪个业务分支喂哪个主分支」。
    # matrix 本身是 GraphContext 上的内存对象，invoke 结束即丢弃，所以必须在此
    # 显式声明并投影 —— 未声明的键会被 LangGraph 静默丢弃（无 warning）。
    topology: list[dict[str, Any]]
    batches: dict[str, list[str]]
    build_models: dict[str, list[str]]
    current_target: str | None
    current_commit: str | None
    branch_results: dict[str, BranchResult]
    status: str
    errors: dict[str, ErrorRecord]
    report: Report | None
