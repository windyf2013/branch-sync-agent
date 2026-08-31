"""failure 归因提取纯函数单测：覆盖各种真实 state.json 数据形状。"""

from __future__ import annotations

from bsa_web.failure import decision_breakdown, failure_summary, failure_text


def test_node_failure_action_required():
    payload = {
        "branch_results": {},
        "action_required": [
            {"node": "branch_matrix", "error": "未解析出任何同步边"},
        ],
    }
    assert failure_summary(payload) == ["branch_matrix 节点错误：未解析出任何同步边"]


def test_manual_review_uses_evidence_not_reason():
    # 真实数据里 ManualReview 只有 evidence，没有 reason
    payload = {
        "branch_results": {},
        "action_required": [
            {
                "sha": "a1",
                "branch": "t",
                "kind": "ManualReview",
                "evidence": ["函数/符号疑似已重命名"],
                "override_stale": False,
            },
        ],
    }
    assert failure_summary(payload, "t") == ["函数/符号疑似已重命名"]


def test_manual_review_reason_fallback():
    # 兼容引擎某处写 reason 的形状
    payload = {
        "branch_results": {},
        "action_required": [
            {"sha": "a1", "branch": "t", "kind": "ManualReview", "reason": "需人工"},
        ],
    }
    assert failure_summary(payload, "t") == ["需人工"]


def test_stop_reason_branch_level():
    payload = {
        "branch_results": {
            "t": {
                "target_branch": "t",
                "status": "PARTIAL",
                "stop_reason": "fail-fast: a1 failed",
                "commits": [],
            }
        },
    }
    assert failure_summary(payload, "t") == ["fail-fast: a1 failed"]


def test_build_failure_reason_and_error():
    payload = {
        "branch_results": {
            "t": {
                "target_branch": "t",
                "commits": [
                    {
                        "sha": "a1b2c3d4e5f6",
                        "cherry_pick": "OK",
                        "build": {
                            "5200": {
                                "status": "FAILED",
                                "reason": "LLM 不可用，无法归因",
                            }
                        },
                    }
                ],
            }
        },
    }
    # reason 是「LLM 不可用」占位 → 只标记编译失败，不把它当归因
    assert failure_summary(payload, "t") == ["a1b2c3d4 5200 编译失败"]


def test_resolution_error():
    payload = {
        "branch_results": {
            "t": {
                "target_branch": "t",
                "commits": [
                    {
                        "sha": "a1b2c3d4e5f6",
                        "cherry_pick": "CONFLICT",
                        "resolution_error": "conflict too complex",
                    }
                ],
            }
        },
    }
    assert failure_summary(payload, "t") == ["a1b2c3d4 冲突解决失败：conflict too complex"]


def test_failure_text_joins_and_dedupes():
    payload = {
        "branch_results": {
            "t": {
                "target_branch": "t",
                "stop_reason": "fail-fast",
                "commits": [],
            }
        },
        "action_required": [{"node": "x", "error": "fail-fast"}],
    }
    text = failure_text(payload, "t")
    assert text == "fail-fast；x 节点错误：fail-fast"


def test_decision_breakdown_counts():
    payload = {
        "decisions": {
            "a1": {"t": {"kind": "NeedSync"}},
            "a2": {"t": {"kind": "OutOfScope"}, "t2": {"kind": "AlreadyIncluded"}},
            "a3": {"t": {"kind": "ManualReview"}},
        }
    }
    counts = decision_breakdown(payload)
    assert counts == {
        "NeedSync": 1,
        "AlreadyIncluded": 1,
        "OutOfScope": 1,
        "ManualReview": 1,
    }
