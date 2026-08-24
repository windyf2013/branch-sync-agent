from __future__ import annotations

import json

import pytest

from bsa.commands.override import apply_override
from bsa.executor.lock import LockTimeoutError, flock_acquire
from tests.test_sync_decision_agent import FakeLLM, make_commit


def _read(log_dir) -> dict:
    return json.loads((log_dir / "judgments.json").read_text(encoding="utf-8"))


def test_writes_new_entry(tmp_path):
    sha = "a1b2c3d4e5f6"
    result = apply_override(tmp_path, sha, is_bug_fix=True, risk="high")

    assert result == {
        "is_bug_fix": True,
        "risk": "high",
        "recognition_source": "manual-override",
    }
    assert _read(tmp_path)[sha] == result


def test_merges_existing_entries_other_sha_preserved(tmp_path):
    other = "ffeeddccbbaa"
    (tmp_path / "judgments.json").write_text(
        json.dumps({other: {"is_bug_fix": False, "recognition_source": "agent:not-bug-fix"}}),
        encoding="utf-8",
    )

    sha = "a1b2c3d4e5f6"
    apply_override(tmp_path, sha, is_bug_fix=True)

    data = _read(tmp_path)
    assert set(data) == {other, sha}
    assert data[other] == {"is_bug_fix": False, "recognition_source": "agent:not-bug-fix"}
    assert data[sha]["is_bug_fix"] is True
    assert data[sha]["recognition_source"] == "manual-override"


def test_partial_params_only_is_bug_fix(tmp_path):
    sha = "a1b2c3d4e5f6"
    result = apply_override(tmp_path, sha, is_bug_fix=False)

    assert result == {"is_bug_fix": False, "recognition_source": "manual-override"}
    assert "risk" not in _read(tmp_path)[sha]


def test_partial_params_only_risk(tmp_path):
    sha = "a1b2c3d4e5f6"
    result = apply_override(tmp_path, sha, risk="low")

    assert result == {"risk": "low", "recognition_source": "manual-override"}
    assert "is_bug_fix" not in _read(tmp_path)[sha]


def test_creates_parent_dir_when_missing(tmp_path):
    log_dir = tmp_path / "nested" / "logs"
    sha = "a1b2c3d4e5f6"

    result = apply_override(log_dir, sha, is_bug_fix=True)

    assert result["recognition_source"] == "manual-override"
    assert (log_dir / "judgments.json").exists()


def test_holds_global_flock_while_writing(tmp_path):
    sha = "a1b2c3d4e5f6"
    with flock_acquire(tmp_path / "bsa.lock"):
        with pytest.raises(LockTimeoutError):
            apply_override(tmp_path, sha, is_bug_fix=True, timeout=0.2)


def test_entry_readable_by_sync_decision_agent(tmp_path):
    from bsa.agents.sync_decision import SyncDecisionAgent

    sha = "a1b2c3d4e5f60000000000000000000000000000"
    apply_override(tmp_path, sha, is_bug_fix=True, risk="medium")

    llm = FakeLLM()
    agent = SyncDecisionAgent(llm, tmp_path / "judgments.json")
    result = agent.run([make_commit(sha=sha)])

    assert result[sha].is_bug_fix is True
    assert result[sha].recognition_source == "manual-override"
    assert result[sha].needs_agent is False
    assert result[sha].risk == "medium"
    assert llm.calls == []


def test_override_cli_writes_judgment(monkeypatch, tmp_path, capsys):
    from tests.test_config import valid_env

    from bsa.cli import main

    env = valid_env()
    env["LOG_DIR"] = str(tmp_path)
    monkeypatch.setattr("bsa.config.settings.os.environ", env)

    code = main(["override", "a1b2c3d4e5f6", "--is-bug-fix", "--risk", "high"])

    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "is_bug_fix": True,
        "risk": "high",
        "recognition_source": "manual-override",
    }
    assert _read(tmp_path)["a1b2c3d4e5f6"] == out


def test_override_cli_missing_env_exits_two(monkeypatch):
    from bsa.cli import main

    monkeypatch.setattr("bsa.config.settings.os.environ", {})

    assert main(["override", "a1b2c3d4e5f6", "--is-bug-fix"]) == 2
