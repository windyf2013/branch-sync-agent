"""周期报告收件人解析：项目经理名单 + 失败时追加失败 commit 的合入人与模块负责人邮箱。

规则（需求）：
- 正常：发 `settings.mail_pm_recipients`（项目经理名单）；为空则回退
  `settings.mail_recipients`（老部署零改动兼容）。
- 有失败/报错：在基础名单外，追加每个失败 commit 的两类相关人邮箱——
  1. 「合入人」：commit 自己的 committer（直提场景 author==committer）+ 若该 commit
     是经个人分支 merge 合入业务分支的，再追引入它的 merge 的 committer。
  2. 「模块负责人」：commit 改动文件路径命中 `settings.module_owner_map_path` 指向的
     module_owner_map.json（`module_owner_map` 段：目录前缀 -> [负责人邮箱]）时，追加
     命中路径的全部负责人邮箱（最长目录前缀匹配，可跨多个负责人模块，目录边界防误配）。
  两者各自取不到邮箱不报错——收件人宁缺不崩。
- 去重保序；基础项目经理名单永远保留，不被追加挤掉。

失败判据与 report 正文 `_branch_failure_lines` 对齐：非 SUCCESS 分支上
cherry_pick 非 OK/EMPTY、或任一型号 build FAILED 的 commit。另 `state.errors`
非空（节点级错误）也会触发追加（此时无具体 commit，只保留基础名单）。
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bsa.config.settings import Settings

if TYPE_CHECKING:
    from bsa.git.service import GitService

logger = logging.getLogger(__name__)


def _base_recipients(settings: Settings) -> list[str]:
    """基础收件人：项目经理名单非空用之，否则回退 mail_recipients。"""
    return list(settings.mail_pm_recipients) if settings.mail_pm_recipients else list(
        settings.mail_recipients
    )


@lru_cache(maxsize=4)
def _load_module_owner_map(path: str) -> dict[str, list[str]]:
    """读 ``module_owner_map`` 段：目录前缀 -> [负责人邮箱]，去空邮箱。

    文件缺失/解析失败记 warning 并返回空（收件人宁缺不崩）；按 path 缓存，周期进程内
    配置不变。空 path 直接返回空（负责人维度关闭）。
    """
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("module_owner_map 读取失败,负责人维度关闭: %s", path)
        return {}
    raw = data.get("module_owner_map") or {}
    return {
        str(key): [e for e in value if isinstance(e, str) and e.strip()]
        for key, value in raw.items()
        if isinstance(value, list)
    }


def _owner_emails_for_file(owner_map: dict[str, list[str]], path: str) -> list[str]:
    """单文件最长目录前缀匹配负责人。命中判据 path==key 或以 key+"/" 开头
    （目录边界，防 ``.../security`` 误配 ``.../securityDomain``）。未命中返回 []。
    """
    best: str | None = None
    for key in owner_map:
        if path == key or path.startswith(key + "/"):
            if best is None or len(key) > len(best):
                best = key
    return owner_map[best] if best is not None else []


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


def _failure_related_emails(
    state: dict[str, Any], git: GitService, owner_map: dict[str, list[str]]
) -> list[str]:
    """失败 commit 的合入人 + 模块负责人邮箱，去重保序。

    失败 commit 的 sha 是源业务分支上的 commit。「合入人」指把它带进该业务分支的人：
    直提 = commit 自己的 committer；经个人分支 merge = 引入 merge 的 committer。
    「模块负责人」= 改动文件最长前缀命中的 owner（空 owner_map 则整层跳过）。
    git 追溯/owner 匹配取不到返回空，不报错——收件人宁缺不崩。
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
        # 模块负责人：改动文件命中 owner map 的全部负责人（跨模块都加）
        if owner_map:
            for path in git.changed_files(sha):
                for email in _owner_emails_for_file(owner_map, path):
                    _add(email)
    return emails


def resolve_report_recipients(
    settings: Settings, state: dict[str, Any], git: GitService
) -> list[str]:
    """周期报告收件人：基础（PM 或回退）+ 失败时追加失败 commit 合入人与模块负责人邮箱。

    ``state`` 是周期终态（含 detected_commits / branch_results / errors），
    ``git`` 是 GitService（可取 commit/merge 的 committer 邮箱与改动文件）。返回去重
    保序的收件人列表，供 ``make_smtp_sender(mail_to=...)`` 使用。
    有失败/报错时，若 ``settings.mail_append_failure_related`` 为 False 则只返回
    基础名单（不追加合入人/模块负责人）。
    """
    recipients = _base_recipients(settings)
    seen = {r.lower() for r in recipients}

    has_error = bool(state.get("errors")) or any(
        branch.status != "SUCCESS"
        for branch in (state.get("branch_results") or {}).values()
    )
    if not has_error:
        return recipients

    if not settings.mail_append_failure_related:
        # 定向/测试发信开关：即便失败/报错也不追加合入人与模块负责人，
        # 收件人锁死基础名单（PM 或回退 mail_recipients）。
        return recipients

    owner_map = _load_module_owner_map(settings.module_owner_map_path)
    for email in _failure_related_emails(state, git, owner_map):
        if email.lower() not in seen:
            recipients.append(email)
            seen.add(email.lower())
    return recipients
