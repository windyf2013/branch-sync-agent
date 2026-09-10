from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from bsa.scheduler.cycle import _execute_locked, list_cycle_records, setup_agents_run_logger
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


def test_tail_exception_still_writes_terminal_cycle_json(monkeypatch, tmp_path):
    """F3 回归：图 invoke 之后的收尾段(render 等)抛异常，不得让 cycle.json 停在
    running 变僵尸——异常被吞记日志，仍落终态写盘。"""
    import json
    from contextlib import contextmanager

    ctx = make_ctx(tmp_path)
    cycle_dir = Path(ctx.settings.log_dir) / CYCLE_ID

    @contextmanager
    def fake_open_checkpointer(conn_string):
        yield _FakeCheckpointer()

    class _FakeGraph:
        def invoke(self, state, config):
            return {"status": "COMPLETED", "report": {"cycle_id": CYCLE_ID}}

    def fake_build_workflow(context, *, checkpointer=None):
        return _FakeGraph()

    def boom_render(final, report):
        raise RuntimeError("render exploded")

    monkeypatch.setattr("bsa.scheduler.cycle._setup_run_logger", lambda path: _FakeLogger())
    monkeypatch.setattr("bsa.scheduler.cycle.cleanup_worktrees", lambda ctx_, cid: None)
    monkeypatch.setattr("bsa.scheduler.cycle.open_checkpointer", fake_open_checkpointer)
    monkeypatch.setattr("bsa.scheduler.cycle.build_workflow", fake_build_workflow)
    # 收尾段第一步就炸：write_state_json 后 render_html_report 抛异常
    monkeypatch.setattr("bsa.scheduler.cycle.render_html_report", boom_render)

    code = _execute_locked(
        ctx, CYCLE_ID, cycle_dir, since=None, until=None, dry_run=True
    )
    final = json.loads((cycle_dir / "cycle.json").read_text(encoding="utf-8"))

    # 不僵尸：终态被写盘，status 沿用图终态 COMPLETED（render 失败不影响周期判定）
    assert code == 0
    assert final["status"] == "COMPLETED"
    assert final["mail_status"] is None
    assert final["report_path"] is None
    # cycle.json 仍被 list_cycle_records 枚举
    records = list_cycle_records(Path(ctx.settings.log_dir))
    assert any(r["cycle_id"] == CYCLE_ID for r in records)


def test_list_cycle_records_sees_running_record(monkeypatch, tmp_path):
    seen, _ = _run_locked(monkeypatch, tmp_path)

    running = [r for r in seen["listed"] if r["status"] == "running"]
    assert len(running) == 1
    assert running[0]["cycle_id"] == CYCLE_ID


def _write_record(log_dir: Path, cycle_id: str, status: str, started_at: str) -> None:
    d = log_dir / cycle_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "cycle.json").write_text(
        json.dumps(
            {
                "cycle_id": cycle_id,
                "status": status,
                "report_path": None,
                "mail_status": None,
                "started_at": started_at,
                "finished_at": started_at,
            }
        ),
        encoding="utf-8",
    )


def test_setup_agents_run_logger_writes_llm_errors_to_file(tmp_path):
    """手动 sync/rerun 路径挂 run.log：bsa.agents 的 INFO（LLM 失败原因）落盘，
    且 stream=False 时不加 StreamHandler（避免污染 executor 已流式的 task 日志）。"""
    import logging

    log_path = tmp_path / "nested" / "run.log"
    setup_agents_run_logger(log_path)

    logging.getLogger("bsa.agents").info("llm method=classify_build_error err=boom")

    text = log_path.read_text(encoding="utf-8")
    assert "err=boom" in text
    handlers = logging.getLogger("bsa.agents").handlers
    assert any(isinstance(h, logging.FileHandler) for h in handlers)
    # stream=False 不加控制台 StreamHandler（FileHandler 本身继承自 StreamHandler，
    # 须排除它再断言，否则误判）。
    assert not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in handlers
    )


def test_list_cycle_records_enumerates_scan_cycles(tmp_path):
    log_dir = tmp_path / "logs"
    _write_record(log_dir, "cycle-2026-09-01", "SUCCESS", "2026-09-01T00:00:04")
    _write_record(
        log_dir,
        "scan-2026-08-30T22:00:00+08:00-2026-08-31T22:00:00+08:00",
        "SUCCESS",
        "2026-09-01T16:10:17",
    )

    records = list_cycle_records(log_dir)

    ids = [r["cycle_id"] for r in records]
    assert "cycle-2026-09-01" in ids
    assert "scan-2026-08-30T22:00:00+08:00-2026-08-31T22:00:00+08:00" in ids
    # 按 started_at 升序：cycle（00:00）在 scan（16:10）之前
    assert ids == [
        "cycle-2026-09-01",
        "scan-2026-08-30T22:00:00+08:00-2026-08-31T22:00:00+08:00",
    ]


def test_list_cycle_records_ignores_manual_dir_without_cycle_json(tmp_path):
    log_dir = tmp_path / "logs"
    _write_record(log_dir, "cycle-2026-09-01", "SUCCESS", "2026-09-01T00:00:04")
    # manual 目录无 cycle.json → 不被枚举
    (log_dir / "manual-20260831-150139826484-688273").mkdir(parents=True)

    records = list_cycle_records(log_dir)

    assert [r["cycle_id"] for r in records] == ["cycle-2026-09-01"]


class TestEmailAssembly:
    """收尾段把 HTML 摘要装进邮件 payload（阶段 E 的装配点回归）。

    只测 build_email_html/build_message 各管各的不够 —— 真正要证明的是
    「cycle 收尾段确实调了它并把结果透传给 MailService」，否则改回纯文本正文
    而所有单测仍然全绿。
    """

    def _capture_payload(self, monkeypatch, tmp_path) -> dict:
        import bsa.scheduler.cycle as cycle_mod
        from bsa.domain.models import Report

        ctx = make_ctx(tmp_path)
        cycle_dir = Path(ctx.settings.log_dir) / CYCLE_ID
        seen: dict = {}

        class _FakeGraph:
            def invoke(self, state, config):
                return {
                    "status": "COMPLETED",
                    "cycle_id": CYCLE_ID,
                    "scan_window": ("S", "U"),
                    "detected_commits": [],
                    "branch_results": {},
                    "topology": [
                        {"section": "4.34", "sources": ["br_src"], "targets": ["br_main"]}
                    ],
                    "sources": ["br_src"],
                    "targets": ["br_main"],
                    "decisions": {},
                    "errors": {},
                    "report": Report(
                        cycle_id=CYCLE_ID,
                        html_path=cycle_dir / "report.html",
                        summary={"status": "COMPLETED"},
                        action_required=[],
                        decisions_json_path=cycle_dir / "decisions.json",
                    ),
                }

        _install_fakes(monkeypatch, ctx, cycle_dir, seen)
        monkeypatch.setattr(
            "bsa.scheduler.cycle.build_workflow", lambda context, *, checkpointer=None: _FakeGraph()
        )

        class _FakeMailService:
            def __init__(self, settings, sender=None) -> None:
                pass

            def send_report(self, subject, body, html_path, attachments, *, body_html=None):
                seen["subject"] = subject
                seen["body"] = body
                seen["body_html"] = body_html
                from bsa.mail.service import MailResult

                return MailResult(status="skipped", error="dry-run")

        monkeypatch.setattr(cycle_mod, "MailService", _FakeMailService)
        seen["code"] = _execute_locked(
            ctx, CYCLE_ID, cycle_dir, since=None, until=None, dry_run=True
        )
        return seen

    def test_summary_html_is_passed_to_mail_service(self, monkeypatch, tmp_path):
        seen = self._capture_payload(monkeypatch, tmp_path)

        assert seen["code"] == 0
        html = seen["body_html"]
        assert html, "收尾段必须构造 HTML 摘要并传给 MailService"
        # 摘要要含核心事实，而不是把完整 report.html 原样当正文。
        assert "br_src" in html and "br_main" in html

    def test_subject_carries_chinese_status(self, monkeypatch, tmp_path):
        seen = self._capture_payload(monkeypatch, tmp_path)

        # 主题里的终态用中文，收件箱列表即可读出成没成。
        assert "已完成" in seen["subject"]

    def test_plain_text_body_still_present(self, monkeypatch, tmp_path):
        # 无 HTML 能力的客户端读纯文本正文，它必须仍然独立完整。
        seen = self._capture_payload(monkeypatch, tmp_path)

        assert CYCLE_ID in seen["body"]
        assert "同步拓扑:" in seen["body"]
