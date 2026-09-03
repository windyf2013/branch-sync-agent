from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from bsa.commands.commits import list_candidate_commits
from bsa.commands.override import apply_override
from bsa.commands.rerun import cleanup_worktree_command, run_rerun_command
from bsa.commands.sync import (
    build_sha_batch,
    build_source_target_batch,
    manual_cycle_id,
    run_sync_command,
)
from bsa.commands.task_reporter import register_finish, register_start
from bsa.config.settings import load_settings
from bsa.executor.exceptions import SafetyViolation
from bsa.graph.factory import _bundled_rules_dir, build_graph_context
from bsa.graph.workflow import open_checkpointer
from bsa.report.projection import (
    _cycle_status,
    read_projection_payload,
    write_state_json,
)
from bsa.rules import BuildConfigError, load_decision_rules, load_safety_rules
from bsa.scheduler.cycle import list_cycle_records, manual_scan_cycle_id, run_cycle


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

    commits_p = sub.add_parser("commits", help="列出源分支最近的候选 commit（只读）")
    commits_p.add_argument("src", help="源分支名")
    commits_p.add_argument("--limit", type=int, default=50, help="返回条数（默认 50）")
    commits_p.add_argument(
        "--refresh",
        action="store_true",
        help="先 git fetch origin <src> 更新远端再列（持全局锁）",
    )
    commits_p.set_defaults(handler=_cmd_commits)

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
    override_p.add_argument(
        "--clear",
        action="store_true",
        help="清除该 sha 的人工覆盖（与 --is-bug-fix/--risk 互斥）",
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

    cleanup_p = sub.add_parser(
        "cleanup-worktree",
        help="删除指定目标分支在某周期的活 worktree（持全局锁，供平台删任务联动清理）",
    )
    cleanup_p.add_argument("target", help="目标分支名")
    cleanup_p.add_argument("cycle", help="周期 id（如 manual-xxx / cycle-xxx）")
    cleanup_p.set_defaults(handler=_cmd_cleanup_worktree)

    cleanup_task_p = sub.add_parser(
        "cleanup-task",
        help="回收某周期 keep-alive 编译容器（docker rm -f，供平台取消任务联动清理）",
    )
    cleanup_task_p.add_argument("cycle", help="周期 id（如 manual-xxx / cycle-xxx）")
    cleanup_task_p.set_defaults(handler=_cmd_cleanup_task)

    return parser


def _cmd_run_cycle(args: argparse.Namespace) -> int:
    try:
        # --since/--until = 手动指定窗口，语义等同 manual-scan（独立扫描），
        # 强制新建 checkpoint 避免撞每日周期旧状态（真机测试: 撞旧 checkpoint
        # 只 resume 不执行同步链路）
        manual = bool(args.since or args.until)
        # BSA_CYCLE_ID 环境变量优先：executor 守护进程预生成周期 id 注入，
        # 使运行期即知 cycle_id（可实时查进度、重启后可重挂）。
        cycle_id = os.environ.get("BSA_CYCLE_ID") or None
        if manual and cycle_id is None:
            # manual-scan 用独立 scan-* 周期 id，登记与 run_cycle 同源一致（P2-7）
            cycle_id = manual_scan_cycle_id(args.since, args.until)
        # 登记（kind=cycle，target=None）：executor 触发回填 cycle_id，CLI 直启自建行
        settings = load_settings()
        log_dir = Path(settings.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        reg_cycle_id = cycle_id or f"cycle-{datetime.now().date().isoformat()}"
        task_id = register_start(
            log_dir, kind="cycle", target=None, cycle_id=reg_cycle_id
        )
        if task_id is None:
            print("已有活动周期任务，请稍后再试", file=sys.stderr)
            return 1
        code = run_cycle(
            args.date,
            since=args.since,
            until=args.until,
            dry_run=args.dry_run,
            manual=manual,
            force_new=manual,
            cycle_id=cycle_id,
        )
        # 折叠周期终态到任务状态（P0-2/G10）：以 cycle.json 终态为准，SUCCESS 才记
        # succeeded，PARTIAL/FAILED（含分支失败、节点错误）一律记 failed，供平台醒目标记。
        terminal = _cycle_terminal_status(log_dir, cycle_id or reg_cycle_id)
        final_state = "succeeded" if terminal == "SUCCESS" else "failed"
        register_finish(
            log_dir, task_id,
            state=final_state,
            cycle_id=cycle_id or reg_cycle_id,
        )
        return code
    except Exception as exc:
        print(f"运行失败: {exc}", file=sys.stderr)
        return 1


def _cycle_terminal_status(log_dir: Path, cycle_id: str) -> str:
    """读 cycle.json 终态；未找到则回退 UNKNOWN（调用方按 failed 处理）。"""
    record = next(
        (r for r in list_cycle_records(log_dir) if r.get("cycle_id") == cycle_id),
        None,
    )
    return (record or {}).get("status", "UNKNOWN")


def _cmd_manual_scan(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
        log_dir = Path(settings.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        # 登记（kind=cycle）：与 run_cycle 同源计算 scan-* 周期 id，保证一致（P2-7）
        cycle_id = manual_scan_cycle_id(args.since, args.until)
        task_id = register_start(
            log_dir, kind="cycle", target=None, cycle_id=cycle_id
        )
        if task_id is None:
            print("已有活动周期任务，请稍后再试", file=sys.stderr)
            return 1
        code = run_cycle(
            args.date,
            since=args.since,
            until=args.until,
            dry_run=args.dry_run,
            manual=True,
            force_new=True,
            cycle_id=cycle_id,
        )
        terminal = _cycle_terminal_status(log_dir, cycle_id)
        register_finish(
            log_dir, task_id,
            state="succeeded" if terminal == "SUCCESS" else "failed",
            cycle_id=cycle_id,
        )
        return code
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
    payload = read_projection_payload(settings, args.cycle)
    if payload is None:
        # 无 state.json 且无 checkpoint 但周期仍在跑 → 平台侧据此轮询
        if _cycle_status(settings, args.cycle) == "running":
            print(json.dumps({"status": "running"}))
            return 0
        print(f"周期不存在或未完成: {args.cycle}", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_commits(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    ctx = build_graph_context(settings)
    try:
        # git fetch 只更新 .git/refs/remotes/* 与对象库（原子、append-only），与
        # worktree 里的编译完全无冲突，不持全局锁——否则正在跑的 sync/cycle 任务
        # 独占 bsa.lock 会让候选 commit 加载一直阻塞到超时。
        commits = list_candidate_commits(
            ctx.git, args.src, limit=args.limit, refresh=args.refresh
        )
    except Exception as exc:
        print(f"候选 commit 加载失败: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(commits, ensure_ascii=False, indent=2))
    return 0


def _cmd_override(args: argparse.Namespace) -> int:
    if args.clear and (args.is_bug_fix is not None or args.risk is not None):
        print("override: --clear 不能与 --is-bug-fix/--risk 同时使用", file=sys.stderr)
        return 2
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    # 尝试从 repo 查 message + patch_id，写 fingerprint 键（决策 5.1：rebase 后
    # sha 漂移仍命中人工覆盖）。查询失败不阻塞 override，只写 sha。
    message = patch_id = None
    try:
        from bsa.executor.subprocess import SubprocessExecutor
        from bsa.executor.whitelist import WhitelistExecutor
        from bsa.git.service import GitService

        git = GitService(
            executor=WhitelistExecutor(SubprocessExecutor()),
            repo_path=Path(settings.repo_path),
        )
        _, _, message = git.commit_metadata(args.sha)
        patch_id = git.patch_id(args.sha)
    except Exception:  # noqa: BLE001 — 查询失败仅降级，不阻塞人工覆盖
        message = patch_id = None
    entry = apply_override(
        settings.log_dir,
        args.sha,
        is_bug_fix=args.is_bug_fix,
        risk=args.risk,
        message=message,
        patch_id=patch_id,
        clear=args.clear,
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
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    task_id = register_start(
        log_dir, kind="sync", target=args.target, cycle_id=cycle_id,
        src=args.src, shas=args.sha,
    )
    if task_id is None:
        print("该目标分支已有任务在运行或排队，请稍后再试", file=sys.stderr)
        return 1
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
        register_finish(log_dir, task_id, state="failed", cycle_id=cycle_id, error=str(exc))
        return 1
    if not batch:
        print("无需同步")
        register_finish(log_dir, task_id, state="succeeded", cycle_id=cycle_id)
        return 0
    try:
        with open_checkpointer(str(log_dir / "state.sqlite3")) as checkpointer:
            final = run_sync_command(
                ctx, cycle_id=cycle_id, target=args.target, batch=batch, checkpointer=checkpointer
            )
    except BuildConfigError as exc:
        print(f"编译型号配置错误: {exc}", file=sys.stderr)
        register_finish(log_dir, task_id, state="failed", cycle_id=cycle_id, error=str(exc))
        return 1
    except SafetyViolation as exc:
        print(f"同步被拒绝: {exc}", file=sys.stderr)
        register_finish(log_dir, task_id, state="failed", cycle_id=cycle_id, error=str(exc))
        return 1
    branch = (final.get("branch_results") or {}).get(args.target)
    status = branch.status if branch is not None else final.get("status", "UNKNOWN")
    patch_path = branch.patch_path if branch is not None else None
    # 投影数据源落盘 state.json（G11），同步线程 id = cycle_id。
    write_state_json(log_dir, cycle_id, final)
    # FAILED/PARTIAL/MANUAL 不是成功：平台任务 runner 依返回码映射终态，
    # 必须非零退出并把最终状态打到 stderr，避免误报 succeeded。
    if status in ("FAILED", "PARTIAL", "MANUAL"):
        print(
            f"同步完成: cycle={cycle_id} target={args.target} 状态={status} "
            f"patch={patch_path}",
            file=sys.stderr,
        )
        register_finish(log_dir, task_id, state="failed", cycle_id=cycle_id)
        return 1
    print(
        f"同步完成: cycle={cycle_id} target={args.target} 状态={status} "
        f"patch={patch_path}"
    )
    register_finish(log_dir, task_id, state="succeeded", cycle_id=cycle_id)
    return 0


def _cmd_rerun(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    # 登记用 cycle_id 与执行一致（P2-3 单一线程 id）：fresh 用 manual id，
    # retained 用 rerun 线程 id；同一值传给 register_start 与 run_rerun_command。
    from bsa.commands.rerun import _rerun_thread_id

    pre_cycle_id = manual_cycle_id() if args.fresh else _rerun_thread_id(args.target)
    task_id = register_start(
        log_dir, kind="rerun", target=args.target, cycle_id=pre_cycle_id
    )
    if task_id is None:
        print("该目标分支已有任务在运行或排队，请稍后再试", file=sys.stderr)
        return 1
    ctx = build_graph_context(settings)
    try:
        with open_checkpointer(str(log_dir / "state.sqlite3")) as checkpointer:
            result = run_rerun_command(
                ctx,
                target=args.target,
                cycle=args.cycle,
                fresh=args.fresh,
                checkpointer=checkpointer,
                thread_id=pre_cycle_id,
            )
    except Exception as exc:
        print(f"重跑失败: {exc}", file=sys.stderr)
        register_finish(log_dir, task_id, state="failed", cycle_id=pre_cycle_id, error=str(exc))
        return 1
    if result.get("stop"):
        reason = result.get("reason")
        if reason == "dirty":
            print(
                f"现场有未提交修改，请先提交或清理: {result.get('worktree')}",
                file=sys.stderr,
            )
            register_finish(log_dir, task_id, state="failed", cycle_id=pre_cycle_id)
            return 1
        if reason == "no-worktree":
            print(
                f"目标分支 {args.target} 的活 worktree 不存在（可能已过期清理），"
                "请使用 --fresh 重新同步。",
                file=sys.stderr,
            )
            register_finish(log_dir, task_id, state="failed", cycle_id=pre_cycle_id)
            return 1
        if reason == "no-cycle":
            print(
                f"未找到包含目标分支 {args.target} 的已完成周期，请先使用 bsa sync 同步。",
                file=sys.stderr,
            )
            register_finish(log_dir, task_id, state="failed", cycle_id=pre_cycle_id)
            return 1
        if reason == "no-batch":
            print(
                f"来源周期 {result.get('cycle_id')} 无目标分支 {args.target} 的待同步批次。",
                file=sys.stderr,
            )
            register_finish(log_dir, task_id, state="failed", cycle_id=pre_cycle_id)
            return 1
        if reason == "conclusion-now-included":
            print("该修复已被合入/超出范围，无需重同步。", file=sys.stderr)
            register_finish(log_dir, task_id, state="succeeded", cycle_id=pre_cycle_id)
            return 0
        print(f"重跑被拦截: {reason}", file=sys.stderr)
        register_finish(log_dir, task_id, state="failed", cycle_id=pre_cycle_id)
        return 1
    rerun = result.get("rerun") or {}
    branch = (result.get("branch_results") or {}).get(args.target)
    status = branch.status if branch is not None else result.get("status", "UNKNOWN")
    patch_path = branch.patch_path if branch is not None else None
    cycle_id = rerun.get("cycle_id") or result.get("cycle_id") or pre_cycle_id
    # 投影数据源落盘 state.json（G11），线程 id = cycle_id。
    write_state_json(log_dir, cycle_id, result)
    print(
        f"重跑完成: mode={rerun.get('mode')} cycle={cycle_id} "
        f"target={args.target} 状态={status} patch={patch_path}"
    )
    final_state = "succeeded" if status == "SUCCESS" else "failed"
    register_finish(
        log_dir, task_id, state=final_state, cycle_id=cycle_id,
        commits=len(branch.commits) if branch is not None else None,
    )
    return 0


def _cmd_cleanup_worktree(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    ctx = build_graph_context(settings)
    try:
        result = cleanup_worktree_command(
            ctx, target=args.target, cycle_id=args.cycle
        )
    except Exception as exc:
        print(f"清理 worktree 失败: {exc}", file=sys.stderr)
        return 1
    print(
        f"清理完成: target={args.target} cycle={args.cycle} "
        f"removed={result.get('removed')}"
    )
    return 0


def _cmd_cleanup_task(args: argparse.Namespace) -> int:
    """回收某周期 keep-alive 编译容器（docker rm -f，best-effort）。

    docker 知识留在 V1：容器名与 build/runner.py:_container_name 一致
    （{docker_container_prefix}-{cycle_id}），docker_prefix（如 sudo）一并继承。
    容器不存在/清理失败不报错（幂等），取消任务不能因回收失败而中断。
    """
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    prefix = settings.docker_prefix.strip().split()
    container = f"{settings.docker_container_prefix}-{args.cycle}"
    proc = subprocess.run(
        [*prefix, "docker", "rm", "-f", container],
        capture_output=True,
        text=True,
        timeout=120,
    )
    print(f"清理完成: container={container} rc={proc.returncode}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
