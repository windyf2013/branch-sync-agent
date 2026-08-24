from __future__ import annotations

import argparse
import json
import sys

from bsa.config.settings import load_settings
from bsa.graph.factory import _bundled_rules_dir
from bsa.report.projection import _cycle_status, projection_payload, read_cycle_state
from bsa.rules import load_decision_rules, load_safety_rules
from bsa.scheduler.cycle import list_cycle_records, run_cycle


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


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
