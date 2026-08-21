"""Build a real git repo fixture with an actual merge conflict (C1 integration).

The fixture mirrors production topology: a main repo with an ``origin`` remote,
a ``develop`` source branch carrying the bug-fix, and a ``release`` target branch
that conflicts when the fix is cherry-picked onto it. All branches are pushed so
``branch_tip`` prefers ``origin/<branch>`` and the worktree is added detached.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _run(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


def build_conflict_repo(root: Path) -> dict[str, Path | str]:
    """Create base/master -> develop (source) + release (target) with a conflict.

    Returns repo path, base/master sha, source (develop) sha, and the original
    release tip sha (stable via ``origin/release``).
    """
    repo = root / "repo"
    repo.mkdir()
    _run(repo, "init", "-q", "-b", "master")
    _run(repo, "config", "user.email", "bsa@test.local")
    _run(repo, "config", "user.name", "BSA Test")
    (repo / "hello.txt").write_text("base\n", encoding="utf-8")
    _run(repo, "add", ".")
    _run(repo, "commit", "-q", "-m", "base")
    base_sha = _run(repo, "rev-parse", "HEAD")

    _run(repo, "checkout", "-q", "-b", "develop")
    (repo / "hello.txt").write_text("source\n", encoding="utf-8")
    _run(repo, "add", ".")
    _run(repo, "commit", "-q", "-m", "[BUG] source change")
    src_sha = _run(repo, "rev-parse", "HEAD")

    _run(repo, "checkout", "-q", "master")
    _run(repo, "checkout", "-q", "-b", "release")
    (repo / "hello.txt").write_text("target\n", encoding="utf-8")
    _run(repo, "add", ".")
    _run(repo, "commit", "-q", "-m", "target change")
    release_tip = _run(repo, "rev-parse", "HEAD")

    origin = root / "origin.git"
    _run(repo, "clone", "-q", "--bare", str(repo), str(origin))
    _run(repo, "remote", "add", "origin", str(origin))
    _run(repo, "fetch", "-q", "origin")
    _run(repo, "push", "-q", "origin", "master", "develop", "release")

    _run(repo, "checkout", "-q", "master")
    return {
        "repo": repo,
        "base_sha": base_sha,
        "src_sha": src_sha,
        "release_tip": release_tip,
    }


def build_resolution_diff(path: str, current: str, resolved: str) -> str:
    """Full-file replacement unified diff against the conflicted content."""
    old = current.splitlines()
    new = resolved.splitlines()
    body = "".join(f"-{line}\n" for line in old) + "".join(f"+{line}\n" for line in new)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1,{len(old)} +1,{len(new)} @@\n"
        f"{body}"
    )


def keep_source_side(marker_text: str) -> str:
    """Return the incoming (source) side of a conflict marker block."""
    lines = marker_text.splitlines()
    out: list[str] = []
    capture = False
    for line in lines:
        if line.startswith("======="):
            capture = True
            continue
        if line.startswith(">>>>>>>"):
            break
        if capture:
            out.append(line)
    return "\n".join(out) + "\n"
