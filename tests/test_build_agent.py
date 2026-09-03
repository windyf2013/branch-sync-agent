from __future__ import annotations

from pathlib import Path

from bsa.agents.base import BuildAttribution, BuildFix, LLMUnavailable
from bsa.agents.build_agent import BuildAgent, _error_files, _error_signatures
from bsa.build.runner import BuildResult
from bsa.domain.models import CommitInfo
from bsa.rules.safety import SafetyEnforcer, SafetyRules

ERROR_BLOCK = "src/dhcp.c:2:9: error: 'bad' undeclared\n    int value = bad;\n"
ABS_ERROR_BLOCK = (
    "/workspace/rcios/build/../src/dhcp.c:2:9: error: 'bad' undeclared\n"
    "    int value = bad;\n"
)
NEW_FILE_ERROR_BLOCK = (
    "src/newfile.c:2:9: error: 'bad' undeclared\n    int value = bad;\n"
)
FIX_DIFF = (
    "diff --git a/src/dhcp.c b/src/dhcp.c\n"
    "--- a/src/dhcp.c\n"
    "+++ b/src/dhcp.c\n"
    "@@ -1,2 +1,2 @@\n"
    " #include <stdlib.h>\n"
    "-int value = bad;\n"
    "+int value = 1;\n"
)
NEW_FILE_FIX_DIFF = (
    "diff --git a/src/newfile.c b/src/newfile.c\n"
    "--- a/src/newfile.c\n"
    "+++ b/src/newfile.c\n"
    "@@ -1,1 +1,1 @@\n"
    "-int value = bad;\n"
    "+int value = 1;\n"
)


def make_commit(**overrides: object) -> CommitInfo:
    values = dict(
        sha="b1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c2d3e",
        message="port dhcp init tweak",
        author="dev",
        committed_at="2026-08-20T10:00:00+08:00",
        changed_files=["src/dhcp.c"],
        patch_text="@@ -1,2 +1,2 @@\n-int value = 0;\n+int value = bad;\n",
        symbols=["dhcp_init"],
        patch_id=None,
        issue_ids=[],
        source_branch="develop",
        homologous_section="RTK",
    )
    values.update(overrides)
    return CommitInfo(**values)


def make_attribution(category: str, files_to_fix: list[str], reason: str = "r") -> BuildAttribution:
    return BuildAttribution(category=category, reason=reason, files_to_fix=files_to_fix)


def make_fix(
    files: list[str] | None = None, diff: str = FIX_DIFF, reason: str = "minimal fix"
) -> BuildFix:
    return BuildFix(files=files or ["src/dhcp.c"], diff=diff, agent_reason=reason)


class FakeLLM:
    def __init__(
        self,
        classification: BuildAttribution,
        fixes: list[BuildFix] | None = None,
        *,
        classify_error: Exception | None = None,
        fix_error: Exception | None = None,
    ) -> None:
        self.classification = classification
        self.fixes = list(fixes or [])
        self.classify_error = classify_error
        self.fix_error = fix_error
        self.classify_calls: list = []
        self.fix_calls: list = []

    def classify_build_error(self, ctx):
        self.classify_calls.append(ctx)
        if self.classify_error is not None:
            raise self.classify_error
        return self.classification

    def fix_build_error(self, ctx):
        self.fix_calls.append(ctx)
        if self.fix_error is not None:
            raise self.fix_error
        if not self.fixes:
            raise AssertionError("FakeLLM has no fix configured")
        return self.fixes.pop(0)


class FakeGit:
    def __init__(self, repo_path: Path, *, original_files: dict[str, str] | None = None) -> None:
        self.repo_path = repo_path
        self.original_files = dict(original_files or {})
        self.snapshots: list[dict[str, str]] = []
        self.restored: list[dict[str, str]] = []
        self.branch_tip_calls: list[str] = []
        self.show_file_calls: list[tuple[str, str]] = []

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

    def branch_tip(self, branch: str) -> tuple[str, str]:
        self.branch_tip_calls.append(branch)
        return f"origin/{branch}", "tipsha"

    def show_file(self, ref: str, path: str) -> str | None:
        self.show_file_calls.append((ref, path))
        return self.original_files.get(path)


class FakeRunner:
    def __init__(
        self,
        responses: list[tuple[list[str], bool]],
        *,
        is_success_results: list[bool] | None = None,
    ) -> None:
        self.responses = list(responses)
        self.is_success_results = list(is_success_results or [])
        self.build_calls: list[dict] = []
        self.is_success_calls = 0

    def build_commit(
        self,
        worktree: Path,
        model: str,
        *,
        clean: bool,
        module: str | None,
        log_path: Path | None = None,
    ) -> BuildResult:
        self.build_calls.append(
            {
                "worktree": worktree,
                "model": model,
                "clean": clean,
                "module": module,
                "files": sorted(
                    str(p.relative_to(worktree)) for p in worktree.rglob("*") if p.is_file()
                ),
            }
        )
        errors, succeeded = self.responses.pop(0)
        return BuildResult(
            model=model,
            returncode=0 if succeeded else 1,
            log_path=log_path or Path("build.log"),
            succeeded=succeeded,
            errors=errors,
        )

    def is_success(self, result: BuildResult) -> bool:
        self.is_success_calls += 1
        if self.is_success_results:
            return self.is_success_results.pop(0)
        return result.succeeded


def make_safety() -> SafetyEnforcer:
    return SafetyEnforcer(
        SafetyRules(
            forbidden_paths=["config/", ".env"],
            required_models=[],
            forbidden_branches=[],
            max_single_edit_lines=200,
        )
    )


def write_file(tmp_path: Path, rel: str, content: str) -> None:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _big_fix_diff(num: int = 250) -> str:
    removed = "".join(f"-int x{i} = {i};\n" for i in range(num))
    added = "".join(f"+int x{i} = 0;\n" for i in range(num))
    return (
        "diff --git a/src/dhcp.c b/src/dhcp.c\n"
        "--- a/src/dhcp.c\n"
        "+++ b/src/dhcp.c\n"
        f"@@ -1,{num} +1,{num} @@\n"
        f"{removed}{added}"
    )


def _big_fix_content(num: int = 250) -> str:
    return "".join(f"int x{i} = {i};\n" for i in range(num))


def test_error_files_extracts_source_paths() -> None:
    errors = [
        "src/dhcp.c:2:9: error: 'bad' undeclared\n",
        "include/net.h:3:5: error: expected ';'\n",
    ]
    assert _error_files(errors) == ["src/dhcp.c", "include/net.h"]


def test_error_files_dedupes() -> None:
    errors = ["src/a.c:1:1: error: x\n", "src/a.c:2:1: error: y\n"]
    assert _error_files(errors) == ["src/a.c"]


def test_environment_attribution_reports_without_fix(tmp_path: Path) -> None:
    git = FakeGit(tmp_path)
    llm = FakeLLM(make_attribution("environment", []))
    runner = FakeRunner([])
    agent = BuildAgent(llm, git, runner, make_safety())

    result = agent.fix(make_commit(), [ERROR_BLOCK], "RTL9617C")

    assert result.category == "environment"
    assert len(llm.classify_calls) == 1
    assert llm.fix_calls == []
    assert git.snapshots == []
    assert runner.build_calls == []


def test_unresolvable_attribution_reports_without_fix(tmp_path: Path) -> None:
    git = FakeGit(tmp_path)
    llm = FakeLLM(make_attribution("unresolvable", []))
    runner = FakeRunner([])
    agent = BuildAgent(llm, git, runner, make_safety())

    result = agent.fix(make_commit(), [ERROR_BLOCK], "RTL9617C")

    assert result.category == "unresolvable"
    assert llm.fix_calls == []
    assert git.snapshots == []
    assert runner.build_calls == []


def test_llm_unavailable_classify_degrades_to_unresolvable(tmp_path: Path) -> None:
    git = FakeGit(tmp_path)
    llm = FakeLLM(make_attribution("introduced_by_commit", ["src/dhcp.c"]),
                  classify_error=LLMUnavailable("api down"))
    runner = FakeRunner([])
    agent = BuildAgent(llm, git, runner, make_safety())

    result = agent.fix(make_commit(), [ERROR_BLOCK], "RTL9617C")

    assert result.category == "unresolvable"
    # 真实失败原因随 reason 透出，而非固定文案「LLM 不可用，无法归因」。
    assert "api down" in result.reason
    assert llm.fix_calls == []
    assert git.snapshots == []
    assert runner.build_calls == []


def test_fix_uses_worktree_scoped_git(tmp_path: Path) -> None:
    write_file(tmp_path, "src/dhcp.c", "#include <stdlib.h>\nint value = bad;\n")
    main = FakeGit(Path("/main"))
    wt = FakeGit(tmp_path)
    llm = FakeLLM(
        make_attribution("introduced_by_commit", ["src/dhcp.c"]),
        fixes=[make_fix()],
    )
    runner = FakeRunner([([], True)])
    agent = BuildAgent(llm, main, runner, make_safety())

    result = agent.fix(
        make_commit(), [ERROR_BLOCK], "RTL9617C", git=wt, target_branch="br_v4.33"
    )

    assert result.category == "introduced_by_commit"
    assert (tmp_path / "src/dhcp.c").read_text(encoding="utf-8") == (
        "#include <stdlib.h>\nint value = 1;\n"
    )
    assert llm.classify_calls[0].worktree == tmp_path
    assert runner.build_calls[0]["worktree"] == tmp_path
    assert not (Path("/main") / "src").exists()


def test_introduced_by_commit_fix_loop_success(tmp_path: Path) -> None:
    write_file(tmp_path, "src/dhcp.c", "#include <stdlib.h>\nint value = bad;\n")
    git = FakeGit(tmp_path)
    llm = FakeLLM(
        make_attribution("introduced_by_commit", ["src/dhcp.c"]),
        fixes=[make_fix()],
    )
    runner = FakeRunner([([], True)])
    agent = BuildAgent(llm, git, runner, make_safety())

    result = agent.fix(make_commit(), [ERROR_BLOCK], "RTL9617C")

    assert result.category == "introduced_by_commit"
    assert len(llm.fix_calls) == 1
    assert (tmp_path / "src/dhcp.c").read_text(encoding="utf-8") == (
        "#include <stdlib.h>\nint value = 1;\n"
    )
    assert len(git.snapshots) == 1
    assert git.restored == []
    assert len(runner.build_calls) == 1
    assert runner.build_calls[0]["model"] == "RTL9617C"
    assert runner.build_calls[0]["clean"] is False
    ctx = llm.classify_calls[0]
    assert ctx.commit.sha == "b1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c2d3e"
    assert ctx.model == "RTL9617C"
    assert ctx.worktree == tmp_path


def test_successful_fix_captures_applied_diff(tmp_path: Path) -> None:
    write_file(tmp_path, "src/dhcp.c", "#include <stdlib.h>\nint value = bad;\n")
    git = FakeGit(tmp_path)
    llm = FakeLLM(
        make_attribution("introduced_by_commit", ["src/dhcp.c"]),
        fixes=[make_fix()],
    )
    runner = FakeRunner([([], True)])
    agent = BuildAgent(llm, git, runner, make_safety())

    result = agent.fix(make_commit(), [ERROR_BLOCK], "RTL9617C")

    assert result.category == "introduced_by_commit"
    assert result.fix_diff == FIX_DIFF


def test_fix_failure_restores_and_retries_three_attempts(tmp_path: Path) -> None:
    original = "#include <stdlib.h>\nint value = bad;\n"
    write_file(tmp_path, "src/dhcp.c", original)
    git = FakeGit(tmp_path)
    llm = FakeLLM(
        make_attribution("introduced_by_commit", ["src/dhcp.c"]),
        fixes=[make_fix(), make_fix(), make_fix()],
    )
    runner = FakeRunner(
        [([ERROR_BLOCK], False), ([ERROR_BLOCK], False), ([ERROR_BLOCK], False)]
    )
    agent = BuildAgent(llm, git, runner, make_safety())

    result = agent.fix(make_commit(), [ERROR_BLOCK], "RTL9617C")

    assert result.category == "introduced_by_commit"
    assert len(llm.fix_calls) == 3
    assert len(git.snapshots) == 3
    assert len(git.restored) == 3
    assert len(runner.build_calls) == 3
    assert (tmp_path / "src/dhcp.c").read_text(encoding="utf-8") == original


def test_safety_violation_on_files_to_fix_returns_without_fix(tmp_path: Path) -> None:
    write_file(tmp_path, "config/app.c", "int x;\n")
    git = FakeGit(tmp_path)
    llm = FakeLLM(make_attribution("introduced_by_commit", ["config/app.c"]))
    runner = FakeRunner([])
    agent = BuildAgent(llm, git, runner, make_safety())

    result = agent.fix(make_commit(), [ERROR_BLOCK], "RTL9617C")

    assert result.category == "introduced_by_commit"
    assert llm.fix_calls == []
    assert git.snapshots == []
    assert runner.build_calls == []


def test_out_of_bounds_files_to_fix_is_invalid_attempt(tmp_path: Path) -> None:
    git = FakeGit(tmp_path)
    llm = FakeLLM(make_attribution("introduced_by_commit", ["src/evil.c"]))
    runner = FakeRunner([])
    agent = BuildAgent(llm, git, runner, make_safety())

    result = agent.fix(make_commit(), [ERROR_BLOCK], "RTL9617C")

    assert result.category == "introduced_by_commit"
    assert llm.fix_calls == []
    assert git.snapshots == []
    assert runner.build_calls == []


def test_pre_existing_verify_confirms_and_reports(tmp_path: Path) -> None:
    write_file(tmp_path, "src/dhcp.c", "#include <stdlib.h>\nint value = bad;\n")
    git = FakeGit(
        tmp_path,
        original_files={"src/dhcp.c": "#include <stdlib.h>\nint value = bad;\n"},
    )
    llm = FakeLLM(make_attribution("pre_existing", []))
    runner = FakeRunner([([ERROR_BLOCK], False)])
    agent = BuildAgent(llm, git, runner, make_safety(), target_branch="br_v4.33")

    result = agent.fix(make_commit(), [ERROR_BLOCK], "RTL9617C")

    assert result.category == "pre_existing"
    assert result.files_to_fix == []
    assert git.branch_tip_calls == ["br_v4.33"]
    assert git.show_file_calls == [("origin/br_v4.33", "src/dhcp.c")]
    assert len(runner.build_calls) == 1
    assert llm.fix_calls == []
    assert (tmp_path / "src/dhcp.c").read_text(encoding="utf-8") == (
        "#include <stdlib.h>\nint value = bad;\n"
    )


def test_pre_existing_verify_denied_then_fix_loop(tmp_path: Path) -> None:
    write_file(tmp_path, "src/dhcp.c", "#include <stdlib.h>\nint value = bad;\n")
    git = FakeGit(tmp_path, original_files={"src/dhcp.c": "#include <stdlib.h>\nint value = 0;\n"})
    llm = FakeLLM(
        make_attribution("pre_existing", ["src/dhcp.c"]),
        fixes=[make_fix()],
    )
    runner = FakeRunner([([], True), ([], True)])
    agent = BuildAgent(llm, git, runner, make_safety(), target_branch="br_v4.33")

    result = agent.fix(make_commit(), [ERROR_BLOCK], "RTL9617C")

    assert result.category == "introduced_by_commit"
    assert len(llm.fix_calls) == 1
    assert len(runner.build_calls) == 2
    assert (tmp_path / "src/dhcp.c").read_text(encoding="utf-8") == (
        "#include <stdlib.h>\nint value = 1;\n"
    )


def test_fix_loop_llm_unavailable_stops_without_retry(tmp_path: Path) -> None:
    write_file(tmp_path, "src/dhcp.c", "#include <stdlib.h>\nint value = bad;\n")
    git = FakeGit(tmp_path)
    llm = FakeLLM(
        make_attribution("introduced_by_commit", ["src/dhcp.c"]),
        fix_error=LLMUnavailable("api down"),
    )
    runner = FakeRunner([])
    agent = BuildAgent(llm, git, runner, make_safety())

    result = agent.fix(make_commit(), [ERROR_BLOCK], "RTL9617C")

    assert result.category == "introduced_by_commit"
    assert len(llm.fix_calls) == 1
    assert len(git.snapshots) == 1
    assert len(git.restored) == 1
    assert runner.build_calls == []


def test_oversized_fix_diff_is_invalid_attempt(tmp_path: Path) -> None:
    content = _big_fix_content()
    write_file(tmp_path, "src/dhcp.c", content)
    git = FakeGit(tmp_path)
    big = make_fix(files=["src/dhcp.c"], diff=_big_fix_diff())
    llm = FakeLLM(
        make_attribution("introduced_by_commit", ["src/dhcp.c"]),
        fixes=[big, big, big],
    )
    runner = FakeRunner([])
    agent = BuildAgent(llm, git, runner, make_safety())

    result = agent.fix(make_commit(), [ERROR_BLOCK], "RTL9617C")

    assert result.category == "introduced_by_commit"
    assert len(llm.fix_calls) == 3
    assert len(git.snapshots) == 3
    assert len(git.restored) == 3
    assert runner.build_calls == []
    assert (tmp_path / "src/dhcp.c").read_text(encoding="utf-8") == content


def test_error_files_normalizes_absolute_docker_path() -> None:
    errors = ["/workspace/rcios/build/../src/dhcp.c:2:9: error: 'bad' undeclared\n"]
    assert _error_files(errors, mount_prefix="/workspace/rcios") == ["src/dhcp.c"]


def test_error_files_strips_repo_root_when_no_mount_prefix() -> None:
    errors = ["/home/ci/repo/src/dhcp.c:2:9: error: 'bad' undeclared\n"]
    assert _error_files(errors, repo_path=Path("/home/ci/repo")) == ["src/dhcp.c"]


def test_error_files_resolves_dotdot_segments() -> None:
    errors = ["build/../src/dhcp.c:2:9: error: 'bad' undeclared\n"]
    assert _error_files(errors) == ["src/dhcp.c"]


def test_error_signatures_normalize_paths_consistently() -> None:
    abs_block = ["/workspace/rcios/build/../src/dhcp.c:2:9: error: 'bad' undeclared\n"]
    rel_block = ["src/dhcp.c:2:9: error: 'bad' undeclared\n"]
    assert _error_signatures(abs_block, mount_prefix="/workspace/rcios") == (
        _error_signatures(rel_block)
    )


def test_absolute_error_path_scope_gate_passes(tmp_path: Path) -> None:
    write_file(tmp_path, "src/dhcp.c", "#include <stdlib.h>\nint value = bad;\n")
    git = FakeGit(tmp_path)
    llm = FakeLLM(
        make_attribution("introduced_by_commit", ["src/dhcp.c"]),
        fixes=[make_fix()],
    )
    runner = FakeRunner([([], True)])
    agent = BuildAgent(
        llm, git, runner, make_safety(), docker_mount_workspace="/workspace/rcios"
    )

    result = agent.fix(make_commit(), [ABS_ERROR_BLOCK], "RTL9617C")

    assert result.category == "introduced_by_commit"
    assert len(llm.fix_calls) == 1
    assert (tmp_path / "src/dhcp.c").read_text(encoding="utf-8") == (
        "#include <stdlib.h>\nint value = 1;\n"
    )


def test_pre_existing_verify_new_file_denied_and_fixed(tmp_path: Path) -> None:
    write_file(tmp_path, "src/newfile.c", "int value = bad;\n")
    git = FakeGit(tmp_path)
    llm = FakeLLM(
        make_attribution("pre_existing", ["src/newfile.c"]),
        fixes=[make_fix(files=["src/newfile.c"], diff=NEW_FILE_FIX_DIFF)],
    )
    runner = FakeRunner([([], True), ([], True)])
    agent = BuildAgent(llm, git, runner, make_safety(), target_branch="br_v4.33")

    result = agent.fix(
        make_commit(changed_files=["src/newfile.c"]), [NEW_FILE_ERROR_BLOCK], "RTL9617C"
    )

    assert result.category == "introduced_by_commit"
    assert len(llm.fix_calls) == 1
    assert git.show_file_calls == [("origin/br_v4.33", "src/newfile.c")]
    assert runner.build_calls[0]["files"] == []
    assert runner.build_calls[1]["files"] == ["src/newfile.c"]
    assert (tmp_path / "src/newfile.c").read_text(encoding="utf-8") == "int value = 1;\n"
