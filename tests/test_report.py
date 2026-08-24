from __future__ import annotations

from pathlib import Path

from bsa.domain.models import (
    BranchResult,
    BuildOutcome,
    CommitResult,
    ConflictResolution,
    Report,
)
from bsa.report.renderer import render_html_report, write_agent_diffs
from tests.test_graph_nodes import TARGET, base_state


def _report(tmp_path: Path) -> Report:
    return Report(
        cycle_id="cycle-2026-08-20",
        html_path=tmp_path / "report.html",
        summary={"status": "REPORTED", "commits_detected": 1},
        action_required=[],
        decisions_json_path=tmp_path / "decisions.json",
    )


def _branch(
    status: str, *, commits: list[CommitResult], patch_path: str | None = None
) -> BranchResult:
    return BranchResult(
        target_branch=TARGET,
        worktree_path=str(Path("/wt")),
        status=status,
        commits=commits,
        patch_path=patch_path,
        stop_reason=None,
    )


class TestFailedBranchDetail:
    def test_build_failure_shows_stage_attempts_recommendation(self, tmp_path):
        state = base_state()
        state["branch_results"] = {
            TARGET: _branch(
                "FAILED",
                commits=[
                    CommitResult(
                        sha="abc123",
                        cherry_pick="OK",
                        conflict_resolution=None,
                        build={
                            "RTL9617C": BuildOutcome(
                                model="RTL9617C",
                                status="FAILED",
                                log_path="/l",
                                errors=["boom"],
                                agent_attempts=3,
                                fix_diff=None,
                            )
                        },
                    )
                ],
            )
        }
        html = render_html_report(state, _report(tmp_path)).read_text(encoding="utf-8")
        assert "失败阶段" in html
        assert "build" in html
        assert "Agent 尝试次数" in html
        assert ">3</span>" in html
        assert "建议" in html
        assert "停批，人工审核" in html

    def test_conflict_failure_shows_conflict_stage(self, tmp_path):
        state = base_state()
        state["branch_results"] = {
            TARGET: _branch(
                "FAILED",
                commits=[
                    CommitResult(
                        sha="abc123",
                        cherry_pick="CONFLICT",
                        conflict_resolution=None,
                        build={},
                    )
                ],
            )
        }
        html = render_html_report(state, _report(tmp_path)).read_text(encoding="utf-8")
        assert "失败阶段" in html
        assert "conflict" in html
        assert "人工解决冲突" in html

    def test_cherry_pick_failure_stage(self, tmp_path):
        state = base_state()
        state["branch_results"] = {
            TARGET: _branch(
                "FAILED",
                commits=[
                    CommitResult(
                        sha="abc123",
                        cherry_pick="FAILED",
                        conflict_resolution=None,
                        build={},
                    )
                ],
            )
        }
        html = render_html_report(state, _report(tmp_path)).read_text(encoding="utf-8")
        assert "cherry_pick" in html

    def test_success_shows_push_command(self, tmp_path):
        patch = tmp_path / "p.patch"
        state = base_state()
        state["branch_results"] = {
            TARGET: _branch(
                "SUCCESS",
                patch_path=str(patch),
                commits=[
                    CommitResult(
                        sha="abc123",
                        cherry_pick="OK",
                        conflict_resolution=None,
                        build={
                            "RTL9617C": BuildOutcome(
                                model="RTL9617C",
                                status="OK",
                                log_path="/l",
                                errors=[],
                                agent_attempts=0,
                                fix_diff=None,
                            )
                        },
                    )
                ],
            )
        }
        html = render_html_report(state, _report(tmp_path)).read_text(encoding="utf-8")
        assert f"git checkout {TARGET} && git am {patch}" in html


class TestWriteAgentDiffs:
    def test_persists_conflict_and_fix_diffs(self, tmp_path):
        conflict = ConflictResolution(
            files=["src/foo.c"],
            diff="diff --git a/src/foo.c b/src/foo.c\n@@ -1 +1 @@\n-x\n+y\n",
            agent_reason="保留双方改动",
        )
        state = base_state()
        state["branch_results"] = {
            TARGET: _branch(
                "PARTIAL",
                commits=[
                    CommitResult(
                        sha="abc123",
                        cherry_pick="OK",
                        conflict_resolution=conflict,
                        build={
                            "RTL9617C": BuildOutcome(
                                model="RTL9617C",
                                status="OK",
                                log_path="/l",
                                errors=[],
                                agent_attempts=1,
                                fix_diff=(
                                    "diff --git a/src/bar.c b/src/bar.c\n"
                                    "@@ -1 +1 @@\n-a\n+b\n"
                                ),
                            )
                        },
                    )
                ],
            )
        }
        written = write_agent_diffs(state, _report(tmp_path))
        audit = tmp_path / "audit" / TARGET
        assert written == [audit / "abc123_conflict.diff", audit / "abc123_RTL9617C_fix.diff"]
        assert (audit / "abc123_conflict.diff").read_text(encoding="utf-8") == conflict.diff
        assert (audit / "abc123_RTL9617C_fix.diff").read_text(encoding="utf-8") == (
            "diff --git a/src/bar.c b/src/bar.c\n@@ -1 +1 @@\n-a\n+b\n"
        )

    def test_skips_when_no_diffs(self, tmp_path):
        assert write_agent_diffs(base_state(), _report(tmp_path)) == []
