from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bsa.cli import main
from bsa.commands.sync import (
    build_sha_batch,
    build_source_target_batch,
    manual_cycle_id,
    run_sync_command,
)
from bsa.domain.models import (
    BranchResult,
    CherryPickResult,
    Conclusion4,
    SyncDecision,
)
from bsa.executor.exceptions import SafetyViolation
from bsa.graph.workflow import open_checkpointer
from bsa.report.projection import read_cycle_state
from bsa.rules import Classification, TargetSnapshot
from tests.test_config import valid_env
from tests.test_graph_nodes import (
    DEVELOP,
    TARGET,
    FakeGit,
    FakeSafety,
    commit,
    make_ctx,
    write_branch_md,
)
from tests.test_workflow import FakeLLM


def worktree_path_for(ctx, target: str, cycle_id: str) -> Path:
    return Path(ctx.settings.worktree_root) / f"{target}-{cycle_id}"


def inject_worktree_git(ctx, fake: FakeGit, target: str, cycle_id: str) -> FakeGit:
    ctx.worktree_gits[str(worktree_path_for(ctx, target, cycle_id))] = fake
    return fake


def ok_worktree_git() -> FakeGit:
    wg = FakeGit()
    wg.cherry_pick_result = CherryPickResult(status="OK")
    wg.tips = {TARGET: ("origin/" + TARGET, "base-tip")}
    return wg


def _target_snapshot(**kw) -> TargetSnapshot:
    fields = {"branch_name": TARGET, "branch_type": "release"}
    fields.update(kw)
    return TargetSnapshot(**fields)


def _setup_target_ctx(ctx) -> FakeGit:
    ctx.git.tips = {TARGET: ("origin/" + TARGET, "tip1")}
    wg = ok_worktree_git()
    inject_worktree_git(ctx, wg, TARGET, "manual-20260824-101010")
    return wg


# --- --sha 直同步 ---


def test_sha_mode_success_path(tmp_path):
    ctx = make_ctx(tmp_path)
    _setup_target_ctx(ctx)

    batch = build_sha_batch(ctx, DEVELOP, ["a1"])
    assert len(batch) == 1
    assert batch[0].source_branch == DEVELOP
    assert batch[0].sha == "a1"

    final = run_sync_command(
        ctx, cycle_id="manual-20260824-101010", target=TARGET, batch=batch, checkpointer=None
    )

    assert final["status"] == "SUCCESS"
    branch = final["branch_results"][TARGET]
    assert branch.status == "SUCCESS"
    assert branch.patch_path is not None
    assert branch.commits[0].cherry_pick == "OK"
    assert branch.commits[0].build["RTL9617C"].status == "OK"


def test_sha_mode_batch_reordered_by_committed_at(tmp_path):
    """--sha 直同步批次按合入时间（committed_at 从旧到新）重排，而非给定/勾选顺序。"""
    ctx = make_ctx(tmp_path)
    ctx.git.metadata_results = {
        "newer": ("dev", "2026-08-25T09:00:00+08:00", "newest"),
        "middle": ("dev", "2026-08-22T09:00:00+08:00", "middle"),
        "older": ("dev", "2026-08-20T09:00:00+08:00", "oldest"),
    }

    batch = build_sha_batch(ctx, DEVELOP, ["newer", "older", "middle"])

    assert [c.sha for c in batch] == ["older", "middle", "newer"]


def _sdwan_build_rules():
    from bsa.rules.build_rules import BuildRules, BuildType

    return BuildRules(
        build_types={
            "5200": BuildType(script="RTL9617C_build.sh", product="5200"),
            "5200B": BuildType(script="X86.sh", product="5200B"),
        },
        build_models_by_section={"4.34 主分支": ["5200", "5200B"]},
    )


def test_sha_mode_build_models_from_build_rules(tmp_path):
    """产品线（branch.md section）→ 编译型号：组网线编 5200 + 5200B 两种型号。"""
    ctx = make_ctx(tmp_path)
    write_branch_md(tmp_path)
    ctx.build_rules = _sdwan_build_rules()
    _setup_target_ctx(ctx)

    batch = build_sha_batch(ctx, DEVELOP, ["a1"])
    final = run_sync_command(
        ctx, cycle_id="manual-20260824-101010", target=TARGET, batch=batch, checkpointer=None
    )

    built = {c["model"] for c in ctx.runner.build_calls}
    assert built == {"5200", "5200B"}
    assert final["status"] == "SUCCESS"
    assert set(final["branch_results"][TARGET].commits[0].build) == {"5200", "5200B"}


def test_sha_mode_unconfigured_product_line_stops(tmp_path):
    """产品线未配置编译型号 → BuildConfigError 报错停止，不静默用错脚本。"""
    from bsa.rules.build_rules import BuildConfigError, BuildRules, BuildType

    ctx = make_ctx(tmp_path)
    write_branch_md(tmp_path)
    ctx.build_rules = BuildRules(
        build_types={"5200": BuildType(script="RTL9617C_build.sh", product="5200")},
        build_models_by_section={},
    )
    _setup_target_ctx(ctx)

    batch = build_sha_batch(ctx, DEVELOP, ["a1"])
    with pytest.raises(BuildConfigError):
        run_sync_command(
            ctx,
            cycle_id="manual-20260824-101010",
            target=TARGET,
            batch=batch,
            checkpointer=None,
        )


def test_sha_mode_conflict_resolved_builds_and_patches(tmp_path):
    from bsa.domain.models import ConflictResolution

    ctx = make_ctx(tmp_path)
    _setup_target_ctx(ctx)
    wg = ctx.worktree_gits[str(worktree_path_for(ctx, TARGET, "manual-20260824-101010"))]
    wg.cherry_pick_result = CherryPickResult(
        status="CONFLICT", conflict_files=["plat/demo.c"]
    )
    wg.unmerged_files_result = ["plat/demo.c"]
    ctx.conflict_agent.resolution = ConflictResolution(
        files=["plat/demo.c"], diff="+fixed", agent_reason="resolved"
    )

    batch = build_sha_batch(ctx, DEVELOP, ["a1"])
    final = run_sync_command(
        ctx, cycle_id="manual-20260824-101010", target=TARGET, batch=batch, checkpointer=None
    )

    branch = final["branch_results"][TARGET]
    assert wg.cherry_pick_continue_calls == 1
    assert branch.commits[0].cherry_pick == "OK"
    assert branch.commits[0].conflict_resolution is not None
    assert branch.commits[0].build["RTL9617C"].status == "OK"
    assert branch.status == "SUCCESS"
    assert branch.patch_path is not None


def test_sha_mode_failfast_related_stops_batch(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.llm = FakeLLM(related=True)
    _setup_target_ctx(ctx)
    wg = ctx.worktree_gits[str(worktree_path_for(ctx, TARGET, "manual-20260824-101010"))]
    wg.cherry_pick_result = CherryPickResult(
        status="CONFLICT", conflict_files=["plat/demo.c"]
    )
    wg.unmerged_files_result = ["plat/demo.c"]
    ctx.conflict_agent.resolution = None
    ctx.git.changed = {"a1": ["plat/demo.c"], "a2": ["plat/demo.c"]}
    ctx.git.patches = {
        "a1": "diff --git a/plat/demo.c b/plat/demo.c\n@@ -1,3 +1,3 @@\n- x\n+ y\n",
        "a2": "diff --git a/plat/demo.c b/plat/demo.c\n@@ -5,3 +5,3 @@\n- x\n+ y\n",
    }

    batch = build_sha_batch(ctx, DEVELOP, ["a1", "a2"])
    final = run_sync_command(
        ctx, cycle_id="manual-20260824-101010", target=TARGET, batch=batch, checkpointer=None
    )

    picked = [args[0] for name, args in wg.calls if name == "cherry_pick"]
    assert picked == ["a1"]
    assert final["branch_results"][TARGET].stop_reason is not None


def test_sha_mode_forbidden_target_rejected(tmp_path):
    ctx = make_ctx(tmp_path, safety=FakeSafety(forbidden_branches={TARGET}))
    batch = build_sha_batch(ctx, DEVELOP, ["a1"])
    assert len(batch) == 1

    with pytest.raises(SafetyViolation):
        run_sync_command(
            ctx, cycle_id="manual-20260824-101010", target=TARGET, batch=batch, checkpointer=None
        )


def test_manual_cycle_id_is_unique(tmp_path):
    ids = {manual_cycle_id() for _ in range(1000)}
    assert len(ids) == 1000


def test_run_sync_command_thread_id_isolates_checkpoint(tmp_path):
    ctx = make_ctx(tmp_path)
    _setup_target_ctx(ctx)
    log_dir = Path(ctx.settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    db_path = log_dir / "state.sqlite3"
    batch = build_sha_batch(ctx, DEVELOP, ["a1"])

    with open_checkpointer(str(db_path)) as cp:
        final = run_sync_command(
            ctx,
            cycle_id="manual-20260824-101010",
            thread_id="rerun-fresh-thread",
            target=TARGET,
            batch=batch,
            checkpointer=cp,
        )

    assert final["status"] == "SUCCESS"
    assert read_cycle_state(ctx.settings, "manual-20260824-101010") is None
    fresh = read_cycle_state(ctx.settings, "rerun-fresh-thread")
    assert fresh is not None
    assert fresh["branch_results"][TARGET].status == "SUCCESS"


# --- 源+目标模式 ---


def test_source_target_mode_only_need_sync_in_batch(tmp_path):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    ctx.git.window_shas = {f"origin/{DEVELOP}": ["a1", "a2"]}
    ctx.git.changed = {"a1": ["plat/demo.c"], "a2": ["plat/demo.c"]}
    ctx.git.patches = {"a1": "+x", "a2": "+y"}
    ctx.git.patch_ids = {"a1": "pid1", "a2": "pid2"}
    ctx.classify.results = {
        "a1": Classification(
            is_bug_fix=True, recognition_source="machine:[BUG]", needs_agent=False
        ),
        "a2": Classification(
            is_bug_fix=True, recognition_source="machine:[BUG]", needs_agent=False
        ),
    }
    ctx.build_snapshot = lambda **kw: _target_snapshot()
    ctx.conclude.results = {
        ("a1", TARGET): Conclusion4(kind="NeedSync", evidence=[], confidence="high"),
        ("a2", TARGET): Conclusion4(
            kind="AlreadyIncluded", evidence=[], confidence="high"
        ),
    }

    batch, conclusions = build_source_target_batch(
        ctx, DEVELOP, TARGET, "2026-01-01T00:00:00+08:00", "2026-01-02T00:00:00+08:00"
    )

    assert [c.sha for c in batch] == ["a1"]
    assert conclusions["a1"].kind == "NeedSync"
    assert conclusions["a2"].kind == "AlreadyIncluded"

    wg = _setup_target_ctx(ctx)
    final = run_sync_command(
        ctx, cycle_id="manual-20260824-101010", target=TARGET, batch=batch, checkpointer=None
    )
    picked = [args[0] for name, args in wg.calls if name == "cherry_pick"]
    assert picked == ["a1"]
    assert final["branch_results"][TARGET].status == "SUCCESS"


def test_source_target_mode_no_need_sync_batch_empty(tmp_path):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    ctx.git.window_shas = {f"origin/{DEVELOP}": ["a1"]}
    ctx.git.changed = {"a1": ["plat/demo.c"]}
    ctx.git.patches = {"a1": "+x"}
    ctx.git.patch_ids = {"a1": "pid1"}
    ctx.classify.results = {
        "a1": Classification(
            is_bug_fix=True, recognition_source="machine:[BUG]", needs_agent=False
        )
    }
    ctx.build_snapshot = lambda **kw: _target_snapshot()
    ctx.conclude.results = {
        ("a1", TARGET): Conclusion4(
            kind="AlreadyIncluded", evidence=[], confidence="high"
        )
    }

    batch, conclusions = build_source_target_batch(
        ctx, DEVELOP, TARGET, "2026-01-01T00:00:00+08:00", "2026-01-02T00:00:00+08:00"
    )

    assert batch == []
    assert conclusions["a1"].kind == "AlreadyIncluded"


def test_source_target_mode_agent_fallback_for_pending(tmp_path):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    ctx.git.window_shas = {f"origin/{DEVELOP}": ["a1"]}
    ctx.git.changed = {"a1": ["plat/demo.c"]}
    ctx.git.patches = {"a1": "+x"}
    ctx.git.patch_ids = {"a1": "pid1"}
    ctx.classify.results = {
        "a1": Classification(
            is_bug_fix=False,
            recognition_source="pending:claude-agent",
            needs_agent=True,
        )
    }
    ctx.sync_decision_agent.results = {
        "a1": SyncDecision(
            sha="a1",
            is_bug_fix=True,
            reason=None,
            recognition_source="agent:bug-fix",
            needs_agent=False,
        )
    }
    ctx.build_snapshot = lambda **kw: _target_snapshot()
    ctx.conclude.results = {
        ("a1", TARGET): Conclusion4(kind="NeedSync", evidence=[], confidence="high")
    }

    batch, _ = build_source_target_batch(
        ctx, DEVELOP, TARGET, "2026-01-01T00:00:00+08:00", "2026-01-02T00:00:00+08:00"
    )

    assert [c.sha for c in ctx.sync_decision_agent.calls[0]] == ["a1"]
    assert [c.sha for c in batch] == ["a1"]


# --- CLI 接线 ---


def test_sync_cli_no_need_sync_prints_and_exits_zero(monkeypatch, tmp_path, capsys):
    env = valid_env()
    env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr("bsa.config.settings.os.environ", env)
    monkeypatch.setattr(
        "bsa.cli.build_graph_context",
        lambda settings, **kw: SimpleNamespace(settings=settings),
    )
    monkeypatch.setattr(
        "bsa.cli.build_source_target_batch",
        lambda ctx, src, target, since, until: ([], {}),
    )

    code = main(["sync", DEVELOP, TARGET])

    assert code == 0
    assert "无需同步" in capsys.readouterr().out


def test_sync_cli_sha_mode_wires_through(monkeypatch, tmp_path, capsys):
    env = valid_env()
    env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr("bsa.config.settings.os.environ", env)
    seen: dict = {}
    monkeypatch.setattr(
        "bsa.cli.build_graph_context",
        lambda settings, **kw: SimpleNamespace(settings=settings),
    )

    def fake_batch(ctx, src, shas):
        seen["batch"] = (src, list(shas))
        return [commit("a1")]

    monkeypatch.setattr("bsa.cli.build_sha_batch", fake_batch)

    def fake_run(ctx, *, cycle_id, target, batch, checkpointer):
        seen["run"] = (cycle_id, target, len(batch))
        return {
            "cycle_id": cycle_id,
            "status": "REPORTED",
            "branch_results": {
                TARGET: BranchResult(
                    target_branch=TARGET,
                    worktree_path="/wt",
                    status="SUCCESS",
                    commits=[],
                    patch_path=str(tmp_path / "p.patch"),
                    stop_reason=None,
                )
            },
        }

    monkeypatch.setattr("bsa.cli.run_sync_command", fake_run)

    code = main(["sync", DEVELOP, TARGET, "--sha", "a1"])

    assert code == 0
    assert seen["batch"] == (DEVELOP, ["a1"])
    assert seen["run"][1] == TARGET
    assert seen["run"][0].startswith("manual-")
    assert "SUCCESS" in capsys.readouterr().out


def test_sync_cli_sha_mode_forbidden_target_refuses(monkeypatch, tmp_path, capsys):
    env = valid_env()
    env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr("bsa.config.settings.os.environ", env)
    monkeypatch.setattr(
        "bsa.cli.build_graph_context",
        lambda settings, **kw: SimpleNamespace(
            settings=settings, safety=FakeSafety(forbidden_branches={TARGET})
        ),
    )
    monkeypatch.setattr(
        "bsa.cli.build_sha_batch",
        lambda ctx, src, shas: [commit("a1")],
    )

    code = main(["sync", DEVELOP, TARGET, "--sha", "a1"])

    assert code == 1
    assert "禁止" in capsys.readouterr().err


def test_sync_cli_rejects_sha_with_window(monkeypatch, tmp_path):
    env = valid_env()
    env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr("bsa.config.settings.os.environ", env)

    code = main(
        [
            "sync",
            DEVELOP,
            TARGET,
            "--sha",
            "a1",
            "--since",
            "2026-01-01T00:00:00+08:00",
        ]
    )

    assert code == 1
