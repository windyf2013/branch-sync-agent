from __future__ import annotations

import re

from pydantic import BaseModel, Field

TYPE_PRIORITY: dict[str, int] = {
    "develop": 1,
    "release": 2,
    "feature": 3,
    "fix": 3,
    "personal": 4,
}

SOURCE_TYPES = frozenset({"develop"})
TARGET_TYPES = frozenset({"release", "fix"})

_DATE_SUFFIX_RE = re.compile(r"_\d{8}$")


class BranchRef(BaseModel):
    name: str
    branch_type: str
    section: str


class BranchSection(BaseModel):
    title: str
    branches: list[BranchRef] = Field(default_factory=list)


class BranchMdDocument(BaseModel):
    title: str | None
    sections: list[BranchSection]
    duplicates: list[dict] = Field(default_factory=list)


class HomologousSet(BaseModel):
    section: str
    sources: list[BranchRef]
    need_sync_targets: list[BranchRef]


def _normalize_branch_name_for_type(name: str) -> str:
    return name.lower().replace("psersonal", "personal")


def _detect_markers(normalized: str) -> list[str]:
    markers: list[str] = []
    for marker in ("develop", "release", "feature", "fix", "personal"):
        if marker in normalized:
            markers.append(marker)
    return markers


def resolve_branch_type(name: str, branch_mapping: dict[str, str] | None = None) -> str:
    """Infer a branch type by name markers, or honor an explicit mapping (决策 19).

    ``branch_mapping`` (branch name → type) takes precedence over name-pattern
    inference: an exact branch-name match wins, then the longest prefix match.
    """
    normalized = _normalize_branch_name_for_type(name)
    if branch_mapping:
        mapped = branch_mapping.get(normalized) or branch_mapping.get(name)
        if mapped is not None:
            return mapped
        best: tuple[str, str] | None = None
        for key, branch_type in branch_mapping.items():
            if normalized.startswith(_normalize_branch_name_for_type(key)):
                if best is None or len(key) > len(best[0]):
                    best = (key, branch_type)
        if best is not None:
            return best[1]
    markers = _detect_markers(normalized)
    if not markers:
        return "unknown"
    if "feature" in markers and "fix" in markers:
        markers = [marker for marker in markers if marker != "fix"]
    return max(markers, key=lambda marker: TYPE_PRIORITY[marker])


def _strip_bullet_branch(line: str) -> str | None:
    match = re.match(r"^\s*-\s+(.+?)\s*$", line)
    if match is None:
        return None
    branch_name = match.group(1).strip()
    return branch_name or None


def _parse_path_bullet(line: str) -> str | None:
    branch_name = _strip_bullet_branch(line)
    if branch_name is None:
        return None
    for prefix in ("路径：", "路径:", "path:", "Path:", "PATH:"):
        if branch_name.startswith(prefix):
            return branch_name[len(prefix) :].strip()
    return None


def _looks_like_inventory(text: str) -> bool:
    """True when branch.md 使用多仓库清单（路径 / ### 产品线小节）。"""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("### "):
            return True
        if _parse_path_bullet(line) is not None:
            return True
    return False


def _collect_duplicates(sections: list[BranchSection]) -> list[dict]:
    occurrence_map: dict[str, list[str]] = {}
    for section in sections:
        for branch in section.branches:
            occurrence_map.setdefault(branch.name, []).append(section.title)

    duplicates: list[dict] = []
    for name, section_titles in occurrence_map.items():
        if len(section_titles) > 1:
            duplicates.append(
                {
                    "name": name,
                    "count": len(section_titles),
                    "sections": list(section_titles),
                }
            )
    return duplicates


def parse_branch_md(
    text: str, branch_mapping: dict[str, str] | None = None
) -> BranchMdDocument:
    title: str | None = None
    sections: list[BranchSection] = []
    current_section: BranchSection | None = None
    inventory = _looks_like_inventory(text)
    in_repo = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("# ") and not line.startswith("## "):
            title = line[2:].strip()
            continue

        if line.startswith("## "):
            if not inventory:
                current_section = BranchSection(title=line[3:].strip())
                sections.append(current_section)
            else:
                in_repo = True
                current_section = None
            continue

        if inventory:
            if line.startswith("### "):
                if in_repo:
                    current_section = BranchSection(title=line[4:].strip())
                    sections.append(current_section)
                continue
            if _parse_path_bullet(line) is not None:
                continue

        branch_name = _strip_bullet_branch(line)
        if branch_name is None or current_section is None:
            continue

        branch_ref = BranchRef(
            name=branch_name,
            branch_type=resolve_branch_type(branch_name, branch_mapping=branch_mapping),
            section=current_section.title,
        )
        current_section.branches.append(branch_ref)

    return BranchMdDocument(
        title=title,
        sections=sections,
        duplicates=_collect_duplicates(sections),
    )


def branch_prefix(name: str) -> str:
    """分支前缀 = 分支名去掉末尾 _YYYYMMDD 日期段（规范 3.2）。"""
    return _DATE_SUFFIX_RE.sub("", name)


def first_occurrence_sections(doc: BranchMdDocument) -> dict[str, str]:
    first_seen: dict[str, str] = {}
    for section in doc.sections:
        for branch in section.branches:
            if branch.name not in first_seen:
                first_seen[branch.name] = section.title
    return first_seen


def build_matrix(doc: BranchMdDocument) -> list[HomologousSet]:
    """同源矩阵（决策 35）。

    双源判定：同产品线 = branch.md section；同步方向 = develop 父 → 前缀下子
    release/fix 分支（child 分支名以 develop 分支前缀 + "_" 开头）。
    决策 20：feature/personal 全排除；决策 36：develop 只作源不作目标。
    """
    first_seen = first_occurrence_sections(doc)
    homologous_sets: list[HomologousSet] = []
    for section in doc.sections:
        branches = [
            branch
            for branch in section.branches
            if first_seen.get(branch.name) == section.title
        ]
        sources = [branch for branch in branches if branch.branch_type in SOURCE_TYPES]
        develop_prefixes = [branch_prefix(branch.name) for branch in sources]
        need_sync_targets = [
            branch
            for branch in branches
            if branch.branch_type in TARGET_TYPES
            and any(branch.name.startswith(prefix + "_") for prefix in develop_prefixes)
        ]
        homologous_sets.append(
            HomologousSet(
                section=section.title,
                sources=sources,
                need_sync_targets=need_sync_targets,
            )
        )
    return homologous_sets
