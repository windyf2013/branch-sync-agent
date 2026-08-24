from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bsa.commands.override import apply_override
from bsa.commands.rerun import run_rerun_command
from bsa.commands.sync import (
    build_sha_batch,
    build_source_target_batch,
    manual_cycle_id,
    run_sync_command,
)
from bsa.config.settings import load_settings
from bsa.executor.exceptions import SafetyViolation
from bsa.graph.factory import _bundled_rules_dir, build_graph_context
from bsa.graph.workflow import open_checkpointer
from bsa.report.projection import _cycle_status, projection_payload, read_cycle_state
from bsa.rules import load_decision_rules, load_safety_rules
from bsa.scheduler.cycle import list_cycle_records, run_cycle


def _parse_bool(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise argparse.ArgumentTypeError(f"必须为 true 或 false，收到: {value!r}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bsa", description="Branch Sync Agent")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run-cycle", help="run a full sync cycle")
    run_p.add_argument("--date", help="cycle date YYYY-MM-DD (default today)")
    run_p.add_argument("--since", help="override scan window start (ISO8601)")
    run_p.add_argument("--until", help="override scan window end (ISO8601)")
    run_p.add_argument("--dry-run", action="store_true", help="force dry-run mail")
    run_p.set_defaults(handler=_cmd_run_cycle)

    scan_p = sub.add_parser("manual-scan", help="full cycle with explicit scan window")
    scan_p.add_argument("--since", required=True, help="scan window start (ISO8601)")
    scan_p.add_argument("--until", required=True, help="scan window end (ISO8601)")
    scan_p.add_argument("--date", default=None, help="cycle date (default today)")
    scan_p.add_argument("--dry-run", action="store_true", help="force dry-run mail")
    scan_p.set_defaults(handler=_cmd_manual_scan)

    status_p = sub.add_parser("status", help="show latest cycle status")
    status_p.set_defaults(handler=_cmd_status)

    validate_p = sub.add_parser("validate-config", help="validate settings and rules")
    validate_p.set_defaults(handler=_cmd_validate_config)

    report_p = sub.add_parser("report", help="project a cycle's state as JSON (read-only)")
    report_p.add_argument("cycle", help="cycle id, e.g. cycle-2026-08-20")
    report_p.add_argument("--json", action="store_true", help="emit JSON on stdout")
    report_p.set_defaults(handler=_cmd_report)

    override_p = sub.add_parser(
        "override", help="人工覆盖 commit 的 is_bug_fix/risk 判定（写 judgments.json）"
    )
    override_p.add_argument("sha", help="commit sha（完整或前 7+ 位）")
    override_p.add_argument(
        "--is-bug-fix",
        type=_parse_bool,
        help="人工判定为 bug fix（true/false）",
    )
    override_p.add_argument(
        "--risk", choices=["low", "medium", "high"], help="人工判定严重性"
    )
    override_p.set_defaults(handler=_cmd_override)

    sync_p = sub.add_parser(
        "sync",
        help="分支级同步：--sha 直同步，或源+目标判定同步（自动筛选 NeedSync）",
    )
    sync_p.add_argument("src", help="源分支名")
    sync_p.add_argument("target", help="目标分支名")
    sync_p.add_argument("--sha", nargs="+", help="直同步指定 commit sha（跳过决策）")
    sync_p.add_argument("--since", help="扫描窗口起点 (ISO8601)，源+目标模式生效")
    sync_p.add_argument("--until", help="扫描窗口终点 (ISO8601)，源+目标模式生效")
    sync_p.set_defaults(handler=_cmd_sync)

    rerun_p = sub.add_parser(
        "rerun",
        help="分支级重跑：默认保留现场续跑；--fresh 丢弃现场重建并先对当前远端重判",
    )
    rerun_p.add_argument("target", help="目标分支名")
    rerun_p.add_argument("--cycle", help="来源周期 id（缺省取最近含该分支的周期）")
    rerun_p.add_argument(
        "--fresh",
        action="store_true",
        help="丢弃旧 worktree 重建，并先对当前远端重判结论（已合入则停止）",
    )
    rerun_p.set_defaults(handler=_cmd_rerun)

    return parser


def _cmd_run_cycle(args: argparse.Namespace) -> int:
    try:
        # --since/--until = 手动指定窗口，语义等同 manual-scan（独立扫描），
        # 强制新建 checkpoint 避免撞每日周期旧状态（真机测试: 撞旧 checkpoint
        # 只 resume 不执行同步链路）
        manual = bool(args.since or args.until)
        return run_cycle(
            args.date,
            since=args.since,
            until=args.until,
            dry_run=args.dry_run,
            manual=manual,
            force_new=manual,
        )
    except Exception as exc:
        print(f"运行失败: {exc}", file=sys.stderr)
        return 1


def _cmd_manual_scan(args: argparse.Namespace) -> int:
    try:
        return run_cycle(
            args.date,
            since=args.since,
            until=args.until,
            dry_run=args.dry_run,
            manual=True,
            force_new=True,
        )
    except Exception as exc:
        print(f"运行失败: {exc}", file=sys.stderr)
        return 1


def _cmd_status(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    records = list_cycle_records(settings.log_dir)
    if not records:
        print("暂无周期记录")
        return 1
    latest = records[-1]
    print(f"周期: {latest['cycle_id']}")
    print(f"状态: {latest['status']}")
    print(f"报告: {latest.get('report_path')}")
    print(f"邮件: {latest.get('mail_status')}")
    print(f"开始: {latest.get('started_at')}")
    print(f"结束: {latest.get('finished_at')}")
    return 0


def _cmd_validate_config(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"配置校验失败: {exc}", file=sys.stderr)
        return 2
    try:
        rules_dir = _bundled_rules_dir()
        safety = load_safety_rules(rules_dir / "safety_rules.yaml")
        decision_rules = load_decision_rules(rules_dir / "decision_rules.yaml")
    except Exception as exc:
        print(f"规则校验失败: {exc}", file=sys.stderr)
        return 2
    print("配置校验通过")
    print(f"  repo_path: {settings.repo_path}")
    print(f"  branch_file: {settings.branch_file}")
    print(f"  worktree_root: {settings.worktree_root}")
    print(f"  log_dir: {settings.log_dir}")
    print(f"  llm_backend: {settings.llm_backend}")
    print(f"  mail_dry_run: {settings.mail_dry_run}")
    print(f"  required_models: {safety.required_models}")
    print(
        "  similarity_high/low: "
        f"{decision_rules.conclude.similarity_high}/{decision_rules.conclude.similarity_low}"
    )
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    state = read_cycle_state(settings, args.cycle)
    if state is None:
        # 无 checkpoint 但周期仍在跑 → 平台侧据此轮询
        if _cycle_status(settings, args.cycle) == "running":
            print(json.dumps({"status": "running"}))
            return 0
        print(f"周期不存在或未完成: {args.cycle}", file=sys.stderr)
        return 1
    print(json.dumps(projection_payload(state), ensure_ascii=False, indent=2))
    return 0


def _cmd_override(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    entry = apply_override(
        settings.log_dir,
        args.sha,
        is_bug_fix=args.is_bug_fix,
        risk=args.risk,
    )
    print(json.dumps(entry, ensure_ascii=False, indent=2))
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    if args.sha and (args.since or args.until):
        print("sync: --sha 模式不能与 --since/--until 同时使用", file=sys.stderr)
        return 1
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    cycle_id = manual_cycle_id()
    ctx = build_graph_context(settings, cycle_id=cycle_id)
    try:
        if args.sha:
            batch = build_sha_batch(ctx, args.src, args.sha)
        else:
            batch, conclusions = build_source_target_batch(
                ctx, args.src, args.target, args.since, args.until
            )
            for sha in sorted(conclusions):
                conclusion = conclusions[sha]
                print(
                    f"判定 {sha[:12]}: {conclusion.kind} "
                    f"(confidence={conclusion.confidence})"
                )
    except Exception as exc:
        print(f"准备同步失败: {exc}", file=sys.stderr)
        return 1
    if not batch:
        print("无需同步")
        return 0
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        with open_checkpointer(str(log_dir / "state.sqlite3")) as checkpointer:
            final = run_sync_command(
                ctx, cycle_id=cycle_id, target=args.target, batch=batch, checkpointer=checkpointer
            )
    except SafetyViolation as exc:
        print(f"同步被拒绝: {exc}", file=sys.stderr)
        return 1
    branch = (final.get("branch_results") or {}).get(args.target)
    status = branch.status if branch is not None else final.get("status", "UNKNOWN")
    patch_path = branch.patch_path if branch is not None else None
    print(
        f"同步完成: cycle={cycle_id} target={args.target} 状态={status} "
        f"patch={patch_path}"
    )
    return 0


def _cmd_rerun(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    ctx = build_graph_context(settings)
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        with open_checkpointer(str(log_dir / "state.sqlite3")) as checkpointer:
            result = run_rerun_command(
                ctx,
                target=args.target,
                cycle=args.cycle,
                fresh=args.fresh,
                checkpointer=checkpointer,
            )
    except Exception as exc:
        print(f"重跑失败: {exc}", file=sys.stderr)
        return 1
    if result.get("stop"):
        reason = result.get("reason")
        if reason == "dirty":
            print(
                f"现场有未提交修改，请先提交或清理: {result.get('worktree')}",
                file=sys.stderr,
            )
            return 1
        if reason == "no-worktree":
            print(
                f"目标分支 {args.target} 的活 worktree 不存在（可能已过期清理），"
                "请使用 --fresh 重新同步。",
                file=sys.stderr,
            )
            return 1
        if reason == "no-cycle":
            print(
                f"未找到包含目标分支 {args.target} 的已完成周期，请先使用 bsa sync 同步。",
                file=sys.stderr,
            )
            return 1
        if reason == "no-batch":
            print(
                f"来源周期 {result.get('cycle_id')} 无目标分支 {args.target} 的待同步批次。",
                file=sys.stderr,
            )
            return 1
        if reason == "conclusion-now-included":
            print("该修复已被合入/超出范围，无需重同步。", file=sys.stderr)
            return 0
        print(f"重跑被拦截: {reason}", file=sys.stderr)
        return 1
    rerun = result.get("rerun") or {}
    branch = (result.get("branch_results") or {}).get(args.target)
    status = branch.status if branch is not None else result.get("status", "UNKNOWN")
    patch_path = branch.patch_path if branch is not None else None
    cycle_id = rerun.get("cycle_id") or result.get("cycle_id")
    print(
        f"重跑完成: mode={rerun.get('mode')} cycle={cycle_id} "
        f"target={args.target} 状态={status} patch={patch_path}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
