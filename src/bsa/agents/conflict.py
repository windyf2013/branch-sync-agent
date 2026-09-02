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


def _snapshot_bytes(conflict_files: list[str], *, git: GitService) -> dict[str, bytes]:
    """字节级快照冲突文件，回滚时字节无损恢复（不依赖 utf-8 假设）。"""
    snapshots: dict[str, bytes] = {}
    for rel in conflict_files:
        path = git.repo_path / rel
        if path.is_file():
            snapshots[rel] = path.read_bytes()
    return snapshots


def _decode_text(data: bytes) -> str | None:
    """把冲突文件字节解码为 Unicode 文本；非 UTF-8 编码统一转 UTF-8 合入。

    git 冲突是字节级、按行合并的，冲突标记是 ASCII。RCIOS 源文件常见 GBK 中文
    注释（非 UTF-8 字节）：utf-8 严格解码失败时用 gb18030（GBK 超集）兜底，二者
    解出的中文都是正确 Unicode。两者都失败时（真机：GBK 主体 + 冲突标记行里 git
    追加的 UTF-8 提交标题混编，任一种编码都解不了全文件）用 gb18030 + replace
    兜底——主体中文正确解码，仅冲突标记提示行里的 UTF-8 片段变 U+FFFD，而该标记
    行在解决时会被删除。含 NUL 字节才是真二进制（git 判定 binary 的依据），返回
    None 转人工。所有非 UTF-8 内容解码后都按 UTF-8 写回（合入统一编码）。
    """
    if b"\x00" in data:
        return None
    for enc in ("utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("gb18030", errors="replace")


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
        self.last_reason: str | None = None

    def resolve(
        self,
        commit: CommitInfo,
        conflict_files: list[str],
        *,
        git: GitService | None = None,
        target_branch: str | None = None,
    ) -> ConflictResolution | None:
        """Return the resolution, or None for manual review / fail-fast.

        ``git`` is the worktree-scoped GitService: all file I/O and git
        commands run against the target worktree, never the main repo (C2).
        None is returned when: a conflict file hits a forbidden path (manual),
        the LLM is unavailable (manual), or max_attempts rounds fail (fail-fast).
        """
        wgit = git or self._git
        tgt = target_branch or self._target_branch
        self.last_reason = None
        try:
            self._safety.check_editable(conflict_files)
        except SafetyViolation:
            return None
        # 冲突文件解码为 Unicode 文本（utf-8 → gb18030 → gb18030+replace 兜底），
        # 非 UTF-8 内容统一转 UTF-8 合入。真二进制（含 NUL 字节）才转人工；RCIOS
        # 常见 GBK 注释与 GBK+UTF-8 混编照常进入 LLM 解决，非冲突行字节无损保留。
        texts: dict[str, str] = {}
        for rel in conflict_files:
            path = wgit.repo_path / rel
            if not path.is_file():
                continue
            text = _decode_text(path.read_bytes())
            if text is None:
                self.last_reason = (
                    f"冲突文件 {rel} 疑似二进制（含 NUL 字节），"
                    "无法安全自动解决，转人工处理"
                )
                return None
            texts[rel] = text
        self._texts = texts
        for _ in range(self._max_attempts):
            snapshots = _snapshot_bytes(conflict_files, git=wgit)
            try:
                resolution = self._attempt(commit, conflict_files, git=wgit, target_branch=tgt)
            except LLMUnavailable:
                return None
            if resolution is not None:
                return resolution
            self._rollback(conflict_files, snapshots, git=wgit)
        return None

    def _attempt(
        self,
        commit: CommitInfo,
        conflict_files: list[str],
        *,
        git: GitService,
        target_branch: str | None,
    ) -> ConflictResolution | None:
        markers: dict[str, str] = {}
        for rel in conflict_files:
            if rel in self._texts:
                markers[rel] = self._texts[rel]
        ctx = ConflictContext(
            commit=commit,
            conflict_files=conflict_files,
            conflict_markers=markers,
            source_branch=commit.source_branch,
            target_branch=target_branch or commit.source_branch,
            worktree=git.repo_path,
        )
        resolution = self._llm.solve_conflict(ctx)
        if not self._apply(resolution, conflict_files, git=git):
            return None
        try:
            if not self._verified(conflict_files, git=git):
                return None
            git.stage(conflict_files)
        except InfrastructureError:
            return None
        return resolution

    def _apply(
        self,
        resolution: ConflictResolution,
        conflict_files: list[str],
        *,
        git: GitService,
    ) -> bool:
        allowed = set(conflict_files)
        if not set(resolution.files) <= allowed:
            return False
        try:
            for path, patch in _diff_files(resolution.diff).items():
                if path not in allowed:
                    return False
                target = git.repo_path / path
                current = self._texts.get(path, "")
                target.parent.mkdir(parents=True, exist_ok=True)
                # 统一按 UTF-8 写回合入：非 UTF-8 源文件（GBK / 混编）解码后在此转码。
                target.write_text(_apply_patch(current, patch), encoding="utf-8")
        except Exception:
            return False
        try:
            self._safety.check_edit_scale(resolution.diff)
        except SafetyViolation:
            return False
        return True

    def _verified(self, conflict_files: list[str], *, git: GitService) -> bool:
        for rel in conflict_files:
            path = git.repo_path / rel
            if not path.is_file():
                continue
            # _apply 已按 UTF-8 写回，这里用 UTF-8 读回校验冲突标记是否清空。
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith(_MARKERS):
                    return False
        if not git.diff_check():
            return False
        if not _modified_paths(git.status()) <= set(conflict_files):
            return False
        try:
            self._safety.check_editable(conflict_files)
        except SafetyViolation:
            return False
        return True

    def _rollback(
        self,
        conflict_files: list[str],
        snapshots: dict[str, bytes],
        *,
        git: GitService,
    ) -> None:
        for rel, data in snapshots.items():
            path = git.repo_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        for rel in conflict_files:
            if rel not in snapshots:
                path = git.repo_path / rel
                if path.is_file():
                    path.unlink()
