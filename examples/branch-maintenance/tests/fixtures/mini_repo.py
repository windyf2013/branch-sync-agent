from __future__ import annotations

import subprocess
from pathlib import Path


def _run(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _run(["git", "init"], cwd=repo)
    _run(["git", "config", "user.email", "bma@test.local"], cwd=repo)
    _run(["git", "config", "user.name", "BMA Test"], cwd=repo)


def commit_all(repo: Path, message: str) -> str:
    _run(["git", "add", "-A"], cwd=repo)
    result = _run(["git", "commit", "-m", message], cwd=repo)
    sha_result = _run(["git", "rev-parse", "HEAD"], cwd=repo)
    return sha_result.stdout.strip()


def create_branch(repo: Path, name: str, *, from_ref: str = "HEAD") -> None:
    _run(["git", "branch", name, from_ref], cwd=repo)


def checkout(repo: Path, ref: str) -> None:
    _run(["git", "checkout", ref], cwd=repo)


def write_file(repo: Path, rel_path: str, content: str) -> None:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def build_multi_develop_fix_repo(repo: Path) -> dict[str, str]:
    """Two develop branches; null-check fix only on develop_a."""
    init_repo(repo)
    write_file(
        repo,
        "plat/demo/demo.c",
        "int demo_check(void *ptr)\n{\n    return ptr != NULL;\n}\n",
    )
    base_sha = commit_all(repo, "Initial demo module")

    create_branch(repo, "br_v4_LineA_develop_a_20260101", from_ref=base_sha)
    create_branch(repo, "br_v4_LineA_develop_b_20260101", from_ref=base_sha)

    checkout(repo, "br_v4_LineA_develop_a_20260101")
    write_file(
        repo,
        "plat/demo/demo.c",
        "int demo_check(void *ptr)\n{\n    if (NULL == ptr)\n    {\n        return -1;\n    }\n    return 0;\n}\n",
    )
    fix_sha = commit_all(repo, "[BUG] CQ12345 Add null guard in demo_check")

    return {
        "base_sha": base_sha,
        "fix_sha": fix_sha,
        "develop_a": "br_v4_LineA_develop_a_20260101",
        "develop_b": "br_v4_LineA_develop_b_20260101",
        "file": "plat/demo/demo.c",
        "symbol": "demo_check",
    }
