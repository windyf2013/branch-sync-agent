from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bsa.agents.base import LLMClient
from bsa.domain.models import CommitInfo, SyncDecision
from bsa.executor.exceptions import InfrastructureError
from bsa.rules.classify import lookup_agent_judgment

_PENDING_SOURCE = "pending:claude-agent"
_AGENT_BUG_FIX_SOURCE = "agent:bug-fix"
_AGENT_NOT_BUG_FIX_SOURCE = "agent:not-bug-fix"


class SyncDecisionAgent:
    """子图：只判 is_bug_fix。按 SHA 缓存 + 人工覆盖优先，不做四态结论。"""

    def __init__(self, llm: LLMClient, judgments_path: Path) -> None:
        self._llm = llm
        self._judgments_path = judgments_path

    def run(self, pending: list[CommitInfo]) -> dict[str, SyncDecision]:
        judgments = self._load()
        decisions: dict[str, SyncDecision] = {}
        for commit in pending:
            entry = lookup_agent_judgment(commit.sha, judgments)
            if entry is not None:
                decisions[commit.sha] = self._from_entry(commit.sha, entry)
                continue
            decision = self._llm.judge_bug_fix(commit)
            judgments[commit.sha] = {
                "is_bug_fix": decision.is_bug_fix,
                "reason": decision.reason,
                "recognition_source": decision.recognition_source,
            }
            decisions[commit.sha] = decision
        self._save(judgments)
        return decisions

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self._judgments_path.exists():
            return {}
        try:
            data = json.loads(self._judgments_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InfrastructureError(
                f"无法读取 judgments 缓存 {self._judgments_path}: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise InfrastructureError(
                f"judgments 缓存格式错误: {self._judgments_path}"
            )
        return {k: v for k, v in data.items() if isinstance(v, dict)}

    def _save(self, judgments: dict[str, dict[str, Any]]) -> None:
        self._judgments_path.parent.mkdir(parents=True, exist_ok=True)
        self._judgments_path.write_text(
            json.dumps(judgments, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _from_entry(sha: str, entry: dict[str, Any]) -> SyncDecision:
        is_bug_fix = bool(entry.get("is_bug_fix"))
        source = str(entry.get("recognition_source") or "").strip()
        if not source:
            source = _AGENT_BUG_FIX_SOURCE if is_bug_fix else _AGENT_NOT_BUG_FIX_SOURCE
        return SyncDecision(
            sha=sha,
            is_bug_fix=is_bug_fix,
            reason=str(entry.get("reason") or "").strip() or None,
            recognition_source=source,
            needs_agent=source == _PENDING_SOURCE,
        )
