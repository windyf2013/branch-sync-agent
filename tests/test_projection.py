from __future__ import annotations

import json
from types import SimpleNamespace

from bsa.cli import main
from bsa.domain.models import (
    BranchResult,
    CommitInfo,
    CommitResult,
    Report,
)
from bsa.report.projection import (
    _cycle_status,
    projection_payload,
    read_cycle_state,
)
from tests.test_config import valid_env
from tests.test_graph_nodes import TARGET, base_state, commit

CYCLE = "cycle-2026-08-20"


def _settings(tmp_path, *, with_db: bool = True):
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    if with_db:
        (log_dir / "state.sqlite3").write_bytes(b"")
    return SimpleNamespace(log_dir=str(log_dir))


def _fake_tuple(channel_values: dict):
    return SimpleNamespace(checkpoint={"channel_values": channel_values})


def test_projection_payload_serializable_and_correct():
    branch = BranchResult(
        target_branch=TARGET,
        worktree_path="/wt",
        status="SUCCESS",
        commits=[
            CommitResult(sha="a1", cherry_pick="OK", conflict_resolution=None, build={})
        ],
        patch_path="/logs/patch/p.patch",
        stop_reason=None,
    )
    report = Report(
        cycle_id=CYCLE,
        html_path="/logs/report.html",
        summary={"status": "REPORTED"},
        action_required=[{"sha": "a1", "kind": "ManualReview"}],
        decisions_json_path="/logs/decisions.json",
    )
    state = base_state(
        cycle_id=CYCLE,
        status="REPORTED",
        detected_commits=[commit("a1")],
        branch_results={TARGET: branch},
        report=report,
    )

    payload = projection_payload(state)

    # 可直接 json.dumps → 平台侧 JSON 消费
    text = json.dumps(payload, ensure_ascii=False)
    assert payload["cycle_id"] == CYCLE
    assert payload["status"] == "REPORTED"
    assert payload["detected_commits"][0]["sha"] == "a1"
    assert isinstance(payload["detected_commits"][0], dict)
    assert payload["branch_results"][TARGET]["status"] == "SUCCESS"
    assert payload["branch_results"][TARGET]["patch_path"] == "/logs/patch/p.patch"
    assert payload["action_required"] == [{"sha": "a1", "kind": "ManualReview"}]
    assert "a1" in text


def test_projection_payload_defaults_with_empty_state():
    payload = projection_payload(base_state())
    assert payload["detected_commits"] == []
    assert payload["decisions"] == {}
    assert payload["branch_results"] == {}
    assert payload["action_required"] == []
    assert payload["status"] == "NEW"


def test_read_cycle_state_returns_channel_values(monkeypatch, tmp_path):
    state = base_state(status="REPORTED", detected_commits=[commit("a1")])
    captured = {}

    class FakeCp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_tuple(self, config):
            captured["config"] = config
            return _fake_tuple(state)

    monkeypatch.setattr(
        "bsa.report.projection.open_checkpointer", lambda db_path: FakeCp()
    )

    result = read_cycle_state(_settings(tmp_path), CYCLE)

    assert result["status"] == "REPORTED"
    assert isinstance(result["detected_commits"][0], CommitInfo)
    assert captured["config"]["configurable"]["thread_id"] == CYCLE


def test_read_cycle_state_none_when_no_checkpoint(monkeypatch, tmp_path):
    class FakeCp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_tuple(self, config):
            return None

    monkeypatch.setattr(
        "bsa.report.projection.open_checkpointer", lambda db_path: FakeCp()
    )

    assert read_cycle_state(_settings(tmp_path), CYCLE) is None


def test_read_cycle_state_none_when_db_missing(tmp_path):
    assert read_cycle_state(_settings(tmp_path, with_db=False), CYCLE) is None


def test_cycle_status_from_record(tmp_path):
    log_dir = tmp_path / "logs"
    d = log_dir / CYCLE
    d.mkdir(parents=True)
    (d / "cycle.json").write_text(
        json.dumps(
            {
                "cycle_id": CYCLE,
                "status": "REPORTED",
                "report_path": None,
                "mail_status": None,
                "started_at": "t",
                "finished_at": "t",
            }
        ),
        encoding="utf-8",
    )
    assert _cycle_status(SimpleNamespace(log_dir=str(log_dir)), CYCLE) == "REPORTED"


def test_cycle_status_defaults_running_when_no_record(tmp_path):
    assert _cycle_status(_settings(tmp_path), CYCLE) == "running"


def test_report_cli_nonexistent_finished_cycle_returns_one(monkeypatch, tmp_path, capsys):
    env = valid_env()
    env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr("bsa.config.settings.os.environ", env)
    monkeypatch.setattr("bsa.cli.read_cycle_state", lambda settings, cycle_id: None)
    monkeypatch.setattr("bsa.cli._cycle_status", lambda settings, cycle_id: "REPORTED")

    assert main(["report", CYCLE, "--json"]) == 1
    assert "不存在" in capsys.readouterr().err


def test_report_cli_running_cycle_prints_running(monkeypatch, tmp_path, capsys):
    env = valid_env()
    env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr("bsa.config.settings.os.environ", env)
    monkeypatch.setattr("bsa.cli.read_cycle_state", lambda settings, cycle_id: None)
    monkeypatch.setattr("bsa.cli._cycle_status", lambda settings, cycle_id: "running")

    assert main(["report", CYCLE, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "running"}


def test_report_cli_prints_projection(monkeypatch, tmp_path, capsys):
    env = valid_env()
    env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr("bsa.config.settings.os.environ", env)
    state = base_state(status="REPORTED", detected_commits=[commit("a1")])
    monkeypatch.setattr("bsa.cli.read_cycle_state", lambda settings, cycle_id: state)

    assert main(["report", CYCLE, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "REPORTED"
    assert out["detected_commits"][0]["sha"] == "a1"
