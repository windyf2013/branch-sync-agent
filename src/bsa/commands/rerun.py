from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

from bsa.commands.sync import manual_cycle_id, run_sync_command
from bsa.domain.models import CommitInfo, Conclusion4, SyncDecision
from bsa.executor.lock import flock_acquire
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
    """目标分支在某周期的 worktree 路径（与 prepare_worktree 命名保持一致）。

    cycle_id 经净化后再拼：scan-*/显式窗口的 ISO 时间戳含 ':'，直接拼会让
    worktree 路径含 ':' 进而 docker -v 挂载报 "too many colons"。
    """
    from bsa.build.runner import _sanitize_container_name

    return Path(ctx.settings.worktree_root) / f"{target}-{_sanitize_container_name(cycle_id)}"


def _rerun_thread_id(target: str) -> str:
    """retained 重跑的独立 checkpoint 线程 id：与来源周期隔离，避免污染其投影。

    ``BSA_RERUN_THREAD_ID`` 环境变量优先（executor 预生成注入，运行期即知
    cycle_id），否则本地生成。
    """
    override = os.environ.get("BSA_RERUN_THREAD_ID")
    if override:
        return override
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


def _manual_cycle_ids(ctx: GraphContext) -> list[str]:
    """manual-*/rerun-* 周期 id，按目录 mtime 新→旧。

    ``list_cycle_records`` 只枚举 cron/scan 的 cycle.json；手动同步（manual-*）
    与重跑（rerun-*）走单目标子图、不写 cycle.json，但会落盘 state.json 目录，
    故重跑定位来源周期时须额外按目录枚举。
    """
    ids: list[tuple[float, str]] = []
    for pattern in ("manual-*", "rerun-*"):
        for path in Path(ctx.settings.log_dir).glob(pattern):
            if path.is_dir():
                ids.append((path.stat().st_mtime, path.name))
    ids.sort(reverse=True)
    return [name for _, name in ids]


def _locate_cycle(
    ctx: GraphContext, target: str, cycle_id: str | None
) -> tuple[str | None, dict | None]:
    """定位最近含 target 的已完成周期，返回 (cycle_id, 冻结 state)。

    ``--cycle`` 指定时直接读该周期冻结 state——manual-*/rerun-* 不写 cycle.json，
    只能经 checkpoint 直接取，不能再走 ``list_cycle_records`` 过滤；未指定时从
    新到旧遍历 cron/scan 周期记录与 manual/rerun 周期目录，跳过仍在运行、或
    冻结批次里不含该 target 的周期。
    """
    if cycle_id is not None:
        state = read_cycle_state(ctx.settings, cycle_id)
        if state is not None and (state.get("batches") or {}).get(target):
            return cycle_id, state
        return None, None

    for record in _cycle_records_newest_first(ctx):
        if record.get("status") == "running":
            continue
        state = read_cycle_state(ctx.settings, record["cycle_id"])
        if state is None or not (state.get("batches") or {}).get(target):
            continue
        return record["cycle_id"], state

    for cid in _manual_cycle_ids(ctx):
        state = read_cycle_state(ctx.settings, cid)
        if state is not None and (state.get("batches") or {}).get(target):
            return cid, state
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
    """对当前远端重判 batch 四态；返回 (仍需重同步的 commits, 全部结论)。

    --fresh 先判结论再动现场：重判只「降级」客观已合入/超范围的 commit
    （AlreadyIncluded/OutOfScope），其余（NeedSync 与 ManualReview）一律保留进
    重同步批次。冻结批次里的 commit 本就已经过 NeedSync 判定（否则不会进
    batch），而 ManualReview 是决策层「无法自动判定」，并非「已合入」的客观
    证据——不能据此把用户要重同步的 commit 静默丢弃，否则失败分支重跑会被
    误报成功却不做任何同步。
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
        if conclusion.kind not in ("AlreadyIncluded", "OutOfScope"):
            remaining.append(commit)
    return remaining, conclusions


def _rerun_retained(
    ctx: GraphContext, *, target: str, cycle: str | None, checkpointer,
    thread_id: str | None = None,
) -> dict:
    """保留现场续跑：复用活 worktree 与冻结批次，重跑同步验证链路。

    已应用的 commit 由 cherry_pick 判 EMPTY 跳过应用、仅全量 build 验证，
    最终 regenerate patch。``thread_id`` 由调用方预生成（P2-3 单一线程 id），
    缺省时内部生成，保证任务登记与实际 checkpoint 线程一致。
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
    thread_id = thread_id or _rerun_thread_id(target)
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
    ctx: GraphContext, *, target: str, cycle: str | None, checkpointer,
    thread_id: str | None = None,
) -> dict:
    """--fresh 重建重同步：先对当前远端重判，未全部合入/超范围才丢弃现场重建。"""
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
    new_cycle_id = thread_id or manual_cycle_id()
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
    thread_id: str | None = None,
) -> dict:
    """分支级重跑：默认保留现场续跑；--fresh 重建并对当前远端重判。

    返回值统一为 dict：拦截场景含 ``stop=True`` 与 ``reason``
    （dirty / no-cycle / no-worktree / no-batch / conclusion-now-included），
    正常场景返回同步最终 state 并附 ``rerun`` 元信息。retained 模式将
    checkpoint 写入独立 ``rerun-*`` 线程（``rerun.cycle_id``），不覆盖来源
    周期投影。

    ``thread_id`` 由调用方预生成（P2-3 单一线程 id）：``_cmd_rerun`` 用它做
    register_start，与本函数内 checkpoint 线程一致，避免二次生成导致任务行
    cycle_id 与真实线程错位。缺省时内部生成（兼容直接调用/测试）。
    """
    if fresh:
        return _rerun_fresh(
            ctx, target=target, cycle=cycle, checkpointer=checkpointer,
            thread_id=thread_id,
        )
    return _rerun_retained(
        ctx, target=target, cycle=cycle, checkpointer=checkpointer,
        thread_id=thread_id,
    )


def cleanup_worktree_command(
    ctx: GraphContext, *, target: str, cycle_id: str
) -> dict:
    """删除指定目标分支在某周期的活 worktree（供平台删任务联动清理）。

    经全局 flock 串行化，避免与周期执行/清理并发；worktree 不存在时幂等成功。
    返回 dict：``removed=True``（删除了）/ ``removed=False``（不存在）。
    """
    path = _worktree_path(ctx, target, cycle_id)
    if not path.exists():
        return {"removed": False, "target": target, "cycle_id": cycle_id}
    with flock_acquire(Path(ctx.settings.log_dir) / "bsa.lock"):
        if path.exists():
            ctx.git.remove_worktree(path)
            return {"removed": True, "target": target, "cycle_id": cycle_id}
    return {"removed": False, "target": target, "cycle_id": cycle_id}
