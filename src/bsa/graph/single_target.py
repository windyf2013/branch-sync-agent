from __future__ import annotations

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from bsa.graph.nodes import (
    GraphContext,
    baseline_build,
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
    _make_route_after_build_cron,
    _make_route_after_fix_build,
    _route_after_baseline,
    _route_after_cherry_pick,
    _route_after_cherry_pick_cron,
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
        "baseline_build": baseline_build,
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


def build_lean_single_target_workflow(
    ctx: GraphContext,
    checkpointer: SqliteSaver | None = None,
) -> CompiledStateGraph:
    """单 target 的 cron 精简子图：同步→编译→停批，无判断/解决类 LLM 节点。

    与 ``build_single_target_workflow`` 的唯一区别是冲突/编译失败的路由：这里复用
    cron 主图的 ``_route_after_cherry_pick_cron`` 与 ``_make_route_after_build_cron``
    —— cherry-pick 冲突直接 ``generate_patch`` 停批转人工（不调 ConflictAgent），
    编译失败直接停批（不走 BuildAgent 自动修复、不 fail_fast 重试）。

    存在的原因是 rerun 必须复刻原任务的流程语义：cron 周期的活儿本就跑在无 LLM 的
    精简链路上（``build_workflow``），但那条图入口是 ``detect_commits`` 会重新扫描，
    而 rerun 的批次已经冻结，只能用 ``next_branch`` 入口。故需要这张「精简路由 +
    单目标入口」的图。用错了就会把 cron 有意解耦掉的 LLM 又接回来（task 42 实测：
    cherry-pick 撞冲突时凭空调 LLM 并留下 UU 现场）。
    """
    graph = StateGraph(TaskState)

    # 刻意不注册 resolve_conflict / fix_build / fail_fast：精简语义下没有这些节点，
    # 路由函数也不会指向它们（与 cron 主图摘除这三个节点一致）。
    ctx_nodes = {
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

    graph.add_edge(START, "next_branch")
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
        {
            "cherry_pick": "cherry_pick",
            "generate_patch": "generate_patch",
            _END_NODE: _END_NODE,
        },
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
        {"build": "build", "next_commit": "next_commit", "generate_patch": "generate_patch",
         _END_NODE: _END_NODE},
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
