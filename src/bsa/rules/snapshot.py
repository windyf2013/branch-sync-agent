from __future__ import annotations

import difflib
import re
from typing import Protocol

from bsa.rules.classify import classify_commit
from bsa.rules.conclude import CommitAnalysis, TargetSnapshot

SYMBOL_RE = re.compile(r"^\+.*?\b([A-Za-z_]\w*)\s*\(", re.MULTILINE)
FIX_GUARD_RE = re.compile(r"NULL\s*==|return\s+-1", re.IGNORECASE)

_TARGET_HISTORY_LIMIT = 100
_TARGET_PATCH_LIMIT = 40
_MAX_SIMILARITY_CHARS = 20000
_EVER_WINDOW = ("1970-01-01", "2100-01-01")


class GitLike(Protocol):
    """Structural subset of GitService the snapshot builder needs."""

    def file_exists(self, ref: str, path: str) -> bool: ...

    def show_file(self, ref: str, path: str) -> str | None: ...

    def is_ancestor(self, sha: str, ref: str) -> bool: ...

    def commits_in_window(self, since: str, until: str, ref: str) -> list[str]: ...

    def commit_metadata(self, sha: str) -> tuple[str, str, str]: ...

    def patch_id(self, sha: str) -> str | None: ...

    def changed_files(self, sha: str) -> list[str]: ...


def extract_symbols(patch_text: str) -> list[str]:
    seen: set[str] = set()
    symbols: list[str] = []
    for match in SYMBOL_RE.finditer(patch_text):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            symbols.append(name)
    return symbols


def _file_similarity(source_text: str | None, target_text: str | None) -> float | None:
    if source_text is None or target_text is None:
        return None
    if not source_text and not target_text:
        return 1.0
    if source_text == target_text:
        return 1.0
    left = (
        source_text
        if len(source_text) <= _MAX_SIMILARITY_CHARS
        else source_text[:_MAX_SIMILARITY_CHARS]
    )
    right = (
        target_text
        if len(target_text) <= _MAX_SIMILARITY_CHARS
        else target_text[:_MAX_SIMILARITY_CHARS]
    )
    return difflib.SequenceMatcher(None, left, right).ratio()


def _fix_clearly_missing(source_text: str | None, target_text: str | None) -> bool:
    if source_text is None or target_text is None:
        return False
    return bool(FIX_GUARD_RE.search(source_text)) and not bool(FIX_GUARD_RE.search(target_text))


def _symbols_on_target(target_text: str | None, symbols: list[str]) -> dict[str, bool]:
    if target_text is None:
        return {symbol: False for symbol in symbols}
    return {symbol: symbol in target_text for symbol in symbols}


def _function_renamed(source_symbols: list[str], target_symbols: dict[str, bool]) -> bool:
    if not source_symbols:
        return False
    if all(target_symbols.get(symbol, False) for symbol in source_symbols):
        return False
    return any(target_symbols.values())


def _target_issue_ids(messages: list[str]) -> set[str]:
    ids: set[str] = set()
    for message in messages:
        ids.update(classify_commit(message, ["placeholder"], [], "").issue_ids)
    return ids


def build_target_snapshot(
    *,
    source: CommitAnalysis,
    target_branch: str,
    target_branch_type: str,
    git: GitLike,
    target_ref: str,
    source_ref: str,
    similarity_high: float = 0.90,
) -> TargetSnapshot:
    """Build the complete TargetSnapshot for one source commit vs one target branch."""
    _ = similarity_high
    snapshot = TargetSnapshot(
        branch_name=target_branch,
        branch_type=target_branch_type,
        has_source_sha=git.is_ancestor(source.sha, target_ref),
        in_same_homologous_set=True,
    )
    if snapshot.has_source_sha:
        return snapshot

    shas = git.commits_in_window(*_EVER_WINDOW, target_ref)
    recent = shas[-_TARGET_HISTORY_LIMIT:]
    snapshot.target_commit_messages = [git.commit_metadata(sha)[2] for sha in recent]
    snapshot.target_issue_ids = _target_issue_ids(snapshot.target_commit_messages)

    source_files = set(source.changed_files)
    recent_shas = shas[-_TARGET_PATCH_LIMIT:]
    snapshot.target_patch_ids = {
        pid
        for sha in recent_shas
        if set(git.changed_files(sha)) & source_files
        if (pid := git.patch_id(sha))
    }

    snapshot.files_on_target = {
        path for path in source.changed_files if git.file_exists(target_ref, path)
    }

    source_texts = [git.show_file(source_ref, path) for path in source.changed_files]
    target_texts = [git.show_file(target_ref, path) for path in source.changed_files]
    similarities = [
        value
        for src, tgt in zip(source_texts, target_texts, strict=False)
        if (value := _file_similarity(src, tgt)) is not None
    ]
    if similarities:
        snapshot.file_similarity = sum(similarities) / len(similarities)

    primary_source_text = source_texts[0] if source_texts else None
    primary_target_text = target_texts[0] if target_texts else None
    snapshot.symbols_on_target = _symbols_on_target(primary_target_text, source.symbols)
    snapshot.fix_clearly_missing = _fix_clearly_missing(
        primary_source_text, primary_target_text
    )
    snapshot.function_renamed = _function_renamed(source.symbols, snapshot.symbols_on_target)
    return snapshot
