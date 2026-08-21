from __future__ import annotations

import sqlite3
from collections.abc import Callable

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
    build,
    cherry_pick,
    detect_commits,
    fix_build,
    generate_patch,
    node_wrapper,
    prepare_worktree,
    report,
    resolve_conflict,
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


def make_checkpointer(conn_string: str = ":memory:") -> SqliteSaver:
    """SqliteSaver whose serde allowlists bsa domain types (future-proof)."""
    conn = sqlite3.connect(conn_string, check_same_thread=False)
    serde = JsonPlusSerializer(allowed_msgpack_modules=_state_model_types())
    return SqliteSaver(conn, serde=serde)


_DEFAULT_CHECKPOINTER: SqliteSaver | None = None


def thread_config(cycle_id: str) -> dict[str, dict[str, str]]:
    """LangGraph config binding one cycle to one checkpoint thread (决策 12)."""
    return {"configurable": {"thread_id": cycle_id}}


def _next_target(state: dict) -> str | None:
    for target in state.get("batches") or {}:
        branch = (state.get("branch_results") or {}).get(target)
        if branch is None or branch.patch_path is None:
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
    models = ctx.safety.required_models()
    built = set(_built_outcomes(state))
    return any(model not in built for model in models)


def _agent_attempts(state: dict, ctx: GraphContext) -> int:
    outcomes = _built_outcomes(state)
    for model in reversed(ctx.safety.required_models()):
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


def fail_fast(state: dict, *, ctx: GraphContext) -> dict:
    """决策 7/32: judge whether subsequent commits relate to the failed one.

    Related → stop this branch's batch; unrelated → continue. LLM degraded /
    absent → conservative stop (停批). Branch-local, never leaks across targets.
    """
    target = state["current_target"]
    sha = state["current_commit"]
    commit = _current_commit_result(state)
    failed = FailedCommit(
        sha=sha,
        failure_summary=_failure_summary(state),
        changed_files=list(commit.changed_files),
    )
    if ctx.llm is None:
        related = True
    else:
        related = ctx.llm.judge_failfast_related(failed, _subsequent_commits(state))
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
        return "next_commit"
    if status == "CHERRY_PICK_FAILED":
        return "fail_fast"
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


def build_workflow(
    ctx: GraphContext,
    checkpointer: SqliteSaver | None = None,
) -> CompiledStateGraph:
    """Assemble the 9 nodes + selectors into the LangGraph StateGraph.

    All loops (branch / commit / model / fix-retry) and fail-fast routing run
    through state-reading conditional edges — never Python for-loops inside a
    node. ``checkpointer`` defaults to an in-memory SqliteSaver; production
    passes a persistent one via ``make_checkpointer``.
    """
    graph = StateGraph(TaskState)

    ctx_nodes = {
        "detect_commits": detect_commits,
        "sync_decision": sync_decision,
        "prepare_worktree": prepare_worktree,
        "cherry_pick": cherry_pick,
        "resolve_conflict": resolve_conflict,
        "build": build,
        "fix_build": fix_build,
        "generate_patch": generate_patch,
        "report": report,
    }
    for name, node in ctx_nodes.items():
        graph.add_node(name, node_wrapper(node, ctx=ctx))
    graph.add_node("next_branch", node_wrapper(next_branch))
    graph.add_node("next_commit", node_wrapper(next_commit))
    graph.add_node("fail_fast", node_wrapper(fail_fast, ctx=ctx))

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
        {"next_commit": "next_commit", _END_NODE: _END_NODE},
    )
    graph.add_conditional_edges(
        "next_commit",
        _route_after_next_commit,
        {"cherry_pick": "cherry_pick", "generate_patch": "generate_patch", _END_NODE: _END_NODE},
    )
    graph.add_conditional_edges(
        "cherry_pick",
        _route_after_cherry_pick,
        {
            "build": "build",
            "resolve_conflict": "resolve_conflict",
            "fail_fast": "fail_fast",
            "next_commit": "next_commit",
            _END_NODE: _END_NODE,
        },
    )
    graph.add_conditional_edges(
        "resolve_conflict",
        _route_after_resolve,
        {"build": "build", "fail_fast": "fail_fast", _END_NODE: _END_NODE},
    )
    graph.add_conditional_edges(
        "build",
        _make_route_after_build(ctx),
        {
            "build": "build",
            "fix_build": "fix_build",
            "next_commit": "next_commit",
            _END_NODE: _END_NODE,
        },
    )
    graph.add_conditional_edges(
        "fix_build",
        _make_route_after_fix_build(ctx),
        {
            "build": "build",
            "fix_build": "fix_build",
            "fail_fast": "fail_fast",
            "next_commit": "next_commit",
            _END_NODE: _END_NODE,
        },
    )
    graph.add_conditional_edges(
        "fail_fast",
        _route_after_failfast,
        {"generate_patch": "generate_patch", "next_commit": "next_commit", _END_NODE: _END_NODE},
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
