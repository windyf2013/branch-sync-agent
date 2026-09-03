from __future__ import annotations

from pathlib import Path

import pytest

from bsa.agents.base import ConflictContext, LLMUnavailable
from bsa.agents.conflict import ConflictAgent, _apply_patch, _modified_paths
from bsa.domain.models import CommitInfo, ConflictResolution
from bsa.executor.exceptions import InfrastructureError
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
        self,
        repo_path: Path,
        *,
        status_text: str = "",
        diff_check_ok: bool = True,
        fail_stage: bool = False,
        fail_status: bool = False,
        fail_diff_check: bool = False,
    ) -> None:
        self.repo_path = repo_path
        self.status_text = status_text
        self.diff_check_ok = diff_check_ok
        self.fail_stage = fail_stage
        self.fail_status = fail_status
        self.fail_diff_check = fail_diff_check
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
        if self.fail_diff_check:
            raise InfrastructureError("git diff --check exploded")
        return self.diff_check_ok

    def stage(self, paths: list[str]) -> None:
        if self.fail_stage:
            raise InfrastructureError("git add exploded")
        self.staged.append(list(paths))

    def status(self) -> str:
        self.status_calls += 1
        if self.fail_status:
            raise InfrastructureError("git status exploded")
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


def _big_diff(num: int = 250, path: str = "src/net.c") -> str:
    removed = "".join(f"-line {i}\n" for i in range(num))
    added = "".join(f"+replacement {i}\n" for i in range(num))
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1,{num} +1,{num} @@\n"
        f"{removed}{added}"
    )


def _big_content(num: int = 250) -> str:
    return "".join(f"line {i}\n" for i in range(num))


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


def _gbk_resolution() -> ConflictResolution:
    return ConflictResolution(
        files=["src/cell_lib.c"],
        diff=(
            "diff --git a/src/cell_lib.c b/src/cell_lib.c\n"
            "--- a/src/cell_lib.c\n"
            "+++ b/src/cell_lib.c\n"
            "@@ -1,7 +1,4 @@\n"
            " int x;\n"
            " /* 项目中文注释 */\n"
            "-<<<<<<< HEAD\n"
            " int x = 0;\n"
            "-=======\n"
            "-int x = 1;\n"
            "->>>>>>> develop\n"
        ),
        agent_reason="keep HEAD side",
    )


def test_gbk_conflict_file_resolved_to_utf8(tmp_path: Path) -> None:
    # RCIOS 源文件含 GBK 中文注释（非 UTF-8 字节）：conflict.py 解码为 Unicode，
    # 解决后统一按 UTF-8 写回（合入转 UTF-8），非冲突行中文正确转换。
    gbk_comment = "/* 项目中文注释 */".encode("gbk")
    conflicted = (
        b"int x;\n"
        + gbk_comment
        + b"\n<<<<<<< HEAD\nint x = 0;\n=======\nint x = 1;\n>>>>>>> develop\n"
    )
    target = tmp_path / "src" / "cell_lib.c"
    target.parent.mkdir(parents=True)
    target.write_bytes(conflicted)
    git = FakeGit(tmp_path, status_text="UU src/cell_lib.c\n")
    llm = FakeLLM([_gbk_resolution()])
    agent = ConflictAgent(llm, git, make_safety())

    result = agent.resolve(make_commit(), ["src/cell_lib.c"])

    assert result is not None
    assert len(llm.calls) == 1
    data = target.read_bytes()
    assert b"<<<<<<<" not in data
    assert b"=======" not in data
    assert b">>>>>>>" not in data
    assert data == b"int x;\n" + "/* 项目中文注释 */".encode("utf-8") + b"\nint x = 0;\n"


def test_undecodable_conflict_file_returns_none_with_clear_reason(tmp_path: Path) -> None:
    # 含 NUL 字节的真二进制（git 判定 binary 的依据）：返回 None 转人工并记录清晰原因
    path = tmp_path / "src" / "net.c"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\x00\x00")
    git = FakeGit(tmp_path)
    llm = FakeLLM()
    agent = ConflictAgent(llm, git, make_safety())

    result = agent.resolve(make_commit(), ["src/net.c"])

    assert result is None
    assert agent.last_reason is not None and "二进制" in agent.last_reason
    assert "转人工" in agent.last_reason
    assert llm.calls == []
    assert git.staged == []


def test_mixed_encoding_conflict_resolved_to_utf8(tmp_path: Path) -> None:
    # 真机：GBK 主体 + 冲突标记行里 git 追加的 UTF-8 提交标题混编，任一种编码都
    # 解不了全文件 → gb18030+replace 兜底解码，主体中文正确、标记行 UTF-8 片段变
    # U+FFFD（该行解决时删除），合入统一转 UTF-8。
    from bsa.agents.conflict import _decode_text

    gbk_comment = "/* GBK 中文 */".encode("gbk")
    utf8_subject = "4G/5G模块".encode("utf-8")
    conflicted = (
        b"int x;\n"
        + gbk_comment
        + b"\n<<<<<<< HEAD\nint x = 0;\n=======\nint x = 1;\n"
        + b">>>>>>> abc (" + utf8_subject + b")\n"
    )
    decoded = _decode_text(conflicted)
    marker_line = [ln for ln in decoded.split("\n") if ln.startswith(">>>>>>>")][0]
    diff = (
        "diff --git a/src/cell_lib.c b/src/cell_lib.c\n"
        "--- a/src/cell_lib.c\n"
        "+++ b/src/cell_lib.c\n"
        "@@ -1,7 +1,3 @@\n"
        " int x;\n"
        " /* GBK 中文 */\n"
        "-<<<<<<< HEAD\n"
        " int x = 0;\n"
        "-=======\n"
        "-int x = 1;\n"
        f"-{marker_line}\n"
    )

    target = tmp_path / "src" / "cell_lib.c"
    target.parent.mkdir(parents=True)
    target.write_bytes(conflicted)
    git = FakeGit(tmp_path, status_text="UU src/cell_lib.c\n")
    llm = FakeLLM(
        [ConflictResolution(files=["src/cell_lib.c"], diff=diff, agent_reason="keep HEAD")]
    )
    agent = ConflictAgent(llm, git, make_safety())

    result = agent.resolve(make_commit(), ["src/cell_lib.c"])

    assert result is not None
    data = target.read_bytes()
    assert b"<<<<<<<" not in data and b"=======" not in data and b">>>>>>>" not in data
    assert "/* GBK 中文 */".encode("utf-8") in data
    assert "4G/5G模块".encode("utf-8") not in data


def test_decode_text_utf8() -> None:
    from bsa.agents.conflict import _decode_text

    assert _decode_text("int x;\n".encode("utf-8")) == "int x;\n"


def test_decode_text_gb18030() -> None:
    from bsa.agents.conflict import _decode_text

    data = b"int x;\n" + "/* 项目中文注释 */".encode("gbk") + b"\n"
    assert _decode_text(data) == "int x;\n/* 项目中文注释 */\n"


def test_decode_text_mixed_returns_gb18030_replace() -> None:
    from bsa.agents.conflict import _decode_text

    gbk = "/* GBK 中文 */".encode("gbk")
    utf8 = "透".encode("utf-8")  # 含 0x80 连续字节，gb18030 解不了
    data = b"int x;\n" + gbk + b"\n" + utf8 + b"\n"
    text = _decode_text(data)
    # 主体 GBK 中文正确解码，UTF-8 片段变 U+FFFD
    assert "/* GBK 中文 */" in text
    assert "�" in text


def test_decode_text_nul_returns_none() -> None:
    from bsa.agents.conflict import _decode_text

    assert _decode_text(b"\x00\x00") is None
    assert _decode_text(b"int x;\n\x00binary") is None


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


def test_resolve_uses_worktree_scoped_git(tmp_path: Path) -> None:
    main = FakeGit(Path("/main"))
    wt = FakeGit(tmp_path, status_text="UU src/net.c\n")
    write_conflicted(tmp_path)
    llm = FakeLLM([resolved()])
    agent = ConflictAgent(llm, main, make_safety())

    result = agent.resolve(make_commit(), CONFLICT_FILES, git=wt, target_branch="br_target")

    assert result is not None
    assert wt.staged == [CONFLICT_FILES]
    assert main.staged == []
    assert (tmp_path / "src/net.c").read_text(encoding="utf-8") == "int value = 1;\n"
    assert not (Path("/main") / "src").exists()
    assert llm.calls[0].target_branch == "br_target"
    assert llm.calls[0].worktree == tmp_path


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
    # 真实失败原因必须随 last_reason 透出（UI 冲突失败归因），而非无任何缘由。
    assert agent.last_reason is not None and "llm down" in agent.last_reason


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
    assert not (tmp_path / "src/evil.c").exists()
    assert (tmp_path / "src/net.c").read_text(encoding="utf-8") == CONFLICT_TEXT


def test_modified_file_outside_conflict_files_rejected(tmp_path: Path) -> None:
    write_conflicted(tmp_path)
    git = FakeGit(tmp_path, status_text="UU src/net.c\n M src/other.c\n")
    llm = FakeLLM([resolved(), resolved(), resolved()])
    agent = ConflictAgent(llm, git, make_safety())

    result = agent.resolve(make_commit(), CONFLICT_FILES)

    assert result is None
    assert git.staged == []
    assert (tmp_path / "src/net.c").read_text(encoding="utf-8") == CONFLICT_TEXT


def test_diff_check_failure_rejected(tmp_path: Path) -> None:
    write_conflicted(tmp_path)
    git = FakeGit(tmp_path, status_text="UU src/net.c\n", diff_check_ok=False)
    llm = FakeLLM([resolved(), resolved(), resolved()])
    agent = ConflictAgent(llm, git, make_safety())

    result = agent.resolve(make_commit(), CONFLICT_FILES)

    assert result is None
    assert git.staged == []
    assert (tmp_path / "src/net.c").read_text(encoding="utf-8") == CONFLICT_TEXT


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
    assert git.staged == []
    assert (tmp_path / "src/net.c").read_text(encoding="utf-8") == CONFLICT_TEXT
    assert (tmp_path / "src/udp.c").read_text(encoding="utf-8") == CONFLICT_TEXT


def test_modified_paths_reads_worktree_column_only() -> None:
    assert _modified_paths("M  a.txt\nUU b.txt\n") == {"b.txt"}
    assert _modified_paths(" M a.txt\nUU b.txt\n") == {"a.txt", "b.txt"}


def test_cherry_pick_staged_file_does_not_fail_gate(tmp_path: Path) -> None:
    write_conflicted(tmp_path)
    git = FakeGit(tmp_path, status_text="M  src/other.c\nUU src/net.c\n")
    llm = FakeLLM([resolved()])
    agent = ConflictAgent(llm, git, make_safety())

    result = agent.resolve(make_commit(), CONFLICT_FILES)

    assert result is not None
    assert git.staged == [CONFLICT_FILES]


def test_worktree_modified_file_outside_conflict_rejected(tmp_path: Path) -> None:
    write_conflicted(tmp_path)
    git = FakeGit(tmp_path, status_text=" M src/other.c\nUU src/net.c\n")
    llm = FakeLLM([resolved(), resolved(), resolved()])
    agent = ConflictAgent(llm, git, make_safety())

    result = agent.resolve(make_commit(), CONFLICT_FILES)

    assert result is None
    assert git.staged == []
    assert (tmp_path / "src/net.c").read_text(encoding="utf-8") == CONFLICT_TEXT


def test_apply_patch_rejects_context_mismatch() -> None:
    content = "alpha\nbeta\ngamma\n"
    patch = (
        "@@ -1,3 +1,3 @@\n"
        " alpha\n"
        " WRONG\n"
        " gamma\n"
    )
    with pytest.raises(ValueError):
        _apply_patch(content, patch)


def test_apply_patch_rejects_removed_line_mismatch() -> None:
    content = "alpha\nbeta\ngamma\n"
    patch = (
        "@@ -1,3 +1,2 @@\n"
        " alpha\n"
        "-WRONG\n"
        " gamma\n"
    )
    with pytest.raises(ValueError):
        _apply_patch(content, patch)


def test_apply_patch_drops_no_newline_markers() -> None:
    content = "alpha\nbeta\n"
    patch = (
        "@@ -1,2 +1,2 @@\n"
        " alpha\n"
        " beta\n"
        "\\ No newline at end of file\n"
    )
    result = _apply_patch(content, patch)
    assert result == "alpha\nbeta\n"
    assert "\\ No newline" not in result


def test_context_mismatch_diff_causes_invalid_attempt(tmp_path: Path) -> None:
    write_conflicted(tmp_path)
    git = FakeGit(tmp_path, status_text="UU src/net.c\n")
    bad = ConflictResolution(
        files=["src/net.c"],
        diff=(
            "diff --git a/src/net.c b/src/net.c\n"
            "--- a/src/net.c\n"
            "+++ b/src/net.c\n"
            "@@ -1,5 +1,3 @@\n"
            "-<<<<<<< HEAD\n"
            " int value = WRONG;\n"
            "-=======\n"
            "-int value = 2;\n"
            "->>>>>>> 2a5b6c7 (fix bug)\n"
        ),
        agent_reason="misaligned diff",
    )
    llm = FakeLLM([bad, bad, bad])
    agent = ConflictAgent(llm, git, make_safety())

    result = agent.resolve(make_commit(), CONFLICT_FILES)

    assert result is None
    assert git.staged == []
    assert (tmp_path / "src/net.c").read_text(encoding="utf-8") == CONFLICT_TEXT


def test_infrastructure_error_during_diff_check_rolls_back(tmp_path: Path) -> None:
    write_conflicted(tmp_path)
    git = FakeGit(tmp_path, status_text="UU src/net.c\n", fail_diff_check=True)
    llm = FakeLLM([resolved(), resolved(), resolved()])
    agent = ConflictAgent(llm, git, make_safety())

    result = agent.resolve(make_commit(), CONFLICT_FILES)

    assert result is None
    assert git.staged == []
    assert (tmp_path / "src/net.c").read_text(encoding="utf-8") == CONFLICT_TEXT


def test_infrastructure_error_during_status_rolls_back(tmp_path: Path) -> None:
    write_conflicted(tmp_path)
    git = FakeGit(tmp_path, status_text="UU src/net.c\n", fail_status=True)
    llm = FakeLLM([resolved(), resolved(), resolved()])
    agent = ConflictAgent(llm, git, make_safety())

    result = agent.resolve(make_commit(), CONFLICT_FILES)

    assert result is None
    assert git.staged == []
    assert (tmp_path / "src/net.c").read_text(encoding="utf-8") == CONFLICT_TEXT


def test_infrastructure_error_during_stage_rolls_back(tmp_path: Path) -> None:
    write_conflicted(tmp_path)
    git = FakeGit(tmp_path, status_text="UU src/net.c\n", fail_stage=True)
    llm = FakeLLM([resolved(), resolved(), resolved()])
    agent = ConflictAgent(llm, git, make_safety())

    result = agent.resolve(make_commit(), CONFLICT_FILES)

    assert result is None
    assert git.staged == []
    assert (tmp_path / "src/net.c").read_text(encoding="utf-8") == CONFLICT_TEXT


def test_oversized_resolution_diff_is_invalid_attempt(tmp_path: Path) -> None:
    content = _big_content()
    write_conflicted(tmp_path, {"src/net.c": content})
    git = FakeGit(tmp_path, status_text="UU src/net.c\n")
    big = ConflictResolution(
        files=["src/net.c"], diff=_big_diff(), agent_reason="oversized"
    )
    llm = FakeLLM([big, big, big])
    agent = ConflictAgent(llm, git, make_safety())

    result = agent.resolve(make_commit(), CONFLICT_FILES)

    assert result is None
    assert git.staged == []
    assert (tmp_path / "src/net.c").read_text(encoding="utf-8") == content
