from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from bsa.domain.models import CommitInfo, Conclusion4, SyncDecision
from bsa.executor.exceptions import SafetyViolation
from bsa.executor.lock import flock_acquire
from bsa.graph.nodes import (
    GraphContext,
    _build_target_snapshot,
    _derive_window,
    _to_analysis,
)
from bsa.graph.single_target import build_single_target_workflow
from bsa.graph.workflow import thread_config
from bsa.rules import classify_severity, extract_symbols, resolve_branch_type
from bsa.rules.branch_md import BranchRef
from bsa.scheduler.cycle import _initial_state


def manual_cycle_id() -> str:
    """手动同步周期 id：manual-<时间戳(微秒)>-<pid>，每次调用唯一，与每日周期隔离。

    ``BSA_MANUAL_CYCLE_ID`` 环境变量优先：executor 守护进程预生成周期 id 并
    注入子进程，使运行期即知 cycle_id（可实时查进度、重启后可重挂），
    否则回退本地生成。
    """
    override = os.environ.get("BSA_MANUAL_CYCLE_ID")
    if override:
        return override
    ts = datetime.now().strftime("%Y%m%d-%H%M%S%f")
    return f"manual-{ts}-{os.getpid()}"


def _commit_info_from_git(ctx: GraphContext, source: str, sha: str) -> CommitInfo:
    """从 git 元数据构造最小 CommitInfo（--sha 直同步与源+目标模式共用）。"""
    author, committed_at, message = ctx.git.commit_metadata(sha)
    changed_files = ctx.git.changed_files(sha)
    patch_text = ctx.git.commit_patch(sha)
    symbols = extract_symbols(patch_text)
    return CommitInfo(
        sha=sha,
        message=message,
        author=author,
        committed_at=committed_at,
        changed_files=changed_files,
        patch_text=patch_text,
        symbols=symbols,
        patch_id=ctx.git.patch_id(sha),
        issue_ids=[],
        source_branch=source,
        homologous_section="manual",
    )


def build_sha_batch(ctx: GraphContext, source: str, shas: list[str]) -> list[CommitInfo]:
    """--sha 直同步：按用户给定 sha 构造 batch，不做决策直接同步。"""
    return [_commit_info_from_git(ctx, source, sha) for sha in shas]


def _detect_source_commits(
    ctx: GraphContext, source: str, since: str | None, until: str | None
) -> tuple[list[CommitInfo], dict[str, SyncDecision]]:
    """单源分支窗口检测：复用 detect_commits 的窗口/分类逻辑（不跑矩阵）。"""
    since, until = _derive_window(since, until)
    ctx.git.fetch_all()
    resolved, _ = ctx.git.branch_tip(source)
    detected: list[CommitInfo] = []
    classifications: dict[str, SyncDecision] = {}
    for sha in ctx.git.commits_in_window(since, until, resolved):
        author, committed_at, message = ctx.git.commit_metadata(sha)
        changed_files = ctx.git.changed_files(sha)
        patch_text = ctx.git.commit_patch(sha)
        symbols = extract_symbols(patch_text)
        classification = ctx.classify(message, changed_files, symbols, patch_text, sha=sha)
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
                patch_id=ctx.git.patch_id(sha),
                issue_ids=list(classification.issue_ids),
                source_branch=source,
                homologous_section="manual",
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
    return detected, classifications


def _resolve_classifications(
    ctx: GraphContext, detected: list[CommitInfo], classifications: dict[str, SyncDecision]
) -> dict[str, SyncDecision]:
    """复用 sync_decision 的 agent 兜底：pending→is_bug_fix，bug-fix 无 risk→resolve。"""
    classifications = dict(classifications)
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
        classifications.update(ctx.sync_decision_agent.run(pending, prior_risks=prior_risks))
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
    return classifications


def _target_branch_ref(target: str, branch_mapping: dict[str, str]) -> BranchRef:
    return BranchRef(
        name=target,
        branch_type=resolve_branch_type(target, branch_mapping),
        section="manual",
    )


def build_source_target_batch(
    ctx: GraphContext,
    source: str,
    target: str,
    since: str | None,
    until: str | None,
) -> tuple[list[CommitInfo], dict[str, Conclusion4]]:
    """源+目标模式：检测+分类+对 target 判定四态，只返回 NeedSync 批次。

    复用 sync_decision 的 ``_to_analysis``/``_build_target_snapshot``/``ctx.conclude``
    判定链路；全部判定结论随返回值暴露，供 CLI 打印与复核。
    """
    detected, classifications = _detect_source_commits(ctx, source, since, until)
    if not detected:
        return [], {}
    classifications = _resolve_classifications(ctx, detected, classifications)
    thresholds = ctx.decision_rules.conclude
    branch = _target_branch_ref(target, ctx.decision_rules.branch_mapping)

    if not ctx.safety.check_sync_branch(target):
        conclusions = {
            commit.sha: Conclusion4(
                kind="OutOfScope",
                evidence=[f"目标分支 {target} 命中禁止同步清单，跳过。"],
                confidence="high",
            )
            for commit in detected
        }
        return [], conclusions

    analyses = {
        commit.sha: _to_analysis(
            commit, classifications[commit.sha], ctx.decision_rules.branch_mapping
        )
        for commit in detected
        if commit.sha in classifications
    }
    batch: list[CommitInfo] = []
    conclusions: dict[str, Conclusion4] = {}
    for commit in detected:
        analysis = analyses.get(commit.sha)
        if analysis is None:
            continue
        snapshot = _build_target_snapshot(ctx, analysis, branch)
        conclusion = ctx.conclude(
            analysis,
            snapshot,
            similarity_high=thresholds.similarity_high,
            similarity_low=thresholds.similarity_low,
        )
        conclusions[commit.sha] = conclusion
        if conclusion.kind == "NeedSync":
            batch.append(commit)
    return batch, conclusions


def run_sync_command(
    ctx: GraphContext,
    *,
    cycle_id: str,
    target: str,
    batch: list[CommitInfo],
    checkpointer,
    thread_id: str | None = None,
) -> dict:
    """执行单 target 同步：构造初始 state、持全局 flock、invoke 子图。

    决策已在命令层完成（batch 内 commit 全部视为 NeedSync），子图只跑同步阶段。
    ``thread_id`` 缺省等于 ``cycle_id``：checkpoint 线程与 worktree/报告命名
    绑定同一周期。retained 重跑传入独立线程 id，使重跑 state 落到独立线程，
    不再覆盖来源周期的 checkpoint 投影。返回最终 state（含
    branch_results[target]、patch 生成）。
    """
    if not ctx.safety.check_sync_branch(target):
        raise SafetyViolation(f"目标分支 {target} 命中禁止同步清单，拒绝同步。")
    initial = _initial_state(cycle_id)
    initial.update(
        {
            "detected_commits": list(batch),
            "decisions": {
                commit.sha: {
                    target: Conclusion4(
                        kind="NeedSync", evidence=[], confidence="high"
                    )
                }
                for commit in batch
            },
            "batches": {target: [commit.sha for commit in batch]},
            "current_target": target,
        }
    )
    checkpoint_thread = thread_id or cycle_id
    with flock_acquire(Path(ctx.settings.log_dir) / "bsa.lock"):
        graph = build_single_target_workflow(ctx, checkpointer)
        return graph.invoke(initial, thread_config(checkpoint_thread))
