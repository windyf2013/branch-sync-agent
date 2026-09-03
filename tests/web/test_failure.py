"""failure 归因提取纯函数单测：覆盖各种真实 state.json 数据形状。"""

from __future__ import annotations

from bsa_web.failure import (
    commit_bucket,
    commit_destinations,
    commit_rationale,
    decision_breakdown,
    failure_summary,
    failure_text,
)


def test_node_failure_action_required():
    payload = {
        "branch_results": {},
        "action_required": [
            {"node": "branch_matrix", "error": "未解析出任何同步边"},
        ],
    }
    assert failure_summary(payload) == ["分支拓扑解析 节点错误：未解析出任何同步边"]


def test_node_failure_with_branch_scoped_to_that_branch():
    # 绑定具体分支的节点失败只在该分支的 failure_summary 出现；
    # 成功分支不得复制他分支的 prepare_worktree 失败（避免绿分支显红）。
    payload = {
        "branch_results": {},
        "action_required": [
            {
                "node": "prepare_worktree",
                "branch": "br_A",
                "error": "br_A 基线编译失败",
            },
        ],
    }
    assert failure_summary(payload, "br_B") == []
    assert failure_summary(payload, "br_A") == ["建立 worktree 节点错误：br_A 基线编译失败"]
    # 周期级（target=None）展示，并带分支前缀便于定位
    assert failure_summary(payload) == ["br_A: 建立 worktree 节点错误：br_A 基线编译失败"]


def test_manual_review_not_a_failure_reason():
    # ManualReview 是待人工裁决，不是执行失败：不得混进 failure_summary
    # （否则周期概览「周期失败原因」会被待确认 commit 污染）。
    payload = {
        "branch_results": {},
        "detected_commits": [{"sha": "a1"}],
        "decisions": {},
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
    assert failure_summary(payload, "t") == []
    assert failure_summary(payload) == []
    # 但它在去向归类里是「待确认」，不被漏掉
    assert commit_bucket(payload, "a1") == "review"
    assert commit_destinations(payload) == {
        "detected": 1, "synced": 0, "skipped": 0, "review": 1, "unhandled": 0,
    }


def test_failure_summary_still_shows_real_failure_alongside_manual_review():
    # 真实失败（stop_reason）与 ManualReview 并存时，只出失败、不出人工项
    payload = {
        "branch_results": {
            "t": {
                "target_branch": "t",
                "status": "FAILED",
                "stop_reason": "baseline build failed on 2600m",
                "commits": [],
            }
        },
        "action_required": [
            {"sha": "a1", "branch": "t", "kind": "ManualReview",
             "evidence": ["关联文件存在，但目标分支上函数/符号疑似已重命名。"]},
        ],
    }
    assert failure_summary(payload, "t") == ["型号 2600m 基线编译失败"]
    assert failure_summary(payload) == ["t: 型号 2600m 基线编译失败"]


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
    assert failure_summary(payload, "t") == ["a1 失败"]


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


def test_commit_destinations_conservation():
    # 守恒：detected = synced + skipped + review + unhandled，每个 commit 恰好一个去向。
    payload = {
        "detected_commits": [
            {"sha": "s1"},
            {"sha": "s2"},
            {"sha": "s3"},
            {"sha": "s4"},
            {"sha": "s5"},
        ],
        "decisions": {
            "s2": {"t": {"kind": "AlreadyIncluded"}},
            "s3": {"t": {"kind": "OutOfScope"}},
            "s4": {"t": {"kind": "ManualReview"}},
            "s5": {"t": {"kind": "NeedSync"}},  # 判定待同步但未落地 → 未处理
        },
        "branch_results": {
            "t": {"commits": [{"sha": "s1", "cherry_pick": "OK"}]}
        },
        "action_required": [],
    }
    d = commit_destinations(payload)
    assert d == {
        "detected": 5,
        "synced": 1,
        "skipped": 2,
        "review": 1,
        "unhandled": 1,
    }
    assert d["detected"] == d["synced"] + d["skipped"] + d["review"] + d["unhandled"]


def test_commit_destinations_review_from_action_required():
    # ManualReview 可能只出现在 action_required（无 decisions），也算 review 桶。
    payload = {
        "detected_commits": [{"sha": "a1"}],
        "decisions": {},
        "branch_results": {},
        "action_required": [{"sha": "a1", "branch": "t", "kind": "ManualReview"}],
    }
    d = commit_destinations(payload)
    assert d == {"detected": 1, "synced": 0, "skipped": 0, "review": 1, "unhandled": 0}


def test_commit_destinations_no_detection_is_all_zero():
    assert commit_destinations({}) == {
        "detected": 0,
        "synced": 0,
        "skipped": 0,
        "review": 0,
        "unhandled": 0,
    }


def test_commit_rationale_mirrors_decision_evidence():
    # 判定说明 = 决定该 commit 去向的那条判定的 evidence 原文
    payload = {
        "detected_commits": [
            {"sha": "s1"}, {"sha": "s2"}, {"sha": "s3"},
            {"sha": "s4"}, {"sha": "s5"}, {"sha": "s6"},
        ],
        "decisions": {
            "s2": {"t": {"kind": "AlreadyIncluded",
                         "evidence": ["关联改动相似度 1.00 达到已包含阈值。"]}},
            "s4": {"t": {"kind": "ManualReview",
                         "evidence": ["关联文件存在，但目标分支上函数/符号疑似已重命名。"]}},
        },
        "branch_results": {},
        "action_required": [
            {"sha": "s4", "branch": "t", "kind": "ManualReview"},
        ],
    }
    # skipped：取 AlreadyIncluded evidence
    assert commit_rationale(payload, "s2") == "关联改动相似度 1.00 达到已包含阈值。"
    # review：取 ManualReview evidence
    assert commit_rationale(payload, "s4") == "关联文件存在，但目标分支上函数/符号疑似已重命名。"
    # unhandled：无判定 → 排查归因兜底
    assert commit_rationale(payload, "s5") == "无判定记录，需排查归因"
    # synced 但无判定记录 → 桶级兜底
    assert commit_rationale(payload, "s6") == "无判定记录，需排查归因"


def test_commit_rationale_synced_uses_applied_target_decision():
    # synced：应取「实际同步到的目标分支」上那条 NeedSync 的 evidence
    payload = {
        "detected_commits": [{"sha": "s1"}],
        "decisions": {
            "s1": {
                "tA": {"kind": "ManualReview", "evidence": ["tA 需人工"]},
                "tB": {"kind": "NeedSync", "evidence": ["tB 缺少该修复，需同步"]},
            }
        },
        # s1 实际只同步到 tB
        "branch_results": {"tB": {"target_branch": "tB",
                                  "commits": [{"sha": "s1", "cherry_pick": "OK"}]}},
        "action_required": [],
    }
    assert commit_bucket(payload, "s1") == "synced"
    assert commit_rationale(payload, "s1") == "tB 缺少该修复，需同步"


def test_commit_bucket_each_verdict():
    # commit 表「判定结果」列 + 去向归类的共同单一来源（幂等、互斥）
    payload = {
        "detected_commits": [
            {"sha": "s1"}, {"sha": "s2"}, {"sha": "s3"},
            {"sha": "s4"}, {"sha": "s5"}, {"sha": "s6"},
        ],
        "decisions": {
            "s2": {"t": {"kind": "AlreadyIncluded"}},
            "s3": {"t": {"kind": "OutOfScope"}},
            "s4": {"t": {"kind": "ManualReview"}},
            # s5 无判定记录、s6 NeedSync 未落地 → unhandled
        },
        "branch_results": {
            "t": {"commits": [{"sha": "s1", "cherry_pick": "EMPTY"}]}
        },
        "action_required": [
            {"sha": "s4", "branch": "t", "kind": "ManualReview"},
        ],
    }
    assert commit_bucket(payload, "s1") == "synced"
    assert commit_bucket(payload, "s2") == "skipped"
    assert commit_bucket(payload, "s3") == "skipped"
    assert commit_bucket(payload, "s4") == "review"
    assert commit_bucket(payload, "s5") == "unhandled"
    assert commit_bucket(payload, "s6") == "unhandled"
