from __future__ import annotations

from pathlib import Path

from bsa.graph import build_graph_context, build_workflow
from tests.test_graph_nodes import make_settings


def test_build_graph_context_constructs_real_services(tmp_path):
    settings = make_settings(tmp_path)

    ctx = build_graph_context(settings)

    assert ctx.settings is settings
    assert ctx.git is not None
    assert ctx.git.repo_path == Path(settings.repo_path)
    assert ctx.runner is not None
    assert ctx.safety is not None
    assert ctx.decision_rules is not None
    assert ctx.llm is not None
    assert ctx.sync_decision_agent is not None
    assert ctx.conflict_agent is not None
    assert ctx.build_agent is not None
    assert ctx.safety.required_models() == ["RTL9617C", "RTL9617C_DVB"]
    assert ctx.worktree_gits == {}


def test_build_graph_context_smoke_compile(tmp_path):
    settings = make_settings(tmp_path)

    ctx = build_graph_context(settings)
    graph = build_workflow(ctx)

    assert graph is not None


def test_build_graph_context_exposes_cycle_id_to_runner(tmp_path):
    settings = make_settings(tmp_path)

    ctx = build_graph_context(settings, cycle_id="cycle-abc")

    assert ctx.runner.cycle_id == "cycle-abc"


def test_factory_entry_point_matches_module_function():
    from bsa.graph import factory as factory_module

    assert build_graph_context is factory_module.build_graph_context
