from __future__ import annotations

from dataclasses import dataclass, field

from branch_maintenance.branch_md import (
    SOURCE_TYPES,
    TARGET_TYPES,
    TARGET_TYPES_WITH_DEVELOP_BACKFILL,
    BranchMdDocument,
    BranchRef,
    first_occurrence_sections,
)


@dataclass
class HomologousSet:
    section: str
    sources: list[BranchRef] = field(default_factory=list)
    need_sync_targets: list[BranchRef] = field(default_factory=list)


def _eligible_branches(
    section_branches: list[BranchRef],
    *,
    first_seen: dict[str, str],
    section_title: str,
) -> list[BranchRef]:
    eligible: list[BranchRef] = []
    for branch in section_branches:
        if first_seen.get(branch.name) != section_title:
            continue
        eligible.append(branch)
    return eligible


def build_matrix(
    doc: BranchMdDocument,
    *,
    develop_backfill_enabled: bool,
) -> list[HomologousSet]:
    first_seen = first_occurrence_sections(doc)
    target_types = (
        TARGET_TYPES_WITH_DEVELOP_BACKFILL
        if develop_backfill_enabled
        else TARGET_TYPES
    )

    homologous_sets: list[HomologousSet] = []
    for section in doc.sections:
        eligible = _eligible_branches(
            section.branches,
            first_seen=first_seen,
            section_title=section.title,
        )

        sources = [branch for branch in eligible if branch.branch_type in SOURCE_TYPES]
        need_sync_targets = [
            branch for branch in eligible if branch.branch_type in target_types
        ]

        homologous_sets.append(
            HomologousSet(
                section=section.title,
                sources=sources,
                need_sync_targets=need_sync_targets,
            )
        )

    return homologous_sets
