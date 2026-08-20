from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from branch_maintenance.classify import classify_commit
from branch_maintenance.conclude import CommitAnalysis, TargetSnapshot, conclude_pair
from fixtures.mini_repo import build_multi_develop_fix_repo
from branch_maintenance.git_scan import (
    commits_since,
    file_exists,
    patch_id,
    show_file,
)
def _bug_fix_analysis(**overrides) -> CommitAnalysis:
    base = CommitAnalysis(
        sha="abcdef1234567890",
        message="[BUG] CQ12345 Fix null deref in demo_check",
        changed_files=["plat/demo/demo.c"],
        symbols=["demo_check"],
        patch_text=(
            "@@ -1,4 +1,8 @@\n"
            "+    if (NULL == ptr)\n"
            "+    {\n"
            "+        return -1;\n"
            "+    }\n"
        ),
        patch_id="patch-abc",
        issue_ids=["CQ12345"],
        recognition_source="machine:[BUG]",
        source_branch="br_v4_LineA_develop_a_20260101",
        source_branch_type="develop",
        homologous_section="LineA",
        has_high_priority_rule=True,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _target(**overrides) -> TargetSnapshot:
    base = TargetSnapshot(
        branch_name="br_v4_LineA_develop_b_20260101",
        branch_type="develop",
        lifecycle="active",
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
        develop_backfill_allowed=True,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


# --- classify tests ---


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
        (
            "+int ios_get_device_uuid(char *buf)\n"
            "+{\n"
            "+    if (buf == NULL)\n"
            "+    {\n"
            "+        return -1;\n"
            "+    }\n"
            "+    strcpy(buf, \"uuid\");\n"
            "+    return 0;\n"
            "+}\n"
        ),
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


def test_classify_extracts_cherry_pick_marker():
    result = classify_commit(
        "Backport fix (cherry picked from commit deadbeef)",
        ["plat/demo/demo.c"],
        [],
        "",
    )
    assert result.cherry_pick_from == "deadbeef"


# --- conclude tests ---


def test_missing_sha_without_files_not_need_sync():
    source = _bug_fix_analysis(
        changed_files=["plat/missing/feature.c"],
        symbols=["missing_fn"],
    )
    target = _target(
        files_on_target=set(),
        symbols_on_target={},
        fix_clearly_missing=False,
        has_source_sha=False,
    )
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind in {"OutOfScope", "ManualReview"}
    assert conclusion.kind != "NeedSync"


def test_same_sha_already_included_before_similarity():
    source = _bug_fix_analysis(sha="abcdef1234567890")
    target = _target(
        has_source_sha=True,
        file_similarity=0.99,
        fix_clearly_missing=True,
    )
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "AlreadyIncluded"
    assert conclusion.confidence == "high"
    assert any("相同提交" in item for item in conclusion.evidence)


def test_cherry_pick_already_included():
    source = _bug_fix_analysis(sha="abcdef1234567890")
    target = _target(
        target_commit_messages=[
            "Fix null deref (cherry picked from commit abcdef1234567890)",
        ],
        fix_clearly_missing=True,
    )
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "AlreadyIncluded"
    assert any("cherry-pick" in item.lower() for item in conclusion.evidence)


def test_same_issue_already_included():
    source = _bug_fix_analysis(issue_ids=["CQ12345"])
    target = _target(
        target_issue_ids={"CQ12345"},
        fix_clearly_missing=True,
    )
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "AlreadyIncluded"
    assert any("CQ12345" in item for item in conclusion.evidence)


def test_same_patch_id_already_included():
    source = _bug_fix_analysis(patch_id="patch-shared")
    target = _target(
        target_patch_ids={"patch-shared"},
        fix_clearly_missing=True,
    )
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "AlreadyIncluded"


def test_high_similarity_already_included_low_confidence():
    source = _bug_fix_analysis()
    target = _target(
        file_similarity=0.95,
        fix_clearly_missing=False,
    )
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "AlreadyIncluded"
    assert conclusion.confidence == "low"


def test_gray_zone_similarity_manual_review():
    source = _bug_fix_analysis()
    target = _target(
        file_similarity=0.70,
        fix_clearly_missing=False,
    )
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "ManualReview"


def test_feature_target_out_of_scope():
    source = _bug_fix_analysis()
    target = _target(branch_type="feature")
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "OutOfScope"


def test_need_sync_with_clear_anchor():
    source = _bug_fix_analysis()
    target = _target(
        fix_clearly_missing=True,
        has_source_sha=False,
    )
    conclusion = conclude_pair(
        source,
        target,
        similarity_high=0.90,
        similarity_low=0.50,
        develop_backfill_enabled=True,
    )
    assert conclusion.kind == "NeedSync"
    assert conclusion.confidence in {"high", "medium", "low"}


def test_function_renamed_manual_review_not_need_sync():
    source = _bug_fix_analysis()
    target = _target(
        function_renamed=True,
        fix_clearly_missing=True,
    )
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "ManualReview"
    assert conclusion.kind != "NeedSync"


def test_heuristic_only_weak_manual_review_bias():
    source = _bug_fix_analysis(recognition_source="heuristic:files+functions", issue_ids=[])
    target = _target(
        fix_clearly_missing=True,
        file_similarity=0.30,
    )
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind in {"ManualReview", "NeedSync"}
    if conclusion.kind == "NeedSync":
        assert conclusion.confidence == "low"


def test_cross_homologous_out_of_scope():
    source = _bug_fix_analysis()
    target = _target(in_same_homologous_set=False, cross_product_linked=False)
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "OutOfScope"


def test_eol_target_out_of_scope():
    source = _bug_fix_analysis()
    target = _target(lifecycle="eol")
    conclusion = conclude_pair(source, target, similarity_high=0.90, similarity_low=0.50)
    assert conclusion.kind == "OutOfScope"


# --- git_scan + integration ---


def test_git_scan_mini_repo_multi_develop_need_sync(tmp_path: Path):
    repo = tmp_path / "mini.git"
    meta = build_multi_develop_fix_repo(repo)

    assert file_exists(repo, meta["develop_b"], meta["file"]) is True
    target_content = show_file(repo, meta["develop_b"], meta["file"])
    assert target_content is not None
    assert "NULL == ptr" not in target_content

    source_content = show_file(repo, meta["develop_a"], meta["file"])
    assert source_content is not None
    assert "NULL == ptr" in source_content

    fix_patch_id = patch_id(repo, meta["fix_sha"])
    assert fix_patch_id is not None

    commits = commits_since(repo, meta["develop_a"], meta["base_sha"])
    assert meta["fix_sha"] in commits or meta["fix_sha"].startswith(commits[-1][:7])

    classification = classify_commit(
        "[BUG] CQ12345 Add null guard in demo_check",
        [meta["file"]],
        [meta["symbol"]],
        source_content or "",
    )
    assert classification.is_bug_fix is True

    source = CommitAnalysis(
        sha=meta["fix_sha"],
        message="[BUG] CQ12345 Add null guard in demo_check",
        changed_files=[meta["file"]],
        symbols=[meta["symbol"]],
        patch_text=source_content or "",
        patch_id=fix_patch_id,
        issue_ids=classification.issue_ids,
        recognition_source=classification.recognition_source,
        source_branch=meta["develop_a"],
        source_branch_type="develop",
        homologous_section="LineA",
        has_high_priority_rule=True,
    )
    target = TargetSnapshot(
        branch_name=meta["develop_b"],
        branch_type="develop",
        lifecycle="active",
        has_source_sha=False,
        target_commit_messages=[],
        target_patch_ids=set(),
        target_issue_ids=set(),
        files_on_target={meta["file"]},
        symbols_on_target={meta["symbol"]: True},
        file_similarity=0.20,
        fix_clearly_missing=True,
        function_renamed=False,
        in_same_homologous_set=True,
        develop_backfill_allowed=True,
    )
    conclusion = conclude_pair(
        source,
        target,
        similarity_high=0.90,
        similarity_low=0.50,
        develop_backfill_enabled=True,
    )
    assert conclusion.kind == "NeedSync"
    assert any(meta["file"] in item or meta["symbol"] in item for item in conclusion.evidence)
