from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from bsa.cli import main
from bsa.commands.rerun import _batch_from_state, run_rerun_command
from bsa.commands.sync import run_sync_command
from bsa.domain.models import BranchResult, CherryPickResult, Conclusion4
from bsa.graph.workflow import open_checkpointer
from bsa.report.projection import read_cycle_state
from bsa.rules import TargetSnapshot
from tests.test_config import valid_env
from tests.test_graph_nodes import DEVELOP, TARGET, FakeGit, commit, make_ctx

RETAINED_CYCLE = "cycle-20260101"


def worktree_path(ctx, target: str, cycle_id: str) -> Path:
    return Path(ctx.settings.worktree_root) / f"{target}-{cycle_id}"


def make_valid_worktree(ctx, target: str, cycle_id: str) -> Path:
    wt = worktree_path(ctx, target, cycle_id)
    wt.mkdir(parents=True, exist_ok=True)
    gitdir = wt / ".gitdir"
    gitdir.mkdir()
    (wt / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    return wt


def cycle_record(
    cycle_id: str,
    *,
    started_at="2026-01-02T00:00:00+00:00",
    status="REPORTED",
    flow: str | None = None,
):
    record = {"cycle_id": cycle_id, "status": status, "started_at": started_at}
    if flow is not None:
        record["flow"] = flow
    return record


def frozen_state(cycle_id: str, target: str, shas: list[str]) -> dict:
    return {
        "cycle_id": cycle_id,
        "detected_commits": [commit(sha) for sha in shas],
        "batches": {target: list(shas)},
        "classifications": {},
        "decisions": {},
        "branch_results": {},
        "status": "REPORTED",
    }


def patch_cycle_lookup(monkeypatch, records, state=None):
    monkeypatch.setattr("bsa.commands.rerun.list_cycle_records", lambda log_dir: records)
    if state is None:
        state = frozen_state(RETAINED_CYCLE, TARGET, ["a1"])
    monkeypatch.setattr(
        "bsa.commands.rerun.read_cycle_state",
        lambda settings, cycle_id: state,
    )


def target_snapshot(**kw) -> TargetSnapshot:
    fields = {"branch_name": TARGET, "branch_type": "release"}
    fields.update(kw)
    return TargetSnapshot(**fields)


def ok_worktree_git(*, cherry_pick="EMPTY", status=""):
    wg = FakeGit()
    wg.cherry_pick_result = CherryPickResult(status=cherry_pick)
    wg.tips = {TARGET: ("origin/" + TARGET, "base-tip")}
    wg.status_result = status
    return wg


def branch_ok(target: str, patch_path: str) -> BranchResult:
    return BranchResult(
        target_branch=target,
        worktree_path="/wt",
        status="SUCCESS",
        commits=[],
        patch_path=patch_path,
        stop_reason=None,
    )


# --- 默认：保留现场续跑 ---


def test_rerun_retained_honors_thread_id_param(tmp_path, monkeypatch):
    # P2-3：retained 重跑接受显式 thread_id，不再内部二次生成（单一线程 id）
    ctx = make_ctx(tmp_path)
    ctx.git.tips = {TARGET: ("origin/" + TARGET, "tip1")}
    patch_cycle_lookup(monkeypatch, [cycle_record(RETAINED_CYCLE)])
    wt = make_valid_worktree(ctx, TARGET, RETAINED_CYCLE)
    wg = ok_worktree_git()
    ctx.worktree_gits[str(wt)] = wg

    final = run_rerun_command(ctx, target=TARGET, thread_id="rerun-custom-thread")

    assert final["rerun"]["mode"] == "retained"
    assert final["rerun"]["cycle_id"] == "rerun-custom-thread"


def test_rerun_retained_reuses_worktree_and_updates_patch(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    ctx.git.tips = {TARGET: ("origin/" + TARGET, "tip1")}
    patch_cycle_lookup(monkeypatch, [cycle_record(RETAINED_CYCLE)])
    wt = make_valid_worktree(ctx, TARGET, RETAINED_CYCLE)
    wg = ok_worktree_git()
    ctx.worktree_gits[str(wt)] = wg

    final = run_rerun_command(ctx, target=TARGET)

    assert not final.get("stop")
    assert final["rerun"]["mode"] == "retained"
    assert final["rerun"]["cycle_id"] != RETAINED_CYCLE
    assert final["rerun"]["cycle_id"].startswith("rerun-")
    assert final["rerun"]["source_cycle_id"] == RETAINED_CYCLE
    assert final["rerun"]["worktree"] == str(wt)
    branch = final["branch_results"][TARGET]
    assert branch.status == "SUCCESS"
    assert branch.patch_path is not None
    picked = [args[0] for name, args in wg.calls if name == "cherry_pick"]
    assert picked == ["a1"]
    assert not any(name == "add_worktree" for name, args in ctx.git.calls)


def test_rerun_retained_dirty_worktree_stops(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    ctx.git.tips = {TARGET: ("origin/" + TARGET, "tip1")}
    patch_cycle_lookup(monkeypatch, [cycle_record(RETAINED_CYCLE)])
    wt = make_valid_worktree(ctx, TARGET, RETAINED_CYCLE)
    wg = ok_worktree_git(status=" M plat/demo.c\n")
    ctx.worktree_gits[str(wt)] = wg

    result = run_rerun_command(ctx, target=TARGET)

    assert result["stop"] is True
    assert result["reason"] == "dirty"
    assert result.get("branch_results") is None
    assert not any(name == "cherry_pick" for name, args in wg.calls)


def test_rerun_retained_ignores_build_managed_component_dirt(tmp_path, monkeypatch):
    """基线编译自己留下的组件目录脚印不算「现场脏」。

    ``code_update.sh -d``（全量编译前置）会 ``rm -rf`` 再 ``git clone``
    voip_main/xpon/wlan/ac/ponolt，把主仓里被跟踪的 gitlink 一并删掉；克隆
    回来的新仓库没有该条目，于是 worktree 永远带着一条 `` D``。若照旧拦截，
    基线编译成功即宣告该分支的保留现场重跑必然失败（task 35 根因）。
    """
    ctx = make_ctx(tmp_path)
    ctx.git.tips = {TARGET: ("origin/" + TARGET, "tip1")}
    patch_cycle_lookup(monkeypatch, [cycle_record(RETAINED_CYCLE)])
    wt = make_valid_worktree(ctx, TARGET, RETAINED_CYCLE)
    wg = ok_worktree_git(
        status=(
            " D component/ponolt/mini_olt/50h/FTTR_FIRMWARE\n"
            " D component/xpon/src/demo.c\n"
            "?? component/ac/build/\n"
        )
    )
    ctx.worktree_gits[str(wt)] = wg

    result = run_rerun_command(ctx, target=TARGET)

    assert not result.get("stop")
    assert result["rerun"]["mode"] == "retained"
    assert [args[0] for name, args in wg.calls if name == "cherry_pick"] == ["a1"]


def test_rerun_retained_of_cron_cycle_uses_lean_flow(tmp_path, monkeypatch):
    """rerun 复刻原任务的流程语义：cron 周期的活儿不接回 LLM 冲突解决。

    cron 图（build_workflow）已摘除 resolve_conflict/fix_build/fail_fast，
    冲突直接停批转人工。若 rerun 无条件走 single_target，就把 cron 有意解耦
    掉的判断修改类 LLM 又接了回来——cherry-pick 撞冲突时凭空调 LLM，且留下
    UU 冲突现场（task 42 实测）。
    """
    ctx = make_ctx(tmp_path)
    ctx.git.tips = {TARGET: ("origin/" + TARGET, "tip1")}
    patch_cycle_lookup(
        monkeypatch, [cycle_record(RETAINED_CYCLE, flow="cron")]
    )
    wt = make_valid_worktree(ctx, TARGET, RETAINED_CYCLE)
    wg = ok_worktree_git(cherry_pick="CONFLICT", status="")
    ctx.worktree_gits[str(wt)] = wg

    final = run_rerun_command(ctx, target=TARGET)

    assert not final.get("stop")
    # 关键断言：LLM 冲突解决绝不能被调用。
    assert ctx.conflict_agent.calls == []


def test_rerun_retained_of_manual_cycle_keeps_agent_flow(tmp_path, monkeypatch):
    """manual 周期（平台 sync 发起）的 rerun 仍走完整 agent 流程，保留 LLM 解决。"""
    ctx = make_ctx(tmp_path)
    ctx.git.tips = {TARGET: ("origin/" + TARGET, "tip1")}
    patch_cycle_lookup(
        monkeypatch, [cycle_record(RETAINED_CYCLE, flow="manual")]
    )
    wt = make_valid_worktree(ctx, TARGET, RETAINED_CYCLE)
    wg = ok_worktree_git(cherry_pick="CONFLICT", status="")
    ctx.worktree_gits[str(wt)] = wg

    run_rerun_command(ctx, target=TARGET)

    assert len(ctx.conflict_agent.calls) == 1


def test_rerun_retained_legacy_record_without_flow_defaults_to_agent(
    tmp_path, monkeypatch
):
    """老周期记录没有 flow 字段：默认沿用 single_target，不臆测成 cron。"""
    ctx = make_ctx(tmp_path)
    ctx.git.tips = {TARGET: ("origin/" + TARGET, "tip1")}
    patch_cycle_lookup(monkeypatch, [cycle_record(RETAINED_CYCLE)])
    wt = make_valid_worktree(ctx, TARGET, RETAINED_CYCLE)
    wg = ok_worktree_git(cherry_pick="CONFLICT", status="")
    ctx.worktree_gits[str(wt)] = wg

    run_rerun_command(ctx, target=TARGET)

    assert len(ctx.conflict_agent.calls) == 1


def test_rerun_retained_source_change_still_stops(tmp_path, monkeypatch):
    """组件脚印之外的真实源码改动仍必须拦截（不能把 dirty 检查整体废掉）。"""
    ctx = make_ctx(tmp_path)
    ctx.git.tips = {TARGET: ("origin/" + TARGET, "tip1")}
    patch_cycle_lookup(monkeypatch, [cycle_record(RETAINED_CYCLE)])
    wt = make_valid_worktree(ctx, TARGET, RETAINED_CYCLE)
    wg = ok_worktree_git(
        status=" D component/ponolt/x.c\n M plat/demo.c\n"
    )
    ctx.worktree_gits[str(wt)] = wg

    result = run_rerun_command(ctx, target=TARGET)

    assert result["stop"] is True
    assert result["reason"] == "dirty"


def test_rerun_retained_missing_worktree_stops_with_fresh_hint(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    ctx.git.tips = {TARGET: ("origin/" + TARGET, "tip1")}
    patch_cycle_lookup(monkeypatch, [cycle_record(RETAINED_CYCLE)])

    result = run_rerun_command(ctx, target=TARGET)

    assert result["stop"] is True
    assert result["reason"] == "no-worktree"
    assert result.get("branch_results") is None


def test_rerun_no_cycle_for_target_stops(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    monkeypatch.setattr("bsa.commands.rerun.list_cycle_records", lambda log_dir: [])
    monkeypatch.setattr(
        "bsa.commands.rerun.read_cycle_state", lambda settings, c: None
    )

    result = run_rerun_command(ctx, target=TARGET)

    assert result["stop"] is True
    assert result["reason"] == "no-cycle"


# --- --fresh：重建重同步 ---


def test_rerun_fresh_conclusion_now_included_stops(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    ctx.git.tips = {
        TARGET: ("origin/" + TARGET, "tip1"),
        DEVELOP: ("origin/" + DEVELOP, "tip1"),
    }
    patch_cycle_lookup(monkeypatch, [cycle_record(RETAINED_CYCLE)])
    ctx.build_snapshot = lambda **kw: target_snapshot()
    ctx.conclude.results = {
        ("a1", TARGET): Conclusion4(kind="AlreadyIncluded", evidence=[], confidence="high")
    }

    result = run_rerun_command(ctx, target=TARGET, fresh=True)

    assert result["stop"] is True
    assert result["reason"] == "conclusion-now-included"
    assert result.get("rerun") is None
    assert not any(name == "remove_worktree" for name, args in ctx.git.calls)
    assert not any(name == "add_worktree" for name, args in ctx.git.calls)


def test_rerun_fresh_still_need_sync_discards_and_rebuilds(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    ctx.git.tips = {
        TARGET: ("origin/" + TARGET, "tip1"),
        DEVELOP: ("origin/" + DEVELOP, "tip1"),
    }
    patch_cycle_lookup(monkeypatch, [cycle_record(RETAINED_CYCLE)])
    ctx.build_snapshot = lambda **kw: target_snapshot()
    ctx.conclude.results = {
        ("a1", TARGET): Conclusion4(kind="NeedSync", evidence=[], confidence="high")
    }
    old_wt = make_valid_worktree(ctx, TARGET, RETAINED_CYCLE)

    monkeypatch.setattr(
        "bsa.commands.rerun.manual_cycle_id", lambda: "manual-20260824-101010"
    )
    new_wt = worktree_path(ctx, TARGET, "manual-20260824-101010")
    wg = ok_worktree_git(cherry_pick="OK")
    ctx.worktree_gits[str(new_wt)] = wg

    final = run_rerun_command(ctx, target=TARGET, fresh=True)

    assert not final.get("stop")
    assert final["rerun"]["mode"] == "fresh"
    assert final["rerun"]["source_cycle_id"] == RETAINED_CYCLE
    assert final["rerun"]["cycle_id"] == "manual-20260824-101010"
    assert ("remove_worktree", (old_wt,)) in ctx.git.calls
    assert ("add_worktree", ("origin/" + TARGET, new_wt)) in ctx.git.calls
    branch = final["branch_results"][TARGET]
    assert branch.status == "SUCCESS"
    assert branch.patch_path is not None


def test_rerun_fresh_locates_manual_cycle_without_cycle_json(tmp_path, monkeypatch):
    # 回归：手动同步（manual-*）不写 cycle.json，list_cycle_records 枚举不到；
    # fresh 重跑须按目录枚举 manual-*/rerun-* 才能定位来源周期，否则误报 no-cycle。
    ctx = make_ctx(tmp_path)
    ctx.git.tips = {
        TARGET: ("origin/" + TARGET, "tip1"),
        DEVELOP: ("origin/" + DEVELOP, "tip1"),
    }
    monkeypatch.setattr("bsa.commands.rerun.list_cycle_records", lambda log_dir: [])
    manual_cid = "manual-20260101-000000"
    monkeypatch.setattr(
        "bsa.commands.rerun.read_cycle_state",
        lambda settings, cid: frozen_state(manual_cid, TARGET, ["a1"])
        if cid == manual_cid
        else None,
    )
    # 手动周期只落 state.json 目录，无 cycle.json。
    (Path(ctx.settings.log_dir) / manual_cid).mkdir(parents=True, exist_ok=True)
    ctx.build_snapshot = lambda **kw: target_snapshot()
    ctx.conclude.results = {
        ("a1", TARGET): Conclusion4(kind="NeedSync", evidence=[], confidence="high")
    }
    old_wt = make_valid_worktree(ctx, TARGET, manual_cid)
    monkeypatch.setattr(
        "bsa.commands.rerun.manual_cycle_id", lambda: "manual-20260824-101010"
    )
    new_wt = worktree_path(ctx, TARGET, "manual-20260824-101010")
    wg = ok_worktree_git(cherry_pick="OK")
    ctx.worktree_gits[str(new_wt)] = wg

    final = run_rerun_command(ctx, target=TARGET, fresh=True)

    assert not final.get("stop")
    assert final["rerun"]["mode"] == "fresh"
    assert final["rerun"]["source_cycle_id"] == manual_cid
    assert final["rerun"]["cycle_id"] == "manual-20260824-101010"
    assert ("remove_worktree", (old_wt,)) in ctx.git.calls
    assert ("add_worktree", ("origin/" + TARGET, new_wt)) in ctx.git.calls


def test_rerun_fresh_manual_review_keeps_and_resyncs(tmp_path, monkeypatch):
    # 回归：--fresh 重判时 ManualReview 不是「已合入」的客观证据，不能据此静默
    # 丢弃用户要重同步的 commit（否则失败分支重跑会被误报成功却不做任何同步）。
    # 冻结批次里的 commit 本就已经过 NeedSync 判定，重判只降级客观已合入/超范围。
    ctx = make_ctx(tmp_path)
    ctx.git.tips = {
        TARGET: ("origin/" + TARGET, "tip1"),
        DEVELOP: ("origin/" + DEVELOP, "tip1"),
    }
    patch_cycle_lookup(monkeypatch, [cycle_record(RETAINED_CYCLE)])
    ctx.build_snapshot = lambda **kw: target_snapshot()
    ctx.conclude.results = {
        ("a1", TARGET): Conclusion4(kind="ManualReview", evidence=[], confidence="low")
    }
    old_wt = make_valid_worktree(ctx, TARGET, RETAINED_CYCLE)
    monkeypatch.setattr(
        "bsa.commands.rerun.manual_cycle_id", lambda: "manual-20260824-101010"
    )
    new_wt = worktree_path(ctx, TARGET, "manual-20260824-101010")
    wg = ok_worktree_git(cherry_pick="OK")
    ctx.worktree_gits[str(new_wt)] = wg

    final = run_rerun_command(ctx, target=TARGET, fresh=True)

    assert not final.get("stop")
    assert final["rerun"]["mode"] == "fresh"
    assert final["rerun"]["source_cycle_id"] == RETAINED_CYCLE
    assert final["rerun"]["cycle_id"] == "manual-20260824-101010"
    assert ("remove_worktree", (old_wt,)) in ctx.git.calls
    assert ("add_worktree", ("origin/" + TARGET, new_wt)) in ctx.git.calls
    picked = [args[0] for name, args in wg.calls if name == "cherry_pick"]
    assert picked == ["a1"]


def test_rerun_fresh_mixed_conclusions_drop_only_included(tmp_path, monkeypatch):
    # 重判混合结论：AlreadyIncluded/OutOfScope 降级丢弃，ManualReview/NeedSync 保留重同步。
    shas = ["a1", "a2", "a3", "a4"]
    ctx = make_ctx(tmp_path)
    ctx.git.tips = {
        TARGET: ("origin/" + TARGET, "tip1"),
        DEVELOP: ("origin/" + DEVELOP, "tip1"),
    }
    monkeypatch.setattr(
        "bsa.commands.rerun.list_cycle_records",
        lambda log_dir: [cycle_record(RETAINED_CYCLE)],
    )
    monkeypatch.setattr(
        "bsa.commands.rerun.read_cycle_state",
        lambda settings, c: frozen_state(RETAINED_CYCLE, TARGET, shas),
    )
    ctx.build_snapshot = lambda **kw: target_snapshot()
    ctx.conclude.results = {
        ("a1", TARGET): Conclusion4(kind="AlreadyIncluded", evidence=[], confidence="high"),
        ("a2", TARGET): Conclusion4(kind="OutOfScope", evidence=[], confidence="high"),
        ("a3", TARGET): Conclusion4(kind="ManualReview", evidence=[], confidence="low"),
        ("a4", TARGET): Conclusion4(kind="NeedSync", evidence=[], confidence="high"),
    }
    old_wt = make_valid_worktree(ctx, TARGET, RETAINED_CYCLE)
    monkeypatch.setattr(
        "bsa.commands.rerun.manual_cycle_id", lambda: "manual-20260824-101010"
    )
    new_wt = worktree_path(ctx, TARGET, "manual-20260824-101010")
    wg = ok_worktree_git(cherry_pick="OK")
    ctx.worktree_gits[str(new_wt)] = wg

    final = run_rerun_command(ctx, target=TARGET, fresh=True)

    assert not final.get("stop")
    picked = [args[0] for name, args in wg.calls if name == "cherry_pick"]
    # 丢弃 a1/a2，保留 a3/a4，按 commit 原顺序重同步
    assert picked == ["a3", "a4"]


def test_rerun_fresh_out_of_scope_keeps_now_included_reason(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    ctx.git.tips = {
        TARGET: ("origin/" + TARGET, "tip1"),
        DEVELOP: ("origin/" + DEVELOP, "tip1"),
    }
    patch_cycle_lookup(monkeypatch, [cycle_record(RETAINED_CYCLE)])
    ctx.build_snapshot = lambda **kw: target_snapshot()
    ctx.conclude.results = {
        ("a1", TARGET): Conclusion4(kind="OutOfScope", evidence=[], confidence="high")
    }

    result = run_rerun_command(ctx, target=TARGET, fresh=True)

    assert result["stop"] is True
    assert result["reason"] == "conclusion-now-included"


def test_rerun_retained_isolates_checkpoint_thread(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    ctx.git.tips = {TARGET: ("origin/" + TARGET, "tip1")}
    patch_cycle_lookup(monkeypatch, [cycle_record(RETAINED_CYCLE)])
    wt = make_valid_worktree(ctx, TARGET, RETAINED_CYCLE)
    wg = ok_worktree_git()
    ctx.worktree_gits[str(wt)] = wg
    monkeypatch.setattr(
        "bsa.commands.rerun._rerun_thread_id", lambda target: f"rerun-{target}-thread"
    )

    log_dir = Path(ctx.settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    db_path = log_dir / "state.sqlite3"
    batch = _batch_from_state(frozen_state(RETAINED_CYCLE, TARGET, ["a1"]), TARGET)
    with open_checkpointer(str(db_path)) as cp:
        run_sync_command(
            ctx, cycle_id=RETAINED_CYCLE, target=TARGET, batch=batch, checkpointer=cp
        )
        before = read_cycle_state(ctx.settings, RETAINED_CYCLE)
        assert before is not None
        final = run_rerun_command(ctx, target=TARGET, checkpointer=cp)

    after = read_cycle_state(ctx.settings, RETAINED_CYCLE)
    assert after == before
    fresh = read_cycle_state(ctx.settings, f"rerun-{TARGET}-thread")
    assert fresh is not None
    assert fresh["branch_results"][TARGET].status == "SUCCESS"


# --- CLI 接线 ---


def test_rerun_cli_prints_result(monkeypatch, tmp_path, capsys):
    env = valid_env()
    env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr("bsa.config.settings.os.environ", env)
    monkeypatch.setattr(
        "bsa.cli.build_graph_context",
        lambda settings, **kw: SimpleNamespace(settings=settings),
    )
    monkeypatch.setattr(
        "bsa.cli.run_rerun_command",
        lambda ctx, *, target, cycle, fresh, checkpointer, thread_id=None: {
            "status": "REPORTED",
            "cycle_id": RETAINED_CYCLE,
            "rerun": {"mode": "retained", "cycle_id": RETAINED_CYCLE, "worktree": "/wt"},
            "branch_results": {
                TARGET: branch_ok(TARGET, str(tmp_path / "p.patch"))
            },
        },
    )

    code = main(["rerun", TARGET])

    assert code == 0
    out = capsys.readouterr().out
    assert "重跑完成" in out
    assert RETAINED_CYCLE in out


def test_rerun_cli_dirty_reports_error(monkeypatch, tmp_path, capsys):
    env = valid_env()
    env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr("bsa.config.settings.os.environ", env)
    monkeypatch.setattr(
        "bsa.cli.build_graph_context",
        lambda settings, **kw: SimpleNamespace(settings=settings),
    )
    monkeypatch.setattr(
        "bsa.cli.run_rerun_command",
        lambda ctx, *, target, cycle, fresh, checkpointer, thread_id=None: {
            "stop": True,
            "reason": "dirty",
            "worktree": "/wt",
        },
    )

    code = main(["rerun", TARGET])

    assert code == 1
    assert "现场有未提交修改" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("stop_result", "needles"),
    [
        (
            {"stop": True, "reason": "dirty", "worktree": "/wt", "detail": " M plat/demo.c"},
            ("现场有未提交修改", "/wt", "plat/demo.c"),
        ),
        ({"stop": True, "reason": "no-worktree"}, ("worktree 不存在", TARGET)),
        ({"stop": True, "reason": "no-cycle"}, ("未找到包含目标分支", TARGET)),
        (
            {"stop": True, "reason": "no-batch", "cycle_id": RETAINED_CYCLE},
            ("无目标分支", TARGET),
        ),
    ],
)
def test_rerun_cli_stop_reason_reaches_task_row(
    monkeypatch, tmp_path, capsys, stop_result, needles
):
    """被拦截的 stop 也必须把原因落到 tasks.error。

    平台只读 tasks 表渲染任务详情；原因只 print 到 stderr 时，UI 只能显示一个
    无理由的「失败」——用户无从知道该清理现场还是该用 --fresh（task 35 现象）。
    """
    env = valid_env()
    env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr("bsa.config.settings.os.environ", env)
    monkeypatch.setattr(
        "bsa.cli.build_graph_context",
        lambda settings, **kw: SimpleNamespace(settings=settings),
    )
    monkeypatch.setattr(
        "bsa.cli.run_rerun_command",
        lambda ctx, *, target, cycle, fresh, checkpointer, thread_id=None: dict(
            stop_result
        ),
    )

    code = main(["rerun", TARGET])
    capsys.readouterr()

    assert code == 1
    conn = sqlite3.connect(str(tmp_path / "logs" / "platform.sqlite3"))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT state, error FROM tasks ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["state"] == "failed"
    for needle in needles:
        assert needle in (row["error"] or "")


def test_rerun_cli_conclusion_now_included_exits_zero(monkeypatch, tmp_path, capsys):
    env = valid_env()
    env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr("bsa.config.settings.os.environ", env)
    monkeypatch.setattr(
        "bsa.cli.build_graph_context",
        lambda settings, **kw: SimpleNamespace(settings=settings),
    )
    monkeypatch.setattr(
        "bsa.cli.run_rerun_command",
        lambda ctx, *, target, cycle, fresh, checkpointer, thread_id=None: {
            "stop": True,
            "reason": "conclusion-now-included",
        },
    )

    code = main(["rerun", TARGET, "--fresh"])

    assert code == 0
    assert "无需重同步" in capsys.readouterr().err


def test_cleanup_worktree_removes_existing(tmp_path):
    from bsa.commands.rerun import cleanup_worktree_command

    ctx = make_ctx(tmp_path)
    worktree = Path(ctx.settings.worktree_root) / f"{TARGET}-manual-20260825-1"
    worktree.mkdir(parents=True)
    ctx.git.worktrees = [worktree]

    result = cleanup_worktree_command(
        ctx, target=TARGET, cycle_id="manual-20260825-1"
    )
    assert result["removed"] is True
    assert ("remove_worktree", (worktree,)) in ctx.git.calls


def test_cleanup_worktree_missing_is_idempotent(tmp_path):
    from bsa.commands.rerun import cleanup_worktree_command

    ctx = make_ctx(tmp_path)
    result = cleanup_worktree_command(
        ctx, target=TARGET, cycle_id="manual-20260825-1"
    )
    assert result["removed"] is False
    assert not any(c[0] == "remove_worktree" for c in ctx.git.calls)


def test_cleanup_worktree_cli_prints_result(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        "bsa.cli.build_graph_context",
        lambda settings: SimpleNamespace(settings=settings),
    )
    monkeypatch.setattr(
        "bsa.cli.cleanup_worktree_command",
        lambda ctx, *, target, cycle_id: {"removed": True, "target": target, "cycle_id": cycle_id},
    )
    code = main(["cleanup-worktree", TARGET, "manual-20260825-1"])
    assert code == 0
    assert "removed=True" in capsys.readouterr().out


def test_cleanup_task_cli_docker_rm(monkeypatch, tmp_path, capsys):
    import subprocess

    from bsa.cli import _cmd_cleanup_task

    settings = SimpleNamespace(
        docker_prefix="", docker_container_prefix="rcios-sync"
    )
    monkeypatch.setattr("bsa.cli.load_settings", lambda: settings)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("bsa.cli.subprocess.run", fake_run)
    code = _cmd_cleanup_task(SimpleNamespace(cycle="manual-20260825-1"))
    assert code == 0
    assert calls == [["docker", "rm", "-f", "rcios-sync-manual-20260825-1"]]


def test_cleanup_task_cli_with_docker_prefix_sudo(monkeypatch, tmp_path, capsys):
    from bsa.cli import _cmd_cleanup_task

    settings = SimpleNamespace(
        docker_prefix="sudo ", docker_container_prefix="rcios-sync"
    )
    monkeypatch.setattr("bsa.cli.load_settings", lambda: settings)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("bsa.cli.subprocess.run", fake_run)
    _cmd_cleanup_task(SimpleNamespace(cycle="manual-1"))
    assert calls == [["sudo", "docker", "rm", "-f", "rcios-sync-manual-1"]]
