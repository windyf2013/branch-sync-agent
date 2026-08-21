from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any

from bsa.agents.base import LLMClient
from bsa.agents.build_agent import BuildAgent
from bsa.agents.conflict import ConflictAgent
from bsa.agents.sync_decision import SyncDecisionAgent
from bsa.build.runner import BuildRunner
from bsa.config.settings import Settings
from bsa.domain.models import (
    BranchResult,
    BuildOutcome,
    CommitInfo,
    CommitResult,
    Conclusion4,
    ErrorRecord,
    Report,
    SyncDecision,
)
from bsa.executor.base import CommandExecutor
from bsa.executor.exceptions import InfrastructureError
from bsa.git.service import GitService
from bsa.rules import (
    DecisionRules,
    HomologousSet,
    TargetSnapshot,
    build_matrix,
    classify_commit,
    conclude_pair,
    parse_branch_md,
    resolve_branch_type,
)
from bsa.rules.conclude import CommitAnalysis
from bsa.rules.paths import is_public_file
from bsa.rules.safety import SafetyEnforcer
from bsa.rules.snapshot import build_target_snapshot, extract_symbols

_CST = timezone(timedelta(hours=8))


@dataclass
class GraphContext:
    """All lower-layer services injected into the graph (workflow-built in batch 3.2).

    ``matrix`` is populated by ``detect_commits`` and cached per cycle; the
    worktree-scoped git services are cached per worktree path in
    ``worktree_gits`` (one GitService per target worktree), with
    ``worktree_path`` tracking the current target's path. Rules functions are
    injectable so tests can mock the service layer without real LLM/git/build
    calls.
    """

    settings: Settings
    executor: CommandExecutor
    git: GitService
    runner: BuildRunner
    sync_decision_agent: SyncDecisionAgent
    conflict_agent: ConflictAgent
    build_agent: BuildAgent
    safety: SafetyEnforcer
    decision_rules: DecisionRules
    llm: LLMClient | None = None
    classify: Callable = classify_commit
    conclude: Callable = conclude_pair
    build_snapshot: Callable = build_target_snapshot
    is_public_file: Callable[[str], bool] | None = None
    matrix: list[HomologousSet] | None = None
    worktree_path: Path | None = None
    worktree_gits: dict[str, GitService] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.is_public_file is None:
            path_rules = (self.decision_rules.classify or {}).get("path_rules") or {}
            public_dirs = list(path_rules.get("public_dirs", []))
            self.is_public_file = lambda path: is_public_file(path, public_dirs)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def node_wrapper(node: Callable, ctx: GraphContext | None = None) -> Callable:
    """Wrap a graph node so an exception becomes an errors record, never bubbles.

    On failure the wrapped node returns ``{'errors': ..., 'status': 'FAILED'}``
    which routes the graph to its report/end path (decision 18, node boundary).
    """
    node_name = getattr(node, "__name__", type(node).__name__)
    if ctx is not None:
        node = partial(node, ctx=ctx)

    def wrapped(state: dict) -> dict:
        try:
            return node(state)
        except Exception as exc:  # noqa: BLE001 — node boundary must never bubble
            errors = dict(state.get("errors") or {})
            errors[node_name] = ErrorRecord(node=node_name, error=str(exc), ts=_now_iso())
            return {"errors": errors, "status": "FAILED"}

    return wrapped


def default_window(now: datetime | None = None) -> tuple[str, str]:
    """Return ``(since, until)`` for the last completed 22:00~22:00 window (决策 38).

    Example: at 2026-08-21 12:00+08 → since=2026-08-19 22:00, until=2026-08-20 22:00.
    """
    if now is None:
        now = datetime.now(_CST)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_CST)
    else:
        now = now.astimezone(_CST)
    today_2200 = now.replace(hour=22, minute=0, second=0, microsecond=0)
    until = today_2200 - timedelta(days=1) if now < today_2200 else today_2200
    since = until - timedelta(days=1)
    return (
        since.isoformat(timespec="seconds"),
        until.isoformat(timespec="seconds"),
    )


def _load_matrix(ctx: GraphContext) -> list[HomologousSet]:
    if ctx.matrix is None:
        text = Path(ctx.settings.branch_file).read_text(encoding="utf-8")
        ctx.matrix = build_matrix(parse_branch_md(text))
    return ctx.matrix


def detect_commits(state: dict, ctx: GraphContext) -> dict:
    """Scan window fetch + matrix + classify → detected_commits + classifications."""
    settings = ctx.settings
    since, until = settings.scan_since, settings.scan_until
    if since is None or until is None:
        since, until = default_window()

    ctx.git.fetch_all()

    branch_path = Path(settings.branch_file)
    branch_md_text = branch_path.read_text(encoding="utf-8")
    ctx.matrix = build_matrix(parse_branch_md(branch_md_text))
    branch_md_version = hashlib.sha1(branch_md_text.encode("utf-8")).hexdigest()[:12]

    detected: list[CommitInfo] = []
    classifications: dict[str, SyncDecision] = {}
    for hs in ctx.matrix:
        for source in hs.sources:
            resolved, _ = ctx.git.branch_tip(source.name)
            for sha in ctx.git.commits_in_window(since, until, resolved):
                author, committed_at, message = ctx.git.commit_metadata(sha)
                changed_files = ctx.git.changed_files(sha)
                patch_text = ctx.git.commit_patch(sha)
                symbols = extract_symbols(patch_text)
                classification = ctx.classify(message, changed_files, symbols, patch_text, sha=sha)
                detected.append(
                    CommitInfo(
                        sha=sha,
                        message=message,
                        author=author,
                        committed_at=committed_at,
                        changed_files=changed_files,
                        patch_text=patch_text,
                        symbols=symbols,
                        patch_id=ctx.git.patch_id(sha),
                        issue_ids=list(classification.issue_ids),
                        source_branch=source.name,
                        homologous_section=hs.section,
                    )
                )
                classifications[sha] = SyncDecision(
                    sha=sha,
                    is_bug_fix=classification.is_bug_fix,
                    reason=classification.reason,
                    recognition_source=classification.recognition_source,
                    needs_agent=classification.needs_agent,
                )

    return {
        "scan_window": (since, until),
        "branch_md_version": branch_md_version,
        "detected_commits": detected,
        "classifications": classifications,
        "status": "DETECTED",
    }


def _to_analysis(commit: CommitInfo, decision: SyncDecision) -> CommitAnalysis:
    return CommitAnalysis(
        sha=commit.sha,
        message=commit.message,
        changed_files=commit.changed_files,
        symbols=commit.symbols,
        patch_text=commit.patch_text,
        patch_id=commit.patch_id,
        issue_ids=list(commit.issue_ids),
        recognition_source=decision.recognition_source,
        source_branch=commit.source_branch,
        source_branch_type=resolve_branch_type(commit.source_branch),
        homologous_section=commit.homologous_section,
    )


def _build_target_snapshot(
    ctx: GraphContext, analysis: CommitAnalysis, target: Any
) -> TargetSnapshot:
    target_ref, _ = ctx.git.branch_tip(target.name)
    source_ref, _ = ctx.git.branch_tip(analysis.source_branch)
    return ctx.build_snapshot(
        source=analysis,
        target_branch=target.name,
        target_branch_type=target.branch_type,
        git=ctx.git,
        target_ref=target_ref,
        source_ref=source_ref,
    )


def sync_decision(state: dict, ctx: GraphContext) -> dict:
    """Resolve pending classifications via agent, conclude four-states, freeze batches."""
    detected = list(state.get("detected_commits") or [])
    classifications = dict(state.get("classifications") or {})

    pending = [
        commit
        for commit in detected
        if (entry := classifications.get(commit.sha)) is not None and entry.needs_agent
    ]
    if pending:
        classifications.update(ctx.sync_decision_agent.run(pending))

    analyses = {
        commit.sha: _to_analysis(commit, classifications[commit.sha])
        for commit in detected
        if commit.sha in classifications
    }
    matrix = _load_matrix(ctx)
    thresholds = ctx.decision_rules.conclude

    decisions: dict[str, dict[str, Conclusion4]] = {}
    for hs in matrix:
        source_names = {source.name for source in hs.sources}
        for commit in detected:
            if commit.sha not in analyses or commit.source_branch not in source_names:
                continue
            analysis = analyses[commit.sha]
            for target in hs.need_sync_targets:
                snapshot = _build_target_snapshot(ctx, analysis, target)
                conclusion = ctx.conclude(
                    analysis,
                    snapshot,
                    similarity_high=thresholds.similarity_high,
                    similarity_low=thresholds.similarity_low,
                )
                decisions.setdefault(commit.sha, {})[target.name] = conclusion

    batches: dict[str, list[str]] = {}
    for sha, per_target in decisions.items():
        for target_name, conclusion in per_target.items():
            if conclusion.kind == "NeedSync":
                batches.setdefault(target_name, []).append(sha)

    return {
        "classifications": classifications,
        "decisions": decisions,
        "batches": batches,
        "status": "DECIDED",
    }


def _worktree_git(state: dict, ctx: GraphContext) -> GitService:
    """Resolve the GitService scoped to the current target's worktree path.

    One service per worktree path (keyed off the path, never shared across
    branches), so a branch never operates on another branch's repo.
    """
    path = _worktree_path(state, ctx)
    git = ctx.worktree_gits.get(str(path))
    if git is None:
        git = GitService(executor=ctx.executor, repo_path=path)
        ctx.worktree_gits[str(path)] = git
    return git


def _worktree_path(state: dict, ctx: GraphContext) -> Path:
    target = state.get("current_target")
    branch = (state.get("branch_results") or {}).get(target)
    if branch is not None and branch.worktree_path:
        return Path(branch.worktree_path)
    if ctx.worktree_path is not None:
        return ctx.worktree_path
    raise InfrastructureError(
        f"no worktree prepared for target {target!r}; refusing to operate on the main repo"
    )


def _branch_results(state: dict, ctx: GraphContext, target: str) -> tuple[dict, BranchResult]:
    results = dict(state.get("branch_results") or {})
    branch = results.get(target)
    if branch is None:
        branch = BranchResult(
            target_branch=target,
            worktree_path="",
            status="PARTIAL",
            commits=[],
            patch_path=None,
            stop_reason=None,
        )
        results[target] = branch
    return results, branch


def _find_commit(state: dict, sha: str) -> CommitInfo:
    for commit in state.get("detected_commits") or []:
        if commit.sha == sha:
            return commit
    raise KeyError(f"commit not found in state: {sha}")


def _commit_result_index(branch: BranchResult, sha: str) -> int:
    for index, result in enumerate(branch.commits):
        if result.sha == sha:
            return index
    raise KeyError(f"commit result not found for {sha}")


def _record_commit_result(state: dict, ctx: GraphContext, result: CommitResult) -> dict:
    target = state["current_target"]
    results, branch = _branch_results(state, ctx, target)
    commits = list(branch.commits)
    index = _commit_result_index(branch, result.sha)
    commits[index] = result
    results[target] = branch.model_copy(update={"commits": commits})
    return results


def _record_build(state: dict, ctx: GraphContext, outcome: BuildOutcome) -> dict:
    target = state["current_target"]
    results, branch = _branch_results(state, ctx, target)
    index = _commit_result_index(branch, state["current_commit"])
    current = branch.commits[index]
    build = dict(current.build)
    build[outcome.model] = outcome
    updated = current.model_copy(update={"build": build})
    commits = list(branch.commits)
    commits[index] = updated
    results[target] = branch.model_copy(update={"commits": commits})
    return results


def _current_build(state: dict, ctx: GraphContext) -> tuple[str, BuildOutcome] | None:
    target = state["current_target"]
    _, branch = _branch_results(state, ctx, target)
    try:
        index = _commit_result_index(branch, state["current_commit"])
    except KeyError:
        return None
    build = branch.commits[index].build
    if not build:
        return None
    for model in ctx.safety.required_models():
        if model in build and build[model].status == "FAILED":
            return model, build[model]
    model = next(iter(build))
    return model, build[model]


def prepare_worktree(state: dict, ctx: GraphContext) -> dict:
    """Add a worktree for current_target from its remote tip (决策 24)."""
    target = state["current_target"]
    if target is None:
        raise ValueError("current_target is not set")
    resolved, _ = ctx.git.branch_tip(target)
    worktree_path = Path(ctx.settings.worktree_root) / f"{target}-{state['cycle_id']}"
    ctx.git.add_worktree(resolved, worktree_path)
    ctx.worktree_path = worktree_path
    if str(worktree_path) not in ctx.worktree_gits:
        ctx.worktree_gits[str(worktree_path)] = GitService(
            executor=ctx.executor, repo_path=worktree_path
        )

    results = dict(state.get("branch_results") or {})
    results[target] = BranchResult(
        target_branch=target,
        worktree_path=str(worktree_path),
        status="PARTIAL",
        commits=[],
        patch_path=None,
        stop_reason=None,
    )
    return {"branch_results": results, "status": "PREPARED"}


def cherry_pick(state: dict, ctx: GraphContext) -> dict:
    """Cherry-pick current_commit into the current worktree."""
    target = state["current_target"]
    sha = state["current_commit"]
    wg = _worktree_git(state, ctx)
    result = wg.cherry_pick(sha)
    results, branch = _branch_results(state, ctx, target)
    try:
        index = _commit_result_index(branch, sha)
        current = branch.commits[index]
    except KeyError:
        current = None
        index = -1
    commit_result = CommitResult(
        sha=sha,
        cherry_pick=result.status,
        conflict_resolution=current.conflict_resolution if current else None,
        build=dict(current.build) if current else {},
    )
    commits = list(branch.commits)
    if current is None:
        commits.append(commit_result)
    else:
        commits[index] = commit_result
    results[target] = branch.model_copy(update={"commits": commits})
    return {"branch_results": results, "status": f"CHERRY_PICK_{result.status}"}


def resolve_conflict(state: dict, ctx: GraphContext) -> dict:
    """Resolve the current commit's conflicts; None resolution is fail-fast."""
    target = state["current_target"]
    sha = state["current_commit"]
    commit = _find_commit(state, sha)
    wg = _worktree_git(state, ctx)
    conflict_files = wg.unmerged_files()
    resolution = ctx.conflict_agent.resolve(commit, conflict_files)
    results, branch = _branch_results(state, ctx, target)
    index = _commit_result_index(branch, sha)
    updated = branch.commits[index].model_copy(update={"conflict_resolution": resolution})
    commits = list(branch.commits)
    commits[index] = updated
    results[target] = branch.model_copy(update={"commits": commits})
    return {
        "branch_results": results,
        "status": "RESOLVED" if resolution is not None else "RESOLUTION_FAILED",
    }


def _next_model(state: dict, ctx: GraphContext) -> str | None:
    """Next required model not yet built for current_commit (决策 14 serial)."""
    models = ctx.safety.required_models()
    if not models:
        raise ValueError("safety_rules defines no required_models")
    built: set[str] = set()
    target = state.get("current_target")
    sha = state.get("current_commit")
    if target is not None and sha is not None:
        _, branch = _branch_results(state, ctx, target)
        try:
            index = _commit_result_index(branch, sha)
        except KeyError:
            index = -1
        if index >= 0:
            built = set(branch.commits[index].build)
    for model in models:
        if model not in built:
            return model
    return None


def _log_path(ctx: GraphContext, target: str, sha: str) -> Path:
    return Path(ctx.settings.log_dir) / "build" / target / sha / "build.log"


def build(state: dict, ctx: GraphContext) -> dict:
    """Build current_commit/model in the worktree; clean per batch start / public file."""
    target = state["current_target"]
    sha = state["current_commit"]
    commit = _find_commit(state, sha)
    model = _next_model(state, ctx)
    if model is None:
        return {"status": "BUILD_OK"}
    batch = (state.get("batches") or {}).get(target, [])
    clean = batch[:1] == [sha] or bool(
        ctx.is_public_file is not None and any(ctx.is_public_file(f) for f in commit.changed_files)
    )
    log_path = _log_path(ctx, target, sha)
    result = ctx.runner.build_commit(
        _worktree_path(state, ctx), model, clean=clean, module=None, log_path=log_path
    )
    ok = ctx.runner.is_success(result)
    outcome = BuildOutcome(
        model=model,
        status="OK" if ok else "FAILED",
        log_path=str(log_path),
        errors=result.errors,
        agent_attempts=0,
        fix_diff=None,
    )
    results = _record_build(state, ctx, outcome)
    return {"branch_results": results, "status": "BUILD_OK" if ok else "BUILD_FAILED"}


def fix_build(state: dict, ctx: GraphContext) -> dict:
    """Attribute + auto-fix a failed build via BuildAgent, then re-verify."""
    target = state["current_target"]
    sha = state["current_commit"]
    commit = _find_commit(state, sha)
    current = _current_build(state, ctx)
    if current is None:
        return {"status": "BUILD_OK"}
    model, failed = current
    ctx.build_agent.fix(commit, failed.errors, model)
    log_path = _log_path(ctx, target, sha)
    result = ctx.runner.build_commit(
        _worktree_path(state, ctx), model, clean=False, module=None, log_path=log_path
    )
    ok = ctx.runner.is_success(result)
    outcome = BuildOutcome(
        model=model,
        status="OK" if ok else "FAILED",
        log_path=str(log_path),
        errors=result.errors,
        agent_attempts=failed.agent_attempts + 1,
        fix_diff=None,
    )
    results = _record_build(state, ctx, outcome)
    return {"branch_results": results, "status": "BUILD_OK" if ok else "BUILD_FAILED"}


def _commit_ok(commit: CommitResult) -> bool:
    """A commit counts as synced when cherry-picked (or empty) and fully built."""
    if commit.cherry_pick not in ("OK", "EMPTY"):
        return False
    if commit.cherry_pick == "EMPTY":
        return True
    return bool(commit.build) and all(o.status == "OK" for o in commit.build.values())


def _final_branch_status(commits: list[CommitResult]) -> str:
    if not commits:
        return "FAILED"
    oks = [_commit_ok(commit) for commit in commits]
    if all(oks):
        return "SUCCESS"
    if not any(oks):
        return "FAILED"
    return "PARTIAL"


def generate_patch(state: dict, ctx: GraphContext) -> dict:
    """Export the current branch's sync patch (决策 27): origin tip..HEAD."""
    target = state["current_target"]
    wg = _worktree_git(state, ctx)
    _, base_tip = wg.branch_tip(target)
    out_dir = Path(ctx.settings.log_dir) / "patch"
    prefix = f"{state['cycle_id']}_{target}.patch"
    patch_path = wg.format_patch(base_tip, "HEAD", out_dir, prefix)
    results, branch = _branch_results(state, ctx, target)
    results[target] = branch.model_copy(
        update={
            "patch_path": str(patch_path),
            "status": _final_branch_status(branch.commits),
        }
    )
    return {"branch_results": results, "status": "PATCHED"}


def _action_required(state: dict) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for sha, per_target in (state.get("decisions") or {}).items():
        for branch, conclusion in per_target.items():
            if conclusion.kind == "ManualReview":
                actions.append(
                    {
                        "sha": sha,
                        "branch": branch,
                        "kind": conclusion.kind,
                        "evidence": list(conclusion.evidence),
                    }
                )
    for node_name, err in (state.get("errors") or {}).items():
        actions.append({"node": node_name, "error": err.error})
    return actions


def report(state: dict, ctx: GraphContext) -> dict:
    """Assemble the end-of-cycle Report (HTML rendering lands in batch 3.2/3.3)."""
    cycle_id = state["cycle_id"]
    log_root = Path(ctx.settings.log_dir)
    rep = Report(
        cycle_id=cycle_id,
        html_path=log_root / cycle_id / "report.html",
        summary={
            "status": state.get("status", ""),
            "commits_detected": len(state.get("detected_commits", [])),
            "branches": sorted(state.get("branch_results", {})),
            "decisions": len(state.get("decisions", {})),
        },
        action_required=_action_required(state),
        decisions_json_path=log_root / cycle_id / "decisions.json",
    )
    return {"report": rep, "status": "REPORTED"}
