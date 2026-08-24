from __future__ import annotations

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from bsa.graph.nodes import (
    GraphContext,
    build,
    cherry_pick,
    fix_build,
    generate_patch,
    node_wrapper,
    prepare_worktree,
    report,
    resolve_conflict,
)
from bsa.graph.state import TaskState
from bsa.graph.workflow import (
    _END_NODE,
    _make_route_after_build,
    _make_route_after_fix_build,
    _route_after_cherry_pick,
    _route_after_failfast,
    _route_after_next_branch,
    _route_after_next_commit,
    _route_after_patch,
    _route_after_prepare,
    _route_after_resolve,
    fail_fast,
    make_checkpointer,
    next_branch,
    next_commit,
)


def build_single_target_workflow(
    ctx: GraphContext,
    checkpointer: SqliteSaver | None = None,
) -> CompiledStateGraph:
    """单 target 同步子图：复用主图同步阶段的路由与节点函数。

    入口由 ``START → next_branch`` 直接进入同步阶段（跳过 detect_commits /
    sync_decision，决策已在命令层完成）；``batches`` 预置为 ``{target: [shas]}``，
    ``next_branch`` 自行推出 current_target。节点与条件边全部 import 自主图，
    保持 DRY（同一套 fail-fast / build 重试语义）。``checkpointer`` 默认内存版，
    生产由调用方传入持久化实例。
    """
    graph = StateGraph(TaskState)

    ctx_nodes = {
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

    graph.add_edge(START, "next_branch")
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
        {
            "cherry_pick": "cherry_pick",
            "generate_patch": "generate_patch",
            _END_NODE: _END_NODE,
        },
    )
    graph.add_conditional_edges(
        "cherry_pick",
        _route_after_cherry_pick,
        {
            "build": "build",
            "resolve_conflict": "resolve_conflict",
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
        checkpointer = make_checkpointer()
    return graph.compile(checkpointer=checkpointer)
