import pytest

from bsa.domain.models import CherryPickResult
from bsa.executor import (
    CompletedProcess,
    FakeExecutor,
    InfrastructureError,
    SafetyViolation,
    WhitelistExecutor,
)
from bsa.git.service import GitService

FIXTURE_DIFF = (
    "diff --git a/file2.txt b/file2.txt\n"
    "index c938d6a..b98cee7 100644\n"
    "--- a/file2.txt\n"
    "+++ b/file2.txt\n"
    "@@ -1 +1,2 @@\n"
    " acgfecdfbbgbffea \tbgb\tefdb\n"
    "+ \te\tab\tefbegc ae\tc\tb\n"
    "diff --git a/file3.txt b/file3.txt\n"
    "index 69ae01b..4298ff4 100644\n"
    "--- a/file3.txt\n"
    "+++ b/file3.txt\n"
    "@@ -1 +1,3 @@\n"
    " \tdfadafgebdfd g cecd\t\te\n"
    "+gfdc\t babccgbg\n"
    "+\tdcfc\t\taf \n"
)
FIXTURE_PATCH_ID = "a0b371bb1d8c1d0a42e35fd0a15f1902aeb436f6"
BINARY_DIFF = (
    "diff --git a/img.bin b/img.bin\n"
    "new file mode 100644\n"
    "index 0000000..339cf73\n"
    "Binary files /dev/null and b/img.bin differ\n"
)
BINARY_PATCH_ID = "85de7519499e364399b06b9bd6e3918abdd2524d"


def ok(stdout: str = "", rc: int = 0) -> CompletedProcess:
    return CompletedProcess(returncode=rc, stdout=stdout, stderr="")


class TestFetchAll:
    def test_runs_fetch_all_prune_in_repo(self, tmp_path):
        executor = FakeExecutor()
        GitService(executor, tmp_path).fetch_all()
        args, kwargs = executor.calls[0]
        assert args == ["fetch", "--all", "--prune"]
        assert kwargs["cwd"] == tmp_path

    def test_success_returns_none(self, tmp_path):
        executor = FakeExecutor([ok()])
        assert GitService(executor, tmp_path).fetch_all() is None

    def test_retries_then_succeeds_with_backoff(self, tmp_path, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr("bsa.git.service.time.sleep", slept.append)
        executor = FakeExecutor([ok(rc=1), ok()])
        GitService(executor, tmp_path).fetch_all()
        assert slept == [30.0]
        assert len(executor.calls) == 2

    def test_exhausts_retries_then_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr("bsa.git.service.time.sleep", lambda _: None)
        executor = FakeExecutor([ok(rc=1), ok(rc=1), ok(rc=1)])
        with pytest.raises(InfrastructureError):
            GitService(executor, tmp_path).fetch_all()
        assert len(executor.calls) == 3

    def test_auth_failure_classified(self, tmp_path, monkeypatch):
        monkeypatch.setattr("bsa.git.service.time.sleep", lambda _: None)
        auth = CompletedProcess(
            returncode=128, stdout="", stderr="Permission denied (publickey)."
        )
        executor = FakeExecutor([auth, auth, auth])
        with pytest.raises(InfrastructureError, match=r"\(auth\)"):
            GitService(executor, tmp_path).fetch_all()

    def test_network_failure_classified(self, tmp_path, monkeypatch):
        monkeypatch.setattr("bsa.git.service.time.sleep", lambda _: None)
        net = CompletedProcess(
            returncode=128, stdout="", stderr="Could not resolve host: git.example.com"
        )
        executor = FakeExecutor([net, net, net])
        with pytest.raises(InfrastructureError, match=r"\(network\)"):
            GitService(executor, tmp_path).fetch_all()

    def test_unknown_failure_classified(self, tmp_path, monkeypatch):
        monkeypatch.setattr("bsa.git.service.time.sleep", lambda _: None)
        other = CompletedProcess(returncode=128, stdout="", stderr="fatal: repo gone")
        executor = FakeExecutor([other, other, other])
        with pytest.raises(InfrastructureError, match=r"\(unknown\)"):
            GitService(executor, tmp_path).fetch_all()


class TestBranchTip:
    def test_prefers_origin_ref(self, tmp_path):
        executor = FakeExecutor([ok(), ok("abc123")])
        assert GitService(executor, tmp_path).branch_tip("feature") == ("origin/feature", "abc123")
        assert executor.calls[0][0] == ["rev-parse", "--verify", "--quiet", "origin/feature"]
        assert executor.calls[1][0] == ["rev-parse", "origin/feature"]
        assert len(executor.calls) == 2

    def test_falls_back_to_local_branch(self, tmp_path):
        executor = FakeExecutor([ok(rc=1), ok(), ok("def456")])
        assert GitService(executor, tmp_path).branch_tip("feature") == ("feature", "def456")
        assert executor.calls[1][0] == ["rev-parse", "--verify", "--quiet", "feature"]
        assert executor.calls[2][0] == ["rev-parse", "feature"]

    def test_unknown_branch_raises(self, tmp_path):
        executor = FakeExecutor([ok(rc=1), ok(rc=1)])
        with pytest.raises(InfrastructureError):
            GitService(executor, tmp_path).branch_tip("nope")


class TestCommitsInWindow:
    def test_builds_log_args(self, tmp_path):
        executor = FakeExecutor([ok()])
        GitService(executor, tmp_path).commits_in_window("2026-01-01", "2026-01-02", "origin/main")
        assert executor.calls[0][0] == [
            "log",
            "--reverse",
            "--first-parent",
            "--since=2026-01-01",
            "--until=2026-01-02",
            "--format=%H",
            "origin/main",
        ]

    def test_parses_sha_list(self, tmp_path):
        executor = FakeExecutor([ok("sha1\nsha2\n\nsha3\n")])
        result = GitService(executor, tmp_path).commits_in_window("s", "u", "r")
        assert result == ["sha1", "sha2", "sha3"]


class TestCommitMetadata:
    def test_parses_author_date_message(self, tmp_path):
        executor = FakeExecutor([ok("Alice|2026-08-21T10:00:00+08:00|fix the bug\n\nbody\n")])
        result = GitService(executor, tmp_path).commit_metadata("abc")
        assert result == ("Alice", "2026-08-21T10:00:00+08:00", "fix the bug\n\nbody\n")
        assert executor.calls[0][0] == ["log", "-1", "--format=%an|%aI|%B", "abc"]

    def test_message_may_contain_pipes(self, tmp_path):
        executor = FakeExecutor([ok("Bob|2026-08-21T10:00:00Z|fix | the | thing\n")])
        result = GitService(executor, tmp_path).commit_metadata("abc")
        assert result == ("Bob", "2026-08-21T10:00:00Z", "fix | the | thing\n")


class TestChangedFiles:
    def test_parses_file_names(self, tmp_path):
        executor = FakeExecutor([ok("parentsha"), ok("a.c\nb.h\n\n")])
        assert GitService(executor, tmp_path).changed_files("abc") == ["a.c", "b.h"]
        assert executor.calls[1][0] == [
            "diff-tree", "--no-commit-id", "--name-only", "-r", "parentsha", "abc"
        ]

    def test_merge_commit_uses_first_parent(self, tmp_path):
        # merge commit 的 %P 是多个父，取首个作 diff 基准，避免 git show 合流 diff 为空。
        executor = FakeExecutor([ok("p1 p2"), ok("a.c\nb.h\n\n")])
        assert GitService(executor, tmp_path).changed_files("merge") == ["a.c", "b.h"]
        assert executor.calls[1][0] == [
            "diff-tree", "--no-commit-id", "--name-only", "-r", "p1", "merge"
        ]

    def test_truncates_at_500_files(self, tmp_path):
        names = "\n".join(f"f{i}.c" for i in range(501))
        executor = FakeExecutor([ok("parentsha"), ok(names)])
        result = GitService(executor, tmp_path).changed_files("abc")
        assert len(result) == 500

    def test_root_commit_is_empty(self, tmp_path):
        executor = FakeExecutor([ok("")])
        assert GitService(executor, tmp_path).changed_files("root") == []
        assert len(executor.calls) == 1


class TestCommitPatch:
    def test_returns_first_parent_diff_tree_output(self, tmp_path):
        # commit_patch 与 patch_id 统一走第一父聚合 diff（changed_files 同源），
        # 取代 git show（对 merge 恒空）。
        executor = FakeExecutor([ok("parentsha"), ok("1\t0\tf.c"), ok("+foo\n-bar\n")])
        assert GitService(executor, tmp_path).commit_patch("abc") == "+foo\n-bar\n"
        assert executor.calls[2][0] == [
            "diff-tree", "--no-commit-id", "-p", "-r", "parentsha", "abc",
        ]

    def test_merge_commit_patch_non_empty(self, tmp_path):
        # merge commit（%P 两个父）走第一父聚合 diff，非空；git show 对 merge 恒空。
        executor = FakeExecutor([ok("p1 p2"), ok("1\t0\tf.c"), ok("+foo\n-bar\n")])
        assert GitService(executor, tmp_path).commit_patch("merge") == "+foo\n-bar\n"
        assert executor.calls[2][0] == [
            "diff-tree", "--no-commit-id", "-p", "-r", "p1", "merge",
        ]

    def test_truncates_at_max_chars(self, tmp_path):
        executor = FakeExecutor([ok("parentsha"), ok("1\t0\tf.c"), ok("x" * 150)])
        assert GitService(executor, tmp_path).commit_patch("abc", max_chars=100) == "x" * 100

    def test_root_commit_is_empty(self, tmp_path):
        executor = FakeExecutor([ok("")])
        assert GitService(executor, tmp_path).commit_patch("root") == ""

    def test_skips_commit_with_over_500_files(self, tmp_path):
        numstat = "\n".join(f"1\t0\tf{i}.c" for i in range(501))
        executor = FakeExecutor([ok("parentsha"), ok(numstat)])
        assert GitService(executor, tmp_path).commit_patch("abc") == ""
        assert len(executor.calls) == 2

    def test_skips_commit_with_over_20k_lines(self, tmp_path):
        executor = FakeExecutor([ok("parentsha"), ok("100000\t0\tbig.c")])
        assert GitService(executor, tmp_path).commit_patch("abc") == ""


class TestPatchId:
    def test_matches_real_git_stable_patch_id(self, tmp_path):
        executor = FakeExecutor([ok("parentsha"), ok("2\t3\tfiles"), ok(FIXTURE_DIFF)])
        result = GitService(executor, tmp_path).patch_id("abc")
        assert result == FIXTURE_PATCH_ID

    def test_patch_id_uses_first_parent_diff_tree(self, tmp_path):
        executor = FakeExecutor([ok("parentsha"), ok("2\t3\tfiles"), ok(FIXTURE_DIFF)])
        GitService(executor, tmp_path).patch_id("abc")
        assert executor.calls[2][0] == [
            "diff-tree", "--no-commit-id", "-p", "-r", "parentsha", "abc",
        ]

    def test_binary_commit_patch_id(self, tmp_path):
        executor = FakeExecutor([ok("parentsha"), ok("1\t0\timg.bin"), ok(BINARY_DIFF)])
        result = GitService(executor, tmp_path).patch_id("abc")
        assert result == BINARY_PATCH_ID

    def test_merge_patch_id_not_constant(self, tmp_path):
        # 两个不同 merge 内容不同 → patch_id 互异，不再所有 merge 同一恒空值
        # （git show 对 merge 恒空，_stable_patch_id("") 对所有 merge 同一值）。
        exec1 = FakeExecutor([ok("p1 p2"), ok("2\t3\tfiles"), ok(FIXTURE_DIFF)])
        exec2 = FakeExecutor([ok("p1 p2"), ok("1\t0\timg.bin"), ok(BINARY_DIFF)])
        id1 = GitService(exec1, tmp_path).patch_id("m1")
        id2 = GitService(exec2, tmp_path).patch_id("m2")
        assert id1 == FIXTURE_PATCH_ID
        assert id2 == BINARY_PATCH_ID
        assert id1 != id2

    def test_root_commit_is_none(self, tmp_path):
        executor = FakeExecutor([ok("")])
        assert GitService(executor, tmp_path).patch_id("root") is None

    def test_big_commit_is_none(self, tmp_path):
        numstat = "\n".join(f"1\t0\tf{i}.c" for i in range(501))
        executor = FakeExecutor([ok("parentsha"), ok(numstat)])
        assert GitService(executor, tmp_path).patch_id("abc") is None


class TestDiffStat:
    def test_aggregates_numstat(self, tmp_path):
        executor = FakeExecutor([ok("parentsha"), ok("10\t2\tf.c\n3\t0\tg.h\n")])
        result = GitService(executor, tmp_path).diff_stat("abc")
        assert result == {"files": 2, "insertions": 13, "deletions": 2}
        assert executor.calls[1][0] == [
            "diff-tree", "--no-commit-id", "--numstat", "-r", "parentsha", "abc",
        ]

    def test_binary_files_counted_no_insertions(self, tmp_path):
        executor = FakeExecutor([ok("parentsha"), ok("-\t-\timg.bin\n")])
        result = GitService(executor, tmp_path).diff_stat("abc")
        assert result == {"files": 1, "insertions": 0, "deletions": 0}

    def test_merge_uses_first_parent(self, tmp_path):
        executor = FakeExecutor([ok("p1 p2"), ok("5\t5\tf.c\n")])
        result = GitService(executor, tmp_path).diff_stat("merge")
        assert result == {"files": 1, "insertions": 5, "deletions": 5}
        assert executor.calls[1][0] == [
            "diff-tree", "--no-commit-id", "--numstat", "-r", "p1", "merge",
        ]

    def test_root_commit_is_none(self, tmp_path):
        executor = FakeExecutor([ok("")])
        assert GitService(executor, tmp_path).diff_stat("root") is None


class TestFileOps:
    def test_file_exists_true(self, tmp_path):
        executor = FakeExecutor([ok("blobsha")])
        assert GitService(executor, tmp_path).file_exists("HEAD", "f.c") is True
        assert executor.calls[0][0] == ["rev-parse", "--verify", "HEAD:f.c"]

    def test_file_exists_false(self, tmp_path):
        executor = FakeExecutor([ok(rc=128)])
        assert GitService(executor, tmp_path).file_exists("HEAD", "f.c") is False

    def test_show_file_returns_content(self, tmp_path):
        executor = FakeExecutor([ok("int x;\n")])
        assert GitService(executor, tmp_path).show_file("HEAD", "f.c") == "int x;\n"
        assert executor.calls[0][0] == ["show", "HEAD:f.c"]

    def test_show_file_missing_returns_none(self, tmp_path):
        executor = FakeExecutor([ok(rc=128)])
        assert GitService(executor, tmp_path).show_file("HEAD", "f.c") is None


class TestIsAncestor:
    def test_true_on_zero_returncode(self, tmp_path):
        executor = FakeExecutor([ok()])
        assert GitService(executor, tmp_path).is_ancestor("a", "b") is True
        assert executor.calls[0][0] == ["merge-base", "--is-ancestor", "a", "b"]

    def test_false_on_returncode_one(self, tmp_path):
        executor = FakeExecutor([ok(rc=1)])
        assert GitService(executor, tmp_path).is_ancestor("a", "b") is False

    def test_other_returncode_raises(self, tmp_path):
        executor = FakeExecutor([ok(rc=2, stdout="", )])
        with pytest.raises(InfrastructureError):
            GitService(executor, tmp_path).is_ancestor("a", "b")


class TestWorktree:
    def test_add_worktree(self, tmp_path):
        executor = FakeExecutor()
        svc = GitService(executor, tmp_path)
        wt = tmp_path / "wt"
        svc.add_worktree("feature", wt)
        assert executor.calls[0][0] == ["worktree", "add", str(wt), "feature"]

    def test_remove_worktree_uses_force(self, tmp_path):
        executor = FakeExecutor()
        GitService(executor, tmp_path).remove_worktree(tmp_path / "wt")
        assert executor.calls[0][0] == ["worktree", "remove", "--force", str(tmp_path / "wt")]


class TestCherryPick:
    def test_ok(self, tmp_path):
        executor = FakeExecutor([ok()])
        assert GitService(executor, tmp_path).cherry_pick("abc") == CherryPickResult(status="OK")
        assert executor.calls[0][0] == ["cherry-pick", "abc"]

    def test_empty(self, tmp_path):
        empty_stderr = CompletedProcess(
            returncode=1, stdout="", stderr="The previous cherry-pick is now empty"
        )
        executor = FakeExecutor([empty_stderr, ok()])
        result = GitService(executor, tmp_path).cherry_pick("abc")
        assert result == CherryPickResult(status="EMPTY")
        assert executor.calls[1][0] == ["cherry-pick", "--skip"]
        assert len(executor.calls) == 2

    def test_empty_skip_failure_raises(self, tmp_path):
        empty_stderr = CompletedProcess(
            returncode=1, stdout="", stderr="The previous cherry-pick is now empty"
        )
        executor = FakeExecutor(
            [empty_stderr, CompletedProcess(returncode=128, stdout="", stderr="fatal")]
        )
        with pytest.raises(InfrastructureError):
            GitService(executor, tmp_path).cherry_pick("abc")

    def test_conflict_reports_unmerged_files(self, tmp_path):
        executor = FakeExecutor(
            [
                CompletedProcess(
                    returncode=1, stdout="", stderr="CONFLICT (content): Merge conflict in f.c"
                ),
                ok("f.c\ndir/g.h\n"),
            ]
        )
        result = GitService(executor, tmp_path).cherry_pick("abc")
        assert result == CherryPickResult(status="CONFLICT", conflict_files=["f.c", "dir/g.h"])
        assert executor.calls[1][0] == ["diff", "--name-only", "--diff-filter=U"]

    def test_failed(self, tmp_path):
        executor = FakeExecutor(
            [
                CompletedProcess(returncode=128, stdout="", stderr="fatal: bad revision 'abc'"),
                ok(),
            ]
        )
        result = GitService(executor, tmp_path).cherry_pick("abc")
        assert result == CherryPickResult(status="FAILED")


class TestWorktreeList:
    def test_parses_porcelain_paths(self, tmp_path):
        out = (
            f"worktree {tmp_path}\n"
            "HEAD abc\n"
            "branch refs/heads/master\n"
            "\n"
            f"worktree {tmp_path}/wt\n"
            "HEAD def\n"
            "branch refs/heads/release\n"
        )
        executor = FakeExecutor([ok(out)])
        result = GitService(executor, tmp_path).list_worktrees()
        assert result == [tmp_path, tmp_path / "wt"]
        assert executor.calls[0][0] == ["worktree", "list", "--porcelain"]

    def test_ignores_detached_metadata_lines(self, tmp_path):
        out = f"worktree {tmp_path}/wt\nHEAD def\ndetached\n\n"
        result = GitService(FakeExecutor([ok(out)]), tmp_path).list_worktrees()
        assert result == [tmp_path / "wt"]


class TestCherryPickContinue:
    def test_runs_continue_with_no_edit(self, tmp_path):
        executor = FakeExecutor([ok()])
        GitService(executor, tmp_path).cherry_pick_continue()
        assert executor.calls[0][0] == ["cherry-pick", "--continue", "--no-edit"]
        assert executor.calls[0][1]["cwd"] == tmp_path

    def test_failure_raises(self, tmp_path):
        executor = FakeExecutor([CompletedProcess(returncode=1, stdout="", stderr="cannot commit")])
        with pytest.raises(InfrastructureError):
            GitService(executor, tmp_path).cherry_pick_continue()


class TestUnmergedAndDiffCheck:
    def test_unmerged_files(self, tmp_path):
        executor = FakeExecutor([ok("f.c\n\ng.h\n")])
        assert GitService(executor, tmp_path).unmerged_files() == ["f.c", "g.h"]
        assert executor.calls[0][0] == ["diff", "--name-only", "--diff-filter=U"]

    def test_diff_check_clean(self, tmp_path):
        assert GitService(FakeExecutor([ok()]), tmp_path).diff_check() is True

    def test_diff_check_not_clean(self, tmp_path):
        assert GitService(FakeExecutor([ok(rc=1)]), tmp_path).diff_check() is False


class TestStage:
    def test_adds_paths(self, tmp_path):
        executor = FakeExecutor()
        svc = GitService(executor, tmp_path)
        svc.stage(["a.c", "b.h"])
        assert executor.calls[0][0] == ["add", "a.c", "b.h"]
        assert executor.calls[0][1]["cwd"] == tmp_path


class TestFormatPatch:
    def test_writes_patch_file(self, tmp_path):
        out_dir = tmp_path / "patches"
        executor = FakeExecutor([ok("---\npatch content\n")])
        svc = GitService(executor, tmp_path)
        result = svc.format_patch("base~1", "head", out_dir, "feature.patch")
        assert executor.calls[0][0] == ["format-patch", "--stdout", "base~1..head"]
        assert result == out_dir / "feature.patch"
        assert (out_dir / "feature.patch").read_text() == "---\npatch content\n"


class TestSnapshotRestore:
    def test_snapshot_reads_existing_files_only(self, tmp_path):
        (tmp_path / "a.c").write_text("int x;\n")
        (tmp_path / "dir").mkdir()
        (tmp_path / "dir" / "b.h").write_text("#define X\n")
        svc = GitService(FakeExecutor(), tmp_path)
        snap = svc.snapshot(["a.c", "dir/b.h", "missing.c"])
        assert snap == {"a.c": "int x;\n", "dir/b.h": "#define X\n"}

    def test_restore_round_trip(self, tmp_path):
        (tmp_path / "a.c").write_text("original\n")
        svc = GitService(FakeExecutor(), tmp_path)
        snap = svc.snapshot(["a.c"])
        (tmp_path / "a.c").write_text("mutated\n")
        svc.restore(snap)
        assert (tmp_path / "a.c").read_text() == "original\n"

    def test_restore_creates_parent_dirs(self, tmp_path):
        svc = GitService(FakeExecutor(), tmp_path)
        svc.restore({"sub/deep/f.c": "content"})
        assert (tmp_path / "sub" / "deep" / "f.c").read_text() == "content"


class TestStatus:
    def test_returns_porcelain_stdout(self, tmp_path):
        executor = FakeExecutor([ok(" M f.c\nA  g.h\n")])
        assert GitService(executor, tmp_path).status() == " M f.c\nA  g.h\n"
        assert executor.calls[0][0] == ["status", "--porcelain"]

    def test_nonzero_raises(self, tmp_path):
        executor = FakeExecutor([ok(rc=128, stdout="", )])
        with pytest.raises(InfrastructureError):
            GitService(executor, tmp_path).status()


class TestWhitelistIntegration:
    def test_all_service_subcommands_pass_through_whitelist(self, tmp_path):
        inner = FakeExecutor()
        svc = GitService(WhitelistExecutor(inner), tmp_path)
        svc.fetch_all()
        svc.branch_tip("feature")
        svc.commits_in_window("s", "u", "r")
        svc.commit_metadata("abc")
        svc.changed_files("abc")
        svc.commit_patch("abc")
        svc.patch_id("abc")
        svc.file_exists("HEAD", "f")
        svc.show_file("HEAD", "f")
        svc.is_ancestor("a", "b")
        svc.add_worktree("feature", tmp_path / "wt")
        svc.list_worktrees()
        svc.remove_worktree(tmp_path / "wt")
        svc.cherry_pick("abc")
        svc.cherry_pick_continue()
        svc.unmerged_files()
        svc.diff_check()
        svc.stage(["f.c"])
        svc.format_patch("base", "head", tmp_path, "o.patch")
        svc.status()
        assert inner.calls, "executor should have been invoked"
        assert all(args[0] == "git" for args, _ in inner.calls)
        used = {args[1] for args, _ in inner.calls}
        assert used <= set(WhitelistExecutor.ALLOWED_GIT)

    def test_non_whitelisted_subcommand_raises_safety_violation(self, tmp_path):
        wrapped = WhitelistExecutor(FakeExecutor())
        with pytest.raises(SafetyViolation):
            wrapped.run(["push", "origin", "main"])
