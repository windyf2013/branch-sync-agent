from __future__ import annotations

import os
import re
from pathlib import Path

from bsa.agents.base import (
    BuildAttribution,
    BuildErrorContext,
    LLMClient,
    LLMUnavailable,
)
from bsa.agents.conflict import _apply_patch, _diff_files
from bsa.build.runner import BuildRunner
from bsa.domain.models import CommitInfo
from bsa.executor.exceptions import InfrastructureError, SafetyViolation
from bsa.git.service import GitService
from bsa.rules.safety import SafetyEnforcer

_ERROR_FILE_RE = re.compile(
    r"(?P<path>[^ \t:]+\.(?:c|h|cpp|cc|hpp|cxx|s|S|asm|mk)):(?P<line>\d+)"
)


def _normalize_error_path(
    path: str, repo_path: Path | None, mount_prefix: str | None
) -> str:
    """Map an error-log file path to a repo-relative path.

    Docker build logs yield absolute container paths (e.g.
    ``/workspace/rcios/build/../src/dhcp.c``) while ``changed_files`` are
    repo-relative (``src/dhcp.c``). Strip a known prefix (the docker mount
    point first, else the host repo root) and resolve ``..`` segments so the
    scope gate and signature comparison use consistent paths.
    """
    path = path.strip()
    if not path:
        return path
    norm = os.path.normpath(path)
    if not os.path.isabs(norm):
        return norm
    prefixes: list[str] = []
    if mount_prefix:
        prefixes.append(os.path.normpath(mount_prefix))
    if repo_path is not None:
        prefixes.append(os.path.normpath(os.fspath(repo_path)))
    for prefix in prefixes:
        if norm == prefix:
            return "."
        if norm.startswith(prefix + os.sep):
            return norm[len(prefix) + 1 :]
    return norm.lstrip(os.sep)


def _normalize_error_text(
    text: str, repo_path: Path | None, mount_prefix: str | None
) -> str:
    """Rewrite file paths inside an error line to repo-relative form."""

    def _repl(match: re.Match[str]) -> str:
        rel = _normalize_error_path(match.group("path"), repo_path, mount_prefix)
        return f"{rel}:{match.group('line')}"

    return _ERROR_FILE_RE.sub(_repl, text)


def _error_files(
    errors: list[str],
    *,
    repo_path: Path | None = None,
    mount_prefix: str | None = None,
) -> list[str]:
    """Extract repo-relative file paths (path:line) from compile error blocks."""
    found: list[str] = []
    for block in errors:
        for line in block.splitlines():
            match = _ERROR_FILE_RE.search(line)
            if match is not None:
                found.append(
                    _normalize_error_path(match.group("path"), repo_path, mount_prefix)
                )
    seen: set[str] = set()
    unique: list[str] = []
    for path in found:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _error_signatures(
    errors: list[str],
    *,
    repo_path: Path | None = None,
    mount_prefix: str | None = None,
) -> set[str]:
    """Normalized error-line texts, for deterministic error comparison."""
    sigs: set[str] = set()
    for block in errors:
        for line in block.splitlines():
            if re.search(r"\berror:", line, re.IGNORECASE):
                norm = _normalize_error_text(line, repo_path, mount_prefix)
                sigs.add(" ".join(norm.split()))
    return sigs


class BuildAgent:
    """Attribute compile errors to one of four categories and auto-fix only
    errors introduced by the current cherry-pick (decision 5/30/32)."""

    def __init__(
        self,
        llm: LLMClient,
        git: GitService,
        runner: BuildRunner,
        safety: SafetyEnforcer,
        max_attempts: int = 3,
        *,
        target_branch: str | None = None,
        module: str | None = None,
        log_path: Path | None = None,
        docker_mount_workspace: str | None = None,
    ) -> None:
        self._llm = llm
        self._git = git
        self._runner = runner
        self._safety = safety
        self._max_attempts = max_attempts
        self._target_branch = target_branch
        self._module = module
        self._log_path = log_path
        self._docker_mount_workspace = docker_mount_workspace

    def fix(self, commit: CommitInfo, errors: list[str], model: str) -> BuildAttribution:
        ctx = self._context(commit, errors, model)
        try:
            attribution = self._llm.classify_build_error(ctx)
        except LLMUnavailable:
            return BuildAttribution(
                category="unresolvable",
                reason="LLM 不可用，无法归因",
                files_to_fix=[],
            )
        if attribution.category == "pre_existing":
            attribution = self._verify_pre_existing(commit, errors, model, attribution)
            if attribution.category == "pre_existing":
                return attribution
        if attribution.category != "introduced_by_commit":
            return attribution
        if not attribution.files_to_fix:
            return attribution
        try:
            self._safety.check_editable(attribution.files_to_fix)
        except SafetyViolation:
            return attribution
        self._fix_loop(commit, errors, model, attribution)
        return attribution

    def _context(self, commit: CommitInfo, errors: list[str], model: str) -> BuildErrorContext:
        return BuildErrorContext(
            commit=commit,
            model=model,
            errors=errors,
            log_path=self._log_path or Path(f"build_{model}.log"),
            worktree=self._git.repo_path,
        )

    def _verify_pre_existing(
        self,
        commit: CommitInfo,
        errors: list[str],
        model: str,
        attribution: BuildAttribution,
    ) -> BuildAttribution:
        """Deterministic check (decision 30): revert the commit's files to the
        target tip, compile, and compare error signatures. The original tip
        reproducing the same errors confirms pre_existing; otherwise the
        errors are treated as introduced by the commit and enter the fix loop.
        """
        if not self._target_branch or not commit.changed_files:
            return attribution
        snap = self._git.snapshot(commit.changed_files)
        try:
            ref, _ = self._git.branch_tip(self._target_branch)
            for rel in commit.changed_files:
                original = self._git.show_file(ref, rel)
                path = self._git.repo_path / rel
                if original is None:
                    if path.is_file():
                        path.unlink()
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(original, encoding="utf-8")
            original_errors = self._runner.build_commit(
                self._git.repo_path, model, clean=False, module=self._module
            ).errors
        except InfrastructureError:
            return attribution
        finally:
            self._git.restore(snap)
        repo_path = self._git.repo_path
        mount = self._docker_mount_workspace
        current_sigs = _error_signatures(errors, repo_path=repo_path, mount_prefix=mount)
        original_sigs = _error_signatures(
            original_errors, repo_path=repo_path, mount_prefix=mount
        )
        if original_sigs & current_sigs:
            return BuildAttribution(
                category="pre_existing",
                reason="原 tip 编译复现相同错误，确定为原分支已有",
                files_to_fix=[],
            )
        return BuildAttribution(
            category="introduced_by_commit",
            reason="原 tip 未复现错误，判定为本 commit 引入",
            files_to_fix=attribution.files_to_fix,
        )

    def _fix_loop(
        self,
        commit: CommitInfo,
        errors: list[str],
        model: str,
        attribution: BuildAttribution,
    ) -> bool:
        """Snapshot -> LLM fix -> apply -> rebuild; restore and retry on failure."""
        allowed = set(commit.changed_files) | set(
            _error_files(
                errors,
                repo_path=self._git.repo_path,
                mount_prefix=self._docker_mount_workspace,
            )
        )
        files_to_fix = list(dict.fromkeys(attribution.files_to_fix))
        if not set(files_to_fix) <= allowed:
            return False
        for _ in range(self._max_attempts):
            snap = self._git.snapshot(files_to_fix)
            try:
                if self._attempt_fix(commit, errors, model, files_to_fix):
                    return True
            except LLMUnavailable:
                self._git.restore(snap)
                return False
            self._git.restore(snap)
        return False

    def _attempt_fix(
        self,
        commit: CommitInfo,
        errors: list[str],
        model: str,
        files_to_fix: list[str],
    ) -> bool:
        fix = self._llm.fix_build_error(self._context(commit, errors, model))
        if not set(fix.files) <= set(files_to_fix):
            return False
        try:
            for path, patch in _diff_files(fix.diff).items():
                if path not in files_to_fix:
                    return False
                target = self._git.repo_path / path
                current = target.read_text(encoding="utf-8") if target.is_file() else ""
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(_apply_patch(current, patch), encoding="utf-8")
        except Exception:
            return False
        try:
            result = self._runner.build_commit(
                self._git.repo_path, model, clean=False, module=self._module
            )
        except InfrastructureError:
            return False
        return self._runner.is_success(result)
