from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from bsa.config.settings import load_settings
from bsa.executor.exceptions import InfrastructureError
from bsa.executor.lock import flock_acquire
from bsa.graph import (
    GraphContext,
    build_graph_context,
    build_workflow,
    open_checkpointer,
    thread_config,
)
from bsa.mail import MailService
from bsa.report import (
    build_email_body,
    render_html_report,
    write_agent_diffs,
    write_decisions_json,
)
from bsa.report.projection import write_state_json
from bsa.scheduler.recipients import resolve_report_recipients

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


def manual_scan_cycle_id(since: str | None, until: str | None) -> str:
    """manual-scan 独立周期 id：与每日周期隔离，重扫不撞旧 checkpoint（决策 38）。

    由 CLI 在登记任务时同源计算，保证任务行 cycle_id 与 checkpoint 线程一致
    （P2-7）。
    """
    if since and until:
        return f"scan-{since}-{until}"
    return f"scan-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def _stale_worktree(path: Path, root: Path, cycle_id: str) -> bool:
    """True when ``path`` is a linked worktree under ``root`` from an older cycle.

    The main repo is never under ``root``; a worktree belonging to the current
    cycle is named ``<target>-<净化后的 cycle_id>`` and is kept. ``git worktree
    list`` 的首条是主仓库路径（在 root 之外），必须保留，故 root 之外一律返回
    False。净化与 prepare_worktree/容器名同源：scan-*/显式窗口 cycle_id 含 ':'，
    worktree 目录名与容器名都已去 ':'，回收后缀须按净化后名字匹配。
    """
    try:
        path.relative_to(root)
    except ValueError:
        return False
    from bsa.build.runner import _sanitize_container_name

    return not path.name.endswith(f"-{_sanitize_container_name(cycle_id)}")


def cleanup_worktrees(context: GraphContext, cycle_id: str) -> None:
    """Remove linked worktrees from previous cycles (决策 9).

    Runs at the start of every cycle so a re-run never trips over stale
    worktrees; ``prepare_worktree`` stays idempotent for the current cycle.
    Failures are skipped so cleanup never breaks a cycle.
    """
    root = Path(context.settings.worktree_root)
    try:
        paths = context.git.list_worktrees()
    except (AttributeError, InfrastructureError):
        return
    for path in paths:
        if _stale_worktree(Path(path), root, cycle_id):
            try:
                context.git.remove_worktree(path)
            except InfrastructureError:
                continue


def _initial_state(cycle_id: str) -> dict:
    return {
        "cycle_id": cycle_id,
        "scan_window": ("", ""),
        "branch_md_version": "",
        "detected_commits": [],
        "classifications": {},
        "decisions": {},
        "batches": {},
        "build_models": {},
        "sources": [],
        "targets": [],
        "current_target": None,
        "current_commit": None,
        "branch_results": {},
        "status": "NEW",
        "errors": {},
        "report": None,
    }


def setup_agents_run_logger(log_path: Path, *, stream: bool = False) -> logging.Logger:
    """给 ``bsa.agents`` logger 挂 run.log handler（LLM 失败日志落盘）。

    cron（``_setup_run_logger``）与手动 sync/rerun 两条路径共用：LLMClient.
    ``_complete`` 的 err= 日志是 INFO 级，若无人挂 handler 会被 root 默认
    WARNING 级别吞掉，LLM 失败真实原因（str(exc)）随之消失。``stream`` 仅在
    cron 路径打开（LLM 日志同时打终端）；手动 CLI 的 stdout/stderr 已被 executor
    流式写入 task-<id>.log，再挂 stream handler 会重复污染，故默认关闭。
    """
    agents = logging.getLogger("bsa.agents")
    for handler in agents.handlers[:]:
        agents.removeHandler(handler)
        handler.close()
    agents.setLevel(logging.INFO)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    agents.addHandler(file_handler)
    if stream:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(file_handler.formatter)
        agents.addHandler(stream_handler)
    return agents


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

    # bsa.agents 与 bsa.cycle 同挂 run.log：LLMClient._complete 的 err= 日志在此，
    # 若不挂 handler，其 INFO 记录被 root 默认 WARNING 级别吞掉，LLM 失败的真实
    # 原因（str(exc)）随之消失，run.log 只剩 bsa.cycle 四行。
    setup_agents_run_logger(log_path, stream=True)

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
    # 有 cycle.json 的周期 = run_cycle 落盘的周期，即 cron 每日（cycle-*）与
    # manual-scan 重扫（scan-*）两类。manual-*（手动同步）/ rerun-*（重跑）走
    # 单目标子图、不写 cycle.json，本就不该被枚举。
    for glob in ("cycle-*/cycle.json", "scan-*/cycle.json"):
        for path in root.glob(glob):
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
    force_new: bool = False,
    manual: bool = False,
    cycle_id: str | None = None,
) -> int:
    """Run one full sync cycle: build graph, invoke (resume-aware), produce artifacts.

    ``context`` is injectable so tests can mock the whole service layer
    (验收标准 #4). ``--since/--until`` override the default 22:00~22:00 scan
    window (决策 38, manual-scan semantics); ``dry_run`` forces mail_dry_run.
    ``manual`` (manual-scan) uses an independent cycle_id derived from the scan
    window and forces a fresh checkpoint, so a re-scan is never short-circuited
    by an existing daily-cycle checkpoint (真机测试发现: manual-scan 撞旧
    checkpoint 只 resume 不重扫). ``cycle_id`` 显式指定时覆盖日期推导
    （executor 守护进程注入，运行期即知周期 id）。
    """
    if context is None:
        settings = load_settings()
        if cycle_id is None:
            if manual:
                cycle_id = manual_scan_cycle_id(since, until)
            else:
                cycle_id = _derive_cycle_id(date)
        context = build_graph_context(settings, cycle_id=cycle_id)
    else:
        cycle_id = cycle_id or _derive_cycle_id(date)
    return _execute(
        context, cycle_id, since=since, until=until, dry_run=dry_run, force_new=force_new
    )


def _execute(
    context: GraphContext,
    cycle_id: str,
    *,
    since: str | None,
    until: str | None,
    dry_run: bool,
    force_new: bool = False,
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
    with flock_acquire(log_dir / "bsa.lock"):
        return _execute_locked(
            context,
            cycle_id,
            cycle_dir,
            since=since,
            until=until,
            dry_run=dry_run,
            force_new=force_new,
        )


def _execute_locked(
    context: GraphContext,
    cycle_id: str,
    cycle_dir: Path,
    *,
    since: str | None,
    until: str | None,
    dry_run: bool,
    force_new: bool = False,
) -> int:
    settings = context.settings
    cycle_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now().isoformat(timespec="seconds")
    _write_cycle_record(
        cycle_dir,
        cycle_id,
        status="running",
        report_path=None,
        mail_status=None,
        started_at=started_at,
        finished_at=started_at,
    )
    logger = _setup_run_logger(cycle_dir / "run.log")

    cleanup_worktrees(context, cycle_id)

    logger.info(
        "cycle %s start since=%s until=%s dry_run=%s", cycle_id, since, until, dry_run
    )

    log_dir = Path(settings.log_dir)
    db_path = log_dir / "state.sqlite3"
    final: dict = {}
    with open_checkpointer(str(db_path)) as checkpointer:
        graph = build_workflow(context, checkpointer=checkpointer)
        config = thread_config(cycle_id)
        resume = (not force_new) and checkpointer.get_tuple(config) is not None
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
    # 收尾段（投影/HTML/邮件）在图 invoke 之后、写终态之前。任一步抛异常都不得让
    # cycle.json 停在 running 变僵尸：一律吞下记日志，仍落到下方终态写盘
    # （状态沿用图终态；收尾产物缺失由日志/无 report_path 反映，不改变周期判定）。
    try:
        if final:
            # 投影数据源落盘为结构化 state.json（G11），平台只消费 JSON。
            write_state_json(log_dir, cycle_id, final)
        if report is not None:
            report_path = render_html_report(final, report)
            write_decisions_json(final, report)
            write_agent_diffs(final, report)
            subject = f"Branch Sync Agent 周期报告 {cycle_id} [{final_status}]"
            body = build_email_body(report, final)
            # 收件人：PM 名单（或回退 mail_recipients），有失败/报错时追加失败 commit 的
            # 合入人邮箱（见 recipients.py）。mail_phase 走配置（默认 test 保兼容，正式
            # 对 PM 发信需配 prod，否则 bridge 的 test 白名单会滤掉非测试收件人）。
            recipients = resolve_report_recipients(settings, final, context.git)
            sender = None
            if not settings.mail_dry_run:
                from bsa.mail.bridge_sender import make_bridge_sender

                sender = make_bridge_sender(
                    workspace_root=Path(settings.log_dir),
                    output_dir=cycle_dir,
                    mail_phase=settings.mail_phase,
                    mail_to=recipients,
                    mail_cc=settings.mail_cc_recipients,
                    bridge_path=(
                        Path(settings.mail_bridge_path) if settings.mail_bridge_path else None
                    ),
                )
            result = MailService(settings, sender=sender).send_report(
                subject, body, report_path, _patch_attachments(final)
            )
            mail_status = result.status
            if mail_status == "failed":
                logger.warning(
                    "report rendered %s; mail=FAILED to=%s error=%s",
                    report_path, recipients, result.error,
                )
            else:
                logger.info(
                    "report rendered %s; mail=%s to=%s", report_path, mail_status, recipients
                )
        else:
            logger.warning("no report produced; status=%s", final_status)
    except Exception:  # noqa: BLE001 — 收尾失败记日志，绝不阻断终态写盘
        logger.exception("cycle %s post-invoke tail failed", cycle_id)

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
