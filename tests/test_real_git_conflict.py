from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from bsa.agents.conflict import ConflictAgent
from bsa.domain.models import BranchResult, CommitInfo, CommitResult, ConflictResolution
from bsa.executor.subprocess import SubprocessExecutor
from bsa.executor.whitelist import WhitelistExecutor
from bsa.git.service import GitService
from bsa.graph.nodes import GraphContext, resolve_conflict
from bsa.rules import ConcludeThresholds, DecisionRules
from bsa.rules.safety import SafetyEnforcer, SafetyRules
from tests.fixtures.real_git_conflict import (
    build_conflict_repo,
    build_resolution_diff,
    keep_source_side,
)
from tests.test_graph_nodes import base_state, make_settings


class ResolverLLM:
    """Deterministic resolver: keep the source side of every conflict."""

    def solve_conflict(self, ctx):
        diff = ""
        for rel in ctx.conflict_files:
            current = ctx.conflict_markers.get(rel, "")
            diff += build_resolution_diff(rel, current, keep_source_side(current))
        return ConflictResolution(
            files=list(ctx.conflict_files), diff=diff, agent_reason="keep source side"
        )


def _commit_info(src_sha: str) -> CommitInfo:
    return CommitInfo(
        sha=src_sha,
        message="[BUG] source change",
        author="BSA Test",
        committed_at="2026-08-20T10:00:00+08:00",
        changed_files=["hello.txt"],
        patch_text="",
        symbols=[],
        patch_id=None,
        issue_ids=[],
        source_branch="develop",
        homologous_section="组网",
    )


def test_real_git_conflict_resolution_continues_and_patch_non_empty(tmp_path):
    fixture = build_conflict_repo(tmp_path)
    repo = Path(fixture["repo"])
    src_sha = str(fixture["src_sha"])
    release_tip = str(fixture["release_tip"])

    executor = WhitelistExecutor(SubprocessExecutor())
    main_git = GitService(executor=executor, repo_path=repo)
    safety = SafetyEnforcer(
        SafetyRules(
            forbidden_paths=[],
            required_models=[],
            forbidden_branches=[],
            max_single_edit_lines=200,
        )
    )
    settings = make_settings(tmp_path, repo_path=str(repo))
    ctx = GraphContext(
        settings=settings,
        executor=executor,
        git=main_git,
        runner=SimpleNamespace(),
        sync_decision_agent=SimpleNamespace(),
        conflict_agent=ConflictAgent(ResolverLLM(), git=main_git, safety=safety, max_attempts=3),
        build_agent=SimpleNamespace(),
        safety=safety,
        decision_rules=DecisionRules(classify={}, conclude=ConcludeThresholds(), branch_mapping={}),
    )

    resolved_ref, _ = main_git.branch_tip("release")
    assert resolved_ref == "origin/release"
    wt_path = Path(settings.worktree_root) / "release-cycle-20260101"
    main_git.add_worktree(resolved_ref, wt_path)
    wg = GitService(executor=executor, repo_path=wt_path)
    ctx.worktree_gits[str(wt_path)] = wg

    conflict = wg.cherry_pick(src_sha)
    assert conflict.status == "CONFLICT"

    state = base_state(
        current_target="release",
        current_commit=src_sha,
        detected_commits=[_commit_info(src_sha)],
        branch_results={
            "release": BranchResult(
                target_branch="release",
                worktree_path=str(wt_path),
                status="PARTIAL",
                commits=[
                    CommitResult(
                        sha=src_sha,
                        cherry_pick="CONFLICT",
                        conflict_resolution=None,
                        build={},
                    )
                ],
                patch_path=None,
                stop_reason=None,
            )
        },
    )

    update = resolve_conflict(state, ctx)

    assert update["status"] == "RESOLVED"
    branch = update["branch_results"]["release"]
    assert branch.commits[0].cherry_pick == "OK"
    assert branch.commits[0].conflict_resolution is not None
    assert (wt_path / "hello.txt").read_text(encoding="utf-8") == "source\n"

    sequencer = subprocess.run(
        ["git", "-C", str(wt_path), "rev-parse", "--verify", "--quiet", "CHERRY_PICK_HEAD"],
        capture_output=True,
        text=True,
    )
    assert sequencer.returncode != 0, "sequencer must be cleared after cherry-pick --continue"
    clean = subprocess.run(
        ["git", "-C", str(wt_path), "status", "--porcelain"], capture_output=True, text=True
    )
    assert clean.stdout.strip() == ""

    out_dir = Path(settings.log_dir) / "patch"
    base_tip, _ = wg.branch_tip("release")
    assert base_tip == "origin/release"
    patch_path = wg.format_patch(release_tip, "HEAD", out_dir, "cycle_x_release.patch")
    patch_text = patch_path.read_text(encoding="utf-8")
    assert "diff --git" in patch_text, "sync patch must be non-empty after conflict resolution"
    assert "+source" in patch_text
    assert "target" in patch_text


def test_real_git_next_cherry_pick_succeeds_after_continue(tmp_path):
    """Prove the sequencer is cleared: a subsequent clean cherry-pick applies."""
    fixture = build_conflict_repo(tmp_path)
    repo = Path(fixture["repo"])
    src_sha = str(fixture["src_sha"])

    _run = subprocess.run
    _run(["git", "-C", str(repo), "checkout", "-q", "develop"], check=True)
    (repo / "second.txt").write_text("second\n", encoding="utf-8")
    _run(["git", "-C", str(repo), "add", "second.txt"], check=True)
    _run(["git", "-C", str(repo), "commit", "-q", "-m", "[BUG] second change"], check=True)
    second_sha = _run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    _run(["git", "-C", str(repo), "checkout", "-q", "master"], check=True)

    executor = WhitelistExecutor(SubprocessExecutor())
    main_git = GitService(executor=executor, repo_path=repo)
    safety = SafetyEnforcer(
        SafetyRules(
            forbidden_paths=[], required_models=[], forbidden_branches=[], max_single_edit_lines=200
        )
    )
    settings = make_settings(tmp_path, repo_path=str(repo))
    ctx = GraphContext(
        settings=settings,
        executor=executor,
        git=main_git,
        runner=SimpleNamespace(),
        sync_decision_agent=SimpleNamespace(),
        conflict_agent=ConflictAgent(ResolverLLM(), git=main_git, safety=safety, max_attempts=3),
        build_agent=SimpleNamespace(),
        safety=safety,
        decision_rules=DecisionRules(classify={}, conclude=ConcludeThresholds(), branch_mapping={}),
    )

    resolved_ref, _ = main_git.branch_tip("release")
    wt_path = Path(settings.worktree_root) / "release-cycle-20260101"
    main_git.add_worktree(resolved_ref, wt_path)
    wg = GitService(executor=executor, repo_path=wt_path)
    ctx.worktree_gits[str(wt_path)] = wg

    assert wg.cherry_pick(src_sha).status == "CONFLICT"
    state = base_state(
        current_target="release",
        current_commit=src_sha,
        detected_commits=[_commit_info(src_sha)],
        branch_results={
            "release": BranchResult(
                target_branch="release",
                worktree_path=str(wt_path),
                status="PARTIAL",
                commits=[
                    CommitResult(
                        sha=src_sha,
                        cherry_pick="CONFLICT",
                        conflict_resolution=None,
                        build={},
                    )
                ],
                patch_path=None,
                stop_reason=None,
            )
        },
    )
    assert resolve_conflict(state, ctx)["status"] == "RESOLVED"

    assert wg.cherry_pick(second_sha).status == "OK"
    patch = wg.format_patch(
        fixture["release_tip"], "HEAD", Path(settings.log_dir) / "patch", "p.patch"
    )
    text = patch.read_text(encoding="utf-8")
    assert "+source" in text and "+second" in text


def _build_gbk_conflict_repo(root: Path) -> tuple[Path, str, bytes]:
    """Real repo where the conflicting file carries GBK 中文注释 bytes."""
    repo = root / "repo"
    repo.mkdir()

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    run("init", "-q", "-b", "master")
    run("config", "user.email", "bsa@test.local")
    run("config", "user.name", "BSA Test")
    gbk = "/* 中文注释 */".encode("gbk")

    def write_cell(content: bytes) -> None:
        (repo / "cell_lib.c").write_bytes(content)
        run("add", ".")
        run("commit", "-q", "-m", "cell change")

    write_cell(b"int x;\n" + gbk + b"\nint y;\n")
    run("checkout", "-q", "-b", "develop")
    write_cell(b"int x;\n" + gbk + b"\nint y = 2;\n")
    src_sha = run("rev-parse", "HEAD").stdout.strip()
    run("checkout", "-q", "master")
    run("checkout", "-q", "-b", "release")
    write_cell(b"int x;\n" + gbk + b"\nint y = 3;\n")
    origin = root / "origin.git"
    run("clone", "-q", "--bare", str(repo), str(origin))
    run("remote", "add", "origin", str(origin))
    run("fetch", "-q", "origin")
    run("push", "-q", "origin", "master", "develop", "release")
    run("checkout", "-q", "master")
    return repo, src_sha, gbk


def _replace_conflict_block(current: str) -> str:
    """替换冲突标记块为源侧内容，保留块外（含 GBK 注释）的所有行。"""
    lines = current.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.startswith("<<<<<<<")), None
    )
    eq = next((i for i, line in enumerate(lines) if line.startswith("=======")), None)
    end = next(
        (i for i, line in enumerate(lines) if line.startswith(">>>>>>>")), None
    )
    if start is None or eq is None or end is None or not (start < eq < end):
        return current
    source = lines[eq + 1 : end]
    body = lines[:start] + source + lines[end + 1 :]
    return "\n".join(body) + ("\n" if current.endswith("\n") else "")


class GBKSourceResolver:
    """Deterministic resolver: keep source side only inside the conflict block."""

    def solve_conflict(self, ctx):
        diff = ""
        for rel in ctx.conflict_files:
            current = ctx.conflict_markers.get(rel, "")
            diff += build_resolution_diff(rel, current, _replace_conflict_block(current))
        return ConflictResolution(
            files=list(ctx.conflict_files), diff=diff, agent_reason="keep source side in block"
        )


def test_real_git_gbk_conflict_resolves_losslessly(tmp_path):
    """真实 git：GBK 中文注释 + 代码行冲突，LLM 解决后 GBK 注释字节无损保留。"""
    repo, src_sha, gbk = _build_gbk_conflict_repo(tmp_path)

    executor = WhitelistExecutor(SubprocessExecutor())
    main_git = GitService(executor=executor, repo_path=repo)
    safety = SafetyEnforcer(
        SafetyRules(
            forbidden_paths=[], required_models=[], forbidden_branches=[], max_single_edit_lines=200
        )
    )
    settings = make_settings(tmp_path, repo_path=str(repo))
    ctx = GraphContext(
        settings=settings,
        executor=executor,
        git=main_git,
        runner=SimpleNamespace(),
        sync_decision_agent=SimpleNamespace(),
        conflict_agent=ConflictAgent(
            GBKSourceResolver(), git=main_git, safety=safety, max_attempts=3
        ),
        build_agent=SimpleNamespace(),
        safety=safety,
        decision_rules=DecisionRules(classify={}, conclude=ConcludeThresholds(), branch_mapping={}),
    )

    resolved_ref, _ = main_git.branch_tip("release")
    wt_path = Path(settings.worktree_root) / "release-cycle-20260101"
    main_git.add_worktree(resolved_ref, wt_path)
    wg = GitService(executor=executor, repo_path=wt_path)
    ctx.worktree_gits[str(wt_path)] = wg

    conflict = wg.cherry_pick(src_sha)
    assert conflict.status == "CONFLICT"

    state = base_state(
        current_target="release",
        current_commit=src_sha,
        detected_commits=[_commit_info(src_sha)],
        branch_results={
            "release": BranchResult(
                target_branch="release",
                worktree_path=str(wt_path),
                status="PARTIAL",
                commits=[
                    CommitResult(
                        sha=src_sha,
                        cherry_pick="CONFLICT",
                        conflict_resolution=None,
                        build={},
                    )
                ],
                patch_path=None,
                stop_reason=None,
            )
        },
    )

    update = resolve_conflict(state, ctx)

    assert update["status"] == "RESOLVED"
    data = (wt_path / "cell_lib.c").read_bytes()
    assert b"<<<<<<<" not in data
    assert gbk in data
    assert data == b"int x;\n" + gbk + b"\nint y = 2;\n"

    sequencer = subprocess.run(
        ["git", "-C", str(wt_path), "rev-parse", "--verify", "--quiet", "CHERRY_PICK_HEAD"],
        capture_output=True,
        text=True,
    )
    assert sequencer.returncode != 0, "sequencer must be cleared after cherry-pick --continue"
