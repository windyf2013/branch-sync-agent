from __future__ import annotations

import pytest

from bsa.rules.branch_md import first_occurrence_sections, parse_branch_md
from bsa.rules.build_rules import (
    BuildConfigError,
    BuildRules,
    BuildType,
    load_build_rules,
    resolve_build_models,
)

_BRANCH_MD = """\
# 所有待审核分支
## 1 RCIOS代码库
- 路径：rcios

### 1.1 组网产品分支
- br_v4.33_5200_CU_develop_20260518
- br_v4.33_develop_20260702

### 1.2 FTTR-B产品分支
- br_v4.34_develop_20260130
"""

_RULES_YAML = """\
build_types:
  "5200":
    script: "RTL9617C_build.sh"
    product: "5200"
  "5200B":
    script: "X86.sh"
    product: "5200B"
build_models_by_section:
  "组网产品分支":
    - "5200"
    - "5200B"
"""


def _rules() -> BuildRules:
    return load_build_rules_from_text(_RULES_YAML)


def load_build_rules_from_text(text: str) -> BuildRules:
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(text)
        path = Path(f.name)
    try:
        return load_build_rules(path)
    finally:
        path.unlink()


def _doc():
    return parse_branch_md(_BRANCH_MD)


def test_load_build_rules_parses_yaml():
    rules = _rules()
    assert rules.build_types["5200"].script == "RTL9617C_build.sh"
    assert rules.build_types["5200B"].product == "5200B"
    assert rules.build_models_by_section["组网产品分支"] == ["5200", "5200B"]


def test_resolve_build_models_matches_section_with_numbering_prefix():
    rules = _rules()
    sections = first_occurrence_sections(_doc())
    assert resolve_build_models(sections, "br_v4.33_develop_20260702", rules) == [
        "5200",
        "5200B",
    ]
    assert resolve_build_models(sections, "br_v4.33_5200_CU_develop_20260518", rules) == [
        "5200",
        "5200B",
    ]


def test_resolve_build_models_branch_missing_raises():
    rules = _rules()
    sections = first_occurrence_sections(_doc())
    with pytest.raises(BuildConfigError, match="branch.md"):
        resolve_build_models(sections, "br_unknown_develop_20260101", rules)


def test_resolve_build_models_unconfigured_section_raises():
    rules = _rules()
    sections = first_occurrence_sections(_doc())
    with pytest.raises(BuildConfigError, match="FTTR-B"):
        resolve_build_models(sections, "br_v4.34_develop_20260130", rules)


def test_resolve_build_models_ambiguous_section_raises():
    """一个 section 命中多个配置 key 时必须报错，绝不按书写顺序取首个。

    cron 侧 section 是产品键（如 "4.34"），完整清单里若同时存在
    "4.34 主分支" 与 "4.34产品主线分支"，双向子串会同时命中两者。
    """
    rules = BuildRules(
        build_types={"a": BuildType(script="a.sh", product="a")},
        build_models_by_section={
            "4.34 主分支": ["a"],
            "4.34产品主线分支": ["a"],
        },
    )
    with pytest.raises(BuildConfigError, match="同时命中"):
        resolve_build_models({"br_x": "4.34"}, "br_x", rules)


def test_resolve_build_models_single_match_still_returns():
    """非歧义场景不受影响：cron 产品键只命中 cron 键。"""
    rules = BuildRules(
        build_types={"a": BuildType(script="a.sh", product="a")},
        build_models_by_section={
            "4.34 主分支": ["a"],
            "组网产品分支": ["b"],
        },
    )
    assert resolve_build_models({"br_x": "4.34"}, "br_x", rules) == ["a"]
    assert resolve_build_models({"br_y": "1.1 组网产品分支"}, "br_y", rules) == ["b"]
