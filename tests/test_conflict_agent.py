from __future__ import annotations

from pathlib import Path

from bsa.agents.base import ConflictContext, LLMUnavailable
from bsa.agents.conflict import ConflictAgent, _apply_patch
from bsa.domain.models import CommitInfo, ConflictResolution
from bsa.rules.safety import SafetyEnforcer, SafetyRules

CONFLICT_TEXT = (
    "<<<<<<< HEAD\n"
    "int value = 1;\n"
    "=======\n"
    "int value = 2;\n"
    ">>>>>>> 2a5b6c7 (fix bug)\n"
)
RESOLVED_DIFF = (
    "diff --git a/src/net.c b/src/net.c\n"
    "--- a/src/net.c\n"
    "+++ b/src/net.c\n"
    "@@ -1,5 +1,3 @@\n"
    "-<<<<<<< HEAD\n"
    " int value = 1;\n"
    "-=======\n"
    "-int value = 2;\n"
    "->>>>>>> 2a5b6c7 (fix bug)\n"
)
CONFLICT_FILES = ["src/net.c"]


def make_commit(**overrides: object) -> CommitInfo:
    values = dict(
        sha="a1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c2d3e",
        message="fix net init race",
        author="dev",
        committed_at="2026-08-20T10:00:00+08:00",
        changed_files=["src/net.c"],
        patch_text="@@ -1 +1 @@\n-int value = 0;\n+int value = 2;\n",
        symbols=["net_init"],
        patch_id=None,
        issue_ids=[],
        source_branch="develop",
        homologous_section="RTK",
    )
    values.update(overrides)
    return CommitInfo(**values)


def resolved(files: list[str] | None = None) -> ConflictResolution:
    return ConflictResolution(
        files=files or ["src/net.c"],
        diff=RESOLVED_DIFF,
        agent_reason="keep HEAD side, port introduces the init-order guard",
    )


class FakeGit:
    def __init__(
        self, repo_path: Path, *, status_text: str = "", diff_check_ok: bool = True
    ) -> None:
        self.repo_path = repo_path
        self.status_text = status_text
        self.diff_check_ok = diff_check_ok
        self.snapshots: list[dict[str, str]] = []
        self.restored: list[dict[str, str]] = []
        self.staged: list[list[str]] = []
        self.status_calls = 0

    def snapshot(self, paths: list[str]) -> dict[str, str]:
        snap = {}
        for rel in paths:
            path = self.repo_path / rel
            if path.is_file():
                snap[rel] = path.read_text(encoding="utf-8")
        self.snapshots.append(snap)
        return snap

    def restore(self, snapshots: dict[str, str]) -> None:
        self.restored.append(snapshots)
        for rel, content in snapshots.items():
            path = self.repo_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def diff_check(self) -> bool:
        return self.diff_check_ok

    def stage(self, paths: list[str]) -> None:
        self.staged.append(list(paths))

    def status(self) -> str:
        self.status_calls += 1
        return self.status_text


class FakeLLM:
    def __init__(self, results: list[ConflictResolution] | None = None, *, error=None) -> None:
        self.results = list(results) if results else []
        self.error = error
        self.calls: list[ConflictContext] = []

    def solve_conflict(self, ctx: ConflictContext) -> ConflictResolution:
        self.calls.append(ctx)
        if self.error is not None:
            raise self.error
        if not self.results:
            raise AssertionError("FakeLLM has no result configured")
        return self.results.pop(0)


def make_safety() -> SafetyEnforcer:
    return SafetyEnforcer(
        SafetyRules(
            forbidden_paths=["config/", ".env"],
            required_models=[],
            forbidden_branches=[],
            max_single_edit_lines=200,
        )
    )


def write_conflicted(tmp_path: Path, files: dict[str, str] | None = None) -> None:
    for rel, content in (files or {"src/net.c": CONFLICT_TEXT}).items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def test_apply_patch_removes_markers() -> None:
    assert _apply_patch(CONFLICT_TEXT, RESOLVED_DIFF) == "int value = 1;\n"


def test_forbidden_path_returns_none_without_llm_or_git(tmp_path: Path) -> None:
    git = FakeGit(tmp_path)
    llm = FakeLLM()
    agent = ConflictAgent(llm, git, make_safety())

    result = agent.resolve(make_commit(), ["config/app.c"])

    assert result is None
    assert llm.calls == []
    assert git.snapshots == []
    assert git.staged == []
    assert git.status_calls == 0


def test_successful_resolution_removes_markers_stages_and_returns(tmp_path: Path) -> None:
    write_conflicted(tmp_path)
    git = FakeGit(tmp_path, status_text="UU src/net.c\n")
    llm = FakeLLM([resolved()])
    agent = ConflictAgent(llm, git, make_safety())

    result = agent.resolve(make_commit(), CONFLICT_FILES)

    assert result is not None
    assert result.agent_reason == "keep HEAD side, port introduces the init-order guard"
    assert (tmp_path / "src/net.c").read_text(encoding="utf-8") == "int value = 1;\n"
    assert git.staged == [CONFLICT_FILES]
    assert len(llm.calls) == 1
    ctx = llm.calls[0]
    assert ctx.conflict_files == CONFLICT_FILES
    assert ctx.source_branch == "develop"
    assert ctx.target_branch == "develop"
    assert ctx.conflict_markers == {"src/net.c": CONFLICT_TEXT}


def test_target_branch_from_constructor_reaches_context(tmp_path: Path) -> None:
    write_conflicted(tmp_path)
    git = FakeGit(tmp_path, status_text="UU src/net.c\n")
    llm = FakeLLM([resolved()])
    agent = ConflictAgent(
        llm, git, make_safety(), target_branch="br_v4.33_5200_sdwan_release"
    )

    result = agent.resolve(make_commit(), CONFLICT_FILES)

    assert result is not None
    assert llm.calls[0].target_branch == "br_v4.33_5200_sdwan_release"


def test_retries_restores_then_returns_none_after_max_attempts(tmp_path: Path) -> None:
    write_conflicted(tmp_path)
    git = FakeGit(tmp_path, status_text="UU src/net.c\n")
    bad = ConflictResolution(files=["src/net.c"], diff="", agent_reason="no change")
    llm = FakeLLM([bad, bad, bad])
    agent = ConflictAgent(llm, git, make_safety())

    result = agent.resolve(make_commit(), CONFLICT_FILES)

    assert result is None
    assert len(llm.calls) == 3
    assert len(git.snapshots) == 3
    assert len(git.restored) == 3
    assert git.staged == []
    assert (tmp_path / "src/net.c").read_text(encoding="utf-8") == CONFLICT_TEXT


def test_llm_unavailable_returns_none_without_retry_or_rollback(tmp_path: Path) -> None:
    write_conflicted(tmp_path)
    git = FakeGit(tmp_path)
    llm = FakeLLM(error=LLMUnavailable("llm down"))
    agent = ConflictAgent(llm, git, make_safety())

    result = agent.resolve(make_commit(), CONFLICT_FILES)

    assert result is None
    assert len(llm.calls) == 1
    assert git.staged == []
    assert git.restored == []


def test_out_of_bounds_file_in_resolution_rejected(tmp_path: Path) -> None:
    write_conflicted(tmp_path)
    git = FakeGit(tmp_path, status_text="UU src/net.c\n")
    evil = ConflictResolution(
        files=["src/net.c", "src/evil.c"], diff=RESOLVED_DIFF, agent_reason="oops"
    )
    llm = FakeLLM([evil, evil, evil])
    agent = ConflictAgent(llm, git, make_safety())

    result = agent.resolve(make_commit(), CONFLICT_FILES)

    assert result is None
    assert git.staged == []
    assert len(git.restored) == 3
    assert not (tmp_path / "src/evil.c").exists()


def test_modified_file_outside_conflict_files_rejected(tmp_path: Path) -> None:
    write_conflicted(tmp_path)
    git = FakeGit(tmp_path, status_text="UU src/net.c\n M src/other.c\n")
    llm = FakeLLM([resolved(), resolved(), resolved()])
    agent = ConflictAgent(llm, git, make_safety())

    result = agent.resolve(make_commit(), CONFLICT_FILES)

    assert result is None
    assert git.staged == []
    assert len(git.restored) == 3


def test_diff_check_failure_rejected(tmp_path: Path) -> None:
    write_conflicted(tmp_path)
    git = FakeGit(tmp_path, status_text="UU src/net.c\n", diff_check_ok=False)
    llm = FakeLLM([resolved(), resolved(), resolved()])
    agent = ConflictAgent(llm, git, make_safety())

    result = agent.resolve(make_commit(), CONFLICT_FILES)

    assert result is None
    assert git.staged == []
    assert len(git.restored) == 3


def test_unresolved_conflict_file_causes_retry(tmp_path: Path) -> None:
    files = {"src/net.c": CONFLICT_TEXT, "src/udp.c": CONFLICT_TEXT}
    write_conflicted(tmp_path, files)
    git = FakeGit(tmp_path, status_text="UU src/net.c\nUU src/udp.c\n")
    partial = ConflictResolution(
        files=["src/net.c", "src/udp.c"], diff=RESOLVED_DIFF, agent_reason="partial"
    )
    llm = FakeLLM([partial, partial, partial])
    agent = ConflictAgent(llm, git, make_safety())

    result = agent.resolve(make_commit(), ["src/net.c", "src/udp.c"])

    assert result is None
    assert len(git.restored) == 3
    assert git.staged == []
    assert (tmp_path / "src/udp.c").read_text(encoding="utf-8") == CONFLICT_TEXT
