from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

ISSUE_ID_RE = re.compile(
    r"\b(?:CQ|ISSUE|BUG|JIRA|RT)[-# ]?(\d+)\b",
    re.IGNORECASE,
)
CHERRY_PICK_RE = re.compile(
    r"\(cherry picked from commit ([0-9a-f]{6,40})\)",
    re.IGNORECASE,
)

PENDING_AGENT_SOURCE = "pending:claude-agent"
AGENT_BUG_FIX_SOURCE = "agent:bug-fix"
AGENT_NOT_BUG_FIX_SOURCE = "agent:not-bug-fix"


@dataclass
class Classification:
    is_bug_fix: bool
    recognition_source: str
    issue_ids: list[str] = field(default_factory=list)
    cherry_pick_from: str | None = None
    reason: str | None = None
    needs_agent: bool = False


def _extract_issue_ids(message: str) -> list[str]:
    seen: set[str] = set()
    issue_ids: list[str] = []
    for match in ISSUE_ID_RE.finditer(message):
        prefix = match.group(0).split("-")[0].split("#")[0].strip().upper()
        if prefix.startswith("CQ"):
            token = f"CQ{match.group(1)}"
        elif prefix.startswith("ISSUE"):
            token = f"ISSUE{match.group(1)}"
        elif prefix.startswith("BUG"):
            token = f"BUG{match.group(1)}"
        elif prefix.startswith("JIRA"):
            token = f"JIRA{match.group(1)}"
        elif prefix.startswith("RT"):
            token = f"RT{match.group(1)}"
        else:
            token = match.group(0).upper().replace(" ", "").replace("#", "")
        if token not in seen:
            seen.add(token)
            issue_ids.append(token)
    return issue_ids


def lookup_agent_judgment(
    sha: str,
    judgments: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Match full SHA or any judgment key that is a prefix of sha (min 7 chars)."""
    if not judgments or not sha:
        return None
    sha_l = sha.lower()
    if sha_l in judgments and isinstance(judgments[sha_l], dict):
        return judgments[sha_l]
    if sha in judgments and isinstance(judgments[sha], dict):
        return judgments[sha]
    best_key = ""
    best_val: dict[str, Any] | None = None
    for key, value in judgments.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        key_l = key.lower().strip()
        if len(key_l) < 7:
            continue
        if sha_l.startswith(key_l) and len(key_l) > len(best_key):
            best_key = key_l
            best_val = value
    return best_val


def classify_commit(
    message: str,
    changed_files: list[str],
    symbols: list[str],
    patch_text: str,
    *,
    sha: str | None = None,
    agent_judgments: Mapping[str, Any] | None = None,
) -> Classification:
    _ = (symbols, patch_text)
    cherry_match = CHERRY_PICK_RE.search(message)
    cherry_pick_from = cherry_match.group(1) if cherry_match else None
    issue_ids = _extract_issue_ids(message)

    if re.search(r"\[BUG\]", message, re.IGNORECASE):
        return Classification(
            is_bug_fix=True,
            recognition_source="machine:[BUG]",
            issue_ids=issue_ids,
            cherry_pick_from=cherry_pick_from,
        )

    if re.search(r":bug:", message, re.IGNORECASE):
        return Classification(
            is_bug_fix=True,
            recognition_source="machine::bug:",
            issue_ids=issue_ids,
            cherry_pick_from=cherry_pick_from,
        )

    if re.search(r"(?i)\bfix\s*[:(]", message):
        return Classification(
            is_bug_fix=True,
            recognition_source="machine:fix:",
            issue_ids=issue_ids,
            cherry_pick_from=cherry_pick_from,
        )

    if re.search(
        r"(修复|缺陷|故障|空指针|内存泄漏|宕机|崩溃|挂死|重启|段错误|踩内存|非法内存|segmentation\s*fault|segfault)",
        message,
        re.IGNORECASE,
    ):
        return Classification(
            is_bug_fix=True,
            recognition_source="machine:zh-fix",
            issue_ids=issue_ids,
            cherry_pick_from=cherry_pick_from,
        )

    if issue_ids:
        return Classification(
            is_bug_fix=True,
            recognition_source="machine:issue",
            issue_ids=issue_ids,
            cherry_pick_from=cherry_pick_from,
        )

    if cherry_pick_from is not None:
        return Classification(
            is_bug_fix=True,
            recognition_source="machine:cherry-pick",
            issue_ids=issue_ids,
            cherry_pick_from=cherry_pick_from,
        )

    if not changed_files:
        return Classification(
            is_bug_fix=False,
            recognition_source="not-included",
            reason="无关联文件可提取（多为 merge 等），不交 Agent 判定。",
        )

    judgment = lookup_agent_judgment(sha or "", agent_judgments)
    if judgment is not None:
        is_bug = bool(judgment.get("is_bug_fix"))
        reason = str(judgment.get("reason") or "").strip() or (
            "Claude 主 Agent 判定结果。"
        )
        return Classification(
            is_bug_fix=is_bug,
            recognition_source=AGENT_BUG_FIX_SOURCE if is_bug else AGENT_NOT_BUG_FIX_SOURCE,
            issue_ids=issue_ids,
            cherry_pick_from=cherry_pick_from,
            reason=reason,
            needs_agent=False,
        )

    # --- machine:not-bug-fix 自动判定（减少 pending 量） ---

    if re.search(r"(?i)^build version", message):
        return Classification(
            is_bug_fix=False,
            recognition_source="machine:version-bump",
            reason="版本号升级。",
        )

    if (
        len(changed_files) == 1
        and changed_files[0].endswith("ios_version.h")
    ):
        return Classification(
            is_bug_fix=False,
            recognition_source="machine:version-file",
            reason="仅修改版本文件 ios_version.h。",
        )

    if re.search(r"(?i)^(chore|docs)\(", message):
        return Classification(
            is_bug_fix=False,
            recognition_source="machine:chore-docs",
            reason="工程/文档类提交（chore/docs 前缀）。",
        )

    if re.search(r"AI\s+IGNOR", message, re.IGNORECASE):
        return Classification(
            is_bug_fix=False,
            recognition_source="machine:ai-ignor",
            reason="AI IGNOR 标记。",
        )

    if changed_files and all(
        f.startswith("openspec/") or f == ".gitignore"
        for f in changed_files
    ):
        return Classification(
            is_bug_fix=False,
            recognition_source="machine:docs-only",
            reason="仅涉及 openspec/ 或 .gitignore 文件。",
        )

    return Classification(
        is_bug_fix=False,
        recognition_source=PENDING_AGENT_SOURCE,
        issue_ids=issue_ids,
        cherry_pick_from=cherry_pick_from,
        reason="无机器可读修复标记；交由 Claude 主 Agent 阅读 diff 后判定。",
        needs_agent=True,
    )
