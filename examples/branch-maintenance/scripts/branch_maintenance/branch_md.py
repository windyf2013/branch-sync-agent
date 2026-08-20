from __future__ import annotations

import re
from dataclasses import dataclass, field

TYPE_PRIORITY: dict[str, int] = {
    "develop": 1,
    "release": 2,
    "feature": 3,
    "fix": 3,
    "personal": 4,
}

SOURCE_TYPES = frozenset({"develop", "release", "fix"})
TARGET_TYPES = frozenset({"release", "fix"})
TARGET_TYPES_WITH_DEVELOP_BACKFILL = frozenset({"release", "fix", "develop"})


@dataclass
class BranchRef:
    name: str
    branch_type: str
    section: str


@dataclass
class BranchSection:
    title: str
    branches: list[BranchRef] = field(default_factory=list)


@dataclass
class DocBug:
    name: str
    count: int
    sections: list[str]


@dataclass
class BranchMdDocument:
    title: str | None
    sections: list[BranchSection]
    duplicates: list[DocBug] = field(default_factory=list)


def _normalize_branch_name_for_type(name: str) -> str:
    return name.lower().replace("psersonal", "personal")


def _detect_markers(normalized: str) -> list[str]:
    markers: list[str] = []
    if "develop" in normalized:
        markers.append("develop")
    if "release" in normalized:
        markers.append("release")
    if "feature" in normalized:
        markers.append("feature")
    if "fix" in normalized:
        markers.append("fix")
    if "personal" in normalized:
        markers.append("personal")
    return markers


def resolve_branch_type(name: str) -> str:
    normalized = _normalize_branch_name_for_type(name)
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
    if not branch_name:
        return None
    return branch_name


def _collect_duplicates(sections: list[BranchSection]) -> list[DocBug]:
    occurrence_map: dict[str, list[str]] = {}
    for section in sections:
        for branch in section.branches:
            occurrence_map.setdefault(branch.name, []).append(section.title)

    duplicates: list[DocBug] = []
    for branch_name, section_titles in occurrence_map.items():
        if len(section_titles) > 1:
            duplicates.append(
                DocBug(
                    name=branch_name,
                    count=len(section_titles),
                    sections=list(section_titles),
                )
            )
    return duplicates


def _parse_path_bullet(line: str) -> str | None:
    branch_name = _strip_bullet_branch(line)
    if branch_name is None:
        return None
    for prefix in ("路径：", "路径:", "path:", "Path:", "PATH:"):
        if branch_name.startswith(prefix):
            return branch_name[len(prefix) :].strip()
    return None


@dataclass
class RepoBlock:
    title: str
    path: str
    sections: list[BranchSection] = field(default_factory=list)


@dataclass
class BranchInventory:
    title: str | None
    repos: list[RepoBlock]


def looks_like_inventory(text: str) -> bool:
    """Return True when branch.md uses multi-repo inventory (路径 / ###)."""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("### "):
            return True
        if _parse_path_bullet(line) is not None:
            return True
    return False


def parse_branch_inventory(text: str) -> BranchInventory:
    """Parse multi-repo inventory: ## repo + 路径 + ### product-line sections."""
    title: str | None = None
    repos: list[RepoBlock] = []
    current_repo: RepoBlock | None = None
    current_section: BranchSection | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("# ") and not line.startswith("## "):
            title = line[2:].strip()
            continue

        if line.startswith("### "):
            if current_repo is None:
                continue
            current_section = BranchSection(title=line[4:].strip())
            current_repo.sections.append(current_section)
            continue

        if line.startswith("## "):
            current_repo = RepoBlock(title=line[3:].strip(), path="")
            repos.append(current_repo)
            current_section = None
            continue

        path_value = _parse_path_bullet(line)
        if path_value is not None and current_repo is not None:
            current_repo.path = path_value
            continue

        branch_name = _strip_bullet_branch(line)
        if branch_name is None or current_section is None:
            continue

        branch_ref = BranchRef(
            name=branch_name,
            branch_type=resolve_branch_type(branch_name),
            section=current_section.title,
        )
        current_section.branches.append(branch_ref)

    return BranchInventory(title=title, repos=repos)


def inventory_repo_to_document(repo: RepoBlock) -> BranchMdDocument:
    """Convert one inventory repo block into a homologous-matrix document."""
    sections = list(repo.sections)
    return BranchMdDocument(
        title=repo.title,
        sections=sections,
        duplicates=_collect_duplicates(sections),
    )


def parse_branch_md(text: str) -> BranchMdDocument:
    """Parse legacy or inventory branch.md into a single document.

    Inventory files should prefer parse_branch_inventory + inventory_repo_to_document
    per repo; this helper flattens all product-line sections for simple callers.
    """
    if looks_like_inventory(text):
        inventory = parse_branch_inventory(text)
        sections: list[BranchSection] = []
        for repo in inventory.repos:
            sections.extend(repo.sections)
        return BranchMdDocument(
            title=inventory.title,
            sections=sections,
            duplicates=_collect_duplicates(sections),
        )

    title: str | None = None
    sections: list[BranchSection] = []
    current_section: BranchSection | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("# ") and not line.startswith("## "):
            title = line[2:].strip()
            continue

        if line.startswith("## "):
            current_section = BranchSection(title=line[3:].strip())
            sections.append(current_section)
            continue

        branch_name = _strip_bullet_branch(line)
        if branch_name is None or current_section is None:
            continue

        branch_ref = BranchRef(
            name=branch_name,
            branch_type=resolve_branch_type(branch_name),
            section=current_section.title,
        )
        current_section.branches.append(branch_ref)

    return BranchMdDocument(
        title=title,
        sections=sections,
        duplicates=_collect_duplicates(sections),
    )


def first_occurrence_sections(doc: BranchMdDocument) -> dict[str, str]:
    first_seen: dict[str, str] = {}
    for section in doc.sections:
        for branch in section.branches:
            if branch.name not in first_seen:
                first_seen[branch.name] = section.title
    return first_seen
