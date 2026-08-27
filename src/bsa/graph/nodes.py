from __future__ import annotations

import hashlib
import json
import shutil
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
    BuildConfigError,
    BuildRules,
    DecisionRules,
    HomologousSet,
    TargetSnapshot,
    build_matrix,
    classify_commit,
    classify_severity,
    conclude_pair,
    parse_branch_md,
    resolve_branch_type,
    resolve_build_models,
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
    build_rules: BuildRules | None = None
    worktree_path: Path | None = None
    worktree_gits: dict[str, GitService] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.is_public_file is None:
            path_rules = (self.decision_rules.classify or {}).get("path_rules") or {}
            public_dirs = list(path_rules.get("public_dirs", []))
            self.is_public_file = lambda path: is_public_file(path, public_dirs)
        if self.conclude is conclude_pair:
            # 目标类型由 decision_rules.yaml 单一驱动（P2 治理），从 ConcludeThresholds
            # 注入到 conclude_pair，避免与硬编码常量双源并存。
            thresholds = self.decision_rules.conclude
            self.conclude = partial(
                conclude_pair,
                need_sync_target_types=list(thresholds.need_sync_target_types),
                ineligible_target_types=list(thresholds.ineligible_target_types),
            )


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


def _parse_dt(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_CST)
    return dt


def _derive_window(
    since: str | None, until: str | None, now: datetime | None = None
) -> tuple[str, str]:
    """Resolve a scan window, deriving a missing bound (决策 38, partial override).

    A partial ``--since``/``--until`` override is honored instead of silently
    falling back to the default window: only ``--since`` → until = now; only
    ``--until`` → since = until − 1 day. Both or neither keep the existing
    behaviour.
    """
    if since is not None and until is not None:
        return since, until
    if since is None and until is None:
        return default_window(now) if now is not None else default_window()
    if until is None:
        if now is None:
            now = datetime.now(_CST)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=_CST)
        else:
            now = now.astimezone(_CST)
        return since, now.isoformat(timespec="seconds")
    derived = _parse_dt(until) - timedelta(days=1)
    return derived.isoformat(timespec="seconds"), until


def _load_judgments(log_dir: Path) -> dict[str, Any]:
    """加载人工覆盖判定 judgments.json；缺失/损坏返回空（节点级容错，不崩周期）。"""
    path = log_dir / "judgments.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _load_matrix(ctx: GraphContext) -> list[HomologousSet]:
    if ctx.matrix is None:
        text = Path(ctx.settings.branch_file).read_text(encoding="utf-8")
        ctx.matrix = build_matrix(
            parse_branch_md(text, branch_mapping=ctx.decision_rules.branch_mapping)
        )
    return ctx.matrix


def _matrix_section_of(ctx: GraphContext) -> dict[str, str]:
    """分支名 → branch.md section（首个出现），等价 first_occurrence_sections。"""
    section_of: dict[str, str] = {}
    for hs in _load_matrix(ctx):
        for branch in hs.sources + hs.need_sync_targets:
            section_of.setdefault(branch.name, hs.section)
    return section_of


def _target_models(state: dict, ctx: GraphContext) -> list[str]:
    """当前目标分支的编译型号列表；未解析（无 build_rules）回退 safety 全局默认。"""
    target = state.get("current_target")
    models = (state.get("build_models") or {}).get(target) if target else None
    if models:
        return list(models)
    return ctx.safety.required_models()


def detect_commits(state: dict, ctx: GraphContext) -> dict:
    """Scan window fetch + matrix + classify → detected_commits + classifications."""
    settings = ctx.settings
    since, until = _derive_window(settings.scan_since, settings.scan_until)

    ctx.git.fetch_all()

    # 人工覆盖判定贯通全量 commit（G9）：加载 judgments.json 传入 classify，
    # 使 override 对机器已判定的 commit 也生效（人工判定优先级最高，见 classify_commit）。
    judgments = _load_judgments(Path(settings.log_dir))

    branch_path = Path(settings.branch_file)
    branch_md_text = branch_path.read_text(encoding="utf-8")
    ctx.matrix = build_matrix(
        parse_branch_md(branch_md_text, branch_mapping=ctx.decision_rules.branch_mapping)
    )
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
                patch_id = ctx.git.patch_id(sha)
                classification = ctx.classify(
                    message,
                    changed_files,
                    symbols,
                    patch_text,
                    sha=sha,
                    patch_id=patch_id,
                    agent_judgments=judgments,
                )
                severity = classify_severity(message, changed_files)
                risk = severity if severity in ("low", "medium", "high") else None
                detected.append(
                    CommitInfo(
                        sha=sha,
                        message=message,
                        author=author,
                        committed_at=committed_at,
                        changed_files=changed_files,
                        patch_text=patch_text,
                        symbols=symbols,
                        patch_id=patch_id,
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
                    risk=risk,
                )

    return {
        "scan_window": (since, until),
        "branch_md_version": branch_md_version,
        "detected_commits": detected,
        "classifications": classifications,
        "status": "DETECTED",
    }


def _to_analysis(
    commit: CommitInfo, decision: SyncDecision, branch_mapping: dict[str, str] | None = None
) -> CommitAnalysis:
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
        source_branch_type=resolve_branch_type(commit.source_branch, branch_mapping),
        homologous_section=commit.homologous_section,
        risk=decision.risk,
        needs_agent=decision.needs_agent,
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
        prior_risks = {
            commit.sha: classifications[commit.sha].risk
            for commit in pending
            if classifications[commit.sha].risk is not None
        }
        classifications.update(
            ctx.sync_decision_agent.run(pending, prior_risks=prior_risks)
        )

    risk_pending = [
        commit
        for commit in detected
        if (entry := classifications.get(commit.sha)) is not None
        and not entry.needs_agent
        and entry.is_bug_fix
        and entry.risk is None
    ]
    if risk_pending:
        risks = ctx.sync_decision_agent.resolve_risks(risk_pending)
        for sha, risk in risks.items():
            classifications[sha] = classifications[sha].model_copy(update={"risk": risk})

    analyses = {
        commit.sha: _to_analysis(
            commit, classifications[commit.sha], ctx.decision_rules.branch_mapping
        )
        for commit in detected
        if commit.sha in classifications
    }
    matrix = _load_matrix(ctx)
    thresholds = ctx.decision_rules.conclude

    decisions: dict[str, dict[str, Conclusion4]] = {}
    from bsa.ledger import is_synced

    ledger_dir = Path(ctx.settings.log_dir)
    for hs in matrix:
        source_names = {source.name for source in hs.sources}
        for commit in detected:
            if commit.sha not in analyses or commit.source_branch not in source_names:
                continue
            analysis = analyses[commit.sha]
            for target in hs.need_sync_targets:
                if not ctx.safety.check_sync_branch(target.name):
                    decisions.setdefault(commit.sha, {})[target.name] = Conclusion4(
                        kind="OutOfScope",
                        evidence=[f"目标分支 {target.name} 命中禁止同步清单，跳过。"],
                        confidence="high",
                    )
                    continue
                # 台账短路（决策 5.2）：该 patch-id 对目标分支已同步过 → 直接已含，
                # 跳过快照构建与相似度/LLM 判定（跨天/跨周期幂等）。
                if analysis.patch_id and is_synced(
                    ledger_dir, analysis.patch_id, target.name
                ):
                    decisions.setdefault(commit.sha, {})[target.name] = Conclusion4(
                        kind="AlreadyIncluded",
                        evidence=[
                            f"台账记录：patch-id {analysis.patch_id} 已同步到 {target.name}。"
                        ],
                        confidence="high",
                    )
                    continue
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

    # 产品线 → 编译型号：每目标解析一次；未配置/未知产品线报错并移出批次，
    # 错误进 errors → action_required（绝不静默用错脚本或静默跳过编译）。
    build_models: dict[str, list[str]] = {}
    errors = dict(state.get("errors") or {})
    if ctx.build_rules is not None:
        section_of = _matrix_section_of(ctx)
        for target_name in list(batches.keys()):
            try:
                models = resolve_build_models(section_of, target_name, ctx.build_rules)
            except BuildConfigError as exc:
                errors[f"build_models:{target_name}"] = ErrorRecord(
                    node="decide", error=str(exc), ts=_now_iso()
                )
                batches.pop(target_name)
                continue
            build_models[target_name] = models

    update: dict[str, Any] = {
        "classifications": classifications,
        "decisions": decisions,
        "batches": batches,
        "build_models": build_models,
        "status": "DECIDED",
    }
    if errors:
        update["errors"] = errors
    return update


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


def _is_valid_worktree(path: Path) -> bool:
    """True when path is a usable git worktree.

    A valid worktree has a ``.git`` file pointing to an existing gitdir
    (``.git/worktrees/<name>`` or ``.git`` dir). A leftover dir from a removed
    worktree has a dangling gitdir → invalid (真机测试: cherry_pick 报
    "not a git repository").
    """
    dot_git = path / ".git"
    if not dot_git.exists():
        return False
    if dot_git.is_dir():
        return True
    text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
    if not text.startswith("gitdir:"):
        return False
    gitdir = text[len("gitdir:") :].strip()
    return Path(gitdir).is_dir()


def prepare_worktree(state: dict, ctx: GraphContext) -> dict:
    """Add a worktree for current_target from its remote tip (决策 24).

    Idempotent for resume/re-run: if the worktree path already exists on disk
    (from an interrupted cycle or a previous run), it is reused instead of
    failing ``git worktree add`` (I5). When reusing a valid worktree, an
    existing branch_result (baseline / commits) is preserved so resume never
    re-does completed work. Only a fresh worktree or a rebuilt (invalid) one
    resets the branch record.
    """
    target = state["current_target"]
    if target is None:
        raise ValueError("current_target is not set")
    resolved, _ = ctx.git.branch_tip(target)
    worktree_path = Path(ctx.settings.worktree_root) / f"{target}-{state['cycle_id']}"
    fresh = False
    if worktree_path.exists():
        # 路径存在但可能是残缺 worktree（.git 指向的 gitdir 已删/无效）——
        # 真机测试: git worktree remove 后残留目录, prepare 复用后
        # cherry_pick 报 "not a git repository"。有效校验后重建。
        if not _is_valid_worktree(worktree_path):
            shutil.rmtree(worktree_path, ignore_errors=True)
            ctx.git.add_worktree(resolved, worktree_path)
            fresh = True
    else:
        ctx.git.add_worktree(resolved, worktree_path)
        fresh = True
    ctx.worktree_path = worktree_path
    if str(worktree_path) not in ctx.worktree_gits:
        ctx.worktree_gits[str(worktree_path)] = GitService(
            executor=ctx.executor, repo_path=worktree_path
        )

    results = dict(state.get("branch_results") or {})
    existing = results.get(target)
    if fresh or existing is None or existing.worktree_path != str(worktree_path):
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
    """Resolve the current commit's conflicts; None resolution is fail-fast.

    On a successful resolution the agent stages the resolved files; this node
    then runs ``cherry-pick --continue`` to commit the resolution, clear the
    sequencer, and advance HEAD so the sync patch is non-empty (C1). The
    CommitResult is upgraded to ``cherry_pick=OK``.
    """
    target = state["current_target"]
    sha = state["current_commit"]
    commit = _find_commit(state, sha)
    wg = _worktree_git(state, ctx)
    conflict_files = wg.unmerged_files()
    resolution = ctx.conflict_agent.resolve(commit, conflict_files, git=wg, target_branch=target)
    results, branch = _branch_results(state, ctx, target)
    index = _commit_result_index(branch, sha)
    reason = None if resolution is not None else getattr(ctx.conflict_agent, "last_reason", None)
    if resolution is not None:
        wg.cherry_pick_continue()
        status = "RESOLVED"
    else:
        status = "RESOLUTION_FAILED"
    updated = branch.commits[index].model_copy(
        update={
            "conflict_resolution": resolution,
            "cherry_pick": "OK" if resolution is not None else branch.commits[index].cherry_pick,
        }
    )
    commits = list(branch.commits)
    commits[index] = updated
    results[target] = branch.model_copy(update={"commits": commits})
    update: dict[str, Any] = {"branch_results": results, "status": status}
    if reason:
        # 转人工原因（如冲突文件非 UTF-8）写入 errors → 进 action_required，详情页可见
        errors = dict(state.get("errors") or {})
        errors["resolve_conflict"] = ErrorRecord(
            node="resolve_conflict", error=reason, ts=_now_iso()
        )
        update["errors"] = errors
    return update


def _next_model(state: dict, ctx: GraphContext) -> str | None:
    """Next required model not yet built for current_commit (决策 14 serial)."""
    models = _target_models(state, ctx)
    if not models:
        raise ValueError("未解析到该目标分支的编译型号（build_models 为空）")
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


def _log_path(ctx: GraphContext, state: dict, target: str, sha: str) -> Path:
    return (
        Path(ctx.settings.log_dir) / state["cycle_id"] / "build" / target / sha / "build.log"
    )


def _baseline_log_path(ctx: GraphContext, state: dict, target: str, model: str) -> Path:
    return (
        Path(ctx.settings.log_dir)
        / state["cycle_id"]
        / "build"
        / target
        / "baseline"
        / f"{model}.log"
    )


def baseline_build(state: dict, ctx: GraphContext) -> dict:
    """Baseline full compile on the target's untouched worktree (决策 V2).

    Runs after ``prepare_worktree`` and before the first cherry-pick: proves the
    target branch tip compiles clean before any sync work. Each model is built
    with ``clean=True`` on the raw worktree. On any model failure the branch is
    marked FAILED with a stop_reason (blocked) and the graph routes to the next
    branch — no commits are applied. Idempotent for resume: a branch whose
    ``baseline`` dict already holds records is not rebuilt.
    """
    target = state["current_target"]
    results, branch = _branch_results(state, ctx, target)
    models = _target_models(state, ctx)
    if branch.baseline and all(
        branch.baseline.get(model) is not None for model in models
    ):
        return {"branch_results": results, "status": "BASELINE_OK"}

    baseline: dict[str, BuildOutcome] = dict(branch.baseline or {})
    for model in models:
        if model in baseline:
            continue
        log_path = _baseline_log_path(ctx, state, target, model)
        result = ctx.runner.build_commit(
            _worktree_path(state, ctx), model, clean=True, module=None, log_path=log_path
        )
        ok = ctx.runner.is_success(result)
        baseline[model] = BuildOutcome(
            model=model,
            status="OK" if ok else "FAILED",
            log_path=str(log_path),
            errors=result.errors,
            agent_attempts=0,
            fix_diff=None,
        )
        if not ok:
            # 台账记录（决策 5.2）：基线失败 → 该分支批次 commit 全部 blocked。
            from bsa.ledger import record_status

            by_sha = {c.sha: c for c in state.get("detected_commits") or []}
            for sha in (state.get("batches") or {}).get(target, []):
                src = by_sha.get(sha)
                if src and src.patch_id:
                    record_status(
                        Path(ctx.settings.log_dir),
                        src.patch_id,
                        target,
                        sha,
                        "blocked",
                        reason=f"baseline build failed on {model}",
                    )
            results[target] = branch.model_copy(
                update={
                    "baseline": baseline,
                    "status": "FAILED",
                    "stop_reason": f"baseline build failed on {model}",
                }
            )
            return {"branch_results": results, "status": "BASELINE_FAILED"}

    results[target] = branch.model_copy(update={"baseline": baseline})
    return {"branch_results": results, "status": "BASELINE_OK"}


def build(state: dict, ctx: GraphContext) -> dict:
    """Build current_commit/model in the worktree; clean only on public files.

    基线编译（prepare 后 baseline_build）已全量验证 worktree，批次首个 commit
    不再重复 clean 编译；改动公共文件仍降级全量编译（决策 2）。
    """
    target = state["current_target"]
    sha = state["current_commit"]
    commit = _find_commit(state, sha)
    model = _next_model(state, ctx)
    if model is None:
        return {"status": "BUILD_OK"}
    clean = bool(
        ctx.is_public_file is not None and any(ctx.is_public_file(f) for f in commit.changed_files)
    )
    log_path = _log_path(ctx, state, target, sha)
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
    wg = _worktree_git(state, ctx)
    attribution = ctx.build_agent.fix(
        commit, failed.errors, model, git=wg, target_branch=target
    )
    log_path = _log_path(ctx, state, target, sha)
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
        fix_diff=attribution.fix_diff,
        reason=attribution.reason,
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
    # 台账记录（决策 5.2）：成功同步的 commit 记 synced，失败的记 failed，
    # 按 (patch_id, target) 落台账（跨天幂等 + 审计）。
    by_sha = {commit.sha: commit for commit in state.get("detected_commits") or []}
    from bsa.ledger import record_status, record_synced

    log_dir = Path(ctx.settings.log_dir)
    for result in branch.commits:
        src = by_sha.get(result.sha)
        if not (src and src.patch_id):
            continue
        if _commit_ok(result):
            record_synced(log_dir, src.patch_id, target, result.sha)
        else:
            record_status(log_dir, src.patch_id, target, result.sha, "failed")
    return {"branch_results": results, "status": "PATCHED"}


def _action_required(state: dict, log_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """汇总人工项（ManualReview + 节点错误）。

    ``log_dir`` 提供时，对每个 ManualReview commit 检测其人工覆盖是否已因内容
    变化而失效（fingerprint 不匹配）：附带 ``override_stale`` 供 UI 提示。
    """
    by_sha = {c.sha: c for c in state.get("detected_commits") or []}
    judgments = _load_judgments(Path(log_dir)) if log_dir else {}

    def _override_stale(sha: str) -> bool:
        """该 sha 有 manual-override 覆盖，且对应 fp: 键不匹配当前 fingerprint。"""
        entry = judgments.get(sha)
        if not isinstance(entry, dict) or entry.get("recognition_source") != "manual-override":
            return False
        commit = by_sha.get(sha)
        if commit is None or not commit.patch_id:
            return False
        from bsa.rules.classify import compute_fingerprint

        current_fp = compute_fingerprint(commit.message, commit.patch_id)
        # 找与 sha 覆盖同内容的 fp: 键
        for key, value in judgments.items():
            if not isinstance(key, str) or not key.startswith("fp:"):
                continue
            if isinstance(value, dict) and value == entry and key != f"fp:{current_fp}":
                return True
        return False

    actions: list[dict[str, Any]] = []
    for sha, per_target in (state.get("decisions") or {}).items():
        for branch, conclusion in per_target.items():
            if conclusion.kind == "ManualReview":
                item: dict[str, Any] = {
                    "sha": sha,
                    "branch": branch,
                    "kind": conclusion.kind,
                    "evidence": list(conclusion.evidence),
                }
                if judgments:
                    item["override_stale"] = _override_stale(sha)
                actions.append(item)
    for node_name, err in (state.get("errors") or {}).items():
        actions.append({"node": node_name, "error": err.error})
    return actions


def _cycle_terminal_status(state: dict) -> str:
    """收敛周期终态（P0-2/G10）：全成功 SUCCESS / 部分失败 PARTIAL / 有错误或失败 FAILED。

    供 report 节点写入 state.status，经 cycle.json / tasks.state 传导到平台，
    使"全成功"与"有失败"可区分（工作台失败醒目标记信号源）。
    """
    if state.get("errors"):
        return "FAILED"
    statuses = [b.status for b in (state.get("branch_results") or {}).values()]
    if not statuses:
        return "SUCCESS"
    if any(s in ("FAILED", "MANUAL") for s in statuses):
        return "FAILED"
    if any(s == "PARTIAL" for s in statuses):
        return "PARTIAL"
    return "SUCCESS"


def report(state: dict, ctx: GraphContext) -> dict:
    """Assemble the end-of-cycle Report (HTML rendering lands in batch 3.2/3.3)."""
    cycle_id = state["cycle_id"]
    log_root = Path(ctx.settings.log_dir)
    terminal = _cycle_terminal_status(state)
    rep = Report(
        cycle_id=cycle_id,
        html_path=log_root / cycle_id / "report.html",
        summary={
            "status": terminal,
            "commits_detected": len(state.get("detected_commits", [])),
            "branches": sorted(state.get("branch_results", {})),
            "decisions": len(state.get("decisions", {})),
        },
        action_required=_action_required(state, log_dir=log_root),
        decisions_json_path=log_root / cycle_id / "decisions.json",
    )
    return {"report": rep, "status": terminal}
