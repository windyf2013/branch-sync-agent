from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from bsa.config.settings import load_settings
from bsa.graph import (
    GraphContext,
    build_graph_context,
    build_workflow,
    open_checkpointer,
    thread_config,
)
from bsa.mail import MailService
from bsa.report import build_email_body, render_html_report, write_decisions_json

_RUN_LOGGER = logging.getLogger("bsa.cycle")


def _derive_cycle_id(date: str | None) -> str:
    if date is None:
        day = datetime.now().date()
    else:
        day = _parse_cycle_date(date)
    return f"cycle-{day.isoformat()}"


def _parse_cycle_date(date: str) -> date:
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(date, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"无效的周期日期 {date!r}，期望格式 YYYY-MM-DD")


def _initial_state(cycle_id: str) -> dict:
    return {
        "cycle_id": cycle_id,
        "scan_window": ("", ""),
        "branch_md_version": "",
        "detected_commits": [],
        "classifications": {},
        "decisions": {},
        "batches": {},
        "current_target": None,
        "current_commit": None,
        "branch_results": {},
        "status": "NEW",
        "errors": {},
        "report": None,
    }


def _setup_run_logger(log_path: Path) -> logging.Logger:
    for handler in _RUN_LOGGER.handlers[:]:
        _RUN_LOGGER.removeHandler(handler)
        handler.close()
    _RUN_LOGGER.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    _RUN_LOGGER.addHandler(file_handler)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    _RUN_LOGGER.addHandler(stream_handler)
    return _RUN_LOGGER


def _patch_attachments(state: dict) -> list[Path]:
    return [
        Path(branch.patch_path)
        for branch in (state.get("branch_results") or {}).values()
        if branch.patch_path
    ]


def _write_cycle_record(
    cycle_dir: Path,
    cycle_id: str,
    *,
    status: str,
    report_path: Path | None,
    mail_status: str | None,
    started_at: str,
    finished_at: str,
) -> dict:
    record = {
        "cycle_id": cycle_id,
        "status": status,
        "report_path": str(report_path) if report_path else None,
        "mail_status": mail_status,
        "started_at": started_at,
        "finished_at": finished_at,
    }
    (cycle_dir / "cycle.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return record


def _started_sort_key(record: dict) -> tuple:
    started = record.get("started_at")
    try:
        dt = datetime.fromisoformat(started) if started else None
    except (TypeError, ValueError):
        dt = None
    if dt is None:
        return (datetime.min, record.get("cycle_id", ""))
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return (dt, record.get("cycle_id", ""))


def list_cycle_records(log_dir: str | Path) -> list[dict]:
    root = Path(log_dir)
    if not root.is_dir():
        return []
    records = []
    for path in root.glob("cycle-*/cycle.json"):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    records.sort(key=_started_sort_key)
    return records


def run_cycle(
    date: str | None = None,
    *,
    since: str | None = None,
    until: str | None = None,
    dry_run: bool = False,
    context: GraphContext | None = None,
) -> int:
    """Run one full sync cycle: build graph, invoke (resume-aware), produce artifacts.

    ``context`` is injectable so tests can mock the whole service layer
    (验收标准 #4). ``--since/--until`` override the default 22:00~22:00 scan
    window (决策 38, manual-scan semantics); ``dry_run`` forces mail_dry_run.
    """
    if context is None:
        settings = load_settings()
        cycle_id = _derive_cycle_id(date)
        context = build_graph_context(settings, cycle_id=cycle_id)
    else:
        cycle_id = _derive_cycle_id(date)
    return _execute(context, cycle_id, since=since, until=until, dry_run=dry_run)


def _execute(
    context: GraphContext,
    cycle_id: str,
    *,
    since: str | None,
    until: str | None,
    dry_run: bool,
) -> int:
    settings = context.settings
    if since or until:
        settings = settings.model_copy(
            update={"scan_since": since, "scan_until": until}
        )
    if dry_run:
        settings = settings.model_copy(update={"mail_dry_run": True})
    if settings is not context.settings:
        context = replace(context, settings=settings)

    log_dir = Path(settings.log_dir)
    cycle_dir = log_dir / cycle_id
    cycle_dir.mkdir(parents=True, exist_ok=True)
    logger = _setup_run_logger(cycle_dir / "run.log")

    started_at = datetime.now().isoformat(timespec="seconds")
    logger.info(
        "cycle %s start since=%s until=%s dry_run=%s", cycle_id, since, until, dry_run
    )

    db_path = log_dir / "state.sqlite3"
    final: dict = {}
    with open_checkpointer(str(db_path)) as checkpointer:
        graph = build_workflow(context, checkpointer=checkpointer)
        config = thread_config(cycle_id)
        resume = checkpointer.get_tuple(config) is not None
        try:
            if resume:
                logger.info("resume existing checkpoint for %s", cycle_id)
                final = graph.invoke(None, config)
            else:
                logger.info("start new cycle %s", cycle_id)
                final = graph.invoke(_initial_state(cycle_id), config)
        except Exception:  # noqa: BLE001 — node boundary already handles node errors
            logger.exception("cycle %s invoke failed", cycle_id)
            _write_cycle_record(
                cycle_dir,
                cycle_id,
                status="FAILED",
                report_path=None,
                mail_status=None,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
            return 1

    report = final.get("report")
    final_status = final.get("status", "UNKNOWN")
    report_path: Path | None = None
    mail_status: str | None = None
    if report is not None:
        report_path = render_html_report(final, report)
        write_decisions_json(final, report)
        subject = f"Branch Sync Agent 周期报告 {cycle_id} [{final_status}]"
        body = build_email_body(report, final)
        result = MailService(settings).send_report(
            subject, body, report_path, _patch_attachments(final)
        )
        mail_status = result.status
        logger.info("report rendered %s; mail=%s", report_path, mail_status)
    else:
        logger.warning("no report produced; status=%s", final_status)

    finished_at = datetime.now().isoformat(timespec="seconds")
    _write_cycle_record(
        cycle_dir,
        cycle_id,
        status=final_status,
        report_path=report_path,
        mail_status=mail_status,
        started_at=started_at,
        finished_at=finished_at,
    )
    logger.info("cycle %s finished status=%s", cycle_id, final_status)
    return 0
