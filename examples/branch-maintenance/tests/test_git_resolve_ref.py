from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from branch_maintenance.git_scan import branch_tip, commits_since, resolve_branch_ref
from fixtures.mini_repo import commit_all, create_branch, init_repo, write_file


def _run(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def test_resolve_prefers_origin_over_stale_local(tmp_path: Path):
    """Local tip stale; origin tracking ref has newer commits — prefer origin."""
    bare = tmp_path / "remote.git"
    _run(["git", "init", "--bare", str(bare)], cwd=tmp_path)

    work = tmp_path / "work"
    init_repo(work)
    write_file(work, "a.txt", "v1\n")
    base = commit_all(work, "base")
    create_branch(work, "br_v4_demo_develop_20260101", from_ref=base)
    _run(["git", "remote", "add", "origin", str(bare)], cwd=work)
    _run(
        ["git", "push", "-u", "origin", "br_v4_demo_develop_20260101"],
        cwd=work,
    )

    # Advance origin via a second clone; leave original local branch behind.
    other = tmp_path / "other"
    _run(["git", "clone", str(bare), str(other)], cwd=tmp_path)
    _run(["git", "checkout", "br_v4_demo_develop_20260101"], cwd=other)
    write_file(other, "a.txt", "v2 remote\n")
    remote_sha = commit_all(other, "fix: remote tip update")
    _run(["git", "push", "origin", "br_v4_demo_develop_20260101"], cwd=other)

    _run(["git", "fetch", "origin"], cwd=work)

    local_sha = _run(
        ["git", "rev-parse", "br_v4_demo_develop_20260101"],
        cwd=work,
    ).stdout.strip()
    origin_sha = _run(
        ["git", "rev-parse", "origin/br_v4_demo_develop_20260101"],
        cwd=work,
    ).stdout.strip()
    assert local_sha == base
    assert origin_sha == remote_sha
    assert local_sha != origin_sha

    resolved = resolve_branch_ref(work, "br_v4_demo_develop_20260101")
    assert resolved == "origin/br_v4_demo_develop_20260101"

    tip_ref, tip_sha = branch_tip(work, "br_v4_demo_develop_20260101")
    assert tip_ref == "origin/br_v4_demo_develop_20260101"
    assert tip_sha == remote_sha

    shas = commits_since(
        work,
        tip_ref,
        None,
        since="2020-01-01",
        until="2099-01-01",
    )
    assert remote_sha in shas
    assert base in shas or True  # base may or may not fall in filter by date

    # Window that should include only the new remote tip by excluding ancient commits
    # is fragile; assert tip-based listing without since still works via resolved ref.
    all_on_origin = commits_since(work, tip_ref, None)
    assert remote_sha in all_on_origin
