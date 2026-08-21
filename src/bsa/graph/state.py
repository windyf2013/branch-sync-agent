from __future__ import annotations

from typing import TypedDict

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
    batches: dict[str, list[str]]
    current_target: str | None
    current_commit: str | None
    branch_results: dict[str, BranchResult]
    status: str
    errors: dict[str, ErrorRecord]
    report: Report | None
