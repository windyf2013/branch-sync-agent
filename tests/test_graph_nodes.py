from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bsa.build.runner import BuildResult
from bsa.domain.models import (
    BranchResult,
    BuildOutcome,
    CherryPickResult,
    CommitInfo,
    CommitResult,
    Conclusion4,
    ConflictResolution,
    ErrorRecord,
    SyncDecision,
)
from bsa.executor.exceptions import InfrastructureError
from bsa.graph.nodes import (
    GraphContext,
    _action_required,
    _derive_window,
    _load_matrix,
    _to_analysis,
    baseline_build,
    build,
    cherry_pick,
    default_window,
    detect_commits,
    fix_build,
    generate_patch,
    node_wrapper,
    prepare_worktree,
    report,
    resolve_conflict,
    sync_decision,
)
from bsa.rules import (
    Classification,
    ConcludeThresholds,
    DecisionRules,
)
from bsa.rules.conclude import CommitAnalysis

CST = timezone(timedelta(hours=8))
DEVELOP = "br_v4.33_5200_CU_develop_20260518"
TARGET = "br_v4.33_5200_CU_develop_release_p360_20260625"

BRANCH_MD = f"""# 分支清单
## 1 RCIOS代码库
- 路径：rcios

### 1.1 4.34 主分支
- {TARGET}

### 1.2 4.34 业务分支
- {DEVELOP}
"""

SOURCE_FIX_TEXT = """#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int demo_check(const char *name, size_t len)
{
    if (NULL == name)
    {
        return -1;
    }
    if (0 == len)
    {
        return -1;
    }
    for (size_t i = 0; i < len; i++)
    {
        if (name[i] == '\\0')
        {
            return -1;
        }
    }
    return 0;
}
"""

TARGET_OLD_TEXT = """static int demo_check(const char *name, size_t len)
{
    return 0;
}
"""


OTHER_TARGET = "br_v4.33_5200B_develop_20260702"
OTHER_DEVELOP = "br_v4.33_5200B_develop_fttr_20260811"

# 完整清单：无「主分支/业务分支」标注 → build_matrix 产出零条边。
# 它只服务手动同步的型号解析与平台下拉，cron 绝不应该读它。
FULL_BRANCH_MD = f"""# 分支清单
## 1 RCIOS代码库
- 路径：rcios

### 1.1 组网产品分支
- {OTHER_TARGET}
- {OTHER_DEVELOP}

### 1.2 4.34 产品主线
- {TARGET}
- {DEVELOP}
"""


def write_branch_md(tmp_path: Path) -> Path:
    path = tmp_path / "branch.md"
    path.write_text(BRANCH_MD, encoding="utf-8")
    return path


def write_split_branch_files(tmp_path: Path) -> tuple[Path, Path]:
    """完整清单 branch.md（无标注）+ cron 专用 branch-cron.md（带标注）。"""
    full = tmp_path / "branch.md"
    full.write_text(FULL_BRANCH_MD, encoding="utf-8")
    cron = tmp_path / "branch-cron.md"
    cron.write_text(BRANCH_MD, encoding="utf-8")
    return full, cron


def make_settings(tmp_path: Path, **overrides: object):
    base = {
        "repo_path": str(tmp_path),
        "branch_file": str(tmp_path / "branch.md"),
        "worktree_root": str(tmp_path / "wt"),
        "llm_model": "model",
        "llm_api_key": "key",
        "llm_base_url": "url",
        "docker_image": "img",
        "docker_mount_workspace": "/workspace/rcios",
        "build_script_dir": "build/platform/RTL9617C",
        "mail_sender": "sender",
        "mail_recipients": ["recv"],
        "log_dir": str(tmp_path / "logs"),
    }
    base.update(overrides)
    from bsa.config.settings import Settings

    return Settings(**base, _env_file=None)


class FakeGit:
    def __init__(self, repo_path: str | None = None) -> None:
        self.repo_path = Path(repo_path) if repo_path else Path("/repo")
        self.calls: list[tuple] = []
        self.tips: dict[str, tuple[str, str]] = {}
        self.window_shas: dict[str, list[str]] = {}
        self.changed: dict[str, list[str]] = {}
        self.patches: dict[str, str] = {}
        self.patch_ids: dict[str, str] = {}
        self.is_ancestor_results: dict[tuple[str, str], bool] = {}
        self.file_exists_results: dict[tuple[str, str], bool] = {}
        self.cherry_pick_result: CherryPickResult | None = None
        self.cherry_pick_continue_calls = 0
        self.unmerged_files_result: list[str] = []
        self.format_patch_result: Path | None = None
        self.file_texts: dict[tuple[str, str], str] = {}
        self.metadata_results: dict[str, tuple[str, str, str]] = {}
        self.status_result: str = ""

    def _record(self, name: str, args: tuple) -> None:
        self.calls.append((name, args))

    def fetch_all(self) -> None:
        self._record("fetch_all", ())

    def branch_tip(self, branch: str) -> tuple[str, str]:
        self._record("branch_tip", (branch,))
        return self.tips.get(branch, (f"origin/{branch}", f"tip-{branch}"))

    def commits_in_window(self, since: str, until: str, ref: str) -> list[str]:
        self._record("commits_in_window", (since, until, ref))
        return list(self.window_shas.get(ref, self.window_shas.get("default", [])))

    def commit_metadata(self, sha: str) -> tuple[str, str, str]:
        self._record("commit_metadata", (sha,))
        return self.metadata_results.get(
            sha, ("dev", "2026-01-01T10:00:00+08:00", f"msg-{sha}")
        )

    def changed_files(self, sha: str) -> list[str]:
        self._record("changed_files", (sha,))
        return list(self.changed.get(sha, []))

    def commit_patch(self, sha: str) -> str:
        self._record("commit_patch", (sha,))
        return self.patches.get(sha, "")

    def patch_id(self, sha: str) -> str | None:
        self._record("patch_id", (sha,))
        return self.patch_ids.get(sha)

    def is_ancestor(self, sha: str, ref: str) -> bool:
        self._record("is_ancestor", (sha, ref))
        return self.is_ancestor_results.get((sha, ref), False)

    def file_exists(self, ref: str, path: str) -> bool:
        self._record("file_exists", (ref, path))
        return self.file_exists_results.get((ref, path), True)

    def show_file(self, ref: str, path: str) -> str | None:
        self._record("show_file", (ref, path))
        return self.file_texts.get((ref, path))

    def add_worktree(self, branch: str, path: Path) -> None:
        self._record("add_worktree", (branch, path))

    def list_worktrees(self) -> list[Path]:
        self._record("list_worktrees", ())
        return list(getattr(self, "worktrees", []))

    def remove_worktree(self, path: Path) -> None:
        self._record("remove_worktree", (path,))
        if getattr(self, "fail_remove_worktree", False):
            raise InfrastructureError("cannot remove worktree")

    def cherry_pick(self, sha: str) -> CherryPickResult:
        self._record("cherry_pick", (sha,))
        return self.cherry_pick_result

    def unmerged_files(self) -> list[str]:
        self._record("unmerged_files", ())
        return list(self.unmerged_files_result)

    def cherry_pick_continue(self) -> None:
        self.cherry_pick_continue_calls += 1

    def status(self) -> str:
        self._record("status", ())
        return self.status_result

    def format_patch(self, base: str, head: str, out_dir: Path, prefix: str) -> Path:
        self._record("format_patch", (base, head, out_dir, prefix))
        return self.format_patch_result or (out_dir / prefix)


class FakeClassify:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.results: dict[str, Classification] = {}

    def __call__(
        self,
        message,
        changed_files,
        symbols,
        patch_text,
        *,
        sha=None,
        patch_id=None,
        agent_judgments=None,
    ):
        self.calls.append(sha)
        return self.results.get(
            sha,
            Classification(is_bug_fix=False, recognition_source="fake", needs_agent=False),
        )


class FakeConclude:
    def __init__(self) -> None:
        self.calls: list[tuple[CommitAnalysis, object]] = []
        self.results: dict[tuple[str, str], Conclusion4] = {}

    def __call__(self, analysis, snapshot, *, similarity_high=0.90, similarity_low=0.50):
        self.calls.append((analysis, snapshot))
        return self.results.get(
            (analysis.sha, snapshot.branch_name),
            Conclusion4(kind="AlreadyIncluded", evidence=[], confidence="high"),
        )


class FakeSyncDecisionAgent:
    def __init__(self) -> None:
        self.calls: list[list[CommitInfo]] = []
        self.risk_calls: list[list[CommitInfo]] = []
        self.results: dict[str, SyncDecision] = {}
        self.risk_results: dict[str, str | None] = {}

    def run(
        self,
        pending: list[CommitInfo],
        prior_risks: dict[str, str | None] | None = None,
    ) -> dict[str, SyncDecision]:
        self.calls.append(list(pending))
        out: dict[str, SyncDecision] = {}
        for commit in pending:
            decision = self.results.get(
                commit.sha,
                SyncDecision(
                    sha=commit.sha,
                    is_bug_fix=True,
                    reason=None,
                    recognition_source="agent:bug-fix",
                    needs_agent=False,
                ),
            )
            rule_risk = (prior_risks or {}).get(commit.sha)
            if rule_risk is not None:
                decision = decision.model_copy(update={"risk": rule_risk})
            out[commit.sha] = decision
        return out

    def resolve_risks(self, commits: list[CommitInfo]) -> dict[str, str | None]:
        self.risk_calls.append(list(commits))
        return {commit.sha: self.risk_results.get(commit.sha) for commit in commits}


class FakeRunner:
    def __init__(self, success: bool = True, *, success_until: int | None = None) -> None:
        self.success = success
        self.success_until = success_until
        self.build_calls: list[dict] = []

    def _succeeded(self, prior_calls: int) -> bool:
        if self.success_until is None:
            return self.success
        return prior_calls < self.success_until

    def build_commit(self, worktree, model, *, clean, module=None, log_path=None):
        prior_calls = len(self.build_calls)
        self.build_calls.append(
            {
                "worktree": worktree,
                "model": model,
                "clean": clean,
                "module": module,
                "log_path": log_path,
            }
        )
        ok = self._succeeded(prior_calls)
        return BuildResult(
            model=model,
            returncode=0 if ok else 1,
            log_path=log_path or Path("build.log"),
            succeeded=ok,
            errors=[] if ok else ["compile error"],
        )

    def is_success(self, result: BuildResult) -> bool:
        return result.succeeded


class FakeSafety:
    def __init__(
        self,
        models: tuple[str, ...] = ("RTL9617C",),
        forbidden_branches: set[str] | None = None,
    ) -> None:
        self.models = list(models)
        self.forbidden = set(forbidden_branches or set())

    def required_models(self) -> list[str]:
        return list(self.models)

    def check_editable(self, paths: list[str]) -> None:
        pass

    def check_sync_branch(self, branch: str) -> bool:
        return branch not in self.forbidden


class FakeConflictAgent:
    def __init__(self, resolution: ConflictResolution | None = None) -> None:
        self.resolution = resolution
        self.calls: list[tuple] = []

    def resolve(self, commit, conflict_files, *, git=None, target_branch=None):
        self.calls.append((commit, list(conflict_files), git, target_branch))
        return self.resolution


class FakeBuildAgent:
    def __init__(self, category: str = "introduced_by_commit") -> None:
        self.calls: list[tuple] = []
        from bsa.agents.base import BuildAttribution

        self.attribution = BuildAttribution(
            category=category, reason="fixed", files_to_fix=[]
        )

    def fix(self, commit, errors, model, *, git=None, target_branch=None):
        self.calls.append((commit, errors, model, git, target_branch))
        return self.attribution


def make_ctx(
    tmp_path: Path, *, settings_overrides: dict | None = None, **kw: object
) -> GraphContext:
    settings = make_settings(tmp_path, **(settings_overrides or {}))
    ctx = GraphContext(
        settings=settings,
        executor=SimpleNamespace(),
        git=FakeGit(),
        runner=FakeRunner(),
        sync_decision_agent=FakeSyncDecisionAgent(),
        conflict_agent=FakeConflictAgent(),
        build_agent=FakeBuildAgent(),
        safety=FakeSafety(),
        decision_rules=DecisionRules(classify={}, conclude=ConcludeThresholds(), branch_mapping={}),
        classify=FakeClassify(),
        conclude=FakeConclude(),
    )
    for key, value in kw.items():
        setattr(ctx, key, value)
    return ctx


def base_state(**overrides: object) -> dict:
    state = {
        "cycle_id": "cycle-20260101",
        "scan_window": ("2026-01-01T22:00:00+08:00", "2026-01-02T22:00:00+08:00"),
        "branch_md_version": "v1",
        "detected_commits": [],
        "classifications": {},
        "decisions": {},
        "batches": {},
        "current_target": None,
        "current_commit": None,
        "branch_results": {},
        "status": "NEW",
        "errors": {},
        "report": None,
    }
    state.update(overrides)
    return state


def commit(sha: str, **overrides: object) -> CommitInfo:
    base = CommitInfo(
        sha=sha,
        message=f"msg-{sha}",
        author="dev",
        committed_at="2026-01-02T10:00:00+08:00",
        changed_files=["plat/demo.c"],
        patch_text="+x",
        symbols=[],
        patch_id=f"pid-{sha}",
        issue_ids=[],
        source_branch=DEVELOP,
        homologous_section="组网",
    )
    return base.model_copy(update=overrides)


def branch_result(target: str, *, commits: list[CommitResult] | None = None) -> BranchResult:
    return BranchResult(
        target_branch=target,
        worktree_path="/wt",
        status="PARTIAL",
        commits=commits or [],
        patch_path=None,
        stop_reason=None,
    )


def commit_result(sha: str, cherry_pick_status: str = "OK") -> CommitResult:
    return CommitResult(
        sha=sha,
        cherry_pick=cherry_pick_status,
        conflict_resolution=None,
        build={},
    )


def failed_outcome() -> BuildOutcome:
    return BuildOutcome(
        model="RTL9617C",
        status="FAILED",
        log_path="/l",
        errors=["compile error"],
        agent_attempts=0,
        fix_diff=None,
    )


# --- node_wrapper ---


def test_node_wrapper_catches_exception_into_errors():
    def boom(state):
        raise RuntimeError("kaboom")

    update = node_wrapper(boom)({"cycle_id": "c", "errors": {}})
    assert update["status"] == "FAILED"
    assert update["errors"]["boom"].node == "boom"
    assert update["errors"]["boom"].error == "kaboom"


def test_node_wrapper_preserves_successful_update():
    def ok(state):
        return {"status": "OK"}

    assert node_wrapper(ok)({"cycle_id": "c"}) == {"status": "OK"}


def test_node_wrapper_merges_existing_errors():
    def boom(state):
        raise ValueError("x")

    existing = ErrorRecord(node="detect_commits", error="old", ts="t")
    update = node_wrapper(boom)({"errors": {"detect_commits": existing}})
    assert update["errors"]["detect_commits"] is existing
    assert update["errors"]["boom"].error == "x"


def test_node_wrapper_binds_context_and_records_node_name():
    def node(state, *, ctx):
        raise RuntimeError("nope")

    wrapped = node_wrapper(node, ctx=SimpleNamespace())
    update = wrapped({})
    assert update["errors"]["node"].error == "nope"
    assert update["status"] == "FAILED"


def test_node_wrapper_ctx_success():
    def node(state, *, ctx):
        return {"marker": ctx.marker}

    wrapped = node_wrapper(node, ctx=SimpleNamespace(marker="yes"))
    assert wrapped({})["marker"] == "yes"


# --- default window (决策 38) ---


def test_default_window_previous_complete_day_before_2200():
    now = datetime(2026, 8, 21, 12, 0, tzinfo=CST)
    since, until = default_window(now)
    assert since == "2026-08-19T22:00:00+08:00"
    assert until == "2026-08-20T22:00:00+08:00"


def test_default_window_after_2200_uses_today():
    now = datetime(2026, 8, 21, 23, 0, tzinfo=CST)
    since, until = default_window(now)
    assert since == "2026-08-20T22:00:00+08:00"
    assert until == "2026-08-21T22:00:00+08:00"


# --- partial window override (决策 38) ---


def test_derive_window_both_provided_passthrough():
    assert _derive_window("2026-08-18T22:00:00+08:00", "2026-08-20T22:00:00+08:00") == (
        "2026-08-18T22:00:00+08:00",
        "2026-08-20T22:00:00+08:00",
    )


def test_derive_window_neither_uses_default(monkeypatch):
    monkeypatch.setattr("bsa.graph.nodes.default_window", lambda now=None: ("D", "U"))
    assert _derive_window(None, None) == ("D", "U")


def test_derive_window_only_until_derives_since():
    since, until = _derive_window(None, "2026-08-20T22:00:00+08:00")
    assert since == "2026-08-19T22:00:00+08:00"
    assert until == "2026-08-20T22:00:00+08:00"


def test_derive_window_only_since_derives_until():
    now = datetime(2026, 8, 21, 12, 0, tzinfo=CST)
    since, until = _derive_window("2026-08-18T22:00:00+08:00", None, now=now)
    assert since == "2026-08-18T22:00:00+08:00"
    assert until == "2026-08-21T12:00:00+08:00"


def test_derive_window_naive_until_assumes_cst():
    since, until = _derive_window(None, "2026-08-20T22:00:00")
    assert since == "2026-08-19T22:00:00+08:00"


def test_detect_commits_partial_override_not_dropped(tmp_path, monkeypatch):
    write_branch_md(tmp_path)
    ctx = make_ctx(
        tmp_path,
        settings_overrides={"scan_since": "2026-08-18T22:00:00+08:00", "scan_until": None},
    )
    monkeypatch.setattr("bsa.graph.nodes.default_window", lambda now=None: ("DEFAULT", "WINDOW"))
    update = detect_commits(base_state(), ctx)
    since, until = update["scan_window"]
    assert since == "2026-08-18T22:00:00+08:00"
    assert until != "WINDOW"
    assert update["status"] == "DETECTED"


# --- detect_commits ---


def test_detect_commits_computes_default_window_when_scan_since_none(tmp_path, monkeypatch):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    monkeypatch.setattr("bsa.graph.nodes.default_window", lambda: ("SINCE", "UNTIL"))
    update = detect_commits(base_state(), ctx)
    assert update["scan_window"] == ("SINCE", "UNTIL")
    assert update["status"] == "DETECTED"


def test_detect_commits_uses_settings_window_when_provided(tmp_path, monkeypatch):
    write_branch_md(tmp_path)
    ctx = make_ctx(
        tmp_path,
        settings_overrides={
            "scan_since": "2026-01-01T22:00:00+08:00",
            "scan_until": "2026-01-02T22:00:00+08:00",
        },
    )
    monkeypatch.setattr("bsa.graph.nodes.default_window", lambda: ("SINCE", "UNTIL"))
    update = detect_commits(base_state(), ctx)
    assert update["scan_window"] == ("2026-01-01T22:00:00+08:00", "2026-01-02T22:00:00+08:00")


def test_detect_commits_populates_commits_and_classifications(tmp_path):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    git = ctx.git
    git.window_shas = {f"origin/{DEVELOP}": ["a1", "a2"]}
    git.changed = {"a1": ["plat/demo.c"], "a2": ["plat/other.c"]}
    git.patches = {"a1": "+p1", "a2": "+p2"}
    git.patch_ids = {"a1": "pid1", "a2": "pid2"}
    ctx.classify.results = {
        "a1": Classification(
            is_bug_fix=True,
            recognition_source="machine:[BUG]",
            issue_ids=["CQ1"],
            needs_agent=False,
        ),
        "a2": Classification(
            is_bug_fix=False,
            recognition_source="pending:claude-agent",
            needs_agent=True,
        ),
    }

    update = detect_commits(base_state(), ctx)

    assert git.calls[0][0] == "fetch_all"
    assert [c.sha for c in update["detected_commits"]] == ["a1", "a2"]
    first = update["detected_commits"][0]
    assert first.source_branch == DEVELOP
    assert first.homologous_section == "4.34"
    assert first.issue_ids == ["CQ1"]
    assert first.patch_id == "pid1"
    assert update["classifications"]["a1"].is_bug_fix is True
    assert update["classifications"]["a1"].needs_agent is False
    assert update["classifications"]["a2"].needs_agent is True
    assert update["branch_md_version"]
    assert ctx.matrix is not None


def test_detect_commits_reads_cron_branch_file_not_full_inventory(tmp_path):
    """cron 拓扑来自 CRON_BRANCH_FILE；完整清单里的分支不进 cron 扫描面。"""
    _, cron = write_split_branch_files(tmp_path)
    ctx = make_ctx(tmp_path, settings_overrides={"cron_branch_file": str(cron)})
    git = ctx.git
    git.window_shas = {f"origin/{DEVELOP}": ["a1"]}
    git.changed = {"a1": ["plat/demo.c"]}
    git.patches = {"a1": "+p1"}
    git.patch_ids = {"a1": "pid1"}

    update = detect_commits(base_state(), ctx)

    assert [hs.section for hs in ctx.matrix] == ["4.34"]
    assert [b.name for hs in ctx.matrix for b in hs.sources] == [DEVELOP]
    # 完整清单里的分支既不是源也不是目标
    scanned = [args[0] for name, args in git.calls if name == "branch_tip"]
    assert OTHER_DEVELOP not in scanned
    assert [c.sha for c in update["detected_commits"]] == ["a1"]


def test_detect_commits_branch_md_version_hashes_cron_file(tmp_path):
    """版本号标识 cron 拓扑来源：改完整清单不应让 cron 周期版本漂移。"""
    full, cron = write_split_branch_files(tmp_path)
    ctx = make_ctx(tmp_path, settings_overrides={"cron_branch_file": str(cron)})
    before = detect_commits(base_state(), ctx)["branch_md_version"]

    full.write_text(FULL_BRANCH_MD + "\n- br_v4.33_extra_develop_20260901\n", encoding="utf-8")
    ctx.matrix = None
    after = detect_commits(base_state(), ctx)["branch_md_version"]

    assert before == after


def test_detect_commits_empty_matrix_records_error_not_silent_noop(tmp_path):
    """零同步边必须响亮报错：漏配 CRON_BRANCH_FILE 会让 cron 静默空跑。"""
    path = tmp_path / "branch.md"
    path.write_text(FULL_BRANCH_MD, encoding="utf-8")
    ctx = make_ctx(tmp_path)

    update = detect_commits(base_state(), ctx)

    assert update["detected_commits"] == []
    errors = update["errors"]
    assert "branch_matrix" in errors
    assert "CRON_BRANCH_FILE" in errors["branch_matrix"].error


def test_empty_matrix_makes_cycle_fail_not_silent_success(tmp_path):
    """空扫描面必须收敛成 FAILED 周期 + action_required，而不是"零 commit 的成功"。"""
    path = tmp_path / "branch.md"
    path.write_text(FULL_BRANCH_MD, encoding="utf-8")
    ctx = make_ctx(tmp_path)

    detected = detect_commits(base_state(), ctx)
    final = report(base_state(**{"errors": detected["errors"]}), ctx)

    assert final["status"] == "FAILED"
    nodes_flagged = [a.get("node") for a in final["report"].action_required]
    assert "branch_matrix" in nodes_flagged


def test_detect_commits_non_empty_matrix_records_no_matrix_error(tmp_path):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)

    update = detect_commits(base_state(), ctx)

    assert "branch_matrix" not in (update.get("errors") or {})


def test_load_matrix_falls_back_to_branch_file_when_cron_unset(tmp_path):
    """未配置 CRON_BRANCH_FILE 时回退 branch_file（老部署零改动兼容）。"""
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)

    matrix = _load_matrix(ctx)

    assert [hs.section for hs in matrix] == ["4.34"]
    assert [b.name for hs in matrix for b in hs.sources] == [DEVELOP]


def test_load_matrix_reads_cron_branch_file_when_set(tmp_path):
    _, cron = write_split_branch_files(tmp_path)
    ctx = make_ctx(tmp_path, settings_overrides={"cron_branch_file": str(cron)})

    matrix = _load_matrix(ctx)

    assert [hs.section for hs in matrix] == ["4.34"]


# --- sync_decision ---


def test_detect_commits_extracts_symbols_from_patch(tmp_path):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    git = ctx.git
    git.window_shas = {f"origin/{DEVELOP}": ["a1"]}
    git.changed = {"a1": ["plat/demo.c"]}
    git.patches = {"a1": "+static int demo_check(const char *p)\n+{\n+    return 0;\n+}\n"}
    git.patch_ids = {"a1": "pid1"}

    update = detect_commits(base_state(), ctx)

    assert update["detected_commits"][0].symbols == ["demo_check"]


def test_detect_commits_sets_high_risk_from_severity_rules(tmp_path):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    git = ctx.git
    git.window_shas = {f"origin/{DEVELOP}": ["a1"]}
    git.changed = {"a1": ["plat/demo.c"]}
    git.patches = {"a1": "+x"}
    git.patch_ids = {"a1": "pid1"}
    git.metadata_results = {"a1": ("dev", "2026-01-01T10:00:00+08:00", "[CRITICAL] fix crash")}
    ctx.classify.results = {
        "a1": Classification(
            is_bug_fix=True,
            recognition_source="machine:[BUG]",
            needs_agent=False,
        )
    }

    update = detect_commits(base_state(), ctx)

    assert update["classifications"]["a1"].risk == "high"


def test_detect_commits_leaves_risk_none_when_severity_unknown(tmp_path):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    git = ctx.git
    git.window_shas = {f"origin/{DEVELOP}": ["a1"]}
    git.changed = {"a1": ["plat/demo.c"]}
    git.patches = {"a1": "+x"}
    git.patch_ids = {"a1": "pid1"}

    update = detect_commits(base_state(), ctx)

    assert update["classifications"]["a1"].risk is None


def test_detect_commits_applies_judgments_override_to_machine_classified(tmp_path):
    # G9：人工覆盖（judgments.json）必须贯通全量 commit 判定，含机器已判定 [BUG] 的 commit。
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    from bsa.rules import classify_commit

    ctx.classify = classify_commit  # 真实 classifier，honor judgments
    git = ctx.git
    sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    git.window_shas = {f"origin/{DEVELOP}": [sha]}
    git.changed = {sha: ["plat/demo.c"]}
    git.patches = {sha: "+x"}
    git.patch_ids = {sha: "pid1"}
    git.metadata_results = {
        sha: ("dev", "2026-01-01T10:00:00+08:00", "[BUG] CQ999 fix null deref")
    }
    log_dir = Path(ctx.settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "judgments.json").write_text(
        json.dumps({sha: {"is_bug_fix": False, "reason": "人工判定非 bug-fix"}}),
        encoding="utf-8",
    )

    update = detect_commits(base_state(), ctx)

    assert update["classifications"][sha].is_bug_fix is False
    assert update["classifications"][sha].needs_agent is False


def test_sync_decision_resolves_pending_and_builds_batches(tmp_path):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    c1 = commit("a1", issue_ids=["CQ1"])
    c2 = commit("a2")
    state = base_state(
        detected_commits=[c1, c2],
        classifications={
            "a1": SyncDecision(
                sha="a1",
                is_bug_fix=True,
                reason=None,
                recognition_source="machine:[BUG]",
                needs_agent=False,
            ),
            "a2": SyncDecision(
                sha="a2",
                is_bug_fix=False,
                reason=None,
                recognition_source="pending:claude-agent",
                needs_agent=True,
            ),
        },
    )
    ctx.sync_decision_agent.results = {
        "a2": SyncDecision(
            sha="a2",
            is_bug_fix=True,
            reason=None,
            recognition_source="agent:bug-fix",
            needs_agent=False,
        )
    }
    ctx.conclude.results = {
        ("a1", TARGET): Conclusion4(kind="NeedSync", evidence=["x"], confidence="high"),
        ("a2", TARGET): Conclusion4(kind="AlreadyIncluded", evidence=["y"], confidence="high"),
    }

    update = sync_decision(state, ctx)

    assert [c.sha for c in ctx.sync_decision_agent.calls[0]] == ["a2"]
    assert [c.sha for c in ctx.sync_decision_agent.risk_calls[0]] == ["a1", "a2"]
    assert update["classifications"]["a2"].is_bug_fix is True
    assert update["classifications"]["a2"].needs_agent is False
    assert update["decisions"]["a1"][TARGET].kind == "NeedSync"
    assert update["decisions"]["a2"][TARGET].kind == "AlreadyIncluded"


def test_sync_decision_ledger_short_circuits_already_included(tmp_path):
    # 台账短路：目标分支已同步过该 patch-id → 直接 AlreadyIncluded，跳过快照+conclude。
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    from bsa.ledger import record_synced

    log_dir = Path(ctx.settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    record_synced(log_dir, "pid-a1", TARGET, "a1")
    c1 = commit("a1")
    state = base_state(
        detected_commits=[c1],
        classifications={
            "a1": SyncDecision(
                sha="a1",
                is_bug_fix=True,
                reason=None,
                recognition_source="machine:[BUG]",
                needs_agent=False,
            )
        },
    )

    update = sync_decision(state, ctx)

    assert ctx.conclude.calls == []  # 未调用 conclude（跳过快照/相似度判定）
    assert update["decisions"]["a1"][TARGET].kind == "AlreadyIncluded"
    assert update["batches"] == {}  # 已含 → 不进批次
    assert update["status"] == "DECIDED"


def test_to_analysis_uses_branch_mapping_for_source_type(tmp_path):
    decision = SyncDecision(
        sha="a1",
        is_bug_fix=True,
        reason=None,
        recognition_source="machine:[BUG]",
        needs_agent=False,
    )
    analysis = _to_analysis(commit("a1"), decision, {DEVELOP: "release"})
    assert analysis.source_branch_type == "release"


def test_sync_decision_develop_target_is_valid(tmp_path):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    ctx.decision_rules.branch_mapping = {TARGET: "develop"}
    c1 = commit("a1", issue_ids=["CQ1"])
    state = base_state(
        detected_commits=[c1],
        classifications={
            "a1": SyncDecision(
                sha="a1",
                is_bug_fix=True,
                reason=None,
                recognition_source="machine:[BUG]",
                needs_agent=False,
            )
        },
    )
    ctx.conclude.results = {
        ("a1", TARGET): Conclusion4(kind="NeedSync", evidence=[], confidence="high")
    }

    update = sync_decision(state, ctx)

    assert update["batches"] == {TARGET: ["a1"]}
    assert ctx.matrix is not None


def test_sync_decision_forbidden_branch_routes_out_of_scope(tmp_path):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    ctx.safety.forbidden = {TARGET}
    c1 = commit("a1", issue_ids=["CQ1"])
    state = base_state(
        detected_commits=[c1],
        classifications={
            "a1": SyncDecision(
                sha="a1",
                is_bug_fix=True,
                reason=None,
                recognition_source="machine:[BUG]",
                needs_agent=False,
            )
        },
    )

    update = sync_decision(state, ctx)

    assert update["decisions"]["a1"][TARGET].kind == "OutOfScope"
    assert "禁止" in update["decisions"]["a1"][TARGET].evidence[0]
    assert update["batches"] == {}
    assert update["status"] == "DECIDED"


def test_sync_decision_no_pending_skips_is_bugfix_llm(tmp_path):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    c1 = commit("a1")
    state = base_state(
        detected_commits=[c1],
        classifications={
            "a1": SyncDecision(
                sha="a1",
                is_bug_fix=True,
                reason=None,
                recognition_source="machine:[BUG]",
                needs_agent=False,
            )
        },
    )
    ctx.conclude.results = {
        ("a1", TARGET): Conclusion4(kind="ManualReview", evidence=["z"], confidence="low")
    }
    update = sync_decision(state, ctx)
    assert ctx.sync_decision_agent.calls == []
    assert [c.sha for c in ctx.sync_decision_agent.risk_calls[0]] == ["a1"]
    assert update["decisions"]["a1"][TARGET].kind == "ManualReview"
    assert update["batches"] == {}


def test_sync_decision_resolves_risk_and_threads_into_analysis(tmp_path):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    c1 = commit("a1", issue_ids=["CQ1"])
    state = base_state(
        detected_commits=[c1],
        classifications={
            "a1": SyncDecision(
                sha="a1",
                is_bug_fix=True,
                reason=None,
                recognition_source="machine:[BUG]",
                needs_agent=False,
            )
        },
    )
    ctx.sync_decision_agent.risk_results = {"a1": "high"}
    ctx.conclude.results = {
        ("a1", TARGET): Conclusion4(kind="NeedSync", evidence=["x"], confidence="high")
    }

    update = sync_decision(state, ctx)

    assert ctx.sync_decision_agent.calls == []
    assert [c.sha for c in ctx.sync_decision_agent.risk_calls[0]] == ["a1"]
    assert update["classifications"]["a1"].risk == "high"
    assert ctx.conclude.calls[0][0].risk == "high"
    assert update["decisions"]["a1"][TARGET].kind == "NeedSync"
    assert update["batches"] == {TARGET: ["a1"]}


def test_sync_decision_preserves_rule_layer_high_over_llm(tmp_path):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    c1 = commit("a1")
    state = base_state(
        detected_commits=[c1],
        classifications={
            "a1": SyncDecision(
                sha="a1",
                is_bug_fix=True,
                reason=None,
                recognition_source="pending:claude-agent",
                needs_agent=True,
                risk="high",
            )
        },
    )
    ctx.sync_decision_agent.results = {
        "a1": SyncDecision(
            sha="a1",
            is_bug_fix=True,
            reason="llm says low",
            recognition_source="agent:bug-fix",
            needs_agent=False,
            risk="low",
        )
    }
    ctx.conclude.results = {
        ("a1", TARGET): Conclusion4(kind="NeedSync", evidence=["x"], confidence="high")
    }

    update = sync_decision(state, ctx)

    assert [c.sha for c in ctx.sync_decision_agent.calls[0]] == ["a1"]
    assert update["classifications"]["a1"].risk == "high"
    assert ctx.conclude.calls[0][0].risk == "high"
    assert update["decisions"]["a1"][TARGET].kind == "NeedSync"


# --- sync_decision with real conclude_pair (four-state fidelity, task 3.2a) ---


def test_sync_decision_real_conclude_has_source_sha_already_included(tmp_path):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    from bsa.rules.conclude import conclude_pair
    from bsa.rules.snapshot import build_target_snapshot

    ctx.conclude = conclude_pair
    ctx.build_snapshot = build_target_snapshot
    ctx.git.is_ancestor_results = {("a1", f"origin/{TARGET}"): True}
    c1 = commit("a1", issue_ids=["CQ1"])
    state = base_state(
        detected_commits=[c1],
        classifications={
            "a1": SyncDecision(
                sha="a1",
                is_bug_fix=True,
                reason=None,
                recognition_source="machine:[BUG]",
                needs_agent=False,
            )
        },
    )

    update = sync_decision(state, ctx)

    assert update["decisions"]["a1"][TARGET].kind == "AlreadyIncluded"
    assert update["decisions"]["a1"][TARGET].confidence == "high"
    assert update["batches"] == {}


def test_sync_decision_real_conclude_missing_fix_need_sync(tmp_path):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    from bsa.rules.conclude import conclude_pair
    from bsa.rules.snapshot import build_target_snapshot

    ctx.conclude = conclude_pair
    ctx.build_snapshot = build_target_snapshot
    ctx.git.file_texts = {
        (f"origin/{DEVELOP}", "plat/demo.c"): SOURCE_FIX_TEXT,
        (f"origin/{TARGET}", "plat/demo.c"): TARGET_OLD_TEXT,
    }
    c1 = commit(
        "a1",
        changed_files=["plat/demo.c"],
        symbols=["demo_check"],
        patch_id="pid-a1",
    )
    state = base_state(
        detected_commits=[c1],
        classifications={
            "a1": SyncDecision(
                sha="a1",
                is_bug_fix=True,
                reason=None,
                recognition_source="machine:[BUG]",
                needs_agent=False,
            )
        },
    )

    update = sync_decision(state, ctx)

    assert update["decisions"]["a1"][TARGET].kind == "NeedSync"
    assert update["batches"] == {TARGET: ["a1"]}


# --- prepare_worktree ---


def test_prepare_worktree_adds_worktree_and_records_path(tmp_path):
    ctx = make_ctx(tmp_path)
    git = ctx.git
    git.tips = {TARGET: ("origin/" + TARGET, "tip1")}
    state = base_state(current_target=TARGET)

    update = prepare_worktree(state, ctx)

    assert ("branch_tip", (TARGET,)) in git.calls
    worktree_path = Path(ctx.settings.worktree_root) / f"{TARGET}-cycle-20260101"
    assert ("add_worktree", ("origin/" + TARGET, worktree_path)) in git.calls
    assert update["branch_results"][TARGET].worktree_path == str(worktree_path)
    assert update["branch_results"][TARGET].status == "PARTIAL"
    assert ctx.worktree_path == worktree_path
    assert ctx.worktree_gits[str(worktree_path)] is not None
    assert ctx.worktree_gits[str(worktree_path)].repo_path == worktree_path
    assert update["status"] == "PREPARED"


def test_prepare_worktree_reuses_existing_worktree(tmp_path):
    ctx = make_ctx(tmp_path)
    git = ctx.git
    git.tips = {TARGET: ("origin/" + TARGET, "tip1")}
    worktree_path = Path(ctx.settings.worktree_root) / f"{TARGET}-cycle-20260101"
    worktree_path.mkdir(parents=True)
    # 有效 worktree 需 .git 文件指向存在的 gitdir（否则 prepare 判残缺重建）
    gitdir = worktree_path / ".gitdir"
    gitdir.mkdir()
    (worktree_path / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    state = base_state(current_target=TARGET)

    update = prepare_worktree(state, ctx)

    assert not any(name == "add_worktree" for name, args in git.calls)
    assert update["branch_results"][TARGET].worktree_path == str(worktree_path)
    assert ctx.worktree_path == worktree_path


def test_prepare_worktree_preserves_existing_branch_progress(tmp_path):
    # resume 幂等：复用已建好的 worktree 时，不能清空已算好的 baseline/commits。
    ctx = make_ctx(tmp_path)
    git = ctx.git
    git.tips = {TARGET: ("origin/" + TARGET, "tip1")}
    worktree_path = Path(ctx.settings.worktree_root) / f"{TARGET}-cycle-20260101"
    worktree_path.mkdir(parents=True)
    gitdir = worktree_path / ".gitdir"
    gitdir.mkdir()
    (worktree_path / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    prior = branch_result(TARGET, commits=[commit_result("a1")])
    prior.worktree_path = str(worktree_path)
    prior.baseline = {"RTL9617C": failed_outcome()}
    state = base_state(current_target=TARGET, branch_results={TARGET: prior})

    update = prepare_worktree(state, ctx)

    kept = update["branch_results"][TARGET]
    assert kept.baseline is not None
    assert kept.commits == [commit_result("a1")]


def test_prepare_worktree_rebuilds_invalid_existing_worktree(tmp_path):
    # 残缺 worktree（.git 指向不存在的 gitdir）→ 删除重建（真机测试:
    # 残留目录复用后 cherry_pick 报 "not a git repository"）
    ctx = make_ctx(tmp_path)
    git = ctx.git
    git.tips = {TARGET: ("origin/" + TARGET, "tip1")}
    worktree_path = Path(ctx.settings.worktree_root) / f"{TARGET}-cycle-20260101"
    worktree_path.mkdir(parents=True)
    (worktree_path / ".git").write_text(
        "gitdir: /nonexistent/gitdir\n", encoding="utf-8"
    )
    state = base_state(current_target=TARGET)

    update = prepare_worktree(state, ctx)

    assert any(name == "add_worktree" for name, args in git.calls)
    assert not worktree_path.exists()
    assert update["branch_results"][TARGET].worktree_path == str(worktree_path)
    assert update["status"] == "PREPARED"


# --- cherry_pick ---


def test_cherry_pick_ok_records_commit_result(tmp_path):
    ctx = make_ctx(tmp_path)
    wg = FakeGit()
    wg.cherry_pick_result = CherryPickResult(status="OK")
    ctx.worktree_gits[str(Path("/wt"))] = wg
    state = base_state(
        current_target=TARGET,
        current_commit="a1",
        branch_results={TARGET: branch_result(TARGET)},
    )

    update = cherry_pick(state, ctx)

    assert ("cherry_pick", ("a1",)) in wg.calls
    assert update["branch_results"][TARGET].commits[0].cherry_pick == "OK"
    assert update["status"] == "CHERRY_PICK_OK"


def test_cherry_pick_conflict_records_conflict_files(tmp_path):
    ctx = make_ctx(tmp_path)
    wg = FakeGit()
    wg.cherry_pick_result = CherryPickResult(status="CONFLICT", conflict_files=["plat/demo.c"])
    ctx.worktree_gits[str(Path("/wt"))] = wg
    state = base_state(
        current_target=TARGET,
        current_commit="a1",
        branch_results={TARGET: branch_result(TARGET)},
    )

    update = cherry_pick(state, ctx)

    assert update["branch_results"][TARGET].commits[0].cherry_pick == "CONFLICT"
    assert update["status"] == "CHERRY_PICK_CONFLICT"


# --- resolve_conflict ---


def test_resolve_conflict_resolution_recorded(tmp_path):
    ctx = make_ctx(tmp_path)
    wg = FakeGit()
    wg.unmerged_files_result = ["plat/demo.c"]
    ctx.worktree_gits[str(Path("/wt"))] = wg
    resolution = ConflictResolution(files=["plat/demo.c"], diff="+fixed", agent_reason="merged")
    ctx.conflict_agent.resolution = resolution
    state = base_state(
        current_target=TARGET,
        current_commit="a1",
        detected_commits=[commit("a1")],
        branch_results={
            TARGET: branch_result(
                TARGET,
                commits=[commit_result("a1", cherry_pick_status="CONFLICT")],
            )
        },
    )

    update = resolve_conflict(state, ctx)

    assert ctx.conflict_agent.calls == [(commit("a1"), ["plat/demo.c"], wg, TARGET)]
    assert update["branch_results"][TARGET].commits[0].conflict_resolution == resolution
    assert update["branch_results"][TARGET].commits[0].cherry_pick == "OK"
    assert wg.cherry_pick_continue_calls == 1
    assert update["status"] == "RESOLVED"


def test_resolve_conflict_fail_fast(tmp_path):
    ctx = make_ctx(tmp_path)
    wg = FakeGit()
    wg.unmerged_files_result = ["plat/demo.c"]
    ctx.worktree_gits[str(Path("/wt"))] = wg
    ctx.conflict_agent.resolution = None
    state = base_state(
        current_target=TARGET,
        current_commit="a1",
        detected_commits=[commit("a1")],
        branch_results={
            TARGET: branch_result(
                TARGET,
                commits=[commit_result("a1", cherry_pick_status="CONFLICT")],
            )
        },
    )

    update = resolve_conflict(state, ctx)

    assert update["status"] == "RESOLUTION_FAILED"
    assert update["branch_results"][TARGET].commits[0].conflict_resolution is None
    assert wg.cherry_pick_continue_calls == 0


def test_resolve_conflict_records_last_reason_into_errors(tmp_path):
    # 非 UTF-8 冲突：ConflictAgent 返回 None 并记录 last_reason → 节点写入 state.errors
    ctx = make_ctx(tmp_path)
    wg = FakeGit()
    wg.unmerged_files_result = ["plat/demo.c"]
    ctx.worktree_gits[str(Path("/wt"))] = wg
    ctx.conflict_agent.resolution = None
    ctx.conflict_agent.last_reason = (
        "冲突文件 plat/demo.c 编码无法识别（非 UTF-8/GBK 文本），"
        "无法安全自动解决，转人工处理"
    )
    state = base_state(
        current_target=TARGET,
        current_commit="a1",
        detected_commits=[commit("a1")],
        branch_results={
            TARGET: branch_result(
                TARGET,
                commits=[commit_result("a1", cherry_pick_status="CONFLICT")],
            )
        },
    )

    update = resolve_conflict(state, ctx)

    assert update["status"] == "RESOLUTION_FAILED"
    assert "resolve_conflict" in update["errors"]
    assert "UTF-8" in update["errors"]["resolve_conflict"].error


# --- baseline_build ---


def test_baseline_build_success_records_baseline(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.runner = FakeRunner(success=True)
    ctx.worktree_path = Path("/wt")
    ctx.safety = FakeSafety(models=("RTL9617C",))
    state = base_state(
        current_target=TARGET,
        build_models={TARGET: ["RTL9617C"]},
        branch_results={TARGET: branch_result(TARGET)},
    )

    update = baseline_build(state, ctx)

    assert ctx.runner.build_calls[0]["clean"] is True
    assert ctx.runner.build_calls[0]["worktree"] == Path("/wt")
    assert ctx.runner.build_calls[0]["model"] == "RTL9617C"
    branch = update["branch_results"][TARGET]
    assert branch.baseline["RTL9617C"].status == "OK"
    assert update["status"] == "BASELINE_OK"


def test_baseline_build_failure_marks_branch_failed(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.runner = FakeRunner(success=False)
    ctx.worktree_path = Path("/wt")
    ctx.safety = FakeSafety(models=("RTL9617C",))
    state = base_state(
        current_target=TARGET,
        build_models={TARGET: ["RTL9617C"]},
        branch_results={TARGET: branch_result(TARGET)},
    )

    update = baseline_build(state, ctx)

    branch = update["branch_results"][TARGET]
    assert branch.status == "FAILED"
    assert branch.baseline["RTL9617C"].status == "FAILED"
    assert "baseline build failed" in (branch.stop_reason or "")
    assert update["status"] == "BASELINE_FAILED"


def test_baseline_build_idempotent_skips_rebuild(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.runner = FakeRunner(success=True)
    ctx.worktree_path = Path("/wt")
    ctx.safety = FakeSafety(models=("RTL9617C",))
    prior = branch_result(TARGET)
    prior.baseline = {"RTL9617C": failed_outcome()}
    state = base_state(
        current_target=TARGET,
        build_models={TARGET: ["RTL9617C"]},
        branch_results={TARGET: prior},
    )

    update = baseline_build(state, ctx)

    assert ctx.runner.build_calls == []
    assert update["status"] == "BASELINE_OK"


def test_baseline_build_multiple_models_serial(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.runner = FakeRunner(success=True)
    ctx.worktree_path = Path("/wt")
    ctx.safety = FakeSafety(models=("RTL9617C", "2600"))
    state = base_state(
        current_target=TARGET,
        build_models={TARGET: ["RTL9617C", "2600"]},
        branch_results={TARGET: branch_result(TARGET)},
    )

    update = baseline_build(state, ctx)

    assert [c["model"] for c in ctx.runner.build_calls] == ["RTL9617C", "2600"]
    branch = update["branch_results"][TARGET]
    assert branch.baseline["RTL9617C"].status == "OK"
    assert branch.baseline["2600"].status == "OK"


# --- build ---


def test_build_success_records_outcome_and_incremental(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.runner = FakeRunner(success=True)
    ctx.worktree_path = Path("/wt")
    state = base_state(
        current_target=TARGET,
        current_commit="a1",
        detected_commits=[commit("a1")],
        batches={TARGET: ["a1", "a2"]},
        branch_results={
            TARGET: branch_result(
                TARGET,
                commits=[commit_result("a1")],
            )
        },
    )

    update = build(state, ctx)

    call = ctx.runner.build_calls[0]
    # 基线编译已在 prepare 后全量验证，批次首个 commit 也走增量编译（决策 V2）。
    assert call["clean"] is False
    assert call["worktree"] == Path("/wt")
    assert call["model"] == "RTL9617C"
    assert call["log_path"] == (
        Path(ctx.settings.log_dir) / "cycle-20260101" / "build" / TARGET / "a1" / "build.log"
    )
    outcome = update["branch_results"][TARGET].commits[0].build["RTL9617C"]
    assert outcome.status == "OK"
    assert update["status"] == "BUILD_OK"


def test_build_public_file_triggers_clean(tmp_path):
    # 决策 2：改动公共文件（component/ 等）→ 全量编译降级，基线编译不豁免。
    ctx = make_ctx(tmp_path)
    ctx.runner = FakeRunner(success=True)
    ctx.worktree_path = Path("/wt")
    ctx.is_public_file = lambda path: True
    state = base_state(
        current_target=TARGET,
        current_commit="a1",
        detected_commits=[commit("a1", changed_files=["component/dhcp.c"])],
        batches={TARGET: ["a1"]},
        branch_results={
            TARGET: branch_result(
                TARGET,
                commits=[commit_result("a1")],
            )
        },
    )

    build(state, ctx)

    assert ctx.runner.build_calls[0]["clean"] is True


def test_build_incremental_when_not_first_in_batch(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.runner = FakeRunner(success=True)
    ctx.worktree_path = Path("/wt")
    ctx.is_public_file = lambda path: False
    state = base_state(
        current_target=TARGET,
        current_commit="a2",
        detected_commits=[commit("a2")],
        batches={TARGET: ["a1", "a2"]},
        branch_results={
            TARGET: branch_result(
                TARGET,
                commits=[commit_result("a2")],
            )
        },
    )

    build(state, ctx)

    assert ctx.runner.build_calls[0]["clean"] is False


def test_build_clean_for_public_file(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.runner = FakeRunner(success=True)
    ctx.worktree_path = Path("/wt")
    ctx.is_public_file = lambda path: path == "plat/public.c"
    state = base_state(
        current_target=TARGET,
        current_commit="a2",
        detected_commits=[commit("a2", changed_files=["plat/public.c"])],
        batches={TARGET: ["a1", "a2"]},
        branch_results={
            TARGET: branch_result(
                TARGET,
                commits=[commit_result("a2")],
            )
        },
    )

    build(state, ctx)

    assert ctx.runner.build_calls[0]["clean"] is True


def test_build_failed_status(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.runner = FakeRunner(success=False)
    ctx.worktree_path = Path("/wt")
    state = base_state(
        current_target=TARGET,
        current_commit="a1",
        detected_commits=[commit("a1")],
        batches={TARGET: ["a1"]},
        branch_results={
            TARGET: branch_result(
                TARGET,
                commits=[commit_result("a1")],
            )
        },
    )

    update = build(state, ctx)

    outcome = update["branch_results"][TARGET].commits[0].build["RTL9617C"]
    assert outcome.status == "FAILED"
    assert outcome.errors == ["compile error"]
    assert update["status"] == "BUILD_FAILED"


# --- fix_build ---


def test_fix_build_calls_agent_and_rebuilds(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.runner = FakeRunner(success=False)
    ctx.worktree_path = Path("/wt")
    wg = FakeGit()
    ctx.worktree_gits[str(Path("/wt"))] = wg
    state = base_state(
        current_target=TARGET,
        current_commit="a1",
        detected_commits=[commit("a1")],
        branch_results={
            TARGET: branch_result(
                TARGET,
                commits=[
                    CommitResult(
                        sha="a1",
                        cherry_pick="OK",
                        conflict_resolution=None,
                        build={"RTL9617C": failed_outcome()},
                    )
                ],
            )
        },
    )

    ctx.runner.success = True
    update = fix_build(state, ctx)

    assert ctx.build_agent.calls[0][0] == commit("a1")
    assert ctx.build_agent.calls[0][1] == ["compile error"]
    assert ctx.build_agent.calls[0][3] == wg
    assert ctx.build_agent.calls[0][4] == TARGET
    outcome = update["branch_results"][TARGET].commits[0].build["RTL9617C"]
    assert outcome.status == "OK"
    assert outcome.agent_attempts == 1
    assert update["status"] == "BUILD_OK"


def test_fix_build_persists_agent_fix_diff(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.runner = FakeRunner(success=False)
    ctx.worktree_path = Path("/wt")
    ctx.worktree_gits[str(Path("/wt"))] = FakeGit()
    from bsa.agents.base import BuildAttribution

    ctx.build_agent.attribution = BuildAttribution(
        category="introduced_by_commit", reason="fixed", files_to_fix=[], fix_diff="@@ -1 +1 @@"
    )
    state = base_state(
        current_target=TARGET,
        current_commit="a1",
        detected_commits=[commit("a1")],
        branch_results={
            TARGET: branch_result(
                TARGET,
                commits=[
                    CommitResult(
                        sha="a1",
                        cherry_pick="OK",
                        conflict_resolution=None,
                        build={"RTL9617C": failed_outcome()},
                    )
                ],
            )
        },
    )

    ctx.runner.success = True
    update = fix_build(state, ctx)

    outcome = update["branch_results"][TARGET].commits[0].build["RTL9617C"]
    assert outcome.fix_diff == "@@ -1 +1 @@"
    assert outcome.reason == "fixed"


def test_fix_build_still_failed_after_attempts(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.runner = FakeRunner(success=False)
    ctx.worktree_path = Path("/wt")
    state = base_state(
        current_target=TARGET,
        current_commit="a1",
        detected_commits=[commit("a1")],
        branch_results={
            TARGET: branch_result(
                TARGET,
                commits=[
                    CommitResult(
                        sha="a1",
                        cherry_pick="OK",
                        conflict_resolution=None,
                        build={"RTL9617C": failed_outcome()},
                    )
                ],
            )
        },
    )

    update = fix_build(state, ctx)

    outcome = update["branch_results"][TARGET].commits[0].build["RTL9617C"]
    assert outcome.status == "FAILED"
    assert outcome.agent_attempts == 1
    assert update["status"] == "BUILD_FAILED"


# --- generate_patch ---


def test_generate_patch_writes_patch_path(tmp_path):
    ctx = make_ctx(tmp_path)
    wg = FakeGit()
    wg.tips = {TARGET: ("origin/" + TARGET, "base-tip")}
    ctx.worktree_gits[str(Path("/wt"))] = wg
    state = base_state(
        current_target=TARGET,
        branch_results={
            TARGET: branch_result(
                TARGET,
                commits=[commit_result("a1")],
            )
        },
    )

    update = generate_patch(state, ctx)

    expected = (
        "format_patch",
        (
            "base-tip",
            "HEAD",
            Path(ctx.settings.log_dir) / "patch",
            f"cycle-20260101_{TARGET}.patch",
        ),
    )
    assert expected in wg.calls
    assert update["branch_results"][TARGET].patch_path is not None
    assert update["status"] == "PATCHED"


# --- worktree safety (no silent fallback to main repo) ---


def test_cherry_pick_without_prepared_worktree_raises(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.git.cherry_pick_result = CherryPickResult(status="OK")
    state = base_state(current_target=TARGET, current_commit="a1")

    with pytest.raises(InfrastructureError):
        cherry_pick(state, ctx)


def test_cherry_pick_without_prepared_worktree_node_wrapper_fails(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.git.cherry_pick_result = CherryPickResult(status="OK")
    state = base_state(current_target=TARGET, current_commit="a1")

    update = node_wrapper(cherry_pick, ctx=ctx)(state)

    assert update["status"] == "FAILED"
    assert "cherry_pick" in update["errors"]
    assert "worktree" in update["errors"]["cherry_pick"].error.lower()


def test_build_without_prepared_worktree_raises(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.worktree_path = None
    state = base_state(
        current_target=TARGET,
        current_commit="a1",
        detected_commits=[commit("a1")],
        batches={TARGET: ["a1"]},
        branch_results={
            TARGET: BranchResult(
                target_branch=TARGET,
                worktree_path="",
                status="PARTIAL",
                commits=[commit_result("a1")],
                patch_path=None,
                stop_reason=None,
            )
        },
    )

    with pytest.raises(InfrastructureError):
        build(state, ctx)


def test_generate_patch_without_prepared_worktree_raises(tmp_path):
    ctx = make_ctx(tmp_path)
    state = base_state(current_target=TARGET)

    with pytest.raises(InfrastructureError):
        generate_patch(state, ctx)


def test_resolve_conflict_without_prepared_worktree_raises(tmp_path):
    ctx = make_ctx(tmp_path)
    state = base_state(
        current_target=TARGET,
        current_commit="a1",
        detected_commits=[commit("a1")],
        branch_results={
            TARGET: BranchResult(
                target_branch=TARGET,
                worktree_path="",
                status="PARTIAL",
                commits=[commit_result("a1", cherry_pick_status="CONFLICT")],
                patch_path=None,
                stop_reason=None,
            )
        },
    )

    with pytest.raises(InfrastructureError):
        resolve_conflict(state, ctx)


# --- report ---


def test_report_assembles_basic_report(tmp_path):
    ctx = make_ctx(tmp_path)
    state = base_state(
        status="DECIDED",
        detected_commits=[commit("a1")],
        branch_results={TARGET: branch_result(TARGET)},
        decisions={
            "a1": {TARGET: Conclusion4(kind="ManualReview", evidence=["z"], confidence="low")}
        },
    )

    update = report(state, ctx)

    rep = update["report"]
    assert rep.cycle_id == "cycle-20260101"
    assert rep.html_path == Path(ctx.settings.log_dir) / "cycle-20260101" / "report.html"
    decisions_path = Path(ctx.settings.log_dir) / "cycle-20260101" / "decisions.json"
    assert rep.decisions_json_path == decisions_path
    assert rep.summary["commits_detected"] == 1
    assert len(rep.action_required) == 1
    assert rep.action_required[0]["sha"] == "a1"
    assert update["status"] == "PARTIAL"


def test_action_required_marks_stale_override(tmp_path):
    """覆盖已因内容变化失效（fingerprint 不匹配）→ review_item 附带 override_stale。"""
    ctx = make_ctx(tmp_path)
    from bsa.rules.classify import compute_fingerprint

    sha = "a1"
    c = commit("a1", message="[BUG] fix null deref\n\nrefactor it", patch_id="pid-a1")
    # 覆盖写入时基于旧内容（不同的 message → 不同 fingerprint）
    stale_fp = compute_fingerprint("[BUG] fix null deref OLD\n\nrefactor it", "pid-a1")
    log_dir = Path(ctx.settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "judgments.json").write_text(
        json.dumps(
            {
                sha: {
                    "is_bug_fix": False,
                    "recognition_source": "manual-override",
                    "reason": "人工判定",
                },
                f"fp:{stale_fp}": {
                    "is_bug_fix": False,
                    "recognition_source": "manual-override",
                    "reason": "人工判定",
                },
            }
        ),
        encoding="utf-8",
    )
    state = base_state(
        detected_commits=[c],
        decisions={
            sha: {TARGET: Conclusion4(kind="ManualReview", evidence=["z"], confidence="low")}
        },
    )

    actions = _action_required(state, log_dir=ctx.settings.log_dir)

    assert actions[0]["sha"] == sha
    assert actions[0]["override_stale"] is True


def test_action_required_no_stale_when_override_matches(tmp_path):
    """覆盖 fingerprint 仍匹配 → 不标失效。"""
    ctx = make_ctx(tmp_path)
    from bsa.rules.classify import compute_fingerprint

    sha = "a1"
    c = commit("a1", message="[BUG] fix null deref\n\nrefactor it", patch_id="pid-a1")
    fp = compute_fingerprint(c.message, c.patch_id)
    log_dir = Path(ctx.settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "judgments.json").write_text(
        json.dumps(
            {
                sha: {
                    "is_bug_fix": False,
                    "recognition_source": "manual-override",
                    "reason": "人工判定",
                },
                f"fp:{fp}": {
                    "is_bug_fix": False,
                    "recognition_source": "manual-override",
                    "reason": "人工判定",
                },
            }
        ),
        encoding="utf-8",
    )
    state = base_state(
        detected_commits=[c],
        decisions={
            sha: {TARGET: Conclusion4(kind="ManualReview", evidence=["z"], confidence="low")}
        },
    )

    actions = _action_required(state, log_dir=ctx.settings.log_dir)

    assert actions[0]["override_stale"] is False


def test_report_includes_errors_in_action_required(tmp_path):
    ctx = make_ctx(tmp_path)
    state = base_state(
        errors={"detect_commits": ErrorRecord(node="detect_commits", error="boom", ts="t")}
    )
    update = report(state, ctx)
    assert update["report"].action_required[0]["node"] == "detect_commits"


# --- report: 周期终态可区分（P0-2/G10） ---


def test_report_terminal_status_all_success(tmp_path):
    ctx = make_ctx(tmp_path)
    state = base_state(
        branch_results={
            TARGET: BranchResult(
                target_branch=TARGET,
                worktree_path="/wt",
                status="SUCCESS",
                commits=[commit_result("a1")],
                patch_path=None,
                stop_reason=None,
            )
        }
    )
    update = report(state, ctx)
    assert update["status"] == "SUCCESS"


def test_report_terminal_status_partial(tmp_path):
    ctx = make_ctx(tmp_path)
    state = base_state(branch_results={TARGET: branch_result(TARGET)})
    update = report(state, ctx)
    assert update["status"] == "PARTIAL"


def test_report_terminal_status_failed_on_branch_failure(tmp_path):
    ctx = make_ctx(tmp_path)
    state = base_state(
        branch_results={
            TARGET: BranchResult(
                target_branch=TARGET,
                worktree_path="/wt",
                status="FAILED",
                commits=[commit_result("a1", cherry_pick_status="FAILED")],
                patch_path=None,
                stop_reason=None,
            )
        }
    )
    update = report(state, ctx)
    assert update["status"] == "FAILED"


def test_report_terminal_status_failed_on_node_error(tmp_path):
    ctx = make_ctx(tmp_path)
    state = base_state(
        errors={"detect_commits": ErrorRecord(node="detect_commits", error="boom", ts="t")}
    )
    update = report(state, ctx)
    assert update["status"] == "FAILED"


# --- GraphContext 注入 decision_rules.conclude 目标类型（P2 治理） ---


def test_graph_context_injects_conclude_target_types(tmp_path):
    from bsa.rules.conclude import TargetSnapshot, conclude_pair

    settings = make_settings(tmp_path)
    ctx = GraphContext(
        settings=settings,
        executor=SimpleNamespace(),
        git=FakeGit(),
        runner=FakeRunner(),
        sync_decision_agent=FakeSyncDecisionAgent(),
        conflict_agent=FakeConflictAgent(),
        build_agent=FakeBuildAgent(),
        safety=FakeSafety(),
        decision_rules=DecisionRules(
            classify={},
            conclude=ConcludeThresholds(
                need_sync_target_types=["release"],
                ineligible_target_types=["feature", "personal"],
            ),
            branch_mapping={},
        ),
    )
    # 未显式传 conclude → __post_init__ 绑定为注入目标类型的 partial，不再是裸函数。
    assert ctx.conclude is not conclude_pair

    analysis = _to_analysis(
        commit("a1"),
        SyncDecision(
            sha="a1",
            is_bug_fix=True,
            reason=None,
            recognition_source="machine:[BUG]",
            needs_agent=False,
        ),
        {},
    )
    snapshot = TargetSnapshot(
        branch_name="br_v4_LineA_develop_b_20260101",
        branch_type="develop",
        fix_clearly_missing=True,
        files_on_target={"plat/demo.c"},
        symbols_on_target={},
    )
    conclusion = ctx.conclude(analysis, snapshot, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "OutOfScope"
