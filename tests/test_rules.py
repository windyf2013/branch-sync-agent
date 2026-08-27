from __future__ import annotations

from pathlib import Path

import pytest

from bsa.domain.models import Conclusion4
from bsa.executor.exceptions import SafetyViolation
from bsa.rules import (
    BranchMdDocument,
    CommitAnalysis,
    ConcludeThresholds,
    DecisionRules,
    HomologousSet,
    SafetyEnforcer,
    SafetyRules,
    TargetSnapshot,
    branch_prefix,
    build_matrix,
    classify_commit,
    classify_severity,
    conclude_pair,
    is_public_file,
    load_decision_rules,
    load_safety_rules,
    parse_branch_md,
    resolve_branch_type,
)

RULES_DIR = Path(__file__).resolve().parent.parent / "src" / "bsa" / "rules"
DECISION_RULES_PATH = RULES_DIR / "decision_rules.yaml"
SAFETY_RULES_PATH = RULES_DIR / "safety_rules.yaml"


def _analysis(**overrides: object) -> CommitAnalysis:
    base = CommitAnalysis(
        sha="abcdef1234567890",
        message="[BUG] CQ12345 Fix null deref in demo_check",
        changed_files=["plat/demo/demo.c"],
        symbols=["demo_check"],
        patch_text="+    if (NULL == ptr)\n+    {\n+        return -1;\n+    }\n",
        patch_id="patch-abc",
        issue_ids=["CQ12345"],
        recognition_source="machine:[BUG]",
        source_branch="br_v4_LineA_develop_a_20260101",
        source_branch_type="develop",
        homologous_section="LineA",
    )
    return base.model_copy(update=overrides)


def _target(**overrides: object) -> TargetSnapshot:
    base = TargetSnapshot(
        branch_name="br_v4_LineA_release_20260101",
        branch_type="release",
        has_source_sha=False,
        target_commit_messages=[],
        target_patch_ids=set(),
        target_issue_ids=set(),
        files_on_target={"plat/demo/demo.c"},
        symbols_on_target={"demo_check": True},
        file_similarity=None,
        fix_clearly_missing=True,
        function_renamed=False,
        in_same_homologous_set=True,
    )
    return base.model_copy(update=overrides)


# --- classify: 参考实现 golden-set 移植 ---


def test_classify_machine_readable_bug():
    result = classify_commit(
        "[BUG] CQ99999 Fix dhcp lease timeout",
        ["plat/dhcp/dhcp.c"],
        ["dhcp_lease_timeout"],
        "+if (NULL == cfg) return -1;",
    )
    assert result.is_bug_fix is True
    assert result.recognition_source == "machine:[BUG]"
    assert "CQ99999" in result.issue_ids
    assert result.needs_agent is False


def test_classify_fix_prefix():
    result = classify_commit(
        "fix: CQ88888 correct route lookup",
        ["plat/route/route.c"],
        [],
        "+    return -1;",
    )
    assert result.is_bug_fix is True
    assert result.recognition_source == "machine:fix:"


def test_classify_weak_message_goes_pending_agent():
    result = classify_commit(
        "fix",
        ["plat/demo/demo.c"],
        ["demo_check"],
        "+    if (NULL == ptr)\n+    {\n+        return -1;\n+    }\n",
        sha="abc1234567890",
    )
    assert result.is_bug_fix is False
    assert result.recognition_source == "pending:claude-agent"
    assert result.needs_agent is True


def test_classify_descriptive_api_change_pending_agent():
    result = classify_commit(
        "raisecom-equipment.yang add uuid",
        [
            "component/netconf/openYuma/netconf/modules/netconfcentral/raisecom-equipment.yang",
            "plat/lib/sys/ios_syslib.c",
        ],
        ["ios_get_device_uuid", "if"],
        "+int ios_get_device_uuid(char *buf)\n",
        sha="b3e31641cdcfabc",
    )
    assert result.is_bug_fix is False
    assert result.recognition_source == "pending:claude-agent"
    assert result.needs_agent is True


def test_classify_weak_message_no_files_not_included():
    result = classify_commit("fix", [], [], "")
    assert result.is_bug_fix is False
    assert result.recognition_source == "not-included"
    assert result.needs_agent is False
    assert result.reason is not None


def test_classify_merge_no_files_not_pending():
    result = classify_commit("Merge branch x", [], [], "", sha="abc1234567890")
    assert result.is_bug_fix is False
    assert result.needs_agent is False
    assert result.recognition_source == "not-included"


def test_classify_extracts_cherry_pick_marker():
    result = classify_commit(
        "Backport fix (cherry picked from commit deadbeef)",
        ["plat/demo/demo.c"],
        [],
        "",
    )
    assert result.cherry_pick_from == "deadbeef"


def test_classify_agent_judgment_bug_fix():
    result = classify_commit(
        "update something",
        ["a.c"],
        [],
        "+change\n",
        sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        agent_judgments={
            "deadbeef": {
                "is_bug_fix": True,
                "reason": "Restores missing null check.",
            }
        },
    )
    assert result.is_bug_fix is True
    assert result.recognition_source == "agent:bug-fix"
    assert "null" in (result.reason or "").lower()


def test_classify_agent_judgment_not_bug_fix():
    result = classify_commit(
        "add uuid",
        ["a.c"],
        [],
        "+change\n",
        sha="b3e31641cdcfabc",
        agent_judgments={
            "b3e31641cdcf": {"is_bug_fix": False, "reason": "Feature add."}
        },
    )
    assert result.is_bug_fix is False
    assert result.recognition_source == "agent:not-bug-fix"


def test_classify_lookup_agent_judgment_prefix():
    from bsa.rules.classify import lookup_agent_judgment

    judgments = {"abcdef1": {"is_bug_fix": True, "reason": "x"}}
    assert lookup_agent_judgment("abcdef1234567890", judgments)["is_bug_fix"] is True


def test_classify_agent_judgment_overrides_machine_marker():
    # 决策 6: 人工判定最高优先级，先于所有机器规则（含 [BUG] 标记）。
    result = classify_commit(
        "[BUG] CQ99999 Fix dhcp lease timeout",
        ["plat/dhcp/dhcp.c"],
        ["dhcp_lease_timeout"],
        "+if (NULL == cfg) return -1;",
        sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        agent_judgments={
            "deadbeef": {"is_bug_fix": False, "reason": "False positive, refactor."}
        },
    )
    assert result.is_bug_fix is False
    assert result.recognition_source == "agent:not-bug-fix"
    assert result.needs_agent is False


# --- classify: 完整移植规则（决策 21 FULLY ported） ---


def test_classify_bug_emoji_marker():
    result = classify_commit(":bug: fix ip lease", ["plat/dhcp/dhcp.c"], [], "")
    assert result.is_bug_fix is True
    assert result.recognition_source == "machine::bug:"


def test_classify_chinese_fix_word():
    result = classify_commit("修复空指针问题", ["plat/demo/demo.c"], [], "")
    assert result.is_bug_fix is True
    assert result.recognition_source == "machine:zh-fix"


def test_classify_issue_id_only():
    result = classify_commit("CQ12345 优化日志输出", ["plat/demo/demo.c"], [], "")
    assert result.is_bug_fix is True
    assert result.recognition_source == "machine:issue"
    assert result.issue_ids == ["CQ12345"]


def test_classify_cherry_pick_only():
    result = classify_commit(
        "Backport patch (cherry picked from commit deadbeef)",
        ["plat/demo/demo.c"],
        [],
        "",
    )
    assert result.is_bug_fix is True
    assert result.recognition_source == "machine:cherry-pick"
    assert result.cherry_pick_from == "deadbeef"


def test_classify_version_bump_not_bug():
    result = classify_commit(
        "build version: v4.35.001",
        ["plat/version/ios_version.h"],
        [],
        "",
    )
    assert result.is_bug_fix is False
    assert result.recognition_source == "machine:version-bump"
    assert result.needs_agent is False


def test_classify_version_file_only_not_bug():
    result = classify_commit(
        "update version header",
        ["plat/version/ios_version.h"],
        [],
        "",
    )
    assert result.is_bug_fix is False
    assert result.recognition_source == "machine:version-file"


def test_classify_chore_docs_not_bug():
    result = classify_commit("chore(deps): bump", ["pyproject.toml"], [], "")
    assert result.is_bug_fix is False
    assert result.recognition_source == "machine:chore-docs"


def test_classify_ai_ignor_not_bug():
    result = classify_commit("AI IGNOR: generated", ["plat/gen/gen.c"], [], "")
    assert result.is_bug_fix is False
    assert result.recognition_source == "machine:ai-ignor"


def test_classify_docs_only_not_bug():
    result = classify_commit("add design notes", ["openspec/foo.md"], [], "")
    assert result.is_bug_fix is False
    assert result.recognition_source == "machine:docs-only"


# --- conclude: 参考实现 golden-set 移植（适配决策 35/36/37） ---


def test_missing_sha_without_files_not_need_sync():
    source = _analysis(
        changed_files=["plat/missing/feature.c"],
        symbols=["missing_fn"],
    )
    target = _target(
        files_on_target=set(),
        symbols_on_target={},
        fix_clearly_missing=False,
    )
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind in {"OutOfScope", "ManualReview"}
    assert conclusion.kind != "NeedSync"


def test_same_sha_already_included_before_similarity():
    source = _analysis(sha="abcdef1234567890")
    target = _target(has_source_sha=True, file_similarity=0.99)
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "AlreadyIncluded"
    assert conclusion.confidence == "high"
    assert any("相同提交" in item for item in conclusion.evidence)


def test_cherry_pick_already_included():
    source = _analysis(sha="abcdef1234567890")
    target = _target(
        target_commit_messages=[
            "Fix null deref (cherry picked from commit abcdef1234567890)",
        ]
    )
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "AlreadyIncluded"
    assert any("cherry-pick" in item.lower() for item in conclusion.evidence)


def test_same_issue_already_included():
    source = _analysis(issue_ids=["CQ12345"])
    target = _target(target_issue_ids={"CQ12345"})
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "AlreadyIncluded"
    assert any("CQ12345" in item for item in conclusion.evidence)


def test_same_patch_id_already_included():
    source = _analysis(patch_id="patch-shared")
    target = _target(target_patch_ids={"patch-shared"})
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "AlreadyIncluded"


def test_high_similarity_already_included_low_confidence():
    source = _analysis()
    target = _target(file_similarity=0.95, fix_clearly_missing=False)
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "AlreadyIncluded"
    assert conclusion.confidence == "low"


def test_gray_zone_similarity_manual_review():
    source = _analysis()
    target = _target(file_similarity=0.70)
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "ManualReview"


def test_feature_target_out_of_scope():
    source = _analysis()
    target = _target(branch_type="feature")
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "OutOfScope"


def test_develop_target_need_sync():
    source = _analysis()
    target = _target(branch_type="develop", branch_name="br_v4_LineA_develop_b_20260101")
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "NeedSync"


def test_need_sync_with_clear_anchor():
    source = _analysis()
    target = _target(fix_clearly_missing=True)
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "NeedSync"
    assert conclusion.confidence in {"high", "medium", "low"}


def test_need_sync_below_low_threshold():
    source = _analysis()
    target = _target(file_similarity=0.20)
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "NeedSync"
    assert conclusion.confidence == "high"


def test_function_renamed_manual_review():
    source = _analysis()
    target = _target(function_renamed=True, fix_clearly_missing=True)
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "ManualReview"
    assert conclusion.kind != "NeedSync"


def test_need_sync_high_confidence_with_ticket_and_symbols():
    source = _analysis()
    target = _target(fix_clearly_missing=True)
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "NeedSync"
    assert conclusion.confidence == "high"


def test_need_sync_medium_without_ticket():
    source = _analysis(issue_ids=[], recognition_source="agent:bug-fix")
    target = _target(fix_clearly_missing=True)
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "NeedSync"
    assert conclusion.confidence == "medium"


def test_pending_needs_agent_never_auto_sync():
    # 决策 18: LLM 未判定（pending）→ 即使 risk=high + 锚点齐全也转人工，不自动同步。
    source = _analysis(risk="high", needs_agent=True)
    target = _target(fix_clearly_missing=True)
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "ManualReview"
    assert any("pending" in item for item in conclusion.evidence)


def test_pending_recognition_source_never_auto_sync():
    source = _analysis(recognition_source="pending:claude-agent")
    target = _target(fix_clearly_missing=True)
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "ManualReview"


def test_need_sync_low_without_ticket_or_symbols():
    source = _analysis(
        symbols=[],
        issue_ids=[],
        recognition_source="agent:bug-fix",
    )
    target = _target(fix_clearly_missing=True)
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "NeedSync"
    assert conclusion.confidence == "low"


def test_cross_homologous_out_of_scope():
    source = _analysis()
    target = _target(in_same_homologous_set=False)
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "OutOfScope"


def test_conclude_returns_domain_conclusion4():
    conclusion = conclude_pair(_analysis(), _target())
    assert isinstance(conclusion, Conclusion4)
    assert conclusion.kind in {"NeedSync", "AlreadyIncluded", "ManualReview", "OutOfScope"}
    assert conclusion.confidence in {"high", "medium", "low"}
    assert isinstance(conclusion.evidence, list) and conclusion.evidence


def test_conclude_default_thresholds():
    conclusion = conclude_pair(_analysis(), _target(file_similarity=0.95))
    assert conclusion.kind == "AlreadyIncluded"
    conclusion = conclude_pair(_analysis(), _target(file_similarity=0.80))
    assert conclusion.kind == "ManualReview"


# --- conclude: 目标类型由规则单一驱动（P2 治理） ---


def test_conclude_custom_need_sync_target_types():
    # 决策 2/27 目标类型数据驱动：need_sync 不含 develop 时，develop 目标不再 NeedSync。
    source = _analysis()
    target = _target(branch_type="develop", branch_name="br_v4_LineA_develop_b_20260101")
    conclusion = conclude_pair(
        source,
        target,
        similarity_high=0.90,
        similarity_low=0.50,
        need_sync_target_types=["release", "fix"],
        ineligible_target_types=["feature", "personal"],
    )
    assert conclusion.kind == "OutOfScope"


def test_conclude_custom_ineligible_target_types():
    # ineligible 不含 feature 时，feature 目标不再被硬编码 OutOfScope（可进入后续判定）。
    source = _analysis()
    target = _target(branch_type="feature", branch_name="br_v4_LineA_feature_20260101")
    conclusion = conclude_pair(
        source,
        target,
        similarity_high=0.90,
        similarity_low=0.50,
        need_sync_target_types=["develop", "release", "fix", "feature"],
        ineligible_target_types=["personal"],
    )
    assert conclusion.kind != "OutOfScope"


def test_load_decision_rules_ineligible_target_types():
    rules = load_decision_rules(DECISION_RULES_PATH)
    assert rules.conclude.ineligible_target_types == ["feature", "personal"]


# --- conclude: 发布线严重性门控（决策 41） ---


def test_develop_to_fix_low_risk_gated_manual_review():
    source = _analysis(risk="low")
    target = _target(branch_type="fix", branch_name="br_v4_LineA_fix_20260201")
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "ManualReview"
    assert any("high" in item and "fix" in item for item in conclusion.evidence)


def test_develop_to_fix_high_risk_need_sync():
    source = _analysis(risk="high")
    target = _target(branch_type="fix", branch_name="br_v4_LineA_fix_20260201")
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "NeedSync"


def test_release_to_develop_medium_risk_gated_manual_review():
    source = _analysis(source_branch_type="release", risk="medium")
    target = _target(branch_type="develop", branch_name="br_v4_LineA_develop_b_20260101")
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "ManualReview"


def test_release_to_develop_high_risk_need_sync():
    source = _analysis(source_branch_type="release", risk="high")
    target = _target(branch_type="develop", branch_name="br_v4_LineA_develop_b_20260101")
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "NeedSync"


def test_release_to_develop_high_risk_missing_fix_still_need_sync():
    # 真机测试: zebra_cli.c 初始化修复不属于 NULL==/return -1 模式，
    # fix_clearly_missing=False 但 high 已过发布线门控 → 允许低置信度 NeedSync
    source = _analysis(source_branch_type="release", risk="high")
    target = _target(
        branch_type="develop",
        branch_name="br_v4_LineA_develop_b_20260101",
        fix_clearly_missing=False,
    )
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "NeedSync"
    assert conclusion.confidence == "low"


def test_release_to_develop_unknown_risk_missing_fix_manual_review():
    # fix_clearly_missing=False 且 risk 非 high → 维持 ManualReview（保守）
    source = _analysis(source_branch_type="release", risk=None)
    target = _target(
        branch_type="develop",
        branch_name="br_v4_LineA_develop_b_20260101",
        fix_clearly_missing=False,
    )
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "ManualReview"


def test_fix_to_develop_unknown_risk_gated_manual_review():
    source = _analysis(source_branch_type="fix", risk=None)
    target = _target(branch_type="develop", branch_name="br_v4_LineA_develop_b_20260101")
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "ManualReview"


def test_develop_to_develop_not_gated_even_unknown_risk():
    source = _analysis(risk=None)
    target = _target(branch_type="develop", branch_name="br_v4_LineA_develop_b_20260101")
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "NeedSync"


def test_develop_to_release_not_gated_low_risk():
    source = _analysis(risk="low")
    target = _target()
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "NeedSync"


def test_release_to_release_not_synced():
    source = _analysis(source_branch_type="release", risk="high")
    target = _target(branch_type="release", branch_name="br_v4_LineA_release_p361_20260101")
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "OutOfScope"
    assert any("不互同步" in item for item in conclusion.evidence)


def test_fix_to_fix_not_synced():
    source = _analysis(source_branch_type="fix", risk="high")
    target = _target(branch_type="fix", branch_name="br_v4_LineA_fix_2_20260101")
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "OutOfScope"


def test_release_to_fix_high_risk_need_sync():
    source = _analysis(source_branch_type="release", risk="high")
    target = _target(branch_type="fix", branch_name="br_v4_LineA_fix_20260201")
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "NeedSync"


def test_fix_to_release_normal_need_sync():
    source = _analysis(source_branch_type="fix", risk="medium")
    target = _target(branch_name="br_v4_LineA_release_p361_20260101")
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "NeedSync"


# --- decision_rules: 数据驱动加载 ---


def test_load_decision_rules_bundled_yaml():
    rules = load_decision_rules(DECISION_RULES_PATH)
    assert isinstance(rules, DecisionRules)
    assert isinstance(rules.conclude, ConcludeThresholds)
    assert rules.conclude.similarity_high == pytest.approx(0.90)
    assert rules.conclude.similarity_low == pytest.approx(0.50)
    assert rules.conclude.need_sync_target_types == ["develop", "release", "fix"]
    assert "bugfix_markers" in rules.classify
    assert "not_bugfix_markers" in rules.classify
    assert isinstance(rules.branch_mapping, dict)
    assert isinstance(rules.classify["path_rules"], dict)
    assert "public_dirs" in rules.classify["path_rules"]
    assert rules.severity.high_keywords
    assert isinstance(rules.severity.severity_paths, list)


def test_load_decision_rules_defaults(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text("classify: {}\nconclude: {}\nbranch_mapping: {}\n", encoding="utf-8")
    rules = load_decision_rules(path)
    assert rules.conclude.similarity_high == pytest.approx(0.90)
    assert rules.conclude.similarity_low == pytest.approx(0.50)
    assert rules.conclude.need_sync_target_types == ["develop", "release", "fix"]
    assert rules.severity.high_keywords == []


def test_decision_rules_custom_thresholds(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(
        "classify: {}\n"
        "conclude:\n"
        "  similarity_high: 0.95\n"
        "  similarity_low: 0.60\n"
        "  need_sync_target_types: ['release']\n"
        "severity:\n"
        "  high_keywords: ['\\[CRITICAL\\]']\n"
        "  severity_paths: ['plat/security/']\n"
        "branch_mapping: {br_x: develop}\n",
        encoding="utf-8",
    )
    rules = load_decision_rules(path)
    assert rules.conclude.similarity_high == pytest.approx(0.95)
    assert rules.conclude.similarity_low == pytest.approx(0.60)
    assert rules.conclude.need_sync_target_types == ["release"]
    assert rules.severity.high_keywords == ["\\[CRITICAL\\]"]
    assert rules.severity.severity_paths == ["plat/security/"]
    assert rules.branch_mapping == {"br_x": "develop"}


# --- classify_severity: 规则层严重性判定（决策 41） ---


def test_classify_severity_critical_keyword_high():
    assert classify_severity("[CRITICAL] fix panic in dhcp", ["plat/dhcp/dhcp.c"]) == "high"


def test_classify_severity_urgent_keyword_high():
    assert classify_severity("[URGENT] 修复宕机问题", ["plat/dhcp/dhcp.c"]) == "high"


def test_classify_severity_chinese_high_keywords():
    for keyword in ["崩溃", "宕机", "数据丢失", "内存泄漏", "安全漏洞"]:
        assert classify_severity(f"修复{keyword}问题", ["plat/demo/demo.c"]) == "high"


def test_classify_severity_core_path_high():
    assert classify_severity("refactor auth flow", ["plat/security/auth.c"]) == "high"


def test_classify_severity_core_path_dir_boundary():
    assert classify_severity("refactor auth flow", ["platform/security/auth.c"]) == "unknown"


def test_classify_severity_no_rule_unknown():
    assert classify_severity("fix minor typo", ["plat/demo/demo.c"]) == "unknown"


# --- safety: 数据驱动校验 + SafetyEnforcer ---


def _enforcer(**overrides: object) -> SafetyEnforcer:
    base = {
        "forbidden_paths": ["config/", "secrets.yaml"],
        "required_models": ["2600_CMCC"],
        "forbidden_branches": ["br_qa", "br_production"],
        "max_single_edit_lines": 200,
    }
    base.update(overrides)
    return SafetyEnforcer(SafetyRules(**base))


def test_load_safety_rules_bundled_yaml():
    rules = load_safety_rules(SAFETY_RULES_PATH)
    assert isinstance(rules, SafetyRules)
    assert isinstance(rules.forbidden_paths, list)
    assert isinstance(rules.required_models, list) and rules.required_models
    assert isinstance(rules.forbidden_branches, list)
    assert isinstance(rules.max_single_edit_lines, int) and rules.max_single_edit_lines > 0


def test_safety_enforcer_forbidden_path_prefix_raises():
    enforcer = _enforcer()
    with pytest.raises(SafetyViolation):
        enforcer.check_editable(["config/app.yaml"])


def test_safety_enforcer_forbidden_path_exact_raises():
    enforcer = _enforcer()
    with pytest.raises(SafetyViolation):
        enforcer.check_editable(["secrets.yaml"])


def test_safety_enforcer_allowed_paths_pass():
    enforcer = _enforcer()
    enforcer.check_editable(["plat/demo/demo.c", "component/netconf/x.yang"])


def test_safety_enforcer_check_sync_branch():
    enforcer = _enforcer()
    assert enforcer.check_sync_branch("br_qa") is False
    assert enforcer.check_sync_branch("br_v4_LineA_release_20260101") is True


def test_safety_enforcer_models_and_lines():
    enforcer = _enforcer()
    assert enforcer.required_models() == ["2600_CMCC"]
    assert enforcer.max_single_edit_lines() == 200


def test_safety_enforcer_check_edit_scale_accepts_small_diff():
    enforcer = _enforcer()
    small = (
        "diff --git a/src/net.c b/src/net.c\n"
        "--- a/src/net.c\n"
        "+++ b/src/net.c\n"
        "@@ -1,2 +1,2 @@\n"
        "-int value = bad;\n"
        "+int value = 1;\n"
    )
    enforcer.check_edit_scale(small)


def test_safety_enforcer_check_edit_scale_rejects_large_diff():
    enforcer = _enforcer()
    big = "".join(f"+line {i}\n" for i in range(201))
    with pytest.raises(SafetyViolation, match="超过上限 200"):
        enforcer.check_edit_scale(big)


def test_safety_enforcer_check_edit_scale_counts_removed_lines():
    enforcer = _enforcer()
    diff = "".join(f"-gone {i}\n" for i in range(201))
    with pytest.raises(SafetyViolation):
        enforcer.check_edit_scale(diff)


def test_safety_enforcer_check_edit_scale_ignores_hunk_headers():
    enforcer = _enforcer()
    diff = (
        "--- a/x.c\n"
        "+++ b/x.c\n"
        "@@ -1,200 +1,200 @@\n"
        + "".join(f"+x{i}\n" for i in range(200))
    )
    enforcer.check_edit_scale(diff)


# --- paths: 公共目录判定（决策 2） ---


def test_is_public_file_inside_dir():
    assert is_public_file("plat/lib/sys/ios_syslib.c", ["component/", "plat/"]) is True


def test_is_public_file_outside_dir():
    assert is_public_file("openspec/foo.md", ["component/", "plat/"]) is False


def test_is_public_file_dir_boundary():
    assert is_public_file("plat/x.c", ["plat/"]) is True
    assert is_public_file("platform/x.c", ["plat/"]) is False


def test_is_public_file_exact_entry():
    assert is_public_file("Makefile", ["Makefile"]) is True
    assert is_public_file("Makefile.am", ["Makefile"]) is False


# --- branch_md: 解析 + 前缀血缘矩阵（决策 35） ---


def test_resolve_branch_type_personal_lowest():
    assert resolve_branch_type("br_v4_develop_personal_20260101") == "personal"


def test_resolve_branch_type_feature_plus_fix():
    assert resolve_branch_type("br_v4_feature_fix_20260101") == "feature"


def test_resolve_branch_type_psersonal_typo():
    assert resolve_branch_type("br_v4_psersonal_20260101") == "personal"


def test_resolve_branch_type_develop_feature():
    assert resolve_branch_type("br_v4_develop_feature_20260101") == "feature"


def test_resolve_branch_type_release():
    assert resolve_branch_type("br_v4.34_develop_FTTR_release_p360_20260316") == "release"


def test_resolve_branch_type_fix():
    assert resolve_branch_type("br_v4.34_develop_FTTR_release_p360_fix_20260316") == "fix"


def test_resolve_branch_type_unknown():
    assert resolve_branch_type("br_v4_LineA_main_20260101") == "unknown"


def test_resolve_branch_type_branch_mapping_exact_overrides():
    mapping = {"br_v4_LineA_main_20260101": "release"}
    assert (
        resolve_branch_type("br_v4_LineA_main_20260101", branch_mapping=mapping)
        == "release"
    )


def test_resolve_branch_type_branch_mapping_prefix_overrides():
    mapping = {"br_qa": "release"}
    assert resolve_branch_type("br_qa_fix_20260101", branch_mapping=mapping) == "release"


def test_resolve_branch_type_branch_mapping_falls_back_to_inference():
    mapping = {"br_qa": "release"}
    assert resolve_branch_type("br_v4_LineA_main_20260101", branch_mapping=mapping) == "unknown"


def test_parse_branch_md_honors_branch_mapping():
    text = """# Doc title
## LineOne
- br_v4_LineA_main_20260101
"""
    doc = parse_branch_md(text, branch_mapping={"br_v4_LineA_main_20260101": "release"})
    assert doc.sections[0].branches[0].branch_type == "release"


def test_branch_prefix_strips_date():
    assert (
        branch_prefix("br_v4.34_develop_FTTR_P300_CTEB_20260316")
        == "br_v4.34_develop_FTTR_P300_CTEB"
    )
    assert branch_prefix("br_v4_LineA_develop_a_20260101") == "br_v4_LineA_develop_a"


def test_branch_prefix_without_date_unchanged():
    assert branch_prefix("br_v4_develop") == "br_v4_develop"
    assert branch_prefix("br_v4_develop_2026") == "br_v4_develop_2026"


def test_parse_branch_md_sections():
    text = """# Doc title
## LineOne
- br_a
## LineTwo
- br_b
"""
    doc = parse_branch_md(text)
    assert isinstance(doc, BranchMdDocument)
    assert doc.title == "Doc title"
    assert len(doc.sections) == 2
    assert doc.sections[0].title == "LineOne"
    assert doc.sections[0].branches[0].name == "br_a"
    assert doc.sections[1].title == "LineTwo"
    assert doc.sections[1].branches[0].name == "br_b"


SAMPLE_MD = """# 所有待审核分支
## 1 RCIOS代码库
- 路径：rcios

### 1.1 4.34 主分支
- br_v4.34_develop_20260130

### 1.2 4.34 业务分支
- br_v4.34_develop_fttr_20260811
- br_v4.34_develop_fttr_release_p360_20260625
- br_v4.34_develop_fttr_feature_quantum_20260625
- br_v4.34_develop_fttr_personal_yuhui_20260625
"""


def test_build_matrix_sources_all_eligible():
    doc = parse_branch_md(SAMPLE_MD)
    sets = build_matrix(doc)
    assert isinstance(sets, list) and sets
    assert isinstance(sets[0], HomologousSet)
    s = sets[0]
    assert s.section == "4.34"
    assert [b.name for b in s.sources] == [
        "br_v4.34_develop_fttr_20260811",
        "br_v4.34_develop_fttr_release_p360_20260625",
    ]
    assert s.sources[0].branch_type == "develop"


def test_build_matrix_all_eligible_branches_are_targets():
    doc = parse_branch_md(SAMPLE_MD)
    s = build_matrix(doc)[0]
    targets = {b.name for b in s.need_sync_targets}
    assert targets == {"br_v4.34_develop_20260130"}
    assert "br_v4.34_develop_fttr_20260811" not in targets
    assert "br_v4.34_develop_fttr_release_p360_20260625" not in targets
    assert "br_v4.34_develop_fttr_feature_quantum_20260625" not in targets
    assert "br_v4.34_develop_fttr_personal_yuhui_20260625" not in targets


def test_build_matrix_feature_personal_excluded_everywhere():
    doc = parse_branch_md(SAMPLE_MD)
    s = build_matrix(doc)[0]
    names = {b.name for b in s.sources + s.need_sync_targets}
    assert "br_v4.34_develop_fttr_feature_quantum_20260625" not in names
    assert "br_v4.34_develop_fttr_personal_yuhui_20260625" not in names


def test_build_matrix_develop_is_target():
    doc = parse_branch_md(SAMPLE_MD)
    s = build_matrix(doc)[0]
    assert any(b.branch_type == "develop" for b in s.need_sync_targets)


def test_build_matrix_business_develop_and_release_are_sources():
    """业务 section 内的 develop/release/fix 分支全部是源（不区分类型）。"""
    text = """# 所有待审核分支
## 1 RCIOS代码库
- 路径：rcios

### 1.1 4.34 主分支
- br_v4.34_develop_20260316

### 1.2 4.34 业务分支
- br_v4.34_develop_FTTR_20260316
- br_v4.34_develop_FTTR_release_p360_20260401
- br_v4.34_develop_FTTR_release_p360_fix_20260501
"""
    s = build_matrix(parse_branch_md(text))[0]
    source_names = {b.name for b in s.sources}
    assert "br_v4.34_develop_FTTR_20260316" in source_names
    assert "br_v4.34_develop_FTTR_release_p360_20260401" in source_names
    assert "br_v4.34_develop_FTTR_release_p360_fix_20260501" in source_names
    assert {b.name for b in s.need_sync_targets} == {"br_v4.34_develop_20260316"}


def test_build_matrix_business_to_main_ignores_branch_prefix_lineage():
    """定向不靠分支名前缀血缘；业务/主 section 标题配对即可。"""
    text = """# 所有待审核分支
## 1 RCIOS代码库
- 路径：rcios

### 1.1 4.34 主分支
- br_v4_LineA_develop_20260101

### 1.2 4.34 业务分支
- br_v4_LineB_develop_release_20260101
"""
    s = build_matrix(parse_branch_md(text))[0]
    source_names = {b.name for b in s.sources}
    assert source_names == {"br_v4_LineB_develop_release_20260101"}
    assert {b.name for b in s.need_sync_targets} == {"br_v4_LineA_develop_20260101"}


def test_build_matrix_cross_product_isolation():
    """不同产品线（4.34 / 4.35）的同步边互相隔离，不跨产品配对。"""
    text = """# 所有待审核分支
## 1 RCIOS代码库
- 路径：rcios

### 1.1 4.34 主分支
- br_v4_ProductA_develop_20260101

### 1.2 4.34 业务分支
- br_v4_ProductA_develop_release_20260101

### 2.1 4.35 主分支
- br_v4_ProductB_develop_20260101

### 2.2 4.35 业务分支
- br_v4_ProductB_develop_release_20260101
"""
    sets = build_matrix(parse_branch_md(text))
    assert len(sets) == 2
    by_section = {s.section: s for s in sets}
    assert set(by_section) == {"4.34", "4.35"}
    targets_434 = {b.name for b in by_section["4.34"].need_sync_targets}
    targets_435 = {b.name for b in by_section["4.35"].need_sync_targets}
    assert targets_434 == {"br_v4_ProductA_develop_20260101"}
    assert targets_435 == {"br_v4_ProductB_develop_20260101"}
    assert "br_v4_ProductB_develop_20260101" not in targets_434
    assert "br_v4_ProductA_develop_20260101" not in targets_435


def test_build_matrix_business_to_main_directed():
    """业务分支 section 只当源，主分支 section 只当目标，业务→主单向。

    现状全互联（同 section 互灌）在 ≥3 分支时产生回声重检与平方级无效工作；
    写死语义：标题含"主分支"=目标，含"业务分支"=源，业务之间互不同步。
    """
    text = """# 所有待审核分支
## 1 RCIOS代码库
- 路径：rcios

### 1.1 4.34 主分支
- br_v4.34_develop_20260130

### 1.2 4.34 业务分支
- br_v4.34_develop_fttr_20260811
- br_v4.34_MSG_develop_20260805
"""
    sets = build_matrix(parse_branch_md(text))
    assert len(sets) == 1
    s = sets[0]
    source_names = {b.name for b in s.sources}
    target_names = {b.name for b in s.need_sync_targets}
    assert source_names == {
        "br_v4.34_develop_fttr_20260811",
        "br_v4.34_MSG_develop_20260805",
    }
    assert target_names == {"br_v4.34_develop_20260130"}


def test_build_matrix_main_section_branches_are_targets_not_sources():
    """主分支 section 的分支只当目标，不作为任何分支的源。"""
    text = """# 所有待审核分支
## 1 RCIOS代码库
- 路径：rcios

### 1.1 4.35 主分支
- br_v4.35_develop_20260101

### 1.2 4.35 业务分支
- br_v4.35_develop_fttr_20260201
"""
    s = build_matrix(parse_branch_md(text))[0]
    source_names = {b.name for b in s.sources}
    assert "br_v4.35_develop_20260101" not in source_names
    assert "br_v4.35_develop_fttr_20260201" in source_names


def test_build_matrix_unmarked_section_produces_no_edges():
    """无"主分支"/"业务分支"标注的 section 不产出同步边（不默认全互联）。"""
    text = """# 所有待审核分支
## 1 RCIOS代码库
- 路径：rcios

### 1.1 未标注分支
- br_v4_plain_develop_20260101
- br_v4_plain_release_20260201
"""
    sets = build_matrix(parse_branch_md(text))
    assert sets == []


def test_duplicate_branch_reported_and_used_once():
    text = """# title
## SectionA
- br_v4_dup_develop_20260101
- br_v4_dup_develop_release_20260101
## SectionB
- br_v4_dup_develop_release_20260101
"""
    doc = parse_branch_md(text)
    assert len(doc.duplicates) == 1
    assert doc.duplicates[0]["name"] == "br_v4_dup_develop_release_20260101"
    assert doc.duplicates[0]["count"] == 2
    # 无"主分支"/"业务分支"标注的 section 不产出同步边（新语义）。
    assert build_matrix(doc) == []
