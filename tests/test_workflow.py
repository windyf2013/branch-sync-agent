from __future__ import annotations

import sqlite3
from pathlib import Path

from bsa.domain.models import CherryPickResult, Conclusion4
from bsa.graph.nodes import GraphContext
from bsa.graph.workflow import build_workflow, make_checkpointer, open_checkpointer, thread_config
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
## 1 RCIOS代码库
- 路径：rcios

### 1.1 4.34 主分支
- {TARGET}
- {TARGET2}

### 1.2 4.34 业务分支
- {DEVELOP}
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


def test_baseline_failure_blocks_branch_no_cherry_pick(tmp_path):
    write_branch_md(tmp_path)
    ctx = batch_ctx(tmp_path, shas=["a1"])
    ctx.runner = FakeRunner(success=False)

    out = run(build_workflow(ctx), base_state())

    branch = out["branch_results"][TARGET]
    assert branch.status == "FAILED"
    assert "baseline build failed" in (branch.stop_reason or "")
    assert branch.baseline["RTL9617C"].status == "FAILED"
    # 基线失败 → 不 cherry-pick、不生成 patch
    assert cherry_picked(worktree_git_for(ctx, TARGET)) == []
    assert branch.patch_path is None
    assert out["status"] == "FAILED"


def test_baseline_success_proceeds_to_cherry_pick(tmp_path):
    write_branch_md(tmp_path)
    ctx = batch_ctx(tmp_path, shas=["a1"])

    out = run(build_workflow(ctx), base_state())

    assert out["branch_results"][TARGET].baseline["RTL9617C"].status == "OK"
    assert cherry_picked(worktree_git_for(ctx, TARGET)) == ["a1"]
    assert out["branch_results"][TARGET].status == "SUCCESS"


def test_thread_config_binds_cycle_id():
    assert thread_config("cycle-x") == {"configurable": {"thread_id": "cycle-x"}}


def test_checkpointer_db_uses_wal(tmp_path):
    # 平台投影并发读与周期写入撞锁 → checkpoint 库开启 WAL（任务 3）
    db = tmp_path / "state.sqlite3"
    with open_checkpointer(str(db)) as cp:
        pass
    conn = sqlite3.connect(str(db))
    journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert journal == "wal"


def test_all_already_included_routes_to_report(tmp_path):
    """台账已含（全量冻结的 AlreadyIncluded 短路）→ 空批 → 空周期 SUCCESS 收尾。"""
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    ctx.llm = FakeLLM()
    from bsa.ledger import record_synced

    log_dir = Path(ctx.settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    record_synced(log_dir, "pid-a1", TARGET, "a1")
    ctx.git.window_shas = {f"origin/{DEVELOP}": ["a1"]}
    ctx.git.changed["a1"] = ["plat/demo.c"]
    ctx.git.patches["a1"] = "+x"
    ctx.git.patch_ids["a1"] = "pid-a1"
    _classify(ctx, "a1")

    out = run(build_workflow(ctx), base_state())

    assert out["batches"] == {}
    assert out["report"] is not None
    assert out["report"].summary["commits_detected"] == 1
    assert out["report"].summary["branches"] == []
    assert out["status"] == "SUCCESS"


def test_no_detected_commits_empty_cycle(tmp_path):
    """窗口内无 commit → 空批 → 空周期 SUCCESS（绿条）。"""
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    ctx.llm = FakeLLM()
    ctx.git.window_shas = {f"origin/{DEVELOP}": []}

    out = run(build_workflow(ctx), base_state())

    assert out["batches"] == {}
    assert out["report"] is not None
    assert out["status"] == "SUCCESS"


def test_single_branch_success_patch_and_report(tmp_path):
    write_branch_md(tmp_path)
    ctx = batch_ctx(tmp_path, shas=["a1"])

    out = run(build_workflow(ctx), base_state())

    assert out["status"] == "SUCCESS"
    assert out["report"] is not None
    branch = out["branch_results"][TARGET]
    assert branch.status == "SUCCESS"
    assert branch.patch_path is not None
    assert branch.commits[0].cherry_pick == "OK"
    assert branch.commits[0].build["RTL9617C"].status == "OK"


def test_empty_cherry_pick_skips_build(tmp_path):
    # 不变量 #17：建立 worktree 时 baseline_build 已全量编译，EMPTY（内容已应用）
    # 未引入改动，跳过编译直接下一个 commit，不产生 build 记录。
    write_branch_md(tmp_path)
    ctx = batch_ctx(tmp_path, shas=["a1"])
    wg = worktree_git_for(ctx, TARGET)
    wg.cherry_pick_results = {"a1": CherryPickResult(status="EMPTY")}

    out = run(build_workflow(ctx), base_state())

    assert out["status"] == "SUCCESS"
    branch = out["branch_results"][TARGET]
    assert branch.commits[0].cherry_pick == "EMPTY"
    assert branch.commits[0].build == {}
    assert branch.patch_path is not None
    # 全程只发生 baseline 编译，没有 commit 编译。
    assert [c["model"] for c in ctx.runner.build_calls] == ["RTL9617C"]


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

    # baseline（prepare 后）先串行两模型，commit build 再串行两模型。
    models = [call["model"] for call in ctx.runner.build_calls]
    assert models == ["RTL9617C", "RTL9607F", "RTL9617C", "RTL9607F"]
    baseline = out["branch_results"][TARGET].baseline
    assert baseline["RTL9617C"].status == "OK"
    assert baseline["RTL9607F"].status == "OK"
    build = out["branch_results"][TARGET].commits[0].build
    assert build["RTL9617C"].status == "OK"
    assert build["RTL9607F"].status == "OK"


def test_cycle_rerun_prepare_reuses_existing_worktree(tmp_path):
    write_branch_md(tmp_path)
    ctx = batch_ctx(tmp_path, shas=["a1"])
    wt = worktree_path_for(ctx, TARGET)
    wt.mkdir(parents=True)
    # 有效 worktree：.git 指向存在的 gitdir
    gitdir = wt / ".gitdir"
    gitdir.mkdir()
    (wt / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")

    out = run(build_workflow(ctx), base_state())

    assert out["status"] == "SUCCESS"
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


def test_conflict_stops_branch_immediately_and_patches(tmp_path):
    """cron 精简：cherry-pick 冲突即停本分支批（不 resolve_conflict），出 patch 收尾。"""
    write_branch_md(tmp_path)
    ctx = batch_ctx(tmp_path, shas=["a1", "a2"])
    from bsa.domain.models import ConflictResolution

    # 即使配了能解析的 agent，cron 图也不调用它。
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
    # resolve 节点未装配：conflict_agent 零调用、无 cherry-pick --continue。
    assert ctx.conflict_agent.calls == []
    assert wg.cherry_pick_continue_calls == 0
    # 只 cherry-pick 到冲突的 a1，后续 a2 停批未尝试。
    assert cherry_picked(wg) == ["a1"]
    branch = out["branch_results"][TARGET]
    assert branch.commits[0].cherry_pick == "CONFLICT"
    # 冲突即停批 → 分支 FAILED，但仍出 patch 收尾（供人工处理）。
    assert branch.status == "FAILED"
    assert branch.patch_path is not None
    assert out["report"] is not None
    assert out["status"] == "FAILED"


def test_build_failure_stops_branch_immediately(tmp_path):
    """cron 精简：编译失败即停本分支批（不 fix_build 自动修复），后续 commit 不尝试。"""
    write_branch_md(tmp_path)
    ctx = batch_ctx(tmp_path, shas=["a1", "a2"])
    # baseline（第 1 次 build）成功；a1 cherry-pick 后 commit build 失败。
    ctx.runner = FakeRunner(success_until=1)

    out = run(build_workflow(ctx), base_state())

    # fix_build 节点未装配：build_agent 零调用。
    assert ctx.build_agent.calls == []
    assert cherry_picked(worktree_git_for(ctx, TARGET)) == ["a1"]
    branch = out["branch_results"][TARGET]
    assert branch.commits[0].build["RTL9617C"].status == "FAILED"
    assert branch.commits[0].build["RTL9617C"].agent_attempts == 0
    assert branch.patch_path is not None
    assert branch.status == "FAILED"
    assert out["report"] is not None


def test_conflict_stop_does_not_leak_to_next_target(tmp_path):
    """停批只停本分支：TARGET 冲突停批出 patch 后，next_branch 转 TARGET2 正常同步。"""
    path = tmp_path / "branch.md"
    path.write_text(BRANCH_MD_TWO, encoding="utf-8")
    ctx = batch_ctx(tmp_path, shas=["a1"], targets=[TARGET, TARGET2])
    ctx.conclude.results[("a1", TARGET2)] = Conclusion4(
        kind="NeedSync", evidence=[], confidence="high"
    )
    # TARGET 冲突停批；TARGET2 全程 build 成功（冲突不跨 target 污染）。
    inject_worktree_git(
        ctx,
        FakeWorktreeGit(
            results={"a1": CherryPickResult(status="CONFLICT")}, unmerged=["plat/demo.c"]
        ),
        TARGET,
    )

    out = run(build_workflow(ctx), base_state())

    assert ctx.conflict_agent.calls == []
    # TARGET 冲突停批仍出 patch；TARGET2 正常同步出 patch。
    assert out["branch_results"][TARGET].patch_path is not None
    assert out["branch_results"][TARGET].status == "FAILED"
    assert cherry_picked(worktree_git_for(ctx, TARGET2)) == ["a1"]
    assert out["branch_results"][TARGET2].patch_path is not None
    assert out["branch_results"][TARGET2].status == "SUCCESS"
    # 任一分支 FAILED → 周期 FAILED（分支级 PARTIAL 才是周期 PARTIAL 的前提）。
    assert out["status"] == "FAILED"


def test_cherry_pick_failed_routes_to_report(tmp_path):
    """FAILED 必须留下可归因的终态。

    回归：cron 路由 ``CHERRY_PICK_FAILED → report`` 绕过了 ``generate_patch``，
    分支状态曾停在 prepare 初值 PARTIAL、stop_reason 为 None、git 诊断被丢弃 ——
    `cycle-2026-09-10` 的「停了但说不出为什么」即由此而来。
    """
    write_branch_md(tmp_path)
    ctx = batch_ctx(tmp_path, shas=["a1"])
    inject_worktree_git(
        ctx,
        FakeWorktreeGit(
            results={
                "a1": CherryPickResult(
                    status="FAILED", error="error: commit a1 is a merge but no -m option was given."
                )
            }
        ),
        TARGET,
    )

    out = run(build_workflow(ctx), base_state())

    assert ctx.conflict_agent.calls == []
    assert out["status"] == "FAILED"  # 全批次无成功 commit，不是「部分成功」
    assert out["branch_results"][TARGET].stop_reason == "cherry-pick failed on a1"
    commit = out["branch_results"][TARGET].commits[0]
    assert commit.cherry_pick == "FAILED"
    assert "no -m option" in commit.reason
    assert "cherry_pick" in out["errors"]


def test_cherry_pick_conflict_routes_to_patch_leaves_reason(tmp_path):
    """cron 冲突停批也必须留下可归因的终态（与 FAILED 同一条回归）。

    cron 路由 ``CHERRY_PICK_CONFLICT → generate_patch`` 停本分支批转人工。但
    ``cherry_pick`` 节点只给 ``FAILED`` 写 stop_reason/errors，CONFLICT 只更新
    commits —— 于是分支被算成 FAILED（全批无成功 commit），errors 空、
    stop_reason None、平台 failure_summary 也提不出任何原因。
    `scan-2026-09-08T22:00:00+08:00-2026-09-09T22:00:00+08:00` 实测即此：FAILED
    但一个字的原因都给不出。
    """
    write_branch_md(tmp_path)
    ctx = batch_ctx(tmp_path, shas=["a1"])
    inject_worktree_git(
        ctx,
        FakeWorktreeGit(
            results={
                "a1": CherryPickResult(
                    status="CONFLICT",
                    conflict_files=["component/cwmp_dm/msg_telecom/ADI_get_set.c"],
                    error="Auto-merging component/cwmp_dm/msg_telecom/ADI_get_set.c\n"
                    "CONFLICT (content): Merge conflict in ADI_get_set.c",
                )
            }
        ),
        TARGET,
    )

    out = run(build_workflow(ctx), base_state())

    # cron 绝不调 LLM 解决冲突。
    assert ctx.conflict_agent.calls == []
    branch = out["branch_results"][TARGET]
    commit = branch.commits[0]
    assert commit.cherry_pick == "CONFLICT"
    # 冲突文件要能查到（人工按图索骥的入口）。
    assert commit.conflict_files == [
        "component/cwmp_dm/msg_telecom/ADI_get_set.c"
    ]
    # 分支停批，且留下原因——这正是本次要修的缺口。
    assert branch.status == "FAILED"
    assert branch.stop_reason == "cherry-pick conflict on a1"
    assert "cherry_pick" in out["errors"]
    assert "ADI_get_set.c" in out["errors"]["cherry_pick"].error


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
