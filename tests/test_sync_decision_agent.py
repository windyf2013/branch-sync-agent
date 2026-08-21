from __future__ import annotations

import json
from pathlib import Path

from bsa.agents.sync_decision import SyncDecisionAgent
from bsa.domain.models import CommitInfo, SyncDecision


def make_commit(**overrides: object) -> CommitInfo:
    values = dict(
        sha="a1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c2d3e",
        message="refactor logging util",
        author="dev",
        committed_at="2026-08-20T10:00:00+08:00",
        changed_files=["src/log.c"],
        patch_text="@@ -1 +1 @@\n-print(a);\n+printf(a);\n",
        symbols=["log_init"],
        patch_id=None,
        issue_ids=[],
        source_branch="develop",
        homologous_section="RTK",
    )
    values.update(overrides)
    return CommitInfo(**values)


class FakeLLM:
    def __init__(self, result: SyncDecision | None = None) -> None:
        self.result = result
        self.calls: list[CommitInfo] = []
        self.risk_result: str | None = None
        self.severity_calls: list[CommitInfo] = []

    def judge_bug_fix(self, commit: CommitInfo) -> SyncDecision:
        self.calls.append(commit)
        if self.result is not None:
            return self.result.model_copy(update={"sha": commit.sha})
        return SyncDecision(
            sha=commit.sha,
            is_bug_fix=True,
            reason="mock judgment",
            recognition_source="agent:bug-fix",
            needs_agent=False,
        )

    def judge_severity(self, commit: CommitInfo) -> str | None:
        self.severity_calls.append(commit)
        return self.risk_result


def test_cached_sha_returned_without_llm_call(tmp_path: Path) -> None:
    sha = "c0ffee0000000000000000000000000000000000"
    cache = {
        sha: {
            "is_bug_fix": True,
            "reason": "human says yes",
            "recognition_source": "agent:bug-fix",
        }
    }
    path = tmp_path / "judgments.json"
    path.write_text(json.dumps(cache), encoding="utf-8")
    llm = FakeLLM()
    agent = SyncDecisionAgent(llm, path)

    result = agent.run([make_commit(sha=sha)])

    assert list(result) == [sha]
    assert result[sha].is_bug_fix is True
    assert result[sha].reason == "human says yes"
    assert result[sha].recognition_source == "agent:bug-fix"
    assert result[sha].needs_agent is False
    assert llm.calls == []


def test_new_sha_calls_llm_and_saves_to_cache(tmp_path: Path) -> None:
    sha = "d00d0000000000000000000000000000000000"
    path = tmp_path / "judgments.json"
    decision = SyncDecision(
        sha=sha,
        is_bug_fix=True,
        reason="llm said bug",
        recognition_source="agent:bug-fix",
        needs_agent=False,
    )
    llm = FakeLLM(decision)
    agent = SyncDecisionAgent(llm, path)

    result = agent.run([make_commit(sha=sha)])

    assert llm.calls == [make_commit(sha=sha)]
    assert result[sha] == decision
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved[sha] == {
        "is_bug_fix": True,
        "reason": "llm said bug",
        "recognition_source": "agent:bug-fix",
        "risk": None,
    }


def test_human_override_takes_priority(tmp_path: Path) -> None:
    sha = "deadbeef00000000000000000000000000000000"
    path = tmp_path / "judgments.json"
    path.write_text(
        json.dumps(
            {
                sha: {
                    "is_bug_fix": True,
                    "reason": "manual review says bug",
                    "recognition_source": "agent:bug-fix",
                }
            }
        ),
        encoding="utf-8",
    )
    llm = FakeLLM(
        SyncDecision(
            sha=sha,
            is_bug_fix=False,
            reason="llm said no",
            recognition_source="agent:not-bug-fix",
            needs_agent=False,
        )
    )
    agent = SyncDecisionAgent(llm, path)

    result = agent.run([make_commit(sha=sha)])

    assert result[sha].is_bug_fix is True
    assert result[sha].reason == "manual review says bug"
    assert llm.calls == []


def test_human_edits_file_between_runs(tmp_path: Path) -> None:
    sha = "feedface00000000000000000000000000000000"
    path = tmp_path / "judgments.json"
    llm = FakeLLM(
        SyncDecision(
            sha=sha,
            is_bug_fix=False,
            reason="llm said no",
            recognition_source="agent:not-bug-fix",
            needs_agent=False,
        )
    )
    agent = SyncDecisionAgent(llm, path)

    first = agent.run([make_commit(sha=sha)])
    assert first[sha].is_bug_fix is False
    assert len(llm.calls) == 1

    data = json.loads(path.read_text(encoding="utf-8"))
    data[sha]["is_bug_fix"] = True
    data[sha]["recognition_source"] = "agent:bug-fix"
    path.write_text(json.dumps(data), encoding="utf-8")

    second = agent.run([make_commit(sha=sha)])

    assert second[sha].is_bug_fix is True
    assert second[sha].reason == "llm said no"
    assert len(llm.calls) == 1


def test_short_sha_prefix_match(tmp_path: Path) -> None:
    full = "abcdef1234567890abcdef1234567890abcdef12"
    path = tmp_path / "judgments.json"
    path.write_text(
        json.dumps(
            {
                "abcdef1": {
                    "is_bug_fix": False,
                    "reason": "prefix match",
                    "recognition_source": "agent:not-bug-fix",
                }
            }
        ),
        encoding="utf-8",
    )
    llm = FakeLLM()
    agent = SyncDecisionAgent(llm, path)

    result = agent.run([make_commit(sha=full)])

    assert result[full].is_bug_fix is False
    assert result[full].reason == "prefix match"
    assert llm.calls == []


def test_cache_persisted_across_runs(tmp_path: Path) -> None:
    sha = "bada55e000000000000000000000000000000000"
    path = tmp_path / "judgments.json"
    llm = FakeLLM()
    agent = SyncDecisionAgent(llm, path)

    agent.run([make_commit(sha=sha)])
    agent2 = SyncDecisionAgent(llm, path)
    result = agent2.run([make_commit(sha=sha)])

    assert len(llm.calls) == 1
    assert result[sha].is_bug_fix is True


def test_cache_round_trip_preserves_null_reason(tmp_path: Path) -> None:
    sha = "0d00a11c00000000000000000000000000000000"
    path = tmp_path / "judgments.json"
    decision = SyncDecision(
        sha=sha,
        is_bug_fix=True,
        reason=None,
        recognition_source="agent:bug-fix",
        needs_agent=False,
    )
    llm = FakeLLM(decision)
    agent = SyncDecisionAgent(llm, path)

    agent.run([make_commit(sha=sha)])

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "reason" in saved[sha]
    assert saved[sha]["reason"] is None

    agent2 = SyncDecisionAgent(llm, path)
    result = agent2.run([make_commit(sha=sha)])
    assert result[sha].reason is None
    assert len(llm.calls) == 1


def test_empty_pending_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "judgments.json"
    llm = FakeLLM()
    agent = SyncDecisionAgent(llm, path)

    result = agent.run([])

    assert result == {}
    assert llm.calls == []
    assert path.exists()


def test_degrade_passthrough_returned_but_not_cached(tmp_path: Path) -> None:
    sha = "c0a1d00000000000000000000000000000000000"
    path = tmp_path / "judgments.json"
    pending = SyncDecision(
        sha=sha,
        is_bug_fix=False,
        reason="LLM 不可用，降级人工审核",
        recognition_source="pending:claude-agent",
        needs_agent=True,
    )
    llm = FakeLLM(pending)
    agent = SyncDecisionAgent(llm, path)

    result = agent.run([make_commit(sha=sha)])

    assert result[sha].needs_agent is True
    assert result[sha].recognition_source == "pending:claude-agent"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert sha not in saved


def test_degraded_run_retries_llm_next_cycle(tmp_path: Path) -> None:
    sha = "c0a1d00000000000000000000000000000000000"
    path = tmp_path / "judgments.json"
    pending = SyncDecision(
        sha=sha,
        is_bug_fix=False,
        reason="LLM 不可用，降级人工审核",
        recognition_source="pending:claude-agent",
        needs_agent=True,
    )
    llm = FakeLLM(pending)
    agent = SyncDecisionAgent(llm, path)

    agent.run([make_commit(sha=sha)])
    result = agent.run([make_commit(sha=sha)])

    assert result[sha].needs_agent is True
    assert len(llm.calls) == 2


def test_degraded_run_retried_when_llm_available(tmp_path: Path) -> None:
    sha = "c0a1d00000000000000000000000000000000000"
    path = tmp_path / "judgments.json"
    pending = SyncDecision(
        sha=sha,
        is_bug_fix=False,
        reason="LLM 不可用，降级人工审核",
        recognition_source="pending:claude-agent",
        needs_agent=True,
    )
    llm = FakeLLM(pending)
    agent = SyncDecisionAgent(llm, path)

    first = agent.run([make_commit(sha=sha)])
    assert first[sha].needs_agent is True

    llm.result = SyncDecision(
        sha=sha,
        is_bug_fix=True,
        reason="llm said bug",
        recognition_source="agent:bug-fix",
        needs_agent=False,
    )
    second = agent.run([make_commit(sha=sha)])

    assert second[sha].needs_agent is False
    assert second[sha].is_bug_fix is True
    assert len(llm.calls) == 2
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved[sha]["recognition_source"] == "agent:bug-fix"


def test_minimal_human_entry_defaults_source(tmp_path: Path) -> None:
    sha = "cafebabe00000000000000000000000000000000"
    path = tmp_path / "judgments.json"
    path.write_text(json.dumps({sha: {"is_bug_fix": True}}), encoding="utf-8")
    llm = FakeLLM()
    agent = SyncDecisionAgent(llm, path)

    result = agent.run([make_commit(sha=sha)])

    assert result[sha].is_bug_fix is True
    assert result[sha].recognition_source == "agent:bug-fix"
    assert result[sha].needs_agent is False
    assert llm.calls == []


def test_resolve_risks_calls_llm_and_caches(tmp_path: Path) -> None:
    sha = "5eed00000000000000000000000000000000000"
    path = tmp_path / "judgments.json"
    llm = FakeLLM()
    llm.risk_result = "high"
    agent = SyncDecisionAgent(llm, path)

    result = agent.resolve_risks([make_commit(sha=sha)])

    assert result == {sha: "high"}
    assert llm.severity_calls == [make_commit(sha=sha)]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved[sha]["risk"] == "high"


def test_resolve_risks_uses_cached_risk_without_llm(tmp_path: Path) -> None:
    sha = "c0ffee0000000000000000000000000000000000"
    path = tmp_path / "judgments.json"
    path.write_text(
        json.dumps(
            {
                sha: {
                    "is_bug_fix": True,
                    "reason": "human says yes",
                    "recognition_source": "agent:bug-fix",
                    "risk": "high",
                }
            }
        ),
        encoding="utf-8",
    )
    llm = FakeLLM()
    agent = SyncDecisionAgent(llm, path)

    result = agent.resolve_risks([make_commit(sha=sha)])

    assert result == {sha: "high"}
    assert llm.severity_calls == []


def test_resolve_risks_invalid_cached_risk_ignored(tmp_path: Path) -> None:
    sha = "badc0de000000000000000000000000000000000"
    path = tmp_path / "judgments.json"
    path.write_text(
        json.dumps(
            {
                sha: {
                    "is_bug_fix": True,
                    "recognition_source": "agent:bug-fix",
                    "risk": "CRITICAL",
                }
            }
        ),
        encoding="utf-8",
    )
    llm = FakeLLM()
    llm.risk_result = "medium"
    agent = SyncDecisionAgent(llm, path)

    result = agent.resolve_risks([make_commit(sha=sha)])

    assert result == {sha: "medium"}
    assert llm.severity_calls == [make_commit(sha=sha)]


def test_resolve_risks_none_risk_not_cached(tmp_path: Path) -> None:
    sha = "0dd0000000000000000000000000000000000000"
    path = tmp_path / "judgments.json"
    llm = FakeLLM()
    llm.risk_result = None
    agent = SyncDecisionAgent(llm, path)

    result = agent.resolve_risks([make_commit(sha=sha)])

    assert result == {sha: None}
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert sha not in saved
