from __future__ import annotations

import subprocess
from pathlib import Path


class GitCommandError(RuntimeError):
    def __init__(self, args: list[str], returncode: int, stderr: str) -> None:
        super().__init__(
            f"git command failed ({returncode}): {' '.join(args)}\n{stderr.strip()}"
        )
        self.args = args
        self.returncode = returncode
        self.stderr = stderr


def _run_git(
    args: list[str],
    *,
    repo: Path,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise GitCommandError(["git", *args], completed.returncode, completed.stderr or "")
    return completed


def fetch_all(repo: Path) -> None:
    # Do not capture fetch stdout/stderr into memory — large remotes can OOM.
    completed = subprocess.run(
        ["git", "fetch", "--all", "--prune"],
        cwd=str(repo),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise GitCommandError(
            ["git", "fetch", "--all", "--prune"],
            completed.returncode,
            completed.stderr or "",
        )


def _ref_exists(repo: Path, ref: str) -> bool:
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return completed.returncode == 0


def list_remotes(repo: Path) -> list[str]:
    completed = _run_git(["remote"], repo=repo)
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def resolve_branch_ref(repo: Path, branch: str) -> str:
    """Resolve a logical branch name to the freshest available ref.

    After ``git fetch --all``, remote-tracking refs (e.g. ``origin/branch``) are
    updated but local branch tips may remain stale. Prefer remote-tracking tips
    (``origin`` first) so time-window scans see shared remote activity.
    """
    remotes = list_remotes(repo)
    if "origin" in remotes:
        remotes = ["origin", *[name for name in remotes if name != "origin"]]

    for remote in remotes:
        remote_ref = f"{remote}/{branch}"
        if _ref_exists(repo, remote_ref):
            return remote_ref

    if _ref_exists(repo, branch):
        return branch

    raise GitCommandError(
        ["git", "rev-parse", "--verify", branch],
        1,
        f"unknown branch ref: {branch}",
    )


def branch_tip(repo: Path, branch: str) -> tuple[str, str]:
    """Return ``(resolved_ref, tip_sha)`` for a logical branch name."""
    resolved = resolve_branch_ref(repo, branch)
    completed = _run_git(["rev-parse", resolved], repo=repo)
    return resolved, completed.stdout.strip()


def commits_since(
    repo: Path,
    ref: str,
    baseline: str | None,
    *,
    since: str | None = None,
    until: str | None = None,
) -> list[str]:
    args = ["rev-list", "--reverse"]
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")

    # Explicit time window is authoritative; otherwise resume from baseline.
    if baseline and since is None and until is None:
        rev_range = f"{baseline}..{ref}"
    else:
        rev_range = ref

    args.append(rev_range)
    completed = _run_git(args, repo=repo)
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def patch_id(repo: Path, sha: str) -> str | None:
    parents = subprocess.run(
        ["git", "rev-list", "--parents", "-1", sha],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if parents.returncode != 0 or not parents.stdout.strip():
        return None
    # Root commits (source imports) have no usable patch-id and may be huge.
    if len(parents.stdout.strip().split()) <= 1:
        return None

    # Skip pathological commits (large vendor dumps) — avoid multi-GB git-show.
    numstat = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--numstat", "-r", sha],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if numstat.returncode == 0:
        total_lines = 0
        file_count = 0
        for line in numstat.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) < 2:
                continue
            file_count += 1
            if file_count > 500:
                return None
            try:
                added = 0 if parts[0] == "-" else int(parts[0])
                deleted = 0 if parts[1] == "-" else int(parts[1])
            except ValueError:
                continue
            total_lines += added + deleted
            if total_lines > 20_000:
                return None

    # Stream git-show → patch-id; avoid loading large patches into Python memory.
    show_proc = subprocess.Popen(
        ["git", "show", "--pretty=format:", sha],
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        completed = subprocess.run(
            ["git", "patch-id", "--stable"],
            cwd=str(repo),
            stdin=show_proc.stdout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    finally:
        if show_proc.stdout is not None:
            show_proc.stdout.close()
        show_proc.wait()

    if completed.returncode != 0:
        return None
    patch_id_line = completed.stdout.strip()
    if not patch_id_line:
        return None
    return patch_id_line.split()[0]


def file_exists(repo: Path, ref: str, path: str) -> bool:
    try:
        _run_git(["cat-file", "-e", f"{ref}:{path}"], repo=repo)
    except GitCommandError:
        return False
    return True


def show_file(repo: Path, ref: str, path: str) -> str | None:
    try:
        completed = _run_git(["show", f"{ref}:{path}"], repo=repo)
    except GitCommandError:
        return None
    return completed.stdout
