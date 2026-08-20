#!/usr/bin/env python3
"""Branch Maintenance Agent — thin CLI orchestration."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from branch_maintenance.branch_md import (
    inventory_repo_to_document,
    looks_like_inventory,
    parse_branch_inventory,
)
from branch_maintenance.config import load_config, mail_cfg_dict, merge_cli_overrides
from branch_maintenance.mail_send_bridge import send_bma_reports_via_mail_send
from branch_maintenance.runner import RepoProcessError, process_repo
from branch_maintenance.state import load_state, save_state, update_repo_baselines
from branch_maintenance.time_window import default_daily_2200_window

DEFAULT_CONFIG = SCRIPTS_DIR / "branch_maintenance_config.yaml"


def _detect_workspace_root(scripts_dir: Path) -> Path:
    """Resolve aiskill/business repo root for relative state/output paths.

    - Deployed: ``<repo>/.cursor/scripts`` or ``<repo>/.claude/scripts``
    - aiskill package SSOT: ``common/packages/branch-maintenance/scripts``
      → repo root that contains ``common/packages/branch-maintenance``
    """
    parent = scripts_dir.parent
    if parent.name in {".cursor", ".claude"}:
        return parent.parent
    marker = Path("common") / "packages" / "branch-maintenance"
    for candidate in scripts_dir.parents:
        if (candidate / marker).is_dir():
            return candidate
    return parent.parent


WORKSPACE_ROOT = _detect_workspace_root(SCRIPTS_DIR)


def _parse_bool_flag(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"invalid boolean value: {value}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Branch Maintenance Agent — generate HTML sync-advice reports.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="Path to branch_maintenance_config.yaml (default: %(default)s)",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Analyze a single repository path instead of config repo list",
    )
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Skip git fetch --all --prune before scan",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output directory for HTML reports (overrides config output_dir)",
    )
    parser.add_argument(
        "--branch-file",
        default=None,
        help="Relative path to branch.md inside each repo (overrides config)",
    )
    parser.add_argument(
        "--mail-enabled",
        nargs="?",
        const=True,
        default=None,
        type=_parse_bool_flag,
        help=(
            "Enable mail handoff via mail_send_subflow after reports "
            "(default: config mail_enabled)"
        ),
    )
    parser.add_argument(
        "--mail-dry-run",
        action="store_true",
        help="Validate/build mail payload only (mail_send_subflow --dry-run)",
    )
    parser.add_argument(
        "--since",
        default=None,
        help=(
            "Scan commits since this time (git --since). "
            "Default with --until omitted: last completed 21:00~21:00 (+08:00)"
        ),
    )
    parser.add_argument(
        "--until",
        default=None,
        help=(
            "Scan commits until this time (git --until). "
            "Default with --since omitted: last completed 21:00~21:00 (+08:00)"
        ),
    )
    parser.add_argument(
        "--from-branch-md",
        default=None,
        help="Read multi-repo inventory from this branch.md (supports 路径 + ### sections)",
    )
    parser.add_argument(
        "--no-update-state",
        action="store_true",
        help="Do not persist last_scan_ref (recommended with --since/--until)",
    )
    parser.add_argument(
        "--agent-judgments",
        default=None,
        help="JSON file of Claude-agent Bug Fix judgments (overrides config agent_judgments_path)",
    )
    return parser


def _slug_repo_id(title: str, path: str) -> str:
    raw = path.strip() or title
    slug = re.sub(r"[^\w.\-]+", "_", raw, flags=re.UNICODE).strip("_")
    return slug or "repo"


def _resolve_inventory_path(raw_path: str, workspace_root: Path) -> Path | None:
    raw = raw_path.strip().replace("\\", "/")
    if not raw:
        return None
    if raw in {"rcios", ".", "./"}:
        return workspace_root
    if raw.startswith("rcios/"):
        # Prefer nested monorepo subdir when present; process_repo falls back to
        # workspace git if the subdir has no .git of its own.
        nested = (workspace_root / raw[len("rcios/") :]).resolve()
        if nested.is_dir():
            return nested
        return workspace_root
    candidate = workspace_root / raw
    if candidate.exists():
        return candidate.resolve()
    sibling = workspace_root.parent / raw
    if sibling.exists():
        return sibling.resolve()
    return None


def _document_to_md(doc) -> str:
    lines = [f"# {doc.title or 'branches'}"]
    for section in doc.sections:
        lines.append(f"## {section.title}")
        for branch in section.branches:
            lines.append(f"- {branch.name}")
    return "\n".join(lines) + "\n"


def _normalize_inventory_path(raw_path: str) -> str:
    return raw_path.strip().replace("\\", "/").rstrip("/")


def _inventory_path_allowed(raw_path: str, allowlist: list[str] | None) -> bool:
    """Exact path match against allowlist; empty/None allowlist keeps all."""
    if not allowlist:
        return True
    normalized = _normalize_inventory_path(raw_path)
    allowed = {_normalize_inventory_path(p) for p in allowlist if str(p).strip()}
    if not allowed:
        return True
    return normalized in allowed


def _repos_from_inventory(
    inventory_path: Path,
    *,
    workspace_root: Path,
    allowlist: list[str] | None = None,
) -> list[tuple[dict, str]]:
    text = inventory_path.read_text(encoding="utf-8")
    if not looks_like_inventory(text):
        raise ValueError(f"Not an inventory-style branch.md: {inventory_path}")

    inventory = parse_branch_inventory(text)
    jobs: list[tuple[dict, str]] = []
    for repo in inventory.repos:
        if not repo.path:
            print(f"[FAIL] {repo.title}: missing 路径", file=sys.stderr)
            continue
        if not _inventory_path_allowed(repo.path, allowlist):
            print(
                f"[SKIP] {repo.title}: path {repo.path!r} not in inventory_repo_paths",
                file=sys.stderr,
            )
            continue
        if not any(section.branches for section in repo.sections):
            print(f"[SKIP] {repo.title}: no branches listed", file=sys.stderr)
            continue
        resolved = _resolve_inventory_path(repo.path, workspace_root)
        if resolved is None:
            print(
                f"[FAIL] {_slug_repo_id(repo.title, repo.path)}: path not found ({repo.path})",
                file=sys.stderr,
            )
            continue
        doc = inventory_repo_to_document(repo)
        if not doc.sections:
            continue
        repo_id = _slug_repo_id(repo.title, repo.path)
        jobs.append(({"id": repo_id, "path": str(resolved)}, _document_to_md(doc)))
    return jobs


def _paths_equivalent(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return str(left) == str(right)


def _bind_inventory_jobs_to_git_repo(
    jobs: list[tuple[dict, str]],
    *,
    git_repo: str,
    workspace_root: Path,
) -> list[tuple[dict, str]]:
    """Point inventory jobs at the business-repo git root (aiskill hosts metadata only).

    Prefer path-equivalence when scripts are deployed inside the business repo.
    Otherwise bind by repo id / directory name (e.g. inventory ``rcios`` +
    ``--repo D:/.../cpe/rcios``).
    """
    want = Path(git_repo)
    if not want.is_absolute():
        want = (workspace_root / want).resolve()
    else:
        want = want.resolve()

    matched = _filter_jobs_for_single_repo(
        jobs,
        single_repo=str(want),
        workspace_root=workspace_root,
    )
    if matched:
        return matched

    want_name = want.name.strip() or "repo"
    rebound: list[tuple[dict, str]] = []
    for entry, md in jobs:
        rid = str(entry.get("id") or "").strip()
        if rid == want_name:
            rebound.append(({**entry, "path": str(want)}, md))
    return rebound


def _filter_jobs_for_single_repo(
    jobs: list[tuple[dict, str]],
    *,
    single_repo: str,
    workspace_root: Path,
) -> list[tuple[dict, str]]:
    """Keep inventory jobs whose resolved path matches --repo / single_repo."""
    want = Path(single_repo)
    if not want.is_absolute():
        want = (workspace_root / want).resolve()
    else:
        want = want.resolve()

    matched: list[tuple[dict, str]] = []
    for entry, md in jobs:
        job_path = Path(str(entry.get("path") or ""))
        if not job_path.is_absolute():
            job_path = (workspace_root / job_path).resolve()
        else:
            job_path = job_path.resolve()
        if _paths_equivalent(job_path, want):
            matched.append((entry, md))
            continue
        # Monorepo component job may resolve to a nested dir while --repo is workspace.
        try:
            if job_path.is_relative_to(want) or want.is_relative_to(job_path):
                matched.append((entry, md))
                continue
        except (AttributeError, ValueError, OSError):
            pass
        job_s = str(job_path).replace("\\", "/").rstrip("/")
        want_s = str(want).replace("\\", "/").rstrip("/")
        if job_s.startswith(want_s + "/") or want_s.startswith(job_s + "/"):
            matched.append((entry, md))
    return matched


def _resolve_repos(cfg, *, workspace_root: Path) -> list[dict]:
    if cfg.single_repo:
        repo_path = Path(cfg.single_repo)
        if not repo_path.is_absolute():
            repo_path = (workspace_root / repo_path).resolve()
        repo_id = repo_path.name or "single"
        return [{"id": repo_id, "path": str(repo_path)}]
    return list(cfg.repos)


def _default_inventory_path(cfg, *, workspace_root: Path) -> Path | None:
    """When config repos is empty, use inventory-style branch.md if present."""
    branch_file = Path(cfg.branch_file)
    if not branch_file.is_absolute():
        branch_file = (workspace_root / branch_file).resolve()
    if not branch_file.is_file():
        return None
    try:
        text = branch_file.read_text(encoding="utf-8")
    except OSError:
        return None
    if looks_like_inventory(text):
        return branch_file
    return None


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = (WORKSPACE_ROOT / config_path).resolve()

    update_state = False if args.no_update_state else None
    since = args.since
    until = args.until
    if since is None and until is None:
        since, until = default_daily_2200_window()
        print(f"[INFO] default eval window: {since} ~ {until}", flush=True)
        if update_state is None:
            update_state = False
    elif since is not None or until is not None:
        # Time-window runs should not advance incremental baselines by default.
        if update_state is None:
            update_state = False

    cfg = load_config(config_path)
    cfg = merge_cli_overrides(
        cfg,
        repo=args.repo,
        output=args.output,
        branch_file=args.branch_file,
        mail_enabled=args.mail_enabled,
        skip_fetch=True if args.skip_fetch else None,
        since=since,
        until=until,
        update_state=update_state,
        agent_judgments=args.agent_judgments,
        mail_dry_run=True if args.mail_dry_run else None,
    )

    inventory_jobs: list[tuple[dict, str]] | None = None
    inventory_path: Path | None = None
    if args.from_branch_md:
        inventory_path = Path(args.from_branch_md)
        if not inventory_path.is_absolute():
            inventory_path = (WORKSPACE_ROOT / inventory_path).resolve()
    elif not cfg.repos and not cfg.single_repo:
        inventory_path = _default_inventory_path(cfg, workspace_root=WORKSPACE_ROOT)
        if inventory_path is not None:
            print(
                f"[INFO] repos empty; using inventory branch.md: {inventory_path}",
                flush=True,
            )

    if inventory_path is not None:
        try:
            # Allowlist always comes from aiskill config (not bypassed by --repo).
            allowlist = list(cfg.inventory_repo_paths or []) or None
            inventory_jobs = _repos_from_inventory(
                inventory_path,
                workspace_root=WORKSPACE_ROOT,
                allowlist=allowlist,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to parse inventory: {exc}", file=sys.stderr)
            return 2
        if cfg.single_repo:
            inventory_jobs = _bind_inventory_jobs_to_git_repo(
                inventory_jobs,
                git_repo=cfg.single_repo,
                workspace_root=WORKSPACE_ROOT,
            )
        if not inventory_jobs:
            print("No usable repositories in inventory branch.md.", file=sys.stderr)
            return 2
    else:
        repos = _resolve_repos(cfg, workspace_root=WORKSPACE_ROOT)
        if not repos:
            print(
                "No repositories configured. Fill config repos, pass --repo, "
                "or use an inventory-style branch.md (路径 + ###).",
                file=sys.stderr,
            )
            return 2
        # --repo against workspace inventory: keep matching product libraries only.
        # Config multi-repo lists keep reading each repo's own branch.md.
        if cfg.single_repo:
            branch_path = Path(cfg.branch_file)
            if not branch_path.is_absolute():
                branch_path = (WORKSPACE_ROOT / branch_path).resolve()
            if branch_path.is_file():
                try:
                    text = branch_path.read_text(encoding="utf-8")
                except OSError:
                    text = ""
                if looks_like_inventory(text):
                    try:
                        allowlist = list(cfg.inventory_repo_paths or []) or None
                        inventory_jobs = _repos_from_inventory(
                            branch_path,
                            workspace_root=WORKSPACE_ROOT,
                            allowlist=allowlist,
                        )
                    except Exception as exc:  # noqa: BLE001
                        print(f"Failed to parse inventory: {exc}", file=sys.stderr)
                        return 2
                    inventory_jobs = _bind_inventory_jobs_to_git_repo(
                        inventory_jobs,
                        git_repo=cfg.single_repo,
                        workspace_root=WORKSPACE_ROOT,
                    )
                    if not inventory_jobs:
                        print(
                            "Inventory branch.md has no matching repo for --repo; "
                            "refusing to flatten all product libraries into one scan.",
                            file=sys.stderr,
                        )
                        return 2
                    print(
                        f"[INFO] using inventory sections for --repo "
                        f"({len(inventory_jobs)} product libraries)",
                        flush=True,
                    )
        if inventory_jobs is None:
            inventory_jobs = [(entry, None) for entry in repos]

    state_path = Path(cfg.state_path)
    if not state_path.is_absolute():
        state_path = (WORKSPACE_ROOT / state_path).resolve()

    state = load_state(state_path)
    failures: list[str] = []
    successes: list[str] = []
    mail_summaries: list[dict] = []
    window_desc = ""

    for repo_entry, branch_md_text in inventory_jobs:
        repo_id = str(repo_entry.get("id") or repo_entry.get("path") or "unknown")
        try:
            result = process_repo(
                cfg,
                repo_entry,
                state,
                workspace_root=WORKSPACE_ROOT,
                branch_md_text=branch_md_text,
            )
        except RepoProcessError as exc:
            failures.append(f"{exc.repo_id}: {exc.message}")
            print(f"[FAIL] {exc.repo_id}: {exc.message}", file=sys.stderr)
            continue
        except Exception as exc:  # noqa: BLE001 — CLI boundary
            failures.append(f"{repo_id}: {exc}")
            print(f"[FAIL] {repo_id}: {exc}", file=sys.stderr)
            continue

        abs_report = str(result.report_path.resolve())
        print(f"[OK] 报表已生成: {abs_report}")
        if result.mail_payload.get("window_desc"):
            window_desc = str(result.mail_payload["window_desc"])
            print(f"     window: {window_desc}")
        pending_n = result.mail_payload.get("pending_agent_count") or 0
        if pending_n:
            print(f"     pending agent judgments: {pending_n}")
            pending_path = result.mail_payload.get("pending_agent_path")
            if pending_path:
                print(f"     pending file: {pending_path}")
        skipped = result.mail_payload.get("skipped_branches") or []
        if skipped:
            print(f"     skipped branches: {len(skipped)}")
        successes.append(abs_report)
        mail_summaries.append(
            {
                "repo_id": result.repo_id,
                "commits_scanned": result.mail_payload.get("commits_scanned"),
                "need_sync_count": result.mail_payload.get("need_sync_count"),
                "pending_agent_count": result.mail_payload.get("pending_agent_count"),
            }
        )

        if cfg.update_state and result.new_baselines:
            state = update_repo_baselines(state, result.repo_id, result.new_baselines)

    if successes and cfg.update_state:
        save_state(state_path, state)

    total_pending = sum(
        int(s.get("pending_agent_count") or 0) for s in mail_summaries
    )

    if successes and cfg.mail_enabled and total_pending == 0:
        output_dir = Path(cfg.output_dir)
        if not output_dir.is_absolute():
            output_dir = (WORKSPACE_ROOT / output_dir).resolve()
        try:
            mail_result = send_bma_reports_via_mail_send(
                report_paths=successes,
                window_desc=window_desc,
                repo_summaries=mail_summaries,
                mail_cfg=mail_cfg_dict(cfg),
                workspace_root=WORKSPACE_ROOT,
                output_dir=output_dir,
                dry_run=cfg.mail_dry_run,
            )
        except Exception as exc:  # noqa: BLE001 — mail must not fail the run hard
            print(f"[WARN] mail handoff failed: {exc}", file=sys.stderr)
            mail_result = {"mail_result": "FAILURE", "error_log_excerpt": str(exc)}

        status = str(mail_result.get("mail_result") or "FAILURE")
        to_emails = mail_result.get("to_emails") or cfg.mail_to
        if status in {"SUCCESS", "DRY_RUN"}:
            print(
                f"[OK] mail handoff: {status} -> {', '.join(to_emails)} "
                f"(payload {mail_result.get('payload_path')})",
                flush=True,
            )
        else:
            print(
                f"[WARN] mail handoff failed: {mail_result.get('error_reason_cn') or status}: "
                f"{mail_result.get('error_log_excerpt')}",
                file=sys.stderr,
            )
    elif successes and cfg.mail_enabled and total_pending > 0:
        print(
            f"[INFO] 仍有 {total_pending} 条 pending 待 Agent 判定，跳过发邮件。\n"
            f"       请先用 git show 逐条判定 is_bug_fix → agent_judgments.json，\n"
            f"       再同参重跑 BMA 以完成交付（pending=0 时自动发送）。",
            flush=True,
        )

    if failures:
        print("\nFailure summary:", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
