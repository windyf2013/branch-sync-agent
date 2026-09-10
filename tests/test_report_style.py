"""报告视觉与平台的同源性守卫。

报告是同一系统的交付件，不该有第二套视觉语言。这些断言防的是「有人把配色改回去」
与「有人给报告加了外链样式」—— 后者会同时打破离线单文件打开与邮件附件两个场景。
"""

from __future__ import annotations

import pytest

from bsa.domain.models import BranchResult, Report
from bsa.report.renderer import render_html_report
from tests.test_graph_nodes import TARGET, base_state


def _report(tmp_path):
    return Report(
        cycle_id="cycle-2026-08-20",
        html_path=tmp_path / "report.html",
        summary={"status": "REPORTED"},
        action_required=[],
        decisions_json_path=tmp_path / "decisions.json",
    )


@pytest.fixture
def html(tmp_path) -> str:
    return render_html_report(base_state(status="COMPLETED"), _report(tmp_path)).read_text(
        encoding="utf-8"
    )


class TestTokensMatchWeb:
    """令牌取值必须与 src/bsa_web/static/style.css 的 :root 一致。"""

    @pytest.mark.parametrize(
        ("token", "value"),
        [
            ("--brand", "#2f6bfb"),
            ("--brand-2", "#4f8bff"),
            ("--brand-deep", "#1d4ed8"),
            ("--bg", "#f5f7fa"),
            ("--surface-2", "#f7f9fc"),
            ("--line", "#e9edf4"),
            ("--line-strong", "#d8e0ea"),
            ("--ok", "#16a34a"),
            ("--need", "#dc2626"),
            ("--review", "#d97706"),
            ("--pending", "#0284c7"),
            ("--radius", "12px"),
            ("--radius-lg", "14px"),
            ("--radius-pill", "999px"),
        ],
    )
    def test_token_has_web_value(self, html, token, value):
        assert f"{token}: {value};" in html

    def test_old_dark_teal_palette_is_gone(self, html):
        # 改造前的深藏青品牌色：留着它就意味着两套视觉语言又回来了。
        assert "#0b3d5c" not in html
        assert "#0e7490" not in html


class TestOfflineConstraints:
    def test_no_external_stylesheet(self, html):
        # 硬约束：报告要能离线单文件打开，并作为邮件附件。
        assert "<link" not in html
        assert "@import" not in html
        assert "url(http" not in html

    def test_all_template_tokens_replaced(self, html):
        assert "{{" not in html and "}}" not in html

    def test_has_inline_style_block(self, html):
        assert "<style>" in html


class TestWebShapedComponents:
    def test_badge_is_pill_with_dot(self, html):
        # 平台 .badge 的关键观感：pill 圆角 + ::before 圆点。
        assert "border-radius: var(--radius-pill);" in html
        assert ".badge::before" in html

    def test_kpi_grid_is_single_panel(self, html):
        # 平台 .kpi-grid 是一整块面板（卡片间竖线），不是独立小卡。
        assert "border-left: 1px solid var(--line);" in html

    def test_phase_bar_present(self, html):
        # 平台 .period-bar-prominent 的淡蓝渐变 + 品牌边框。
        assert "linear-gradient(135deg, #f2f7ff 0%, #eaf2ff 100%)" in html
        assert "var(--brand-line)" in html


class TestChineseLabelsInReport:
    def test_cycle_status_is_chinese_with_english_subscript(self, html):
        assert "已完成 (COMPLETED)" in html

    def test_failure_stage_keeps_machine_token(self, html):
        # 中文主 + 英文副标：报告页脚已写明「保留英文枚举便于自动化对接」，
        # 且这是 test_report.py 里 "build"/"cherry_pick" 断言得以不改的原因。
        from bsa.domain.labels import FAILURE_STAGE_ZH

        assert FAILURE_STAGE_ZH["build"] == "编译失败 (build)"
        assert FAILURE_STAGE_ZH["cherry_pick"] == "cherry-pick 未完成 (cherry_pick)"


class TestTopologyRendering:
    def test_pairs_are_rendered_when_topology_present(self, tmp_path):
        state = base_state(
            topology=[
                {"section": "4.34", "sources": ["br_src_a"], "targets": [TARGET]},
            ],
            # 目标卡片（「来源分支」字段所在）只对有执行结果的分支渲染。
            branch_results={
                TARGET: BranchResult(
                    target_branch=TARGET,
                    worktree_path="/wt",
                    status="SUCCESS",
                    commits=[],
                    patch_path=None,
                    stop_reason=None,
                )
            },
        )

        html = render_html_report(state, _report(tmp_path)).read_text(encoding="utf-8")

        assert "同步拓扑（业务分支 → 主分支）" in html
        # 源与目标必须同现，且源分支要出现在目标卡片的来源分支字段里 —— 这正是
        # 「报告看不出源目分支」的正面解法。
        assert "br_src_a" in html and TARGET in html
        assert "来源分支" in html

    def test_degrades_to_flat_lists_without_topology(self, tmp_path):
        # 旧周期无 topology → 退回扁平清单，不崩、也不伪造配对。
        state = base_state(status="COMPLETED", sources=["br_x"], targets=[TARGET])

        html = render_html_report(state, _report(tmp_path)).read_text(encoding="utf-8")

        assert "同步拓扑" in html
        assert "业务分支 → 主分支" not in html
        assert "br_x" in html
