from __future__ import annotations

import re

from bsa.agents.base import ConflictContext, LLMClient, LLMUnavailable
from bsa.domain.models import CommitInfo, ConflictResolution
from bsa.executor.exceptions import InfrastructureError, SafetyViolation
from bsa.git.service import GitService
from bsa.rules.safety import SafetyEnforcer

_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_NO_NEWLINE = "\\ No newline at end of file"


def _diff_files(diff_text: str) -> dict[str, str]:
    """Split a git-format unified diff into per-target-path patches."""
    patches: dict[str, str] = {}
    current: str | None = None
    parts: list[str] = []
    for line in diff_text.split("\n"):
        if line.startswith("diff --git "):
            if current is not None:
                patches[current] = "\n".join(parts)
            match = re.search(r" b/(.+)$", line)
            current = match.group(1).strip() if match is not None else None
            parts = []
        else:
            parts.append(line)
    if current is not None:
        patches[current] = "\n".join(parts)
    return patches


def _apply_patch(content: str, patch: str) -> str:
    """Apply a single-file unified patch to ``content``, returning the result.

    Raises ``ValueError`` if a hunk's old-side lines do not match the current
    content at the expected position (stale/misnumbered diff).
    """
    lines = content.split("\n")
    out: list[str] = []
    src = 1
    for start, body in _parse_hunks(patch):
        while src < start:
            out.append(lines[src - 1] if src <= len(lines) else "")
            src += 1
        _verify_hunk(lines, start, body)
        for line in body:
            if line.startswith("-"):
                src += 1
            elif line.startswith("+"):
                out.append(line[1:])
            else:
                out.append(lines[src - 1] if src <= len(lines) else "")
                src += 1
    while src <= len(lines):
        out.append(lines[src - 1])
        src += 1
    return "\n".join(out)


def _verify_hunk(lines: list[str], start: int, body: list[str]) -> None:
    """Fail fast when a hunk's context/removed lines mismatch the file."""
    pos = start
    for line in body:
        if line.startswith(("-", " ")):
            expected = lines[pos - 1] if pos <= len(lines) else ""
            if line[1:] != expected:
                raise ValueError(
                    f"hunk context mismatch at line {pos}: "
                    f"patch has {line[1:]!r}, file has {expected!r}"
                )
            pos += 1


def _parse_hunks(patch: str) -> list[tuple[int, list[str]]]:
    hunks: list[tuple[int, list[str]]] = []
    body: list[str] | None = None
    for line in patch.split("\n"):
        match = _HUNK_RE.match(line)
        if match is not None:
            body = []
            hunks.append((int(match.group(1)), body))
            continue
        if line.startswith(_NO_NEWLINE):
            continue
        if body is not None and line.startswith((" ", "+", "-")):
            body.append(line)
    return hunks


def _modified_paths(status_text: str) -> set[str]:
    """Extract worktree-modified paths from ``git status --porcelain`` output.

    Only the worktree column (Y, index 1) counts. A cleanly-applied cherry-pick
    stages files in the index column (X, index 0), which must not be treated as
    worktree modifications.
    """
    paths: set[str] = set()
    for line in status_text.splitlines():
        if len(line) < 4:
            continue
        if line[1] == " ":
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ")[-1]
        paths.add(path)
    return paths


class ConflictAgent:
    """Resolve cherry-pick conflicts by reasoned, audited file edits.

    Loop per attempt: snapshot conflict files -> LLM analysis -> apply the
    unified diff from ConflictResolution to the conflict files -> verify
    (markers gone, diff-check clean, only conflict files touched, red-lines
    clear) -> stage on success, else restore the snapshot and retry.
    """

    def __init__(
        self,
        llm: LLMClient,
        git: GitService,
        safety: SafetyEnforcer,
        max_attempts: int = 3,
        target_branch: str | None = None,
    ) -> None:
        self._llm = llm
        self._git = git
        self._safety = safety
        self._max_attempts = max_attempts
        self._target_branch = target_branch

    def resolve(
        self, commit: CommitInfo, conflict_files: list[str]
    ) -> ConflictResolution | None:
        """Return the resolution, or None for manual review / fail-fast.

        None is returned when: a conflict file hits a forbidden path (manual),
        the LLM is unavailable (manual), or max_attempts rounds fail (fail-fast).
        """
        try:
            self._safety.check_editable(conflict_files)
        except SafetyViolation:
            return None
        for _ in range(self._max_attempts):
            snapshots = self._git.snapshot(conflict_files)
            try:
                resolution = self._attempt(commit, conflict_files)
            except LLMUnavailable:
                return None
            if resolution is not None:
                return resolution
            self._rollback(conflict_files, snapshots)
        return None

    def _attempt(
        self, commit: CommitInfo, conflict_files: list[str]
    ) -> ConflictResolution | None:
        markers: dict[str, str] = {}
        for rel in conflict_files:
            path = self._git.repo_path / rel
            if path.is_file():
                markers[rel] = path.read_text(encoding="utf-8")
        ctx = ConflictContext(
            commit=commit,
            conflict_files=conflict_files,
            conflict_markers=markers,
            source_branch=commit.source_branch,
            target_branch=self._target_branch or commit.source_branch,
            worktree=self._git.repo_path,
        )
        resolution = self._llm.solve_conflict(ctx)
        if not self._apply(resolution, conflict_files):
            return None
        try:
            if not self._verified(conflict_files):
                return None
            self._git.stage(conflict_files)
        except InfrastructureError:
            return None
        return resolution

    def _apply(
        self, resolution: ConflictResolution, conflict_files: list[str]
    ) -> bool:
        allowed = set(conflict_files)
        if not set(resolution.files) <= allowed:
            return False
        try:
            for path, patch in _diff_files(resolution.diff).items():
                if path not in allowed:
                    return False
                target = self._git.repo_path / path
                current = target.read_text(encoding="utf-8") if target.is_file() else ""
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(_apply_patch(current, patch), encoding="utf-8")
        except Exception:
            return False
        return True

    def _verified(self, conflict_files: list[str]) -> bool:
        for rel in conflict_files:
            path = self._git.repo_path / rel
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith(_MARKERS):
                    return False
        if not self._git.diff_check():
            return False
        if not _modified_paths(self._git.status()) <= set(conflict_files):
            return False
        try:
            self._safety.check_editable(conflict_files)
        except SafetyViolation:
            return False
        return True

    def _rollback(self, conflict_files: list[str], snapshots: dict[str, str]) -> None:
        self._git.restore(snapshots)
        for rel in conflict_files:
            if rel not in snapshots:
                path = self._git.repo_path / rel
                if path.is_file():
                    path.unlink()
