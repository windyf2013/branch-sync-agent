"""邮件正文 HTML 摘要。

需求 3「核心信息直接在邮件内容中显示」的落点：收件人不点开附件就能判断本周期
成没成、哪些分支同步了、哪里停批。

邮件客户端的硬约束比网页严得多（Gmail 第三方账户剥样式块、Outlook 桌面不认 CSS
自定义属性与伪元素），所以这里既测内容，也测「没有用到那些会失效的特性」。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bsa.domain.models import (
    BranchResult,
    BuildOutcome,
    CommitInfo,
    CommitResult,
    Conclusion4,
    Report,
)
from bsa.report.renderer import _EMAIL_FAILURE_LIMIT, build_email_html
from tests.test_graph_nodes import TARGET, base_state

SRC = "br_v4.34_MSG_develop_20260805"


def _report(tmp_path: Path) -> Report:
    return Report(
        cycle_id="cycle-2026-08-20",
        html_path=tmp_path / "report.html",
        summary={"status": "PARTIAL"},
        action_required=[],
        decisions_json_path=tmp_path / "decisions.json",
    )


def _commit(sha: str, *, build: dict | None = None, cherry_pick: str = "OK") -> CommitResult:
    return CommitResult(
        sha=sha, cherry_pick=cherry_pick, conflict_resolution=None, build=build or {}
    )


def _branch(status: str, commits: list[CommitResult], patch: str | None = None) -> BranchResult:
    return BranchResult(
        target_branch=TARGET,
        worktree_path="/wt",
        status=status,
        commits=commits,
        patch_path=patch,
        stop_reason=None,
    )


def _rich_state() -> dict:
    """一个含拓扑/失败/成功的典型状态。"""
    return base_state(
        status="PARTIAL",
        sources=[SRC],
        targets=[TARGET],
        topology=[{"section": "4.34", "sources": [SRC], "targets": [TARGET]}],
        detected_commits=[
            CommitInfo(
                sha="a5171848deadbeef",
                message="fix: 修复 PPPoE 会话泄漏\n\n正文第二行不该出现在摘要里",
                author="yuanhuaili",
                committed_at="2026-09-09T10:00:00+08:00",
                changed_files=["plat/demo.c"],
                patch_text="+x",
                symbols=[],
                patch_id="pid-1",
                issue_ids=[],
                source_branch=SRC,
                homologous_section="4.34",
            )
        ],
        decisions={
            "a5171848deadbeef": {
                TARGET: Conclusion4(kind="NeedSync", evidence=[], confidence="high")
            }
        },
        branch_results={
            TARGET: _branch(
                "PARTIAL",
                [
                    _commit("aaaa1111", cherry_pick="CONFLICT"),
                    _commit(
                        "bbbb2222",
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
        },
    )


class TestCoreFacts:
    def test_contains_cycle_status_window_and_counts(self, tmp_path):
        html = build_email_html(_rich_state(), _report(tmp_path))

        assert "cycle-2026-08-20" in html
        assert "未完成 (PARTIAL)" in html
        assert "2026-01-01T22:00:00+08:00" in html  # 扫描窗口
        assert "检测 commit" in html

    def test_shows_source_to_target_pairing(self, tmp_path):
        # 需求 2：邮件里也要一眼看出源目分支。
        html = build_email_html(_rich_state(), _report(tmp_path))

        assert "同步拓扑" in html
        assert SRC in html and TARGET in html

    def test_branch_card_lists_origin_branch(self, tmp_path):
        html = build_email_html(_rich_state(), _report(tmp_path))

        assert "来源：" in html and SRC in html

    def test_lists_failures_with_sha_model_and_error(self, tmp_path):
        html = build_email_html(_rich_state(), _report(tmp_path))

        assert "aaaa1111" in html and "冲突 (CONFLICT)" in html
        assert "bbbb2222" in html and "RTL9617C" in html
        assert "compile error: boom" in html

    def test_points_at_the_attachment(self, tmp_path):
        assert "report.html" in build_email_html(_rich_state(), _report(tmp_path))

    def test_subject_used_when_given(self, tmp_path):
        html = build_email_html(_rich_state(), _report(tmp_path), "自定义主题")

        assert "自定义主题" in html


class TestEmailClientConstraints:
    """这些特性在 Gmail/Outlook 上会静默失效 —— 摘要会变成无样式的纯文本。"""

    def test_no_style_block(self, tmp_path):
        assert "<style" not in build_email_html(_rich_state(), _report(tmp_path))

    def test_no_css_custom_properties(self, tmp_path):
        assert "var(--" not in build_email_html(_rich_state(), _report(tmp_path))

    def test_no_flex_or_grid(self, tmp_path):
        html = build_email_html(_rich_state(), _report(tmp_path))

        assert "display:flex" not in html and "display:grid" not in html

    def test_uses_presentation_tables(self, tmp_path):
        assert 'role="presentation"' in build_email_html(_rich_state(), _report(tmp_path))

    def test_no_external_resources(self, tmp_path):
        html = build_email_html(_rich_state(), _report(tmp_path))

        assert "<link" not in html and "url(http" not in html

    def test_all_tokens_replaced(self, tmp_path):
        html = build_email_html(_rich_state(), _report(tmp_path))

        assert "{{" not in html and "}}" not in html

    def test_badge_dot_is_literal_span_not_pseudo_element(self, tmp_path):
        # 平台徽章靠 ::before 画圆点；邮件里必须换成字面 span。
        html = build_email_html(_rich_state(), _report(tmp_path))

        assert "::before" not in html
        assert "&#9679;" in html


class TestSafetyAndDegradation:
    def test_escapes_compiler_error_html(self, tmp_path):
        # 编译器输出会原样进邮件，是真实存在的注入面（工程文件名可含尖括号）。
        state = _rich_state()
        state["branch_results"] = {
            TARGET: _branch(
                "FAILED",
                [
                    _commit(
                        "bbbb2222",
                        build={
                            "RTL9617C": BuildOutcome(
                                model="RTL9617C",
                                status="FAILED",
                                log_path="/l",
                                errors=["<script>alert(1)</script> 注入"],
                                agent_attempts=0,
                                fix_diff=None,
                            )
                        },
                    )
                ],
            )
        }

        html = build_email_html(state, _report(tmp_path))

        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_escapes_malicious_stop_reason(self, tmp_path):
        branch = _branch("FAILED", []).model_copy(
            update={"stop_reason": "<img src=x onerror=alert(1)>"}
        )
        state = _rich_state()
        state["branch_results"] = {TARGET: branch}

        html = build_email_html(state, _report(tmp_path))

        assert "<img src=x" not in html
        assert "&lt;img" in html

    def test_caps_failure_lines_and_says_so(self, tmp_path):
        # 静默截断会让收件人以为已经看全 —— 超出必须明说。
        overflow = 5
        commits = [
            _commit(f"{i:08d}", cherry_pick="CONFLICT")
            for i in range(_EMAIL_FAILURE_LIMIT + overflow)
        ]
        state = _rich_state()
        state["branch_results"] = {TARGET: _branch("FAILED", commits)}

        html = build_email_html(state, _report(tmp_path))

        # 行数 = 1 条分支头 + 每条 commit 一行；超出部分按实际差额告知。
        assert f"还有 {overflow + 1} 条" in html
        assert html.count("cherry-pick 冲突") <= _EMAIL_FAILURE_LIMIT

    def test_empty_state_is_stable(self, tmp_path):
        html = build_email_html(base_state(status="SUCCESS"), _report(tmp_path))

        assert "cycle-2026-08-20" in html  # 报告周期号来自 report，不是 state

    def test_survives_missing_topology(self, tmp_path):
        # 老周期 / 手动链路无 topology → 退回扁平清单，不崩、不伪造配对。
        state = base_state(status="SUCCESS", sources=[SRC], targets=[TARGET])

        html = build_email_html(state, _report(tmp_path))

        assert SRC in html and TARGET in html
        assert "同步拓扑（业务分支 → 主分支）" not in html

    def test_no_crash_when_topology_and_lists_both_absent(self, tmp_path):
        html = build_email_html(base_state(status="SUCCESS"), _report(tmp_path))

        assert "同步拓扑" not in html


class TestTextFallbackUnchanged:
    """纯文本正文继续独立完整（无 HTML 的客户端读它）。"""

    def test_build_email_body_still_has_core_facts(self, tmp_path):
        from bsa.report.renderer import build_email_body

        body = build_email_body(_report(tmp_path), _rich_state())

        assert "cycle-2026-08-20" in body
        assert "未完成 (PARTIAL)" in body
        assert "同步拓扑:" in body
        assert SRC in body and TARGET in body

    def test_build_email_body_lists_failures(self, tmp_path):
        from bsa.report.renderer import build_email_body

        body = build_email_body(_report(tmp_path), _rich_state())

        assert "失败/停批明细" in body
        assert "compile error: boom" in body


@pytest.mark.parametrize("status", ["SUCCESS", "PARTIAL", "FAILED", "MANUAL"])
def test_every_branch_status_renders(tmp_path, status):
    state = _rich_state()
    state["branch_results"] = {TARGET: _branch(status, [])}

    html = build_email_html(state, _report(tmp_path))

    assert TARGET in html
