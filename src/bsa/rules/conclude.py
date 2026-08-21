from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from bsa.domain.models import Conclusion4

CHERRY_PICK_RE = re.compile(
    r"\(cherry picked from commit ([0-9a-f]{6,40})\)",
    re.IGNORECASE,
)

INELIGIBLE_TARGET_TYPES = frozenset({"feature", "personal"})
NEED_SYNC_TARGET_TYPES = frozenset({"develop", "release", "fix"})


class CommitAnalysis(BaseModel):
    sha: str
    message: str
    changed_files: list[str]
    symbols: list[str]
    patch_text: str
    patch_id: str | None
    issue_ids: list[str]
    recognition_source: str
    source_branch: str
    source_branch_type: str
    homologous_section: str
    risk: Literal["low", "medium", "high"] | None = None


class TargetSnapshot(BaseModel):
    branch_name: str
    branch_type: str
    has_source_sha: bool = False
    target_commit_messages: list[str] = Field(default_factory=list)
    target_patch_ids: set[str] = Field(default_factory=set)
    target_issue_ids: set[str] = Field(default_factory=set)
    files_on_target: set[str] = Field(default_factory=set)
    symbols_on_target: dict[str, bool] = Field(default_factory=dict)
    file_similarity: float | None = None
    fix_clearly_missing: bool = False
    function_renamed: bool = False
    in_same_homologous_set: bool = True


def _sha_matches(candidate: str, source_sha: str) -> bool:
    candidate = candidate.lower()
    source_sha = source_sha.lower()
    return (
        candidate == source_sha
        or source_sha.startswith(candidate)
        or candidate.startswith(source_sha)
    )


def _check_cherry_pick(source: CommitAnalysis, target: TargetSnapshot) -> str | None:
    for message in target.target_commit_messages:
        match = CHERRY_PICK_RE.search(message)
        if match is not None and _sha_matches(match.group(1), source.sha):
            return f"目标分支含 cherry-pick 标记，对应源提交 {source.sha}。"
    return None


def _check_issue_match(source: CommitAnalysis, target: TargetSnapshot) -> str | None:
    overlap = set(source.issue_ids) & target.target_issue_ids
    if overlap:
        issue = sorted(overlap)[0]
        return f"目标分支存在相同单号 {issue} 的提交。"
    return None


def _check_patch_id_match(source: CommitAnalysis, target: TargetSnapshot) -> str | None:
    if source.patch_id and source.patch_id in target.target_patch_ids:
        return f"目标分支存在等价 patch-id {source.patch_id}。"
    return None


def _check_high_similarity(
    target: TargetSnapshot,
    *,
    similarity_high: float,
) -> str | None:
    if target.file_similarity is not None and target.file_similarity >= similarity_high:
        score = target.file_similarity
        return f"关联改动相似度 {score:.2f} 达到已包含阈值。"
    return None


def _target_role_eligible(target: TargetSnapshot) -> bool:
    if target.branch_type in INELIGIBLE_TARGET_TYPES:
        return False
    return target.branch_type in NEED_SYNC_TARGET_TYPES


def _release_line_not_synced(source: CommitAnalysis, target: TargetSnapshot) -> bool:
    return (
        source.source_branch_type == target.branch_type
        and target.branch_type in ("release", "fix")
    )


def _severity_gate_reason(source: CommitAnalysis, target: TargetSnapshot) -> str | None:
    source_type = source.source_branch_type
    target_type = target.branch_type
    risk = source.risk
    if target_type == "fix" and risk != "high":
        return (
            f"fix 分支仅接收 high 严重性 bug-fix（决策 41），"
            f"当前 risk={risk or 'unknown'}，不自动传播。"
        )
    if target_type == "develop" and source_type in ("release", "fix") and risk != "high":
        return (
            f"发布线（{source_type}）回灌 develop 仅限 high 严重性 bug-fix（决策 41），"
            f"当前 risk={risk or 'unknown'}，不自动传播。"
        )
    return None


def _anchor_files_exist(source: CommitAnalysis, target: TargetSnapshot) -> bool:
    if not source.changed_files:
        return False
    return any(path in target.files_on_target for path in source.changed_files)


def _anchor_symbols_ok(source: CommitAnalysis, target: TargetSnapshot) -> bool:
    if not source.symbols:
        return True
    return all(target.symbols_on_target.get(symbol, False) for symbol in source.symbols)


def _need_sync_confidence(source: CommitAnalysis, target: TargetSnapshot) -> str:
    has_ticket = bool(source.issue_ids) or source.recognition_source.startswith("machine:")
    has_symbols = bool(source.symbols) and _anchor_symbols_ok(source, target)
    if has_ticket and has_symbols and target.fix_clearly_missing:
        return "high"
    if has_symbols and target.fix_clearly_missing:
        return "medium"
    return "low"


def conclude_pair(
    source: CommitAnalysis,
    target: TargetSnapshot,
    *,
    similarity_high: float = 0.90,
    similarity_low: float = 0.50,
) -> Conclusion4:
    # Same commit SHA on target is authoritative — do this before any similarity work.
    if target.has_source_sha:
        return Conclusion4(
            kind="AlreadyIncluded",
            evidence=[f"目标分支已包含相同提交 {source.sha[:12]}。"],
            confidence="high",
        )

    cherry_evidence = _check_cherry_pick(source, target)
    if cherry_evidence is not None:
        return Conclusion4(
            kind="AlreadyIncluded",
            evidence=[cherry_evidence],
            confidence="high",
        )

    issue_evidence = _check_issue_match(source, target)
    if issue_evidence is not None:
        return Conclusion4(
            kind="AlreadyIncluded",
            evidence=[issue_evidence],
            confidence="high",
        )

    patch_evidence = _check_patch_id_match(source, target)
    if patch_evidence is not None:
        return Conclusion4(
            kind="AlreadyIncluded",
            evidence=[patch_evidence],
            confidence="high",
        )

    similarity_evidence = _check_high_similarity(target, similarity_high=similarity_high)
    if similarity_evidence is not None:
        return Conclusion4(
            kind="AlreadyIncluded",
            evidence=[similarity_evidence],
            confidence="low",
        )

    if not target.in_same_homologous_set:
        return Conclusion4(
            kind="OutOfScope",
            evidence=["源分支与目标分支不在同一同源产品线集合。"],
            confidence="high",
        )

    if target.branch_type in INELIGIBLE_TARGET_TYPES:
        return Conclusion4(
            kind="OutOfScope",
            evidence=[f"目标分支类型 {target.branch_type} 不是需要同步的目标。"],
            confidence="high",
        )

    if not _target_role_eligible(target):
        return Conclusion4(
            kind="OutOfScope",
            evidence=[f"目标分支类型 {target.branch_type} 不在维护矩阵范围内。"],
            confidence="high",
        )

    if source.source_branch == target.branch_name:
        return Conclusion4(
            kind="OutOfScope",
            evidence=["目标分支与源分支相同。"],
            confidence="high",
        )

    if _release_line_not_synced(source, target):
        return Conclusion4(
            kind="OutOfScope",
            evidence=[
                f"{target.branch_type} 分支之间不互同步（决策 41，各发布线独立维护）。"
            ],
            confidence="high",
        )

    if not _anchor_files_exist(source, target):
        return Conclusion4(
            kind="OutOfScope",
            evidence=["源提交关联文件在目标分支上均不存在。"],
            confidence="high",
        )

    if target.function_renamed:
        return Conclusion4(
            kind="ManualReview",
            evidence=["关联文件存在，但目标分支上函数/符号疑似已重命名。"],
            confidence="medium",
        )

    if (
        target.file_similarity is not None
        and similarity_low <= target.file_similarity < similarity_high
    ):
        return Conclusion4(
            kind="ManualReview",
            evidence=[
                f"相似度 {target.file_similarity:.2f} 处于灰区 "
                f"（{similarity_low:.2f}～{similarity_high:.2f}）。"
            ],
            confidence="medium",
        )

    if target.branch_type == "unknown":
        return Conclusion4(
            kind="ManualReview",
            evidence=["目标分支类型未知。"],
            confidence="low",
        )

    if not _anchor_symbols_ok(source, target):
        return Conclusion4(
            kind="ManualReview",
            evidence=["关联符号在目标分支上未全部存在。"],
            confidence="medium",
        )

    if not target.fix_clearly_missing:
        return Conclusion4(
            kind="ManualReview",
            evidence=["目标分支上修复是否缺失无法判定。"],
            confidence="low",
        )

    gate_reason = _severity_gate_reason(source, target)
    if gate_reason is not None:
        return Conclusion4(
            kind="ManualReview",
            evidence=[gate_reason],
            confidence="high",
        )

    anchor_bits: list[str] = []
    matched_files = [
        path for path in source.changed_files if path in target.files_on_target
    ]
    if matched_files:
        anchor_bits.append(f"锚定文件 {', '.join(matched_files)}")
    if source.symbols:
        anchor_bits.append(f"符号 {', '.join(source.symbols)}")
    evidence = [
        "目标分支缺少该修复，且存在明确的文件/函数锚定。",
        "；".join(anchor_bits) + "。" if anchor_bits else "目标分支上存在锚定点。",
    ]
    if source.issue_ids:
        evidence.append(f"源提交单号 {', '.join(source.issue_ids)}。")
    return Conclusion4(
        kind="NeedSync",
        evidence=evidence,
        confidence=_need_sync_confidence(source, target),
    )
