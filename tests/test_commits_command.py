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


def test_refresh_fetches_specific_branch_first(tmp_path):
    log_out = "abc123|fix the bug|2026-08-20T10:00:00+08:00\n"
    ex = FakeExecutor([_ok(), _ok("origin/x\nsha"), _ok(log_out)])
    git = _git(ex, tmp_path)
    list_candidate_commits(git, "x", limit=50, refresh=True)
    assert ex.calls[0][0] == ["fetch", "origin", "x"]
    assert ex.calls[1][0] == ["rev-parse", "--verify", "--quiet", "origin/x"]
    assert ex.calls[2][0][0] == "log"


def test_refresh_fetch_failure_raises(tmp_path):
    ex = FakeExecutor(
        [CompletedProcess(returncode=128, stdout="", stderr="Permission denied (publickey).")]
    )
    with pytest.raises(InfrastructureError, match=r"\(auth\)"):
        list_candidate_commits(_git(ex, tmp_path), "x", limit=50, refresh=True)


def test_without_refresh_does_not_fetch(tmp_path):
    log_out = "abc123|fix the bug|2026-08-20T10:00:00+08:00\n"
    ex = FakeExecutor([_ok("origin/x\nsha"), _ok(log_out)])
    git = _git(ex, tmp_path)
    list_candidate_commits(git, "x", limit=50)
    assert [c[0][0] for c in ex.calls] == ["rev-parse", "log"]


def test_cmd_commits_refresh_passes_through_without_lock(monkeypatch, tmp_path, capsys):
    # --refresh 的 git fetch 只更新远端 ref，不持全局锁，否则被正在跑的任务阻塞。
    # 此处断言 list_candidate_commits 直接收到 refresh=True（无 flock 包裹）。
    from types import SimpleNamespace

    from bsa.cli import _cmd_commits

    received: dict = {}

    def fake_list(git, src, limit=50, refresh=False):
        received["refresh"] = refresh
        return [{"sha": "a", "message": "m", "committed_at": "t"}]

    monkeypatch.setattr("bsa.cli.load_settings", lambda: SimpleNamespace(log_dir=str(tmp_path)))
    monkeypatch.setattr(
        "bsa.cli.build_graph_context",
        lambda settings: SimpleNamespace(git=SimpleNamespace()),
    )
    monkeypatch.setattr("bsa.cli.list_candidate_commits", fake_list)

    code = _cmd_commits(SimpleNamespace(src="x", limit=50, refresh=True))

    assert code == 0
    assert received["refresh"] is True
    assert "a" in capsys.readouterr().out
