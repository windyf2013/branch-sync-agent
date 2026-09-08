"""周期报告收件人解析（scheduler/recipients.py）的单元测试。"""

import json
from pathlib import Path

from bsa.config.settings import Settings
from bsa.domain.models import BranchResult, BuildOutcome, CommitInfo, CommitResult
from bsa.scheduler.recipients import resolve_report_recipients

REPO = "/srv/rcios"


def make_settings(
    tmp_path: Path, *, pm: list[str] | None = None, owner_map_path: str = ""
) -> Settings:
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
        module_owner_map_path=owner_map_path,
    )
    if pm is not None:
        kwargs["mail_pm_recipients"] = pm
    return Settings(**kwargs)


class _FakeGit:
    """假 GitService：只记录被追溯的 sha，返回可控邮箱与改动文件。"""

    def __init__(
        self,
        *,
        commits: dict[str, str] | None = None,
        merges: dict[str, list[str]] | None = None,
        files: dict[str, list[str]] | None = None,
    ):
        self.commits = commits or {}
        self.merges = merges or {}
        self.files = files or {}
        self.asked: list[str] = []

    def committer_email(self, sha: str) -> str:
        self.asked.append(sha)
        return self.commits.get(sha, "")

    def merge_committer_emails(self, sha: str, branch: str) -> list[str]:
        self.asked.append(sha)
        return self.merges.get(sha, [])

    def changed_files(self, sha: str) -> list[str]:
        self.asked.append(sha)
        return self.files.get(sha, [])


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


def _owner_map_file(tmp_path: Path, mapping: dict[str, list[str]]) -> str:
    """写一个临时 module_owner_map.json，返回路径。"""
    p = tmp_path / "owner_map.json"
    p.write_text(json.dumps({"module_owner_map": mapping}), encoding="utf-8")
    return str(p)


class TestOwnerAppend:
    """失败时追加模块负责人邮箱（module_owner_map 最长目录前缀匹配）。"""

    SECURITY = "datapath/kernel/rcios/security"
    WANG = "wanghongbin@raisecom.com"

    def test_owner_hit_appends_owner_email(self, tmp_path):
        git = _FakeGit(files={"c1": [f"{self.SECURITY}/fw.c"]})
        s = make_settings(
            tmp_path,
            pm=["pm@x.com"],
            owner_map_path=_owner_map_file(tmp_path, {self.SECURITY: [self.WANG]}),
        )
        out = resolve_report_recipients(
            s,
            _state([_commit_info("c1", "b1")], _branch("FAILED", [_commit_conflict("c1")])),
            git,
        )
        assert out == ["pm@x.com", self.WANG]

    def test_owner_miss_falls_back_to_committer(self, tmp_path):
        # 改动路径未命中 owner map → 只留合入人 committer
        git = _FakeGit(
            commits={"c1": "renjing@raisecom.com"},
            files={"c1": ["unknown/area/x.c"]},
        )
        s = make_settings(
            tmp_path,
            pm=["pm@x.com"],
            owner_map_path=_owner_map_file(tmp_path, {self.SECURITY: [self.WANG]}),
        )
        out = resolve_report_recipients(
            s,
            _state([_commit_info("c1", "b1")], _branch("FAILED", [_commit_conflict("c1")])),
            git,
        )
        assert out == ["pm@x.com", "renjing@raisecom.com"]

    def test_owner_and_committer_both_appended(self, tmp_path):
        # 负责人与合入人并存去重,两者都发
        git = _FakeGit(
            commits={"c1": "renjing@raisecom.com"},
            files={"c1": [f"{self.SECURITY}/fw.c"]},
        )
        s = make_settings(
            tmp_path,
            pm=["pm@x.com"],
            owner_map_path=_owner_map_file(tmp_path, {self.SECURITY: [self.WANG]}),
        )
        out = resolve_report_recipients(
            s,
            _state([_commit_info("c1", "b1")], _branch("FAILED", [_commit_conflict("c1")])),
            git,
        )
        assert out == ["pm@x.com", "renjing@raisecom.com", self.WANG]

    def test_longest_directory_prefix_wins(self, tmp_path):
        # 父目录与子目录都有 key → 取最长前缀的那个负责人
        git = _FakeGit(files={"c1": [f"{self.SECURITY}/fw.c"]})
        s = make_settings(
            tmp_path,
            pm=["pm@x.com"],
            owner_map_path=_owner_map_file(
                tmp_path,
                {
                    "datapath/kernel/rcios": ["root@raisecom.com"],
                    self.SECURITY: [self.WANG],
                },
            ),
        )
        out = resolve_report_recipients(
            s,
            _state([_commit_info("c1", "b1")], _branch("FAILED", [_commit_conflict("c1")])),
            git,
        )
        assert out == ["pm@x.com", self.WANG]

    def test_directory_boundary_no_false_match(self, tmp_path):
        # security 不匹配 securityDomain（目录边界,防误配）
        git = _FakeGit(
            commits={"c1": "renjing@raisecom.com"},
            files={"c1": ["datapath/kernel/rcios/securityDomain/sd.c"]},
        )
        s = make_settings(
            tmp_path,
            pm=["pm@x.com"],
            owner_map_path=_owner_map_file(tmp_path, {self.SECURITY: [self.WANG]}),
        )
        out = resolve_report_recipients(
            s,
            _state([_commit_info("c1", "b1")], _branch("FAILED", [_commit_conflict("c1")])),
            git,
        )
        assert out == ["pm@x.com", "renjing@raisecom.com"]

    def test_multiple_owners_all_appended(self, tmp_path):
        # 改动跨多个负责人模块 → 所有命中负责人都加
        git = _FakeGit(
            files={"c1": [f"{self.SECURITY}/fw.c", "plat/route/r.c"]},
        )
        s = make_settings(
            tmp_path,
            pm=["pm@x.com"],
            owner_map_path=_owner_map_file(
                tmp_path,
                {
                    self.SECURITY: [self.WANG],
                    "plat/route": ["renguohui@raisecom.com"],
                },
            ),
        )
        out = resolve_report_recipients(
            s,
            _state([_commit_info("c1", "b1")], _branch("FAILED", [_commit_conflict("c1")])),
            git,
        )
        assert out == ["pm@x.com", self.WANG, "renguohui@raisecom.com"]

    def test_owner_map_disabled_when_empty_path(self, tmp_path):
        # module_owner_map_path 留空 = 负责人维度关闭,行为同老部署(只合入人)
        git = _FakeGit(
            commits={"c1": "renjing@raisecom.com"},
            files={"c1": [f"{self.SECURITY}/fw.c"]},
        )
        s = make_settings(tmp_path, pm=["pm@x.com"])  # owner_map_path 默认 ""
        out = resolve_report_recipients(
            s,
            _state([_commit_info("c1", "b1")], _branch("FAILED", [_commit_conflict("c1")])),
            git,
        )
        assert out == ["pm@x.com", "renjing@raisecom.com"]

    def test_unreadable_owner_map_file_degrades(self, tmp_path):
        # owner map 文件缺失/损坏 → 不崩,负责人层跳过,合入人仍追加
        git = _FakeGit(commits={"c1": "renjing@raisecom.com"})
        s = make_settings(
            tmp_path,
            pm=["pm@x.com"],
            owner_map_path=str(tmp_path / "no_such_owner_map.json"),
        )
        out = resolve_report_recipients(
            s,
            _state([_commit_info("c1", "b1")], _branch("FAILED", [_commit_conflict("c1")])),
            git,
        )
        assert out == ["pm@x.com", "renjing@raisecom.com"]
