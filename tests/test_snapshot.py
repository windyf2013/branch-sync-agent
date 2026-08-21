from __future__ import annotations

from bsa.rules import TargetSnapshot
from bsa.rules.conclude import CommitAnalysis
from bsa.rules.snapshot import (
    _file_similarity,
    _fix_clearly_missing,
    _function_renamed,
    _symbols_on_target,
    build_target_snapshot,
    extract_symbols,
)

SOURCE_TEXT = """#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int demo_check(const char *name, size_t len)
{
    if (NULL == name)
    {
        return -1;
    }
    if (0 == len)
    {
        return -1;
    }
    for (size_t i = 0; i < len; i++)
    {
        if (name[i] == '\\0')
        {
            return -1;
        }
    }
    return 0;
}
"""

TARGET_TEXT = """static int demo_check(const char *name, size_t len)
{
    return 0;
}
"""

PATCH_TEXT = (
    "+static int demo_check(const char *name, size_t len)\n"
    "+{\n"
    "+    if (NULL == name)\n"
    "+    {\n"
    "+        return -1;\n"
    "+    }\n"
    "+    return 0;\n"
    "+}\n"
)


class FakeGit:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.files: dict[tuple[str, str], str] = {}
        self.exists: dict[tuple[str, str], bool] = {}
        self.ancestors: set[tuple[str, str]] = set()
        self.target_shas: dict[str, list[str]] = {}
        self.metadata: dict[str, str] = {}
        self.patch_ids: dict[str, str] = {}
        self.changed: dict[str, list[str]] = {}

    def _record(self, name: str, args: tuple) -> None:
        self.calls.append((name, args))

    def add_file(self, ref: str, path: str, text: str) -> None:
        self.files[(ref, path)] = text
        self.exists[(ref, path)] = True

    def is_ancestor(self, sha: str, ref: str) -> bool:
        self._record("is_ancestor", (sha, ref))
        return (sha, ref) in self.ancestors

    def commits_in_window(self, since: str, until: str, ref: str) -> list[str]:
        self._record("commits_in_window", (since, until, ref))
        return list(self.target_shas.get(ref, []))

    def commit_metadata(self, sha: str) -> tuple[str, str, str]:
        self._record("commit_metadata", (sha,))
        return ("dev", "2026-01-01T10:00:00+08:00", self.metadata.get(sha, f"msg-{sha}"))

    def patch_id(self, sha: str) -> str | None:
        self._record("patch_id", (sha,))
        return self.patch_ids.get(sha)

    def changed_files(self, sha: str) -> list[str]:
        self._record("changed_files", (sha,))
        return list(self.changed.get(sha, []))

    def file_exists(self, ref: str, path: str) -> bool:
        self._record("file_exists", (ref, path))
        return self.exists.get((ref, path), False)

    def show_file(self, ref: str, path: str) -> str | None:
        self._record("show_file", (ref, path))
        return self.files.get((ref, path))


def _analysis(**overrides: object) -> CommitAnalysis:
    base = CommitAnalysis(
        sha="a1",
        message="[BUG] CQ12345 Fix null deref",
        changed_files=["plat/demo/demo.c"],
        symbols=["demo_check"],
        patch_text=PATCH_TEXT,
        patch_id="pid-a1",
        issue_ids=["CQ12345"],
        recognition_source="machine:[BUG]",
        source_branch="br_v4_LineA_develop_a_20260101",
        source_branch_type="develop",
        homologous_section="LineA",
    )
    return base.model_copy(update=overrides)


def _build(**overrides: object) -> TargetSnapshot:
    git = FakeGit()
    git.add_file("origin/source", "plat/demo/demo.c", SOURCE_TEXT)
    git.add_file("origin/target", "plat/demo/demo.c", TARGET_TEXT)
    git.target_shas["origin/target"] = ["t1", "t2"]
    git.metadata["t1"] = "CQ99999 fix other"
    git.patch_ids["t2"] = "pid-t2"
    git.changed["t2"] = ["plat/demo/demo.c"]
    base = {
        "source": _analysis(),
        "target_branch": "br_v4_LineA_release_20260101",
        "target_branch_type": "release",
        "git": git,
        "target_ref": "origin/target",
        "source_ref": "origin/source",
    }
    base.update(overrides)
    return build_target_snapshot(**base)


# --- extract_symbols ---


def test_extract_symbols_from_patch_text():
    patch = (
        "+static int demo_check(const char *name, size_t len)\n"
        "+{\n"
        "+    return demo_check_inner(name);\n"
        "+}\n"
    )
    assert extract_symbols(patch) == ["demo_check", "demo_check_inner"]


def test_extract_symbols_dedupes():
    patch = "+foo(a);\n+bar(b);\n+foo(c);\n"
    assert extract_symbols(patch) == ["foo", "bar"]


def test_extract_symbols_no_match():
    assert extract_symbols("+no parens here\n") == []


# --- file_similarity ---


def test_file_similarity_identical():
    assert _file_similarity("abc", "abc") == 1.0


def test_file_similarity_empty_both():
    assert _file_similarity("", "") == 1.0


def test_file_similarity_disjoint():
    assert _file_similarity("abcdef", "xyz") == 0.0


def test_file_similarity_missing_side_none():
    assert _file_similarity(None, "abc") is None
    assert _file_similarity("abc", None) is None


def test_file_similarity_caps_long_inputs_returns_none():
    # 大文件内容不同时，截断后 ratio 不可靠（真机测试 zebra_cli.c 虚高 1.0 误判），返回 None
    assert _file_similarity("a" * 25000, "b" * 25000) is None
    # 大文件完全相同 → 相等短路，1.0 可靠
    assert _file_similarity("a" * 25000, "a" * 25000) == 1.0
    # 小文件正常计算
    assert _file_similarity("a" * 100, "b" * 100) == 0.0


# --- fix_clearly_missing ---


def test_fix_clearly_missing_guard_source_only():
    assert _fix_clearly_missing("if (NULL == p) return -1;", "return 0;") is True


def test_fix_clearly_missing_guard_both():
    assert _fix_clearly_missing("if (NULL == p) return -1;", "if (NULL == p) return -1;") is False


def test_fix_clearly_missing_guard_neither():
    assert _fix_clearly_missing("return 0;", "return 0;") is False


def test_fix_clearly_missing_missing_text_false():
    assert _fix_clearly_missing(None, "return -1;") is False
    assert _fix_clearly_missing("return -1;", None) is False


# --- symbols_on_target ---


def test_symbols_on_target_present_and_absent():
    assert _symbols_on_target("int foo(void) { return 0; }", ["foo", "bar"]) == {
        "foo": True,
        "bar": False,
    }


def test_symbols_on_target_none_text():
    assert _symbols_on_target(None, ["foo"]) == {"foo": False}


def test_symbols_on_target_empty_symbols():
    assert _symbols_on_target("foo", []) == {}


# --- function_renamed ---


def test_function_renamed_some_present():
    assert _function_renamed(["foo", "bar"], {"foo": True, "bar": False}) is True


def test_function_renamed_all_present():
    assert _function_renamed(["foo"], {"foo": True}) is False


def test_function_renamed_none_present():
    assert _function_renamed(["foo"], {"foo": False}) is False


def test_function_renamed_no_symbols():
    assert _function_renamed([], {}) is False


# --- build_target_snapshot ---


def test_build_target_snapshot_full_fake_git():
    snapshot = _build()

    assert isinstance(snapshot, TargetSnapshot)
    assert snapshot.branch_name == "br_v4_LineA_release_20260101"
    assert snapshot.branch_type == "release"
    assert snapshot.has_source_sha is False
    assert snapshot.in_same_homologous_set is True
    assert snapshot.target_commit_messages == ["CQ99999 fix other", "msg-t2"]
    assert snapshot.target_issue_ids == {"CQ99999"}
    assert snapshot.target_patch_ids == {"pid-t2"}
    assert snapshot.files_on_target == {"plat/demo/demo.c"}
    assert snapshot.symbols_on_target == {"demo_check": True}
    assert snapshot.file_similarity is not None and 0.0 < snapshot.file_similarity < 0.50
    assert snapshot.fix_clearly_missing is True
    assert snapshot.function_renamed is False


def test_build_target_snapshot_has_source_sha_short_circuits():
    git = FakeGit()
    git.ancestors = {("a1", "origin/target")}
    snapshot = _build(git=git)

    assert snapshot.has_source_sha is True
    assert snapshot.target_commit_messages == []
    assert snapshot.target_patch_ids == set()
    assert snapshot.files_on_target == set()
    assert snapshot.file_similarity is None


def test_build_target_snapshot_symbol_missing_on_target():
    git = FakeGit()
    git.add_file("origin/source", "plat/demo/demo.c", SOURCE_TEXT)
    git.add_file("origin/target", "plat/demo/demo.c", "int demo_check(void) { return 0; }")
    snapshot = _build(git=git, source=_analysis(symbols=["demo_check", "renamed_api"]))

    assert snapshot.symbols_on_target == {"demo_check": True, "renamed_api": False}
    assert snapshot.function_renamed is True


def test_build_target_snapshot_no_similarity_when_files_missing():
    git = FakeGit()
    snapshot = _build(git=git)

    assert snapshot.files_on_target == set()
    assert snapshot.file_similarity is None
    assert snapshot.symbols_on_target == {"demo_check": False}
