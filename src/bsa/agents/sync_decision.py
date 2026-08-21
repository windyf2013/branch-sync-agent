from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from bsa.agents.base import LLMClient
from bsa.domain.models import CommitInfo, SyncDecision
from bsa.executor.exceptions import InfrastructureError
from bsa.rules.classify import lookup_agent_judgment

_PENDING_SOURCE = "pending:claude-agent"
_AGENT_BUG_FIX_SOURCE = "agent:bug-fix"
_AGENT_NOT_BUG_FIX_SOURCE = "agent:not-bug-fix"

_RISK_VALUES = ("low", "medium", "high")


class SyncDecisionAgent:
    """子图：判 is_bug_fix（决策 6 缓存 + 人工覆盖优先）+ 严重性 risk 兜底（决策 41）。"""

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
            decisions[commit.sha] = decision
            if not decision.needs_agent:
                judgments[commit.sha] = {
                    "is_bug_fix": decision.is_bug_fix,
                    "reason": decision.reason,
                    "recognition_source": decision.recognition_source,
                    "risk": decision.risk,
                }
        self._save(judgments)
        return decisions

    def resolve_risks(self, commits: list[CommitInfo]) -> dict[str, str | None]:
        """规则层无法判定的严重性交 LLM 兜底（决策 41）；返回 {sha: risk|None}。"""
        judgments = self._load()
        out: dict[str, str | None] = {}
        for commit in commits:
            entry = lookup_agent_judgment(commit.sha, judgments)
            cached = _coerce_risk(entry.get("risk")) if entry is not None else None
            if cached is not None:
                out[commit.sha] = cached
                continue
            risk = self._llm.judge_severity(commit)
            out[commit.sha] = risk
            if risk is not None:
                judgments.setdefault(commit.sha, {})["risk"] = risk
        self._save(judgments)
        return out

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
            risk=_coerce_risk(entry.get("risk")),
        )


def _coerce_risk(value: Any) -> Literal["low", "medium", "high"] | None:
    if value in _RISK_VALUES:
        return value
    return None
