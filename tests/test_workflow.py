from __future__ import annotations

from pathlib import Path

from bsa.domain.models import CherryPickResult, Conclusion4
from bsa.graph.nodes import GraphContext
from bsa.graph.workflow import build_workflow, make_checkpointer, thread_config
from bsa.rules import Classification
from tests.test_graph_nodes import (
    DEVELOP,
    TARGET,
    FakeGit,
    FakeRunner,
    base_state,
    make_ctx,
    write_branch_md,
)

TARGET2 = "br_v4.33_5200_CU_develop_release_p361_20260625"

BRANCH_MD_TWO = f"""# 分支清单

## 组网
- {DEVELOP}
- {TARGET}
- {TARGET2}
"""


class FakeLLM:
    def __init__(self, related: bool = False) -> None:
        self.related = related
        self.calls: list[tuple] = []

    def judge_failfast_related(self, failed, subsequent):
        self.calls.append((failed, list(subsequent)))
        return self.related


class FakeWorktreeGit(FakeGit):
    def __init__(
        self,
        results: dict[str, CherryPickResult] | None = None,
        unmerged: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.cherry_pick_results = results or {}
        self.unmerged_files_result = list(unmerged or [])

    def cherry_pick(self, sha: str) -> CherryPickResult:
        self._record("cherry_pick", (sha,))
        return self.cherry_pick_results.get(sha, CherryPickResult(status="OK"))


def _classify(ctx: GraphContext, sha: str) -> None:
    ctx.classify.results[sha] = Classification(
        is_bug_fix=True,
        recognition_source="machine:[BUG]",
        needs_agent=False,
    )


def worktree_path_for(ctx: GraphContext, target: str, cycle_id: str = "cycle-20260101") -> Path:
    return Path(ctx.settings.worktree_root) / f"{target}-{cycle_id}"


def inject_worktree_git(
    ctx: GraphContext, fake: FakeWorktreeGit, target: str, cycle_id: str = "cycle-20260101"
) -> FakeWorktreeGit:
    ctx.worktree_gits[str(worktree_path_for(ctx, target, cycle_id))] = fake
    return fake


def worktree_git_for(
    ctx: GraphContext, target: str, cycle_id: str = "cycle-20260101"
) -> FakeWorktreeGit:
    return ctx.worktree_gits[str(worktree_path_for(ctx, target, cycle_id))]


def batch_ctx(
    tmp_path: Path,
    *,
    shas: list[str],
    target: str = TARGET,
    models: tuple[str, ...] = ("RTL9617C",),
    targets: list[str] | None = None,
) -> GraphContext:
    ctx = make_ctx(tmp_path)
    ctx.llm = FakeLLM()
    ctx.safety.models = list(models)
    git = ctx.git
    git.window_shas = {f"origin/{DEVELOP}": list(shas)}
    for sha in shas:
        git.changed[sha] = ["plat/demo.c"]
        git.patches[sha] = "+x"
        git.patch_ids[sha] = f"pid-{sha}"
        _classify(ctx, sha)
        ctx.conclude.results[(sha, target)] = Conclusion4(
            kind="NeedSync", evidence=[], confidence="high"
        )
    for t in (targets or [target]):
        inject_worktree_git(ctx, FakeWorktreeGit(), t)
    return ctx


def run(graph, state: dict, *, cycle_id: str = "cycle-20260101") -> dict:
    return graph.invoke(state, config=thread_config(cycle_id))


def cherry_picked(git: FakeGit) -> list[str]:
    return [args[0] for name, args in git.calls if name == "cherry_pick"]


def test_thread_config_binds_cycle_id():
    assert thread_config("cycle-x") == {"configurable": {"thread_id": "cycle-x"}}


def test_empty_check_routes_to_report(tmp_path):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    ctx.llm = FakeLLM()
    ctx.git.window_shas = {f"origin/{DEVELOP}": ["a1"]}
    ctx.git.changed["a1"] = ["plat/demo.c"]
    ctx.git.patches["a1"] = "+x"
    _classify(ctx, "a1")

    out = run(build_workflow(ctx), base_state())

    assert out["batches"] == {}
    assert out["report"] is not None
    assert out["report"].summary["commits_detected"] == 1
    assert out["report"].summary["branches"] == []
    assert out["status"] == "REPORTED"


def test_single_branch_success_patch_and_report(tmp_path):
    write_branch_md(tmp_path)
    ctx = batch_ctx(tmp_path, shas=["a1"])

    out = run(build_workflow(ctx), base_state())

    assert out["status"] == "REPORTED"
    assert out["report"] is not None
    branch = out["branch_results"][TARGET]
    assert branch.status == "SUCCESS"
    assert branch.patch_path is not None
    assert branch.commits[0].cherry_pick == "OK"
    assert branch.commits[0].build["RTL9617C"].status == "OK"


def test_multi_commit_order_per_branch(tmp_path):
    write_branch_md(tmp_path)
    ctx = batch_ctx(tmp_path, shas=["a1", "a2"])

    out = run(build_workflow(ctx), base_state())

    assert cherry_picked(worktree_git_for(ctx, TARGET)) == ["a1", "a2"]
    assert out["report"] is not None
    assert out["branch_results"][TARGET].status == "SUCCESS"


def test_per_model_serial_loop(tmp_path):
    write_branch_md(tmp_path)
    ctx = batch_ctx(tmp_path, shas=["a1"], models=("RTL9617C", "RTL9607F"))

    out = run(build_workflow(ctx), base_state())

    models = [call["model"] for call in ctx.runner.build_calls]
    assert models == ["RTL9617C", "RTL9607F"]
    build = out["branch_results"][TARGET].commits[0].build
    assert build["RTL9617C"].status == "OK"
    assert build["RTL9607F"].status == "OK"


def test_cycle_rerun_prepare_reuses_existing_worktree(tmp_path):
    write_branch_md(tmp_path)
    ctx = batch_ctx(tmp_path, shas=["a1"])
    wt = worktree_path_for(ctx, TARGET)
    wt.mkdir(parents=True)

    out = run(build_workflow(ctx), base_state())

    assert out["status"] == "REPORTED"
    assert out["branch_results"][TARGET].status == "SUCCESS"
    assert not any(name == "add_worktree" for name, args in ctx.git.calls)


def test_multi_branch_loop_uses_per_branch_worktree_git(tmp_path):
    path = tmp_path / "branch.md"
    path.write_text(BRANCH_MD_TWO, encoding="utf-8")
    ctx = batch_ctx(tmp_path, shas=["a1"], targets=[TARGET, TARGET2])
    ctx.conclude.results[("a1", TARGET2)] = Conclusion4(
        kind="NeedSync", evidence=[], confidence="high"
    )

    out = run(build_workflow(ctx), base_state())

    assert cherry_picked(worktree_git_for(ctx, TARGET)) == ["a1"]
    assert cherry_picked(worktree_git_for(ctx, TARGET2)) == ["a1"]
    assert ctx.worktree_path == worktree_path_for(ctx, TARGET2)
    assert out["branch_results"][TARGET].patch_path is not None
    assert out["branch_results"][TARGET2].patch_path is not None
    assert out["report"].summary["branches"] == sorted([TARGET, TARGET2])


def test_conflict_resolved_continue_build_patch_success(tmp_path):
    write_branch_md(tmp_path)
    ctx = batch_ctx(tmp_path, shas=["a1"])
    from bsa.domain.models import ConflictResolution

    ctx.conflict_agent.resolution = ConflictResolution(
        files=["plat/demo.c"], diff="+fixed", agent_reason="resolved"
    )
    inject_worktree_git(
        ctx,
        FakeWorktreeGit(
            results={"a1": CherryPickResult(status="CONFLICT")}, unmerged=["plat/demo.c"]
        ),
        TARGET,
    )

    out = run(build_workflow(ctx), base_state())

    wg = worktree_git_for(ctx, TARGET)
    assert wg.cherry_pick_continue_calls == 1
    branch = out["branch_results"][TARGET]
    assert branch.commits[0].cherry_pick == "OK"
    assert branch.commits[0].conflict_resolution is not None
    assert branch.commits[0].build["RTL9617C"].status == "OK"
    assert branch.status == "SUCCESS"
    assert branch.patch_path is not None
    assert out["report"] is not None


def test_failfast_related_stops_batch(tmp_path):
    write_branch_md(tmp_path)
    ctx = batch_ctx(tmp_path, shas=["a1", "a2"])
    ctx.llm = FakeLLM(related=True)
    ctx.conflict_agent.resolution = None
    inject_worktree_git(
        ctx,
        FakeWorktreeGit(
            results={"a1": CherryPickResult(status="CONFLICT")}, unmerged=["plat/demo.c"]
        ),
        TARGET,
    )

    out = run(build_workflow(ctx), base_state())

    assert cherry_picked(worktree_git_for(ctx, TARGET)) == ["a1"]
    assert len(ctx.llm.calls) == 1
    assert out["branch_results"][TARGET].stop_reason is not None
    assert out["report"] is not None


def test_failfast_not_related_continues(tmp_path):
    write_branch_md(tmp_path)
    ctx = batch_ctx(tmp_path, shas=["a1", "a2"])
    ctx.llm = FakeLLM(related=False)
    ctx.conflict_agent.resolution = None
    inject_worktree_git(
        ctx,
        FakeWorktreeGit(
            results={
                "a1": CherryPickResult(status="CONFLICT"),
                "a2": CherryPickResult(status="OK"),
            },
            unmerged=["plat/demo.c"],
        ),
        TARGET,
    )

    out = run(build_workflow(ctx), base_state())

    assert cherry_picked(worktree_git_for(ctx, TARGET)) == ["a1", "a2"]
    assert len(ctx.llm.calls) == 1
    assert out["branch_results"][TARGET].stop_reason is None
    assert out["report"] is not None


def test_cherry_pick_failed_routes_to_report_not_failfast(tmp_path):
    write_branch_md(tmp_path)
    ctx = batch_ctx(tmp_path, shas=["a1"])
    inject_worktree_git(
        ctx,
        FakeWorktreeGit(results={"a1": CherryPickResult(status="FAILED")}),
        TARGET,
    )

    out = run(build_workflow(ctx), base_state())

    assert ctx.llm.calls == []
    assert out["status"] == "REPORTED"
    assert out["branch_results"][TARGET].stop_reason is None
    assert out["branch_results"][TARGET].commits[0].cherry_pick == "FAILED"


def test_fix_build_fails_after_max_attempts_then_failfast(tmp_path):
    write_branch_md(tmp_path)
    ctx = batch_ctx(tmp_path, shas=["a1"])
    ctx.llm = FakeLLM(related=True)
    ctx.runner = FakeRunner(success=False)

    out = run(build_workflow(ctx), base_state())

    assert len(ctx.build_agent.calls) == 3
    assert len(ctx.llm.calls) == 1
    assert out["branch_results"][TARGET].stop_reason is not None
    outcome = out["branch_results"][TARGET].commits[0].build["RTL9617C"]
    assert outcome.status == "FAILED"
    assert outcome.agent_attempts == 3


def test_checkpoint_resume_reuses_state_not_recomputed(tmp_path):
    write_branch_md(tmp_path)
    ctx = batch_ctx(tmp_path, shas=["a1"])
    checkpointer = make_checkpointer()
    try:
        graph = build_workflow(ctx, checkpointer=checkpointer)
        cfg = thread_config("cycle-20260101")

        out1 = graph.invoke(base_state(), cfg)
        assert out1["report"] is not None

        build_calls_before = list(ctx.runner.build_calls)
        classify_calls_before = list(ctx.classify.calls)
        fetch_before = [c for c in ctx.git.calls if c[0] == "fetch_all"]

        out2 = graph.invoke(None, cfg)

        assert out2["report"] == out1["report"]
        assert out2["batches"] == {TARGET: ["a1"]}
        assert ctx.runner.build_calls == build_calls_before
        assert ctx.classify.calls == classify_calls_before
        assert [c for c in ctx.git.calls if c[0] == "fetch_all"] == fetch_before
    finally:
        checkpointer.conn.close()
