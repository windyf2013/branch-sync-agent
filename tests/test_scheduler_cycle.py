from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from bsa.scheduler.cycle import _execute_locked, list_cycle_records
from tests.test_graph_nodes import make_ctx

CYCLE_ID = "cycle-2026-08-20"


class _FakeLogger:
    def info(self, *args, **kwargs) -> None:
        pass

    def warning(self, *args, **kwargs) -> None:
        pass

    def exception(self, *args, **kwargs) -> None:
        pass


class _FakeCheckpointer:
    def get_tuple(self, config):
        return None


def _install_fakes(monkeypatch, ctx, cycle_dir: Path, seen: dict, *, fail: bool = False) -> None:
    log_dir = Path(ctx.settings.log_dir)

    @contextmanager
    def fake_open_checkpointer(conn_string):
        yield _FakeCheckpointer()

    class _FakeGraph:
        def invoke(self, state, config):
            if fail:
                raise RuntimeError("boom")
            return {"status": "COMPLETED", "report": None}

    def fake_build_workflow(context, *, checkpointer=None):
        seen["running"] = json.loads((cycle_dir / "cycle.json").read_text(encoding="utf-8"))
        seen["listed"] = list_cycle_records(log_dir)
        return _FakeGraph()

    monkeypatch.setattr("bsa.scheduler.cycle._setup_run_logger", lambda path: _FakeLogger())
    monkeypatch.setattr("bsa.scheduler.cycle.cleanup_worktrees", lambda ctx_, cid: None)
    monkeypatch.setattr("bsa.scheduler.cycle.open_checkpointer", fake_open_checkpointer)
    monkeypatch.setattr("bsa.scheduler.cycle.build_workflow", fake_build_workflow)


def _run_locked(monkeypatch, tmp_path, *, fail: bool = False) -> tuple[dict, dict]:
    ctx = make_ctx(tmp_path)
    cycle_dir = Path(ctx.settings.log_dir) / CYCLE_ID
    seen: dict = {}
    _install_fakes(monkeypatch, ctx, cycle_dir, seen, fail=fail)
    seen["code"] = _execute_locked(
        ctx, CYCLE_ID, cycle_dir, since=None, until=None, dry_run=True
    )
    seen["final"] = json.loads((cycle_dir / "cycle.json").read_text(encoding="utf-8"))
    return seen, ctx


def test_cycle_running_marker_written_before_work(monkeypatch, tmp_path):
    seen, _ = _run_locked(monkeypatch, tmp_path)

    running = seen["running"]
    assert running["status"] == "running"
    assert running["cycle_id"] == CYCLE_ID
    assert running["report_path"] is None
    assert running["mail_status"] is None
    assert running["started_at"]
    assert running["finished_at"] == running["started_at"]


def test_cycle_terminal_status_overwrites_running(monkeypatch, tmp_path):
    seen, _ = _run_locked(monkeypatch, tmp_path)

    assert seen["code"] == 0
    final = seen["final"]
    assert final["status"] == "COMPLETED"
    assert final["cycle_id"] == CYCLE_ID


def test_cycle_failed_status_overwrites_running(monkeypatch, tmp_path):
    seen, _ = _run_locked(monkeypatch, tmp_path, fail=True)

    assert seen["code"] == 1
    assert seen["final"]["status"] == "FAILED"


def test_list_cycle_records_sees_running_record(monkeypatch, tmp_path):
    seen, _ = _run_locked(monkeypatch, tmp_path)

    running = [r for r in seen["listed"] if r["status"] == "running"]
    assert len(running) == 1
    assert running[0]["cycle_id"] == CYCLE_ID
