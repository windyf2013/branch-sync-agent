from branch_maintenance.branch_md import parse_branch_md, resolve_branch_type
from branch_maintenance.matrix import build_matrix


def test_lowest_wins_personal():
    assert resolve_branch_type("br_v4_develop_personal_20260101") == "personal"


def test_feature_plus_fix():
    assert resolve_branch_type("br_v4_feature_fix_20260101") == "feature"


def test_psersonal_typo_maps_to_personal():
    assert resolve_branch_type("br_v4_psersonal_20260101") == "personal"


def test_develop_plus_feature_lowest_wins_feature():
    assert resolve_branch_type("br_v4_develop_feature_20260101") == "feature"


def test_unknown_when_no_markers():
    assert resolve_branch_type("br_v4_LineA_main_20260101") == "unknown"


def test_multi_develop_targets():
    text = """# 所有待审核分支
## LineA
- br_v4_LineA_develop_a_20260101
- br_v4_LineA_develop_b_20260101
- br_v4_LineA_release_20260101
"""
    doc = parse_branch_md(text)
    sets = build_matrix(doc, develop_backfill_enabled=True)
    assert len(sets) == 1
    names = {b.name for b in sets[0].need_sync_targets}
    assert "br_v4_LineA_develop_a_20260101" in names
    assert "br_v4_LineA_develop_b_20260101" in names
    assert "br_v4_LineA_release_20260101" in names


def test_develop_backfill_disabled_excludes_develop_targets():
    text = """# title
## LineA
- br_v4_LineA_develop_a_20260101
- br_v4_LineA_develop_b_20260101
- br_v4_LineA_release_20260101
"""
    doc = parse_branch_md(text)
    sets = build_matrix(doc, develop_backfill_enabled=False)
    names = {b.name for b in sets[0].need_sync_targets}
    assert "br_v4_LineA_release_20260101" in names
    assert "br_v4_LineA_develop_a_20260101" not in names
    assert "br_v4_LineA_develop_b_20260101" not in names


def test_feature_and_personal_not_need_sync_targets():
    text = """# title
## LineA
- br_v4_LineA_develop_20260101
- br_v4_LineA_feature_20260101
- br_v4_LineA_personal_20260101
- br_v4_LineA_release_20260101
"""
    doc = parse_branch_md(text)
    sets = build_matrix(doc, develop_backfill_enabled=True)
    target_names = {b.name for b in sets[0].need_sync_targets}
    source_names = {b.name for b in sets[0].sources}
    assert "br_v4_LineA_feature_20260101" not in target_names
    assert "br_v4_LineA_personal_20260101" not in target_names
    assert "br_v4_LineA_feature_20260101" not in source_names
    assert "br_v4_LineA_personal_20260101" not in source_names
    assert "br_v4_LineA_develop_20260101" in source_names
    assert "br_v4_LineA_release_20260101" in target_names


def test_duplicate_branch_doc_bug():
    text = """# 所有待审核分支
## SectionA
- br_v4_dup_release_20260101
## SectionB
- br_v4_dup_release_20260101
"""
    doc = parse_branch_md(text)
    assert len(doc.duplicates) == 1
    bug = doc.duplicates[0]
    assert bug.name == "br_v4_dup_release_20260101"
    assert bug.count == 2
    assert bug.sections == ["SectionA", "SectionB"]


def test_duplicate_uses_first_section_for_matrix():
    text = """# title
## SectionA
- br_v4_dup_release_20260101
## SectionB
- br_v4_dup_release_20260101
"""
    doc = parse_branch_md(text)
    sets = build_matrix(doc, develop_backfill_enabled=True)
    assert len(sets) == 2
    section_a_names = {b.name for b in sets[0].sources + sets[0].need_sync_targets}
    section_b_names = {b.name for b in sets[1].sources + sets[1].need_sync_targets}
    assert "br_v4_dup_release_20260101" in section_a_names
    assert "br_v4_dup_release_20260101" not in section_b_names


def test_cross_section_isolation():
    text = """# title
## ProductA
- br_v4_ProductA_develop_20260101
- br_v4_ProductA_release_20260101
## ProductB
- br_v4_ProductB_develop_20260101
- br_v4_ProductB_release_20260101
"""
    doc = parse_branch_md(text)
    sets = build_matrix(doc, develop_backfill_enabled=True)
    assert len(sets) == 2
    assert sets[0].section == "ProductA"
    assert sets[1].section == "ProductB"
    product_a_names = {b.name for b in sets[0].need_sync_targets}
    product_b_names = {b.name for b in sets[1].need_sync_targets}
    assert "br_v4_ProductB_develop_20260101" not in product_a_names
    assert "br_v4_ProductA_develop_20260101" not in product_b_names


def test_parse_branch_md_sections():
    text = """# Doc title
## LineOne
- br_a
## LineTwo
- br_b
"""
    doc = parse_branch_md(text)
    assert doc.title == "Doc title"
    assert len(doc.sections) == 2
    assert doc.sections[0].title == "LineOne"
    assert doc.sections[0].branches[0].name == "br_a"
    assert doc.sections[1].title == "LineTwo"
    assert doc.sections[1].branches[0].name == "br_b"
