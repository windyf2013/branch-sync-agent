"""周期报告收件人解析（scheduler/recipients.py）的单元测试。"""

from pathlib import Path

from bsa.config.settings import Settings
from bsa.domain.models import BranchResult, BuildOutcome, CommitInfo, CommitResult
from bsa.scheduler.recipients import resolve_report_recipients

REPO = "/srv/rcios"


def make_settings(tmp_path: Path, *, pm: list[str] | None = None) -> Settings:
    kwargs = dict(
        _env_file=None,
        repo_path=REPO,
        branch_file=f"{REPO}/branch.md",
        worktree_root="/srv/wt",
        llm_model="m",
        llm_api_key="k",
        llm_base_url="u",
        docker_image="rcios-build:latest",
        docker_mount_workspace="/workspace/rcios",
        build_script_dir="build/platform/RTL9617C",
        mail_dry_run=True,
        mail_sender="s",
        mail_recipients=["ops@x.com"],
        log_dir=str(tmp_path / "logs"),
    )
    if pm is not None:
        kwargs["mail_pm_recipients"] = pm
    return Settings(**kwargs)


class _FakeGit:
    """假 GitService：只记录被追溯的 sha，返回可控邮箱。"""

    def __init__(
        self,
        *,
        commits: dict[str, str] | None = None,
        merges: dict[str, list[str]] | None = None,
    ):
        self.commits = commits or {}
        self.merges = merges or {}
        self.asked: list[str] = []

    def committer_email(self, sha: str) -> str:
        self.asked.append(sha)
        return self.commits.get(sha, "")

    def merge_committer_emails(self, sha: str, branch: str) -> list[str]:
        self.asked.append(sha)
        return self.merges.get(sha, [])


def _commit_info(sha: str, source: str) -> CommitInfo:
    return CommitInfo(
        sha=sha,
        message="msg",
        author="a",
        committed_at="2026-09-01T00:00:00+08:00",
        changed_files=["f.c"],
        patch_text="",
        symbols=[],
        patch_id=None,
        issue_ids=[],
        source_branch=source,
        homologous_section="sec",
    )


def _branch(status: str, commits: list[CommitResult]) -> BranchResult:
    return BranchResult(
        target_branch="br_main",
        worktree_path="/srv/wt/wt",
        status=status,
        commits=commits,
        patch_path=None,
        stop_reason=None,
        baseline=None,
    )


def _commit_ok(sha: str) -> CommitResult:
    return CommitResult(sha=sha, cherry_pick="OK", conflict_resolution=None, build={})


def _commit_conflict(sha: str) -> CommitResult:
    return CommitResult(sha=sha, cherry_pick="CONFLICT", conflict_resolution=None, build={})


def _commit_build_failed(sha: str) -> CommitResult:
    return CommitResult(
        sha=sha,
        cherry_pick="OK",
        conflict_resolution=None,
        build={
            "m1": BuildOutcome(
                model="m1",
                status="FAILED",
                log_path=None,
                errors=["boom"],
                agent_attempts=0,
                fix_diff=None,
            )
        },
    )


def _state(detected: list[CommitInfo], branch: BranchResult, *, errors: dict | None = None) -> dict:
    return {
        "detected_commits": detected,
        "branch_results": {branch.target_branch: branch},
        "errors": errors or {},
    }


class TestBaseRecipients:
    def test_pm_empty_falls_back_to_mail_recipients(self, tmp_path):
        git = _FakeGit()
        s = make_settings(tmp_path)  # pm 默认空
        out = resolve_report_recipients(
            s, _state([_commit_info("c1", "b1")], _branch("SUCCESS", [_commit_ok("c1")])), git
        )
        assert out == ["ops@x.com"]

    def test_pm_present_used_over_mail_recipients(self, tmp_path):
        git = _FakeGit()
        s = make_settings(tmp_path, pm=["pm@x.com"])
        out = resolve_report_recipients(
            s, _state([_commit_info("c1", "b1")], _branch("SUCCESS", [_commit_ok("c1")])), git
        )
        assert out == ["pm@x.com"]
        assert git.asked == []  # 成功无失败,不追溯


class TestFailureAppend:
    def test_no_failure_no_append(self, tmp_path):
        git = _FakeGit(commits={"c1": "renjing@raisecom.com"})
        s = make_settings(tmp_path, pm=["pm@x.com"])
        out = resolve_report_recipients(
            s, _state([_commit_info("c1", "b1")], _branch("SUCCESS", [_commit_ok("c1")])), git
        )
        assert out == ["pm@x.com"]

    def test_conflict_appends_committer(self, tmp_path):
        git = _FakeGit(commits={"c1": "renjing@raisecom.com"})
        s = make_settings(tmp_path, pm=["pm@x.com"])
        out = resolve_report_recipients(
            s,
            _state([_commit_info("c1", "b1")], _branch("FAILED", [_commit_conflict("c1")])),
            git,
        )
        assert out == ["pm@x.com", "renjing@raisecom.com"]

    def test_build_failed_appends_committer(self, tmp_path):
        git = _FakeGit(commits={"c1": "renjing@raisecom.com"})
        s = make_settings(tmp_path, pm=["pm@x.com"])
        out = resolve_report_recipients(
            s,
            _state([_commit_info("c1", "b1")], _branch("PARTIAL", [_commit_build_failed("c1")])),
            git,
        )
        assert out == ["pm@x.com", "renjing@raisecom.com"]

    def test_merge_introduced_appends_merge_committer(self, tmp_path):
        git = _FakeGit(
            commits={"c1": ""},  # 经 merge 引入的叶子,自身 committer 可能是原作者或空
            merges={"c1": ["zhangyingqi@raisecom.com"]},
        )
        s = make_settings(tmp_path, pm=["pm@x.com"])
        out = resolve_report_recipients(
            s,
            _state([_commit_info("c1", "b1")], _branch("FAILED", [_commit_conflict("c1")])),
            git,
        )
        assert out == ["pm@x.com", "zhangyingqi@raisecom.com"]

    def test_direct_commit_also_traced_for_merge(self, tmp_path):
        # 直提 commit:自身 committer=合入人,且无引入 merge → 只追加自身 committer
        git = _FakeGit(commits={"c1": "renjing@raisecom.com"}, merges={})
        s = make_settings(tmp_path, pm=["pm@x.com"])
        out = resolve_report_recipients(
            s,
            _state([_commit_info("c1", "b1")], _branch("FAILED", [_commit_conflict("c1")])),
            git,
        )
        assert out == ["pm@x.com", "renjing@raisecom.com"]

    def test_dedup_same_committer_and_merge(self, tmp_path):
        git = _FakeGit(
            commits={"c1": "zhangyingqi@raisecom.com"},
            merges={"c1": ["zhangyingqi@raisecom.com"]},
        )
        s = make_settings(tmp_path, pm=["pm@x.com"])
        out = resolve_report_recipients(
            s,
            _state([_commit_info("c1", "b1")], _branch("FAILED", [_commit_conflict("c1")])),
            git,
        )
        assert out == ["pm@x.com", "zhangyingqi@raisecom.com"]

    def test_multiple_failed_commits_append_dedup(self, tmp_path):
        git = _FakeGit(
            commits={"c1": "renjing@raisecom.com", "c2": "liuzhifen@raisecom.com"},
            merges={"c2": ["zhangyingqi@raisecom.com"]},
        )
        s = make_settings(tmp_path, pm=["pm@x.com"])
        out = resolve_report_recipients(
            s,
            _state(
                [_commit_info("c1", "b1"), _commit_info("c2", "b1")],
                _branch("FAILED", [_commit_conflict("c1"), _commit_conflict("c2")]),
            ),
            git,
        )
        assert out == [
            "pm@x.com",
            "renjing@raisecom.com",
            "liuzhifen@raisecom.com",
            "zhangyingqi@raisecom.com",
        ]

    def test_ok_commit_skipped_failure_append(self, tmp_path):
        # 同一失败分支里,OK 的 commit 不追溯,只有冲突的那个追加
        git = _FakeGit(commits={"ok1": "", "bad1": "renjing@raisecom.com"})
        s = make_settings(tmp_path, pm=["pm@x.com"])
        out = resolve_report_recipients(
            s,
            _state(
                [_commit_info("ok1", "b1"), _commit_info("bad1", "b1")],
                _branch("FAILED", [_commit_ok("ok1"), _commit_conflict("bad1")]),
            ),
            git,
        )
        assert out == ["pm@x.com", "renjing@raisecom.com"]
        # 每个失败 commit 追溯两次:自身 committer + 引入 merge(可能都查不到)
        assert git.asked == ["bad1", "bad1"]

    def test_state_errors_without_branch_failure_keeps_base(self, tmp_path):
        # state.errors 非空触发「有报错」分支,但无失败 commit → 只保留基础名单
        git = _FakeGit()
        s = make_settings(tmp_path, pm=["pm@x.com"])
        out = resolve_report_recipients(
            s,
            {
                **dict(_state([], _branch("SUCCESS", []))),
                "errors": {"n": {"node": "x", "error": "e", "ts": "t"}},
            },
            git,
        )
        assert out == ["pm@x.com"]

    def test_unreachable_committer_email_is_skipped(self, tmp_path):
        # git 取不到邮箱 → 不追加也不崩
        git = _FakeGit(commits={"c1": ""})
        s = make_settings(tmp_path, pm=["pm@x.com"])
        out = resolve_report_recipients(
            s,
            _state([_commit_info("c1", "b1")], _branch("FAILED", [_commit_conflict("c1")])),
            git,
        )
        assert out == ["pm@x.com"]

    def test_no_source_branch_still_appends_committer(self, tmp_path):
        # detected_commits 查不到来源分支 → 不追 merge,仍追自身 committer
        git = _FakeGit(commits={"c1": "renjing@raisecom.com"})
        s = make_settings(tmp_path, pm=["pm@x.com"])
        out = resolve_report_recipients(
            s,
            _state([], _branch("FAILED", [_commit_conflict("c1")])),
            git,
        )
        assert out == ["pm@x.com", "renjing@raisecom.com"]
