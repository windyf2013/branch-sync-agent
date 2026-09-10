"""同步拓扑（源 → 目标配对）进 state 的回归闸门。

背景：`HomologousSet` 只在 GraphContext.matrix（内存）里缓存，图 invoke 结束后随
ctx 丢弃。报告渲染期只拿得到扁平的 sources/targets 两个无配对列表，「哪个源喂哪个
目标」永久丢失 —— 这是报告「看不出源目分支」的根因。

LangGraph 按 TaskState 的 TypedDict 声明建 channel，**未声明的键在节点 update 中被
静默丢弃**（无 warning、无异常）。所以本文件既验证投影函数，也验证「声明齐了」，
否则 topology 会在真实 invoke 里凭空消失而单测全绿。
"""

from __future__ import annotations

from bsa.graph.state import TaskState
from bsa.report.topology import matrix_to_topology
from bsa.rules.branch_md import BranchRef, HomologousSet
from tests.test_graph_nodes import base_state, make_ctx, write_branch_md


def _ref(name: str, branch_type: str = "develop", section: str = "4.34") -> BranchRef:
    return BranchRef(name=name, branch_type=branch_type, section=section)


def _hs(section: str, sources: list[str], targets: list[str]) -> HomologousSet:
    return HomologousSet(
        section=section,
        sources=[_ref(n, section=section) for n in sources],
        need_sync_targets=[_ref(n, section=section) for n in targets],
    )


class TestMatrixToTopology:
    def test_projects_sections_with_names_only(self):
        matrix = [_hs("4.34", ["br_msg", "br_fttr"], ["br_dev"])]

        assert matrix_to_topology(matrix) == [
            {"section": "4.34", "sources": ["br_fttr", "br_msg"], "targets": ["br_dev"]}
        ]

    def test_sorts_names_for_stable_output(self):
        # 分支文件书写顺序不该影响报告呈现（同一输入永远同一输出）。
        matrix = [
            HomologousSet(
                section="4.34",
                sources=[_ref("br_z"), _ref("br_a")],
                need_sync_targets=[_ref("br_m"), _ref("br_b")],
            )
        ]

        projected = matrix_to_topology(matrix)

        assert projected[0]["sources"] == ["br_a", "br_z"]
        assert projected[0]["targets"] == ["br_b", "br_m"]

    def test_multiple_sections_keep_their_own_pairs(self):
        matrix = [
            _hs("4.34", ["br_a"], ["br_main_a"]),
            _hs("4.36", ["br_b"], ["br_main_b"]),
        ]

        projected = matrix_to_topology(matrix)

        assert [p["section"] for p in projected] == ["4.34", "4.36"]
        assert projected[1] == {
            "section": "4.36",
            "sources": ["br_b"],
            "targets": ["br_main_b"],
        }

    def test_empty_matrix_is_empty_list(self):
        # 空矩阵不是「静默」：detect_commits 已为此写 errors["branch_matrix"] 响亮报错。
        # 这里只保证投影不伪造配对、不抛。
        assert matrix_to_topology([]) == []

    def test_output_is_plain_json_data(self):
        # 必须是纯 dict/list/str —— pydantic 模型进 checkpoint 需要扩 serde allowlist。
        projected = matrix_to_topology([_hs("4.34", ["br_a"], ["br_main"])])

        assert all(isinstance(v, (str, list)) for v in projected[0].values())
        assert all(isinstance(n, str) for n in projected[0]["sources"])


class TestTopologyDeclared:
    """声明闸门：缺任何一处，topology 在真实 invoke 里都会被静默丢弃。"""

    def test_task_state_declares_topology(self):
        assert "topology" in TaskState.__annotations__

    def test_initial_state_has_topology(self):
        from bsa.scheduler.cycle import _initial_state

        assert _initial_state("c")["topology"] == []

    def test_projection_payload_includes_topology(self):
        from bsa.report.projection import projection_payload

        topology = [{"section": "4.34", "sources": ["br_a"], "targets": ["br_main"]}]

        assert projection_payload(base_state(topology=topology))["topology"] == topology

    def test_projection_payload_topology_defaults_empty(self):
        # 旧周期 state.json 无 topology → 空列表兜底，不崩（存量降级）。
        from bsa.report.projection import projection_payload

        assert projection_payload(base_state())["topology"] == []


class TestDetectCommitsWritesTopology:
    def test_update_carries_topology_projections(self, tmp_path):
        write_branch_md(tmp_path)
        ctx = make_ctx(tmp_path)

        from bsa.graph.nodes import detect_commits

        update = detect_commits(base_state(), ctx)

        assert update["topology"], "detect_commits 必须把 ctx.matrix 投影进 state"
        assert set(update["topology"][0]) == {"section", "sources", "targets"}

    def test_workflow_invoke_does_not_drop_topology(self, tmp_path):
        """端到端证明「声明齐了」。

        只测 detect_commits 的返回 dict 是不够的 —— 那个 dict 本来就带 topology；
        真正要证明的是它**活着穿过 LangGraph 的 channel 过滤**。
        """
        from langgraph.graph import END, START, StateGraph

        from bsa.graph.nodes import detect_commits, node_wrapper
        from bsa.graph.workflow import make_checkpointer, thread_config

        write_branch_md(tmp_path)
        ctx = make_ctx(tmp_path)

        builder = StateGraph(TaskState)
        builder.add_node("detect", node_wrapper(detect_commits, ctx))
        builder.add_edge(START, "detect")
        builder.add_edge("detect", END)
        graph = builder.compile(checkpointer=make_checkpointer())

        final = graph.invoke(base_state(), thread_config("t-topology"))

        assert final.get("topology"), "topology 被 LangGraph 丢弃了 —— TaskState 未声明？"
