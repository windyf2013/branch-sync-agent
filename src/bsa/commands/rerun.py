from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

from bsa.commands.sync import manual_cycle_id, run_sync_command
from bsa.domain.models import CommitInfo, Conclusion4, SyncDecision
from bsa.git.service import GitService
from bsa.graph.nodes import (
    GraphContext,
    _build_target_snapshot,
    _is_valid_worktree,
    _to_analysis,
)
from bsa.report.projection import read_cycle_state
from bsa.rules import resolve_branch_type
from bsa.rules.branch_md import BranchRef
from bsa.scheduler.cycle import list_cycle_records


def _worktree_path(ctx: GraphContext, target: str, cycle_id: str) -> Path:
    """目标分支在某周期的 worktree 路径（与 prepare_worktree 命名保持一致）。"""
    return Path(ctx.settings.worktree_root) / f"{target}-{cycle_id}"


def _rerun_thread_id(target: str) -> str:
    """retained 重跑的独立 checkpoint 线程 id：与来源周期隔离，避免污染其投影。"""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S%f")
    return f"rerun-{target}-{ts}-{os.getpid()}"


def _started_at(record: dict) -> datetime:
    started = record.get("started_at")
    try:
        dt = datetime.fromisoformat(started) if started else None
    except (TypeError, ValueError):
        dt = None
    return dt or datetime.min


def _cycle_records_newest_first(ctx: GraphContext) -> list[dict]:
    """周期记录按开始时间倒序（新周期优先）。"""
    return sorted(list_cycle_records(ctx.settings.log_dir), key=_started_at, reverse=True)


def _locate_cycle(
    ctx: GraphContext, target: str, cycle_id: str | None
) -> tuple[str | None, dict | None]:
    """定位最近含 target 的已完成周期，返回 (cycle_id, 冻结 state)。

    从新到旧遍历周期记录，跳过仍在运行、或冻结批次里不含该 target 的周期；
    ``--cycle`` 指定时只接受该周期。
    """
    records = _cycle_records_newest_first(ctx)
    if cycle_id is not None:
        records = [record for record in records if record.get("cycle_id") == cycle_id]
    for record in records:
        if record.get("status") == "running":
            continue
        state = read_cycle_state(ctx.settings, record["cycle_id"])
        if state is None:
            continue
        if not (state.get("batches") or {}).get(target):
            continue
        return record["cycle_id"], state
    return None, None


def _locate_worktree(ctx: GraphContext, target: str, cycle_id: str) -> Path | None:
    """目录探测活 worktree；与 prepare_worktree 幂等复用同一判定。"""
    path = _worktree_path(ctx, target, cycle_id)
    if not path.exists() or not _is_valid_worktree(path):
        return None
    return path


def _worktree_git_for(ctx: GraphContext, worktree_path: Path) -> GitService:
    """取 worktree 作用域的 GitService（dirty 检查与图内复用同一实例）。"""
    git = ctx.worktree_gits.get(str(worktree_path))
    if git is None:
        git = GitService(executor=ctx.executor, repo_path=worktree_path)
        ctx.worktree_gits[str(worktree_path)] = git
    return git


def _batch_from_state(state: dict, target: str) -> list[CommitInfo]:
    """从周期冻结 state 还原 target 的待同步批次（detected_commits ∩ batches）。"""
    by_sha = {commit.sha: commit for commit in state.get("detected_commits") or []}
    shas = (state.get("batches") or {}).get(target, [])
    return [by_sha[sha] for sha in shas if sha in by_sha]


def _decision_for(commit: CommitInfo, state: dict) -> SyncDecision:
    """取冻结 classification；manual 周期无 classification 时按已判 NeedSync 兜底。"""
    entry = (state.get("classifications") or {}).get(commit.sha)
    if entry is not None:
        return entry
    return SyncDecision(
        sha=commit.sha,
        is_bug_fix=True,
        reason=None,
        recognition_source="machine:[BUG]",
        needs_agent=False,
        risk=None,
    )


def _target_branch_ref(target: str, branch_mapping: dict[str, str]) -> BranchRef:
    return BranchRef(
        name=target,
        branch_type=resolve_branch_type(target, branch_mapping),
        section="manual",
    )


def _rejudge_batch(
    ctx: GraphContext, target: str, batch: list[CommitInfo], state: dict
) -> tuple[list[CommitInfo], dict[str, Conclusion4]]:
    """对当前远端重判 batch 四态；返回 (仍 NeedSync 的 commits, 全部结论)。

    --fresh 先判结论再动现场：批次已全部合入/超范围时直接拦截，
    避免无谓丢弃 worktree 重建。
    """
    thresholds = ctx.decision_rules.conclude
    branch = _target_branch_ref(target, ctx.decision_rules.branch_mapping)
    analyses = {
        commit.sha: _to_analysis(
            commit, _decision_for(commit, state), ctx.decision_rules.branch_mapping
        )
        for commit in batch
    }
    remaining: list[CommitInfo] = []
    conclusions: dict[str, Conclusion4] = {}
    for commit in batch:
        analysis = analyses[commit.sha]
        snapshot = _build_target_snapshot(ctx, analysis, branch)
        conclusion = ctx.conclude(
            analysis,
            snapshot,
            similarity_high=thresholds.similarity_high,
            similarity_low=thresholds.similarity_low,
        )
        conclusions[commit.sha] = conclusion
        if conclusion.kind == "NeedSync":
            remaining.append(commit)
    return remaining, conclusions


def _rerun_retained(
    ctx: GraphContext, *, target: str, cycle: str | None, checkpointer
) -> dict:
    """保留现场续跑：复用活 worktree 与冻结批次，重跑同步验证链路。

    已应用的 commit 由 cherry_pick 判 EMPTY 跳过应用、仅全量 build 验证，
    最终 regenerate patch。
    """
    cycle_id, state = _locate_cycle(ctx, target, cycle)
    if cycle_id is None:
        return {"stop": True, "reason": "no-cycle", "target": target}
    worktree_path = _locate_worktree(ctx, target, cycle_id)
    if worktree_path is None:
        return {
            "stop": True,
            "reason": "no-worktree",
            "target": target,
            "cycle_id": cycle_id,
        }
    wg = _worktree_git_for(ctx, worktree_path)
    if wg.status().strip():
        return {
            "stop": True,
            "reason": "dirty",
            "target": target,
            "cycle_id": cycle_id,
            "worktree": str(worktree_path),
        }
    batch = _batch_from_state(state, target)
    if not batch:
        return {
            "stop": True,
            "reason": "no-batch",
            "target": target,
            "cycle_id": cycle_id,
        }
    thread_id = _rerun_thread_id(target)
    final = run_sync_command(
        ctx,
        cycle_id=cycle_id,
        target=target,
        batch=batch,
        checkpointer=checkpointer,
        thread_id=thread_id,
    )
    final["rerun"] = {
        "mode": "retained",
        "cycle_id": thread_id,
        "source_cycle_id": cycle_id,
        "worktree": str(worktree_path),
    }
    return final


def _rerun_fresh(
    ctx: GraphContext, *, target: str, cycle: str | None, checkpointer
) -> dict:
    """--fresh 重建重同步：先对当前远端重判，仍 NeedSync 才丢弃现场重建。"""
    ctx.git.fetch_all()
    cycle_id, state = _locate_cycle(ctx, target, cycle)
    if cycle_id is None:
        return {"stop": True, "reason": "no-cycle", "target": target}
    batch = _batch_from_state(state, target)
    if not batch:
        return {
            "stop": True,
            "reason": "no-batch",
            "target": target,
            "cycle_id": cycle_id,
        }
    remaining, conclusions = _rejudge_batch(ctx, target, batch, state)
    if not remaining:
        if any(
            conclusion.kind == "ManualReview"
            for conclusion in conclusions.values()
        ):
            return {
                "stop": True,
                "reason": "conclusion-manual-review",
                "target": target,
                "cycle_id": cycle_id,
                "conclusions": {sha: conclusion.kind for sha, conclusion in conclusions.items()},
            }
        return {
            "stop": True,
            "reason": "conclusion-now-included",
            "target": target,
            "cycle_id": cycle_id,
            "conclusions": {sha: conclusion.kind for sha, conclusion in conclusions.items()},
        }
    old_path = _worktree_path(ctx, target, cycle_id)
    if old_path.exists():
        if _is_valid_worktree(old_path):
            ctx.git.remove_worktree(old_path)
        else:
            shutil.rmtree(old_path, ignore_errors=True)
    new_cycle_id = manual_cycle_id()
    final = run_sync_command(
        ctx, cycle_id=new_cycle_id, target=target, batch=remaining, checkpointer=checkpointer
    )
    final["rerun"] = {
        "mode": "fresh",
        "source_cycle_id": cycle_id,
        "cycle_id": new_cycle_id,
    }
    return final


def run_rerun_command(
    ctx: GraphContext,
    *,
    target: str,
    cycle: str | None = None,
    fresh: bool = False,
    checkpointer=None,
) -> dict:
    """分支级重跑：默认保留现场续跑；--fresh 重建并对当前远端重判。

    返回值统一为 dict：拦截场景含 ``stop=True`` 与 ``reason``
    （dirty / no-cycle / no-worktree / no-batch / conclusion-now-included /
    conclusion-manual-review），正常场景返回同步最终 state 并附 ``rerun``
    元信息。retained 模式将 checkpoint 写入独立 ``rerun-*`` 线程
    （``rerun.cycle_id``），不覆盖来源周期投影。
    """
    if fresh:
        return _rerun_fresh(ctx, target=target, cycle=cycle, checkpointer=checkpointer)
    return _rerun_retained(ctx, target=target, cycle=cycle, checkpointer=checkpointer)
