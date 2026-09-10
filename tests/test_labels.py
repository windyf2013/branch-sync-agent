"""中文文案映射的单一事实源。

这些映射原先散在 `bsa_web/app.py`（状态/四态）与 `bsa_web/failure.py`（节点名/
stop_reason）。报告侧要显示中文，而 `bsa` 不能 import `bsa_web`（分层方向是
bsa_web → bsa），所以收敛到 `bsa.domain.labels`，web 侧原地 re-export。

本文件锁三件事：映射行为逐字不变、报告超集真的是超集、re-export 是同一对象而非复制。
"""

from __future__ import annotations

import pytest

from bsa.domain import labels


class TestStatusLabels:
    def test_known_status_maps_to_chinese(self):
        assert labels.status_label("SUCCESS") == "成功"
        assert labels.status_label("FAILED") == "失败"
        assert labels.status_label("PARTIAL") == "未完成"

    def test_unknown_status_passes_through(self):
        # 兜底绝不吞信息：未来新增枚举在报告中应原样露出，而不是变成空白。
        assert labels.status_label("WHATEVER") == "WHATEVER"

    def test_report_only_statuses_have_labels(self):
        # 报告要展示周期终态与节点态，web 的 _STATUS_ZH 里没有这些键。
        assert labels.report_status_label("COMPLETED") == "已完成"
        assert labels.report_status_label("DETECTED") == "已检测"

    def test_report_table_is_superset_of_web_table(self):
        # 单一事实源的价值在于「同一枚举值两边取值一致」。派生关系是强制的：
        # REPORT_STATUS_ZH 由 STATUS_ZH 展开而来，这里锁住它没被改成独立字面量。
        assert set(labels.STATUS_ZH) <= set(labels.REPORT_STATUS_ZH)
        for key, value in labels.STATUS_ZH.items():
            assert labels.REPORT_STATUS_ZH[key] == value

    def test_web_table_not_polluted_by_report_keys(self):
        # 把报告专用枚举并进 STATUS_ZH 会静默改变 web 的 status_zh 显示输出
        # （6+ 个消费点）。这里锁住它没被顺手合并。
        assert "COMPLETED" not in labels.STATUS_ZH
        assert "DETECTED" not in labels.STATUS_ZH


class TestKindLabels:
    def test_four_state_maps(self):
        assert labels.kind_label("NeedSync") == "待同步"
        assert labels.kind_label("AlreadyIncluded") == "已包含"
        assert labels.kind_label("ManualReview") == "待人工"
        assert labels.kind_label("OutOfScope") == "不适用"

    def test_unknown_kind_passes_through(self):
        assert labels.kind_label("Nope") == "Nope"


class TestNodeLabels:
    def test_known_node_maps(self):
        assert labels.node_label("detect_commits") == "代码迁出"
        assert labels.node_label("baseline_build") == "基线编译"

    @pytest.mark.parametrize(("value", "expected"), [(None, ""), ("", ""), ("weird", "weird")])
    def test_unknown_or_empty_passes_through(self, value, expected):
        assert labels.node_label(value) == expected


class TestHumanizeStopReason:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (
                "baseline build failed on RTL9617C",
                "型号 RTL9617C 基线编译失败",
            ),
            (
                "cherry-pick failed on abc1234",
                "提交 abc1234 同步中断（cherry-pick 未能完成）",
            ),
            (
                "fail-fast: abc1234 failed; subsequent commits judged related",
                "abc1234 失败，后续关联提交已一并停批",
            ),
            ("fail-fast: abc1234 failed", "abc1234 失败"),
        ],
    )
    def test_engine_written_reasons_are_humanized(self, raw, expected):
        assert labels.humanize_stop_reason(raw) == expected

    def test_unknown_reason_passes_through_verbatim(self):
        assert labels.humanize_stop_reason("something new") == "something new"

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_empty_is_returned_as_is(self, value):
        assert labels.humanize_stop_reason(value) == value


class TestWebReexports:
    """re-export 必须是同一对象 —— 若被改成复制，两处映射就会各自漂移。"""

    def test_failure_module_reexports_same_objects(self):
        from bsa_web import failure

        assert failure.node_label is labels.node_label
        assert failure.humanize_stop_reason is labels.humanize_stop_reason

    def test_app_status_tables_alias_labels(self):
        from bsa_web import app

        assert app._STATUS_ZH is labels.STATUS_ZH
        assert app._FOUR_STATE_ZH is labels.FOUR_STATE_ZH
