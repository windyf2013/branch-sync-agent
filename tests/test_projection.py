from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from bsa.cli import main
from bsa.domain.models import (
    BranchResult,
    CommitInfo,
    CommitResult,
    ErrorRecord,
    Report,
)
from bsa.graph.state import TaskState
from bsa.report.projection import (
    _cycle_status,
    cycle_failure_text,
    projection_payload,
    read_cycle_state,
    read_projection_payload,
    write_state_json,
)
from tests.test_config import valid_env
from tests.test_graph_nodes import TARGET, base_state, commit

CYCLE = "cycle-2026-08-20"


def _patch_cycle_state(monkeypatch, state):
    class FakeCp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_tuple(self, config):
            return None if state is None else _fake_tuple(state)

    monkeypatch.setattr(
        "bsa.report.projection.open_checkpointer", lambda db_path: FakeCp()
    )


def test_taskstate_declares_sources_and_targets():
    # LangGraph 按 TypedDict 声明建 channel，未声明的键在节点 update 中被静默丢弃。
    # 必须显式声明 sources/targets，否则 detect_commits 已算好的完整拓扑（含零检出源）
    # 落不到 checkpoint，投影顶层恒为空，「零检出」与「漏扫」无法区分。
    assert "sources" in TaskState.__annotations__
    assert "targets" in TaskState.__annotations__


def test_projection_payload_includes_sources_and_targets():
    state = base_state(
        status="REPORTED",
        sources=["br_src_zero", "br_src_has_commits"],
        targets=["br_target"],
    )

    payload = projection_payload(state)

    assert payload["sources"] == ["br_src_zero", "br_src_has_commits"]
    assert payload["targets"] == ["br_target"]


def test_projection_payload_sources_default_empty():
    # 旧周期无 sources/targets → 空列表兜底，不崩（存量降级）。
    payload = projection_payload(base_state())
    assert payload["sources"] == []
    assert payload["targets"] == []


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


# --- state.json：投影读结构化 state.json（G11） ---


def test_write_state_json_produces_serializable_payload(tmp_path):
    settings = _settings(tmp_path)
    state = base_state(status="SUCCESS", detected_commits=[commit("a1")])

    path = write_state_json(settings.log_dir, CYCLE, state)

    assert path == Path(settings.log_dir) / CYCLE / "state.json"
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["status"] == "SUCCESS"
    assert data["detected_commits"][0]["sha"] == "a1"


def test_read_projection_payload_prefers_state_json(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    write_state_json(settings.log_dir, CYCLE, base_state(status="SUCCESS"))
    # 即便 checkpoint 能读到不同状态，state.json 优先。
    monkeypatch.setattr(
        "bsa.report.projection.read_cycle_state",
        lambda settings, cycle_id: base_state(status="REPORTED"),
    )

    assert read_projection_payload(settings, CYCLE)["status"] == "SUCCESS"


def test_read_projection_payload_falls_back_to_checkpoint(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        "bsa.report.projection.read_cycle_state",
        lambda settings, cycle_id: base_state(
            status="SUCCESS", detected_commits=[commit("a1")]
        ),
    )

    payload = read_projection_payload(settings, CYCLE)

    assert payload["status"] == "SUCCESS"
    assert payload["detected_commits"][0]["sha"] == "a1"
    assert isinstance(payload["detected_commits"][0], dict)


def test_read_projection_payload_none_when_no_state_json_or_checkpoint(tmp_path):
    assert read_projection_payload(_settings(tmp_path, with_db=False), CYCLE) is None


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
    monkeypatch.setattr("bsa.cli.read_projection_payload", lambda settings, cycle_id: None)
    monkeypatch.setattr("bsa.cli._cycle_status", lambda settings, cycle_id: "REPORTED")

    assert main(["report", CYCLE, "--json"]) == 1
    assert "不存在" in capsys.readouterr().err


def test_report_cli_running_cycle_prints_running(monkeypatch, tmp_path, capsys):
    env = valid_env()
    env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr("bsa.config.settings.os.environ", env)
    monkeypatch.setattr("bsa.cli.read_projection_payload", lambda settings, cycle_id: None)
    monkeypatch.setattr("bsa.cli._cycle_status", lambda settings, cycle_id: "running")

    assert main(["report", CYCLE, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "running"}


def test_report_cli_prints_projection(monkeypatch, tmp_path, capsys):
    env = valid_env()
    env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr("bsa.config.settings.os.environ", env)
    payload = projection_payload(
        base_state(status="REPORTED", detected_commits=[commit("a1")])
    )
    monkeypatch.setattr("bsa.cli.read_projection_payload", lambda settings, cycle_id: payload)

    assert main(["report", CYCLE, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "REPORTED"
    assert out["detected_commits"][0]["sha"] == "a1"


def test_cycle_failure_text_lists_nodes_and_text(monkeypatch, tmp_path):
    """周期失败摘要要带节点名与原文，平台据此才能说清为什么失败。"""
    state = base_state(
        errors={
            "branch_matrix": ErrorRecord(
                node="branch_matrix", error="矩阵解析为空", ts="t"
            ),
            "cherry_pick": ErrorRecord(node="cherry_pick", error="boom", ts="t"),
        }
    )
    _patch_cycle_state(monkeypatch, state)

    text = cycle_failure_text(_settings(tmp_path), CYCLE)

    assert "周期失败" in text
    assert "branch_matrix: 矩阵解析为空" in text
    assert "cherry_pick: boom" in text


def test_cycle_failure_text_without_projection_still_explains(monkeypatch, tmp_path):
    """投影缺失时不留空——空原因正是要消灭的「失败说不清」。"""
    _patch_cycle_state(monkeypatch, None)

    text = cycle_failure_text(_settings(tmp_path), CYCLE)

    assert text.strip()
    assert "周期失败" in text


def test_cycle_failure_text_truncates(monkeypatch, tmp_path):
    state = base_state(
        errors={
            "n": ErrorRecord(node="n", error="x" * 5000, ts="t"),
        }
    )
    _patch_cycle_state(monkeypatch, state)

    assert len(cycle_failure_text(_settings(tmp_path), CYCLE, limit=200)) == 200
