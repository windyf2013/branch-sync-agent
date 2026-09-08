from __future__ import annotations

from pathlib import Path

from bsa.domain.models import (
    BranchResult,
    BuildOutcome,
    CommitResult,
    ConflictResolution,
    Report,
)
from bsa.report.renderer import build_email_body, render_html_report, write_agent_diffs
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


class TestEmailBodyFailureDetail:
    """cron 精简「失败不重试直接发邮件」：邮件正文应含停批分支/commit/型号明细。"""

    def _state_with_failures(self) -> dict:
        state = base_state()
        state["branch_results"] = {
            TARGET: _branch(
                "FAILED",
                commits=[
                    # 冲突停批的 commit
                    CommitResult(
                        sha="aaaa1111",
                        cherry_pick="CONFLICT",
                        conflict_resolution=None,
                        build={},
                    ),
                    # 编译失败型号的 commit
                    CommitResult(
                        sha="bbbb2222",
                        cherry_pick="OK",
                        conflict_resolution=None,
                        build={
                            "RTL9617C": BuildOutcome(
                                model="RTL9617C",
                                status="FAILED",
                                log_path="/l",
                                errors=["compile error: boom"],
                                agent_attempts=0,
                                fix_diff=None,
                            )
                        },
                    ),
                ],
            )
        }
        return state

    def test_failed_branch_lines_list_conflict_and_build(self, tmp_path):
        from bsa.report.renderer import _branch_failure_lines

        lines = _branch_failure_lines(self._state_with_failures())
        joined = "\n".join(lines)
        assert any("aaaa1111" in line and "cherry-pick CONFLICT" in line for line in lines)
        assert any("bbbb2222" in line and "RTL9617C" in line and "编译失败" in line for line in lines)
        assert "compile error: boom" in joined

    def test_build_email_body_includes_failure_detail(self, tmp_path):
        body = build_email_body(_report(tmp_path), self._state_with_failures())
        assert "失败/停批明细" in body
        assert "aaaa1111" in body
        assert "bbbb2222" in body
        assert "cherry-pick CONFLICT" in body
        assert "RTL9617C" in body

    def test_success_branch_omits_failure_section(self, tmp_path):
        state = base_state()
        state["branch_results"] = {
            TARGET: _branch(
                "SUCCESS",
                commits=[
                    CommitResult(
                        sha="cccc3333",
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
        body = build_email_body(_report(tmp_path), state)
        assert "失败/停批明细" not in body
        assert "cccc3333" not in body
