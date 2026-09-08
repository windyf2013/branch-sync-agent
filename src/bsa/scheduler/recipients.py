"""周期报告收件人解析：项目经理名单 + 失败时追加失败 commit 的合入人邮箱。

规则（需求，见 plan）：
- 正常：发 `settings.mail_pm_recipients`（项目经理名单）；为空则回退
  `settings.mail_recipients`（老部署零改动兼容）。
- 有失败/报错：在基础名单外，追加每个失败 commit 的「合入人」邮箱——
  commit 自己的 committer（直提场景 author==committer）+ 若该 commit 是经
  个人分支 merge 合入业务分支的，再追引入它的 merge 的 committer。两者去重。
- 去重保序；基础项目经理名单永远保留，不被追加挤掉。

失败判据与 report 正文 `_branch_failure_lines` 对齐：非 SUCCESS 分支上
cherry_pick 非 OK/EMPTY、或任一型号 build FAILED 的 commit。另 `state.errors`
非空（节点级错误）也会触发追加（此时无具体 commit，只保留基础名单）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bsa.config.settings import Settings

if TYPE_CHECKING:
    from bsa.git.service import GitService


def _base_recipients(settings: Settings) -> list[str]:
    """基础收件人：项目经理名单非空用之，否则回退 mail_recipients。"""
    return list(settings.mail_pm_recipients) if settings.mail_pm_recipients else list(
        settings.mail_recipients
    )


def _failed_commit_shas(state: dict[str, Any]) -> list[str]:
    """非 SUCCESS 分支上失败/停批 commit 的 sha，按出现顺序去重。"""
    shas: list[str] = []
    seen: set[str] = set()
    for branch in (state.get("branch_results") or {}).values():
        if branch.status == "SUCCESS":
            continue
        for commit in branch.commits:
            failed = commit.cherry_pick not in ("OK", "EMPTY") or any(
                outcome.status == "FAILED" for outcome in commit.build.values()
            )
            if failed and commit.sha not in seen:
                shas.append(commit.sha)
                seen.add(commit.sha)
    return shas


def _source_branch(state: dict[str, Any], sha: str) -> str | None:
    """从 detected_commits 反查失败 commit 的来源业务分支名；取不到返回 None。"""
    for info in state.get("detected_commits") or []:
        if info.sha == sha:
            return info.source_branch or None
    return None


def _failure_committer_emails(
    state: dict[str, Any], git: GitService
) -> list[str]:
    """失败 commit 的合入人邮箱（committer + 引入 merge 的 committer），去重保序。

    失败 commit 的 sha 是源业务分支上的 commit；「合入人」指把它带进该业务分支的人：
    直提 = commit 自己的 committer；经个人分支 merge = 引入 merge 的 committer。
    git 追溯取不到（直提无 merge、历史改写等）返回空，不报错——收件人宁缺不崩。
    """
    emails: list[str] = []
    seen: set[str] = set()

    def _add(email: str) -> None:
        e = (email or "").strip()
        if e and e.lower() not in seen:
            emails.append(e)
            seen.add(e.lower())

    for sha in _failed_commit_shas(state):
        branch = _source_branch(state, sha)
        # 直提场景：committer == author == 合入人
        _add(git.committer_email(sha))
        # 经 merge 合入：追引入它的 merge 的 committer（须有来源分支才能沿其主线追溯）
        if branch is not None:
            for email in git.merge_committer_emails(sha, f"origin/{branch}"):
                _add(email)
    return emails


def resolve_report_recipients(
    settings: Settings, state: dict[str, Any], git: GitService
) -> list[str]:
    """周期报告收件人：基础（PM 或回退）+ 失败时追加失败 commit 合入人邮箱。

    ``state`` 是周期终态（含 detected_commits / branch_results / errors），
    ``git`` 是 GitService（可取 commit/merge 的 committer 邮箱）。返回去重保序
    的收件人列表，供 ``make_bridge_sender(mail_to=...)`` 使用。
    """
    recipients = _base_recipients(settings)
    seen = {r.lower() for r in recipients}

    has_error = bool(state.get("errors")) or any(
        branch.status != "SUCCESS"
        for branch in (state.get("branch_results") or {}).values()
    )
    if not has_error:
        return recipients

    for email in _failure_committer_emails(state, git):
        if email.lower() not in seen:
            recipients.append(email)
            seen.add(email.lower())
    return recipients
