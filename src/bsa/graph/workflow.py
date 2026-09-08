from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel

from bsa.agents.base import FailedCommit
from bsa.domain import models as _domain_models
from bsa.domain.models import BuildOutcome, CommitInfo
from bsa.graph.nodes import (
    GraphContext,
    _branch_results,
    _find_commit,
    _target_models,
    baseline_build,
    build,
    cherry_pick,
    detect_commits,
    generate_patch,
    node_wrapper,
    prepare_worktree,
    report,
    sync_decision,
)
from bsa.graph.state import TaskState

_END_NODE = "report"


def _state_model_types() -> tuple[type[BaseModel], ...]:
    types: list[type[BaseModel]] = [
        cls
        for cls in vars(_domain_models).values()
        if isinstance(cls, type) and issubclass(cls, BaseModel)
    ]
    types.append(FailedCommit)
    return tuple(types)


def _new_saver(conn: sqlite3.Connection) -> SqliteSaver:
    """SqliteSaver whose serde allowlists bsa domain types (future-proof)."""
    serde = JsonPlusSerializer(allowed_msgpack_modules=_state_model_types())
    return SqliteSaver(conn, serde=serde)


def make_checkpointer(conn_string: str = ":memory:") -> SqliteSaver:
    """SqliteSaver whose serde allowlists bsa domain types (future-proof)."""
    conn = sqlite3.connect(conn_string, check_same_thread=False)
    return _new_saver(conn)


@contextmanager
def open_checkpointer(conn_string: str) -> Iterator[SqliteSaver]:
    """Context-managed persistent checkpointer; closes the connection on exit.

    Production lifecycle (3.2b concern): the scheduler wraps an entire cycle
    in ``with open_checkpointer(db_path) as cp:`` so the sqlite connection is
    owned and released exactly once.
    """
    conn = sqlite3.connect(conn_string, check_same_thread=False)
    try:
        # WAL + busy_timeout: 平台投影并发读与周期写入不撞锁（任务 3）
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        yield _new_saver(conn)
    finally:
        conn.close()


_DEFAULT_CHECKPOINTER: SqliteSaver | None = None


def thread_config(cycle_id: str) -> dict[str, dict[str, str]]:
    """LangGraph config binding one cycle to one checkpoint thread (决策 12)."""
    return {"configurable": {"thread_id": cycle_id}}


def _next_target(state: dict) -> str | None:
    for target in state.get("batches") or {}:
        branch = (state.get("branch_results") or {}).get(target)
        if branch is None or branch.patch_path is None:
            # 跳过已标记 FAILED 的分支（基线编译失败阻塞），避免重复选中死循环。
            if branch is not None and branch.status == "FAILED":
                continue
            return target
    return None


def next_branch(state: dict) -> dict:
    """Select the next unfinished target branch, else end-of-cycle marker."""
    return {"current_target": _next_target(state)}


def next_commit(state: dict) -> dict:
    """Select the next unprocessed sha in the current branch's frozen batch."""
    target = state.get("current_target")
    batch = (state.get("batches") or {}).get(target, [])
    branch = (state.get("branch_results") or {}).get(target)
    done = {commit.sha for commit in branch.commits} if branch is not None else set()
    for sha in batch:
        if sha not in done:
            return {"current_commit": sha}
    return {"current_commit": None}


def _current_commit_result(state: dict) -> CommitInfo | None:
    sha = state.get("current_commit")
    if not sha:
        return None
    return _find_commit(state, sha)


def _built_outcomes(state: dict) -> dict[str, BuildOutcome]:
    target = state.get("current_target")
    sha = state.get("current_commit")
    branch = (state.get("branch_results") or {}).get(target)
    if branch is None or not sha:
        return {}
    for commit in branch.commits:
        if commit.sha == sha:
            return dict(commit.build)
    return {}


def _remaining_models(state: dict, ctx: GraphContext) -> bool:
    models = _target_models(state, ctx)
    built = set(_built_outcomes(state))
    return any(model not in built for model in models)


def _agent_attempts(state: dict, ctx: GraphContext) -> int:
    outcomes = _built_outcomes(state)
    for model in reversed(_target_models(state, ctx)):
        if model in outcomes:
            return outcomes[model].agent_attempts
    return 0


def _failure_summary(state: dict) -> str:
    for model, outcome in _built_outcomes(state).items():
        if outcome.status == "FAILED":
            return f"build failed on {model} after {outcome.agent_attempts + 1} attempts"
    return f"cherry-pick/conflict unresolved for {state.get('current_commit')}"


def _subsequent_commits(state: dict) -> list[CommitInfo]:
    target = state.get("current_target")
    sha = state.get("current_commit")
    batch = (state.get("batches") or {}).get(target, [])
    if sha not in batch:
        return []
    by_sha = {commit.sha: commit for commit in state.get("detected_commits") or []}
    return [by_sha[s] for s in batch[batch.index(sha) + 1 :] if s in by_sha]


_HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _patch_hunk_ranges(patch_text: str) -> dict[str, list[tuple[int, int]]]:
    """Per-file new-side line ranges from unified-diff hunks (``@@ -a,b +c,d @@``)."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    file: str | None = None
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            match = re.search(r" b/(.+)$", line)
            file = match.group(1) if match else None
            if file is not None:
                ranges.setdefault(file, [])
            continue
        if file is None:
            continue
        m = _HUNK_RE.match(line)
        if m:
            start = int(m.group(1))
            count = int(m.group(2) or "1")
            if start > 0:
                ranges[file].append((start, start + count))
    return ranges


def _ranges_close(
    a: list[tuple[int, int]], b: list[tuple[int, int]], gap: int
) -> bool:
    for start1, end1 in a:
        for start2, end2 in b:
            if max(start1 - end2, start2 - end1) <= gap:
                return True
    return False


def _ambiguous_subsequent(
    failed: CommitInfo, subsequent: list[CommitInfo], *, gap: int
) -> list[CommitInfo]:
    """决策 3 layers 1+2: 过滤确定无关的后续 commit，返回仍模糊者。

    Layer 1 (file-level): 与失败 commit 无 changed_files 交集 → 无关。
    Layer 2 (region-level): 重叠文件 hunk 行区间相距 > gap → 无关；
    区间接近/重叠或无法提取区间 → 模糊，交 layer 3（LLM）。
    """
    failed_files = set(failed.changed_files)
    failed_ranges = _patch_hunk_ranges(failed.patch_text)
    ambiguous: list[CommitInfo] = []
    for ci in subsequent:
        overlap = failed_files & set(ci.changed_files)
        if not overlap:
            continue
        ci_ranges = _patch_hunk_ranges(ci.patch_text)
        close = False
        for path in overlap:
            fr = failed_ranges.get(path)
            cr = ci_ranges.get(path)
            if fr is None or cr is None or _ranges_close(fr, cr, gap):
                close = True
                break
        if close:
            ambiguous.append(ci)
    return ambiguous


def fail_fast(state: dict, *, ctx: GraphContext) -> dict:
    """决策 7/32: judge whether subsequent commits relate to the failed one.

    Related → stop this branch's batch; unrelated → continue. 决策 3 三层方案：
    layer 1 文件级交集、layer 2 区域级行区间距离（failfast_region_gap），仅
    仍模糊的 commit 进 layer 3 LLM。LLM degraded / absent → 模糊者保守停批。
    Branch-local, never leaks across targets.
    """
    target = state["current_target"]
    sha = state["current_commit"]
    commit = _current_commit_result(state)
    subsequent = _subsequent_commits(state)
    failed = FailedCommit(
        sha=sha,
        failure_summary=_failure_summary(state),
        changed_files=list(commit.changed_files),
    )
    ambiguous = _ambiguous_subsequent(
        commit, subsequent, gap=ctx.settings.failfast_region_gap
    )
    if ambiguous and (
        ctx.llm is None or ctx.llm.judge_failfast_related(failed, ambiguous)
    ):
        related = True
    else:
        related = False
    results, branch = _branch_results(state, ctx, target)
    stop_reason = (
        f"fail-fast: {sha} failed; subsequent commits judged related" if related else None
    )
    results[target] = branch.model_copy(update={"stop_reason": stop_reason})
    return {
        "branch_results": results,
        "status": "FAILFAST_STOP" if related else "FAILFAST_CONTINUE",
    }


def _route_after_decision(state: dict) -> str:
    if state.get("status") == "FAILED":
        return _END_NODE
    if not (state.get("batches") or {}):
        return _END_NODE
    return "next_branch"


def _route_after_next_branch(state: dict) -> str:
    if state.get("status") == "FAILED" or not state.get("current_target"):
        return _END_NODE
    return "prepare_worktree"


def _route_after_prepare(state: dict) -> str:
    if state.get("status") == "FAILED":
        return _END_NODE
    return "baseline_build"


def _route_after_baseline(state: dict) -> str:
    if state.get("status") == "FAILED":
        return _END_NODE
    if state.get("status") == "BASELINE_FAILED":
        # 基线编译失败 → 分支阻塞（branch.status=FAILED），跳过该分支去下一分支。
        return "next_branch"
    return "next_commit"


def _route_after_next_commit(state: dict) -> str:
    if state.get("status") == "FAILED":
        return _END_NODE
    if not state.get("current_commit"):
        return "generate_patch"
    return "cherry_pick"


def _route_after_cherry_pick(state: dict) -> str:
    status = state.get("status", "")
    if status == "FAILED":
        return _END_NODE
    if status == "CHERRY_PICK_CONFLICT":
        return "resolve_conflict"
    if status == "CHERRY_PICK_EMPTY":
        # EMPTY = 内容已应用（不变量 #17）：建立 worktree 时 baseline_build 已对
        # 目标 tip 全量编译通过，EMPTY 未引入任何改动，跳过编译直接下一个 commit。
        return "next_commit"
    if status == "CHERRY_PICK_FAILED":
        # 决策 18 node boundary: a non-conflict cherry-pick failure is an
        # infrastructure error → report path, NOT fail-fast (决策 32).
        return _END_NODE
    return "build"


def _route_after_resolve(state: dict) -> str:
    status = state.get("status", "")
    if status == "FAILED":
        return _END_NODE
    if status == "RESOLUTION_FAILED":
        return "fail_fast"
    return "build"


def _make_route_after_build(ctx: GraphContext) -> Callable[[dict], str]:
    def route(state: dict) -> str:
        if state.get("status") == "FAILED":
            return _END_NODE
        if state.get("status") != "BUILD_OK":
            return "fix_build"
        if _remaining_models(state, ctx):
            return "build"
        return "next_commit"

    return route


def _make_route_after_fix_build(ctx: GraphContext) -> Callable[[dict], str]:
    def route(state: dict) -> str:
        if state.get("status") == "FAILED":
            return _END_NODE
        if state.get("status") == "BUILD_OK":
            if _remaining_models(state, ctx):
                return "build"
            return "next_commit"
        # LLM 不可用 / 无法归因：立即停批，不空转重编译
        if state.get("status") == "UNRESOLVABLE":
            return "fail_fast"
        if _agent_attempts(state, ctx) >= ctx.settings.max_build_attempts:
            return "fail_fast"
        return "fix_build"

    return route


def _route_after_failfast(state: dict) -> str:
    if state.get("status") == "FAILED":
        return _END_NODE
    if state.get("status") == "FAILFAST_STOP":
        return "generate_patch"
    return "next_commit"


def _route_after_patch(state: dict) -> str:
    if state.get("status") == "FAILED":
        return _END_NODE
    return "next_branch"


def _route_after_cherry_pick_cron(state: dict) -> str:
    """Cron 专用：cherry-pick 后的停批路由（同步→编译→通知，冲突不自动消解）。

    冲突 → 直接停本分支批收尾 generate_patch（不 resolve_conflict）；EMPTY =
    内容已应用跳过编译去下一 commit（不变量 #17）；非冲突基础设施失败 → 报告。
    """
    status = state.get("status", "")
    if status == "FAILED":
        return _END_NODE
    if status == "CHERRY_PICK_CONFLICT":
        return "generate_patch"
    if status == "CHERRY_PICK_EMPTY":
        return "next_commit"
    if status == "CHERRY_PICK_FAILED":
        return _END_NODE
    return "build"


def _make_route_after_build_cron(ctx: GraphContext) -> Callable[[dict], str]:
    def route(state: dict) -> str:
        """Cron 专用：build 后失败即停批（不 fix_build 自动修复），多型号内循环保留。"""
        if state.get("status") == "FAILED":
            return _END_NODE
        if state.get("status") == "BUILD_OK":
            if _remaining_models(state, ctx):
                return "build"
            return "next_commit"
        return "generate_patch"

    return route


def build_workflow(
    ctx: GraphContext,
    checkpointer: SqliteSaver | None = None,
) -> CompiledStateGraph:
    """Assemble the lean cron graph: 同步→编译→通知，无判断/解决类 LLM 节点。

    cron 只做确定性同步链：detect → sync_decision(全量冻结) → prepare → baseline
    → (cherry_pick → build，型号内循环) → generate_patch → report。冲突与编译失败
    直接停本分支批收尾（不 resolve_conflict / fix_build / fail_fast 重试）。manual
    的完整 agent 流程走 single_target.py 的 build_single_target_workflow，不受影响。
    ``checkpointer`` 默认内存版，生产由调用方传入持久化实例。
    """
    graph = StateGraph(TaskState)

    ctx_nodes = {
        "detect_commits": detect_commits,
        "sync_decision": sync_decision,
        "prepare_worktree": prepare_worktree,
        "baseline_build": baseline_build,
        "cherry_pick": cherry_pick,
        "build": build,
        "generate_patch": generate_patch,
        "report": report,
    }
    for name, node in ctx_nodes.items():
        graph.add_node(name, node_wrapper(node, ctx=ctx))
    graph.add_node("next_branch", node_wrapper(next_branch))
    graph.add_node("next_commit", node_wrapper(next_commit))

    graph.add_edge(START, "detect_commits")
    graph.add_edge("detect_commits", "sync_decision")
    graph.add_conditional_edges(
        "sync_decision",
        _route_after_decision,
        {"next_branch": "next_branch", _END_NODE: _END_NODE},
    )
    graph.add_conditional_edges(
        "next_branch",
        _route_after_next_branch,
        {"prepare_worktree": "prepare_worktree", _END_NODE: _END_NODE},
    )
    graph.add_conditional_edges(
        "prepare_worktree",
        _route_after_prepare,
        {"baseline_build": "baseline_build", _END_NODE: _END_NODE},
    )
    graph.add_conditional_edges(
        "baseline_build",
        _route_after_baseline,
        {
            "next_commit": "next_commit",
            "next_branch": "next_branch",
            _END_NODE: _END_NODE,
        },
    )
    graph.add_conditional_edges(
        "next_commit",
        _route_after_next_commit,
        {"cherry_pick": "cherry_pick", "generate_patch": "generate_patch", _END_NODE: _END_NODE},
    )
    graph.add_conditional_edges(
        "cherry_pick",
        _route_after_cherry_pick_cron,
        {
            "build": "build",
            "generate_patch": "generate_patch",
            "next_commit": "next_commit",
            _END_NODE: _END_NODE,
        },
    )
    graph.add_conditional_edges(
        "build",
        _make_route_after_build_cron(ctx),
        {
            "build": "build",
            "generate_patch": "generate_patch",
            "next_commit": "next_commit",
            _END_NODE: _END_NODE,
        },
    )
    graph.add_conditional_edges(
        "generate_patch",
        _route_after_patch,
        {"next_branch": "next_branch", _END_NODE: _END_NODE},
    )
    graph.add_edge(_END_NODE, END)

    if checkpointer is None:
        global _DEFAULT_CHECKPOINTER
        if _DEFAULT_CHECKPOINTER is None:
            _DEFAULT_CHECKPOINTER = make_checkpointer()
        checkpointer = _DEFAULT_CHECKPOINTER
    return graph.compile(checkpointer=checkpointer)
