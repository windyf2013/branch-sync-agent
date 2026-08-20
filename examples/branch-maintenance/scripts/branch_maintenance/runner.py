from __future__ import annotations

import difflib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from branch_maintenance.branch_md import BranchMdDocument, parse_branch_md
from branch_maintenance.classify import classify_commit
from branch_maintenance.conclude import CommitAnalysis, TargetSnapshot, conclude_pair
from branch_maintenance.config import BmaConfig, load_agent_judgments
from branch_maintenance.git_scan import (
    GitCommandError,
    branch_tip,
    commits_since,
    fetch_all,
    file_exists,
    patch_id,
    show_file,
)
from branch_maintenance.html_report import (
    CommitReport,
    ScanSourceInfo,
    TargetConclusionRow,
    build_report_model,
    default_report_path,
    render_report,
    write_report,
)
from branch_maintenance.matrix import HomologousSet, build_matrix
from branch_maintenance.time_window import EMPTY_SHA_MARK

SYMBOL_RE = re.compile(r"^\+.*?\b([A-Za-z_]\w*)\s*\(", re.MULTILINE)
FIX_GUARD_RE = re.compile(r"NULL\s*==|return\s+-1", re.IGNORECASE)


class RepoProcessError(Exception):
    def __init__(self, repo_id: str, message: str) -> None:
        super().__init__(message)
        self.repo_id = repo_id
        self.message = message


@dataclass
class RepoProcessResult:
    repo_id: str
    report_path: Path
    new_baselines: dict[str, str]
    mail_payload: dict[str, Any]


def _run_git(args: list[str], *, repo: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise GitCommandError(["git", *args], completed.returncode, completed.stderr or "")
    return completed


def _resolve_repo_path(repo_entry: dict[str, Any], workspace_root: Path) -> Path:
    raw_path = repo_entry.get("path")
    if not raw_path:
        raise ValueError("Repo entry missing path.")
    path = Path(str(raw_path))
    if not path.is_absolute():
        path = (workspace_root / path).resolve()
    return path


def _read_branch_md(repo_path: Path, branch_file: str) -> str:
    branch_path = repo_path / branch_file
    if not branch_path.is_file():
        raise FileNotFoundError(f"branch file not found: {branch_path}")
    return branch_path.read_text(encoding="utf-8")


def _branch_tip(repo: Path, branch: str) -> tuple[str, str]:
    """Resolve logical branch to freshest ref (prefer remote-tracking) and tip SHA."""
    return branch_tip(repo, branch)


def _commit_metadata(repo: Path, sha: str) -> tuple[str, str, str]:
    completed = _run_git(
        ["log", "-1", "--format=%an|%aI|%B", sha],
        repo=repo,
    )
    parts = completed.stdout.split("|", 2)
    if len(parts) < 3:
        return "", "", completed.stdout.strip()
    return parts[0], parts[1], parts[2]


def _commit_parent_count(repo: Path, sha: str) -> int:
    completed = subprocess.run(
        ["git", "rev-list", "--parents", "-1", sha],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return 0
    return max(0, len(completed.stdout.strip().split()) - 1)


def _changed_files(repo: Path, sha: str) -> list[str]:
    args = ["diff-tree", "--no-commit-id", "--name-only", "-r"]
    if _commit_parent_count(repo, sha) == 0:
        # Root/orphan imports can list 100k+ paths; treat as opaque blob.
        return []
    args.append(sha)
    completed = _run_git(args, repo=repo)
    names = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if len(names) > 500:
        return names[:500]
    return names


def _commit_patch(repo: Path, sha: str, *, max_chars: int = 120_000) -> str:
    # Root/orphan imports and huge trees must not be fully loaded into memory.
    if _commit_parent_count(repo, sha) == 0:
        return ""

    numstat = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--numstat", "-r", sha],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if numstat.returncode == 0:
        total_lines = 0
        file_count = 0
        for line in numstat.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) < 2:
                continue
            file_count += 1
            if file_count > 500:
                return ""
            try:
                added = 0 if parts[0] == "-" else int(parts[0])
                deleted = 0 if parts[1] == "-" else int(parts[1])
            except ValueError:
                continue
            total_lines += added + deleted
            if total_lines > 20_000:
                return ""

    completed = _run_git(["show", "--pretty=format:", sha], repo=repo)
    text = completed.stdout or ""
    if len(text) > max_chars:
        return text[:max_chars]
    return text


def _has_sha_on_branch(repo: Path, sha: str, branch: str) -> bool:
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha, branch],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return completed.returncode == 0


def _branch_messages(repo: Path, branch: str, *, limit: int = 100) -> list[str]:
    completed = _run_git(
        ["log", branch, f"-{limit}", "--format=%B"],
        repo=repo,
    )
    return [block.strip() for block in completed.stdout.split("\n\n") if block.strip()]


def _branch_patch_ids(
    repo: Path,
    branch: str,
    *,
    limit: int = 40,
    paths: list[str] | None = None,
) -> set[str]:
    args = ["log", branch, f"-{limit}", "--format=%H"]
    if paths:
        args.append("--")
        args.extend(paths[:20])
    completed = _run_git(args, repo=repo)
    ids: set[str] = set()
    for sha in completed.stdout.splitlines():
        sha = sha.strip()
        if not sha:
            continue
        pid = patch_id(repo, sha)
        if pid:
            ids.add(pid)
    return ids


def _extract_symbols(patch_text: str) -> list[str]:
    seen: set[str] = set()
    symbols: list[str] = []
    for match in SYMBOL_RE.finditer(patch_text):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            symbols.append(name)
    return symbols


def _file_similarity(source_text: str | None, target_text: str | None) -> float | None:
    if source_text is None or target_text is None:
        return None
    if not source_text and not target_text:
        return 1.0
    if source_text == target_text:
        return 1.0
    # SequenceMatcher is O(n^2); cap inputs to keep target scans responsive.
    max_chars = 20000
    left = source_text if len(source_text) <= max_chars else source_text[:max_chars]
    right = target_text if len(target_text) <= max_chars else target_text[:max_chars]
    return difflib.SequenceMatcher(None, left, right).ratio()


def _fix_clearly_missing(source_text: str | None, target_text: str | None) -> bool:
    if source_text is None or target_text is None:
        return False
    source_has = bool(FIX_GUARD_RE.search(source_text))
    target_has = bool(FIX_GUARD_RE.search(target_text))
    return source_has and not target_has


def _symbols_on_target(target_text: str | None, symbols: list[str]) -> dict[str, bool]:
    if target_text is None:
        return {symbol: False for symbol in symbols}
    return {symbol: symbol in target_text for symbol in symbols}


def _function_renamed(source_symbols: list[str], target_symbols: dict[str, bool]) -> bool:
    if not source_symbols:
        return False
    if all(target_symbols.get(symbol, False) for symbol in source_symbols):
        return False
    return any(target_symbols.values())


def _issue_ids_from_messages(messages: list[str]) -> set[str]:
    issue_ids: set[str] = set()
    for message in messages:
        classification = classify_commit(message, ["placeholder"], [], "")
        issue_ids.update(classification.issue_ids)
    return issue_ids


def _cross_product_linked(
    source_section: str,
    target_section: str,
    links: list[dict[str, Any]],
) -> bool:
    for link in links:
        src = str(link.get("from", ""))
        dst = str(link.get("to", ""))
        if (src == source_section and dst == target_section) or (
            src == target_section and dst == source_section
        ):
            return True
    return False


def process_repo(
    cfg: BmaConfig,
    repo_entry: dict[str, Any],
    state: dict[str, Any],
    *,
    workspace_root: Path,
    branch_md_text: str | None = None,
) -> RepoProcessResult:
    repo_id = str(repo_entry.get("id") or repo_entry.get("path") or "unknown")
    repo_path = _resolve_repo_path(repo_entry, workspace_root)

    if not repo_path.is_dir():
        raise RepoProcessError(repo_id, f"repository path does not exist: {repo_path}")

    git_dir = repo_path / ".git"
    if not git_dir.exists():
        # Subdirectory of a monorepo: fall back to workspace root git.
        try:
            under_workspace = repo_path.resolve().is_relative_to(workspace_root.resolve())
        except AttributeError:
            under_workspace = str(repo_path.resolve()).startswith(str(workspace_root.resolve()))
        if (workspace_root / ".git").exists() and under_workspace:
            repo_path = workspace_root
        else:
            raise RepoProcessError(repo_id, f"not a git repository: {repo_path}")

    if branch_md_text is None:
        try:
            branch_md_text = _read_branch_md(repo_path, cfg.branch_file)
        except FileNotFoundError as exc:
            raise RepoProcessError(repo_id, str(exc)) from exc

    if not cfg.skip_fetch:
        try:
            fetch_all(repo_path)
        except GitCommandError as exc:
            raise RepoProcessError(repo_id, str(exc)) from exc

    parsed_at = datetime.now(timezone.utc).isoformat()
    doc: BranchMdDocument = parse_branch_md(branch_md_text)
    matrix = build_matrix(doc, develop_backfill_enabled=cfg.develop_backfill_enabled)

    repo_state = state.get("repos", {}).get(repo_id, {})
    last_scan_ref: dict[str, str] = {}
    if isinstance(repo_state, dict):
        raw_refs = repo_state.get("last_scan_ref", {})
        if isinstance(raw_refs, dict):
            last_scan_ref = {str(k): str(v) for k, v in raw_refs.items()}

    unknown_branches = sorted(
        {
            branch.name
            for section in doc.sections
            for branch in section.branches
            if branch.branch_type == "unknown"
        }
    )

    scan_sources: list[ScanSourceInfo] = []
    commit_reports: list[CommitReport] = []
    not_included_count = 0
    new_baselines: dict[str, str] = {}
    skipped_branches: list[str] = []

    seen_patch_ids: set[str] = set()
    seen_issue_ids: set[str] = set()
    use_time_window = bool(cfg.since or cfg.until)
    target_message_cache: dict[str, list[str]] = {}
    target_patch_id_cache: dict[str, set[str]] = {}
    target_issue_id_cache: dict[str, set[str]] = {}
    pending_agent_commits: list[dict[str, Any]] = []

    judgments_path = Path(cfg.agent_judgments_path)
    if not judgments_path.is_absolute():
        judgments_path = (workspace_root / judgments_path).resolve()
    agent_judgments = load_agent_judgments(judgments_path)

    for homologous in matrix:
        for source in homologous.sources:
            branch_name = source.name
            try:
                resolved_ref, tip_sha = _branch_tip(repo_path, branch_name)
            except GitCommandError:
                skipped_branches.append(branch_name)
                continue

            baseline = None if use_time_window else last_scan_ref.get(branch_name)
            new_baselines[branch_name] = tip_sha

            try:
                sha_list = commits_since(
                    repo_path,
                    resolved_ref,
                    baseline,
                    since=cfg.since,
                    until=cfg.until,
                )
            except GitCommandError as exc:
                raise RepoProcessError(
                    repo_id,
                    f"cannot list commits for {branch_name}: {exc}",
                ) from exc

            if not sha_list:
                start_sha = EMPTY_SHA_MARK
                end_sha = EMPTY_SHA_MARK
            elif use_time_window:
                start_sha = sha_list[0]
                end_sha = sha_list[-1]
            elif baseline:
                start_sha = baseline
                end_sha = tip_sha
            else:
                start_sha = sha_list[0]
                end_sha = tip_sha

            scan_sources.append(
                ScanSourceInfo(
                    branch=branch_name,
                    last_scan_ref=start_sha,
                    new_tip=end_sha,
                    resolved_ref=resolved_ref,
                    commits_scanned=len(sha_list),
                )
            )

            for sha in sha_list:
                author, committed_at, message = _commit_metadata(repo_path, sha)
                changed_files = _changed_files(repo_path, sha)
                patch_text = _commit_patch(repo_path, sha)
                symbols = _extract_symbols(patch_text)
                classification = classify_commit(
                    message,
                    changed_files,
                    symbols,
                    patch_text,
                    sha=sha,
                    agent_judgments=agent_judgments,
                )

                commit_patch_id = patch_id(repo_path, sha)
                if commit_patch_id and commit_patch_id in seen_patch_ids:
                    continue
                overlap_issues = set(classification.issue_ids) & seen_issue_ids
                if classification.issue_ids and overlap_issues:
                    continue

                if commit_patch_id:
                    seen_patch_ids.add(commit_patch_id)
                seen_issue_ids.update(classification.issue_ids)

                # Always list scanned commits in the report. Bug Fix recognition only
                # gates cross-branch sync assessment — not visibility.
                if not classification.is_bug_fix:
                    if classification.needs_agent:
                        pending_agent_commits.append(
                            {
                                "sha": sha,
                                "message": message.splitlines()[0] if message.strip() else "",
                                "source_branch": branch_name,
                                "homologous_section": homologous.section,
                                "changed_files": changed_files,
                                "committed_at": committed_at,
                                "author": author,
                            }
                        )
                    else:
                        not_included_count += 1
                    commit_reports.append(
                        CommitReport(
                            sha=sha,
                            author=author,
                            committed_at=committed_at,
                            message=message,
                            recognition_source=classification.recognition_source,
                            changed_files=changed_files,
                            symbols=symbols,
                            source_branch=branch_name,
                            homologous_section=homologous.section,
                            issue_ids=list(classification.issue_ids),
                            target_rows=[],
                            is_bug_fix=False,
                            skip_sync_reason=classification.reason
                            or "未识别为 Bug Fix，仅列入时间窗明细。",
                            judge_reason=classification.reason,
                            needs_agent=classification.needs_agent,
                        )
                    )
                    continue

                source_analysis = CommitAnalysis(
                    sha=sha,
                    message=message,
                    changed_files=changed_files,
                    symbols=symbols,
                    patch_text=patch_text,
                    patch_id=commit_patch_id,
                    issue_ids=list(classification.issue_ids),
                    recognition_source=classification.recognition_source,
                    source_branch=branch_name,
                    source_branch_type=source.branch_type,
                    homologous_section=homologous.section,
                    has_high_priority_rule=classification.recognition_source.startswith(
                        "machine:"
                    ),
                )

                target_rows: list[TargetConclusionRow] = []
                for target in homologous.need_sync_targets:
                    if target.name == branch_name:
                        continue

                    try:
                        target_ref, _target_tip = _branch_tip(repo_path, target.name)
                    except GitCommandError:
                        skipped_branches.append(target.name)
                        continue

                    lifecycle = cfg.lifecycle_overrides.get(target.name, "active")
                    has_source_sha = _has_sha_on_branch(repo_path, sha, target_ref)

                    # Same commit already on target: skip expensive similarity I/O.
                    if has_source_sha:
                        target_snapshot = TargetSnapshot(
                            branch_name=target.name,
                            branch_type=target.branch_type,
                            lifecycle=lifecycle,
                            has_source_sha=True,
                            in_same_homologous_set=True,
                            develop_backfill_allowed=cfg.develop_backfill_enabled,
                            cross_product_linked=_cross_product_linked(
                                homologous.section,
                                homologous.section,
                                cfg.cross_product_links,
                            ),
                        )
                    else:
                        if target_ref not in target_message_cache:
                            target_message_cache[target_ref] = _branch_messages(
                                repo_path,
                                target_ref,
                            )
                        patch_cache_key = (
                            f"{target_ref}|{'|'.join(sorted(changed_files)[:20])}"
                        )
                        if patch_cache_key not in target_patch_id_cache:
                            target_patch_id_cache[patch_cache_key] = _branch_patch_ids(
                                repo_path,
                                target_ref,
                                paths=changed_files,
                            )
                        target_messages = target_message_cache[target_ref]
                        target_patch_ids = target_patch_id_cache[patch_cache_key]
                        if target_ref not in target_issue_id_cache:
                            target_issue_id_cache[target_ref] = _issue_ids_from_messages(
                                target_messages
                            )
                        target_issue_ids = target_issue_id_cache[target_ref]

                        files_on_target = {
                            path
                            for path in changed_files
                            if file_exists(repo_path, target_ref, path)
                        }

                        source_file_texts = [
                            show_file(repo_path, resolved_ref, path)
                            for path in changed_files
                        ]
                        target_file_texts = [
                            show_file(repo_path, target_ref, path)
                            for path in changed_files
                        ]

                        similarity_values = [
                            _file_similarity(src, tgt)
                            for src, tgt in zip(
                                source_file_texts,
                                target_file_texts,
                                strict=False,
                            )
                        ]
                        similarity_values = [
                            value for value in similarity_values if value is not None
                        ]
                        file_similarity = (
                            sum(similarity_values) / len(similarity_values)
                            if similarity_values
                            else None
                        )

                        primary_source_text = (
                            source_file_texts[0] if source_file_texts else None
                        )
                        primary_target_text = (
                            target_file_texts[0] if target_file_texts else None
                        )
                        symbols_map = _symbols_on_target(primary_target_text, symbols)

                        target_snapshot = TargetSnapshot(
                            branch_name=target.name,
                            branch_type=target.branch_type,
                            lifecycle=lifecycle,
                            has_source_sha=False,
                            target_commit_messages=target_messages,
                            target_patch_ids=target_patch_ids,
                            target_issue_ids=target_issue_ids,
                            files_on_target=files_on_target,
                            symbols_on_target=symbols_map,
                            file_similarity=file_similarity,
                            fix_clearly_missing=_fix_clearly_missing(
                                primary_source_text,
                                primary_target_text,
                            ),
                            function_renamed=_function_renamed(symbols, symbols_map),
                            in_same_homologous_set=True,
                            develop_backfill_allowed=cfg.develop_backfill_enabled,
                            cross_product_linked=_cross_product_linked(
                                homologous.section,
                                homologous.section,
                                cfg.cross_product_links,
                            ),
                        )

                    conclusion = conclude_pair(
                        source_analysis,
                        target_snapshot,
                        similarity_high=cfg.similarity_high,
                        similarity_low=cfg.similarity_low,
                        develop_backfill_enabled=cfg.develop_backfill_enabled,
                    )

                    hint = None
                    if conclusion.kind in ("NeedSync", "ManualReview"):
                        hint = _cherry_pick_hint(sha, target.name)

                    target_rows.append(
                        TargetConclusionRow(
                            target_branch=target.name,
                            lifecycle=lifecycle,
                            conclusion=conclusion,
                            cherry_pick_hint=hint,
                        )
                    )

                commit_reports.append(
                    CommitReport(
                        sha=sha,
                        author=author,
                        committed_at=committed_at,
                        message=message,
                        recognition_source=classification.recognition_source,
                        changed_files=changed_files,
                        symbols=symbols,
                        source_branch=branch_name,
                        homologous_section=homologous.section,
                        issue_ids=list(classification.issue_ids),
                        target_rows=target_rows,
                        is_bug_fix=True,
                        skip_sync_reason=None,
                        judge_reason=classification.reason,
                    )
                )

    generated_at = datetime.now(timezone.utc).isoformat()
    output_dir = Path(cfg.output_dir)
    if not output_dir.is_absolute():
        output_dir = (workspace_root / output_dir).resolve()

    report_path = default_report_path(repo_id, str(output_dir))
    if skipped_branches:
        unknown_branches = sorted(set(unknown_branches) | {f"missing:{name}" for name in skipped_branches})

    model = build_report_model(
        repo_id=repo_id,
        repo_path=str(repo_path),
        generated_at=generated_at,
        branch_file=cfg.branch_file,
        branch_md_parsed_at=parsed_at,
        scan_sources=scan_sources,
        document_bugs=doc.duplicates,
        unknown_branches=unknown_branches,
        commits=commit_reports,
        not_included_count=not_included_count,
        since=cfg.since,
        until=cfg.until,
    )
    html = render_report(model)
    written_path = write_report(html, report_path)

    pending_path = None
    if pending_agent_commits:
        pending_path = written_path.with_name(
            written_path.stem + "_pending_agent.json"
        )
        pending_path.write_text(
            json.dumps(
                {
                    "repo_id": repo_id,
                    "generated_at": generated_at,
                    "agent_judgments_path": str(judgments_path),
                    "instruction": (
                        "Claude main agent: for each commit, run git show <sha>, "
                        "decide is_bug_fix, append to agent_judgments_path, then re-run BMA."
                    ),
                    "commits": pending_agent_commits,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    window_desc = "基于 last_scan_ref 增量扫描"
    if cfg.since or cfg.until:
        window_desc = f"时间窗 自 {cfg.since or '-'} 至 {cfg.until or '-'}"

    mail_payload = {
        "report_path": str(written_path.resolve()),
        "repo_id": repo_id,
        "generated_at": generated_at,
        "commits_scanned": model.stats.commits_scanned,
        "need_sync_count": model.stats.need_sync_count,
        "pending_agent_count": model.stats.pending_agent_count,
        "pending_agent_path": str(pending_path.resolve()) if pending_path else None,
        "window_desc": window_desc,
        "skipped_branches": sorted(set(skipped_branches)),
    }

    return RepoProcessResult(
        repo_id=repo_id,
        report_path=written_path,
        new_baselines={} if use_time_window or not cfg.update_state else new_baselines,
        mail_payload=mail_payload,
    )


def _cherry_pick_hint(source_sha: str, target_branch: str) -> str:
    short_sha = source_sha[:12]
    return f"git checkout {target_branch} && git cherry-pick {short_sha}"
