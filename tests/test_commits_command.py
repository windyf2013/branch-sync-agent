import pytest

from bsa.commands.commits import list_candidate_commits
from bsa.executor import CompletedProcess, FakeExecutor, InfrastructureError
from bsa.git.service import GitService


def _ok(stdout="", rc=0):
    return CompletedProcess(returncode=rc, stdout=stdout, stderr="")


def _git(ex, repo):
    return GitService(ex, repo)


def test_lists_recent_commits(tmp_path):
    log_out = (
        "abc123|fix the bug|2026-08-20T10:00:00+08:00\n"
        "def456|chore docs|2026-08-19T09:00:00+08:00\n"
    )
    ex = FakeExecutor([_ok("origin/x\nsha"), _ok(log_out)])
    git = _git(ex, tmp_path)
    commits = list_candidate_commits(git, "x", limit=50)
    assert commits[0] == {
        "sha": "abc123",
        "message": "fix the bug",
        "committed_at": "2026-08-20T10:00:00+08:00",
    }
    assert len(commits) == 2


def test_limit_passed_to_git(tmp_path):
    ex = FakeExecutor([_ok("origin/x\nsha"), _ok("a|m|t\n")])
    git = _git(ex, tmp_path)
    list_candidate_commits(git, "x", limit=10)
    args = ex.calls[1][0]
    assert args[0] == "log" and "10" in args


def test_branch_missing_raises(tmp_path):
    ex = FakeExecutor([_ok(rc=1), _ok(rc=1)])
    with pytest.raises(InfrastructureError):
        list_candidate_commits(_git(ex, tmp_path), "nope", limit=50)
