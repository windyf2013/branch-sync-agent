from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel


class CommitInfo(BaseModel):
    sha: str
    message: str
    author: str
    committed_at: str
    changed_files: list[str]
    patch_text: str
    symbols: list[str]
    patch_id: str | None
    issue_ids: list[str]
    source_branch: str
    homologous_section: str
    diff_stat: dict[str, int] | None = None


class SyncDecision(BaseModel):
    sha: str
    is_bug_fix: bool
    reason: str | None
    recognition_source: str
    needs_agent: bool
    risk: Literal["low", "medium", "high"] | None = None


class Conclusion4(BaseModel):
    kind: Literal["NeedSync", "AlreadyIncluded", "ManualReview", "OutOfScope"]
    evidence: list[str]
    confidence: Literal["high", "medium", "low"]


class ConflictResolution(BaseModel):
    files: list[str]
    diff: str
    agent_reason: str


class CherryPickResult(BaseModel):
    status: Literal["OK", "CONFLICT", "EMPTY", "FAILED"]
    conflict_files: list[str] = []


class BuildOutcome(BaseModel):
    model: str
    status: Literal["OK", "FAILED", "SKIPPED"]
    log_path: str | None
    errors: list[str]
    agent_attempts: int
    fix_diff: str | None
    reason: str | None = None


class CommitResult(BaseModel):
    sha: str
    cherry_pick: Literal["OK", "CONFLICT", "FAILED", "EMPTY"]
    conflict_resolution: ConflictResolution | None
    build: dict[str, BuildOutcome]
    # 冲突解决失败原因（如冲突文件疑似二进制无法安全自动解决）；随 commit 落投影，
    # 供目标详情/commit 详情透出，避免转人工原因只藏在周期级 action_required 里。
    resolution_error: str | None = None


class BranchResult(BaseModel):
    target_branch: str
    worktree_path: str
    status: Literal["SUCCESS", "PARTIAL", "FAILED", "MANUAL"]
    commits: list[CommitResult]
    patch_path: str | None
    stop_reason: str | None
    baseline: dict[str, BuildOutcome] | None = None


class ErrorRecord(BaseModel):
    node: str
    error: str
    ts: str


class Report(BaseModel):
    cycle_id: str
    html_path: Path
    summary: dict[str, Any]
    action_required: list[dict[str, Any]]
    decisions_json_path: Path
