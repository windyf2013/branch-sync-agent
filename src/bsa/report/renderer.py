from __future__ import annotations

import importlib.resources
import json
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any

from bsa.domain.models import Report

_CST = timezone(timedelta(hours=8))


def _esc(text: object) -> str:
    return escape(str(text), quote=True)


def _now_cst() -> str:
    return datetime.now(_CST).isoformat(timespec="seconds")


def _window_str(state: dict) -> str:
    since, until = state.get("scan_window") or ("", "")
    return f"{since} ~ {until}"


def _conclusion_counts(state: dict) -> dict[str, int]:
    counts = {"NeedSync": 0, "AlreadyIncluded": 0, "ManualReview": 0, "OutOfScope": 0}
    for per_target in (state.get("decisions") or {}).values():
        for conclusion in per_target.values():
            kind = conclusion.kind
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def _conclusion_total(state: dict) -> int:
    return sum(len(v) for v in (state.get("decisions") or {}).values())


def _badge(kind: str, label: str | None = None) -> str:
    return f'<span class="badge kind-{kind}">{_esc(label or kind)}</span>'


def _kpi(label: str, value: str, kind: str = "neutral") -> str:
    return (
        f'<div class="kpi kpi-{kind}">'
        f'<span class="label">{_esc(label)}</span>'
        f'<span class="value">{_esc(value)}</span>'
        f"</div>"
    )


def _info_item(key: str, value: str) -> str:
    return (
        '<div class="info-item">'
        f'<span class="k">{_esc(key)}</span>'
        f'<span class="v">{_esc(value)}</span>'
        f"</div>"
    )


def _empty_state(text: str) -> str:
    return f'<div class="empty-state">{_esc(text)}</div>'


def _detection_body(state: dict, report: Report) -> str:
    counts = _conclusion_counts(state)
    commits = state.get("detected_commits") or []
    branches = sorted(state.get("branch_results") or {})
    status = state.get("status", "") or report.summary.get("status", "")
    actions = len(report.action_required)

    kpis = [
        _kpi("状态", str(status), "neutral"),
        _kpi("检测 commit", str(len(commits))),
        _kpi("结论数", str(_conclusion_total(state))),
        _kpi("NeedSync", str(counts["NeedSync"]), "need"),
        _kpi("ManualReview", str(counts["ManualReview"]), "review"),
        _kpi("目标分支", str(len(branches)), "ok"),
        _kpi("Action Required", str(actions), "review" if actions else "neutral"),
    ]
    info = [
        _info_item("周期", report.cycle_id),
        _info_item("扫描窗口", _window_str(state)),
        _info_item("branch.md", str(state.get("branch_md_version", ""))),
        _info_item("生成时间", _now_cst()),
    ]
    parts = [
        '<div class="kpi-grid">' + "\n".join(kpis) + "</div>",
        '<div class="info-grid">' + "\n".join(info) + "</div>",
    ]

    if state.get("errors"):
        errors = len(state["errors"])
        parts.append(
            '<div class="risk-banner">'
            f'<span class="risk-title">节点错误</span>'
            f'<span class="risk-metric">{errors}</span>'
            '<span class="risk-note">错误记录见 Action Required 区块</span>'
            "</div>"
        )
    elif not actions:
        parts.append(
            '<div class="risk-banner ok">'
            '<span class="risk-title">无需人工干预</span>'
            '<span class="risk-metric">0</span>'
            '<span class="risk-note">本周期无 Action Required 项</span>'
            "</div>"
        )

    rows: list[str] = []
    for commit in commits:
        per_target = (state.get("decisions") or {}).get(commit.sha) or {}
        badges = "".join(_badge(c.kind, f"{target}: {c.kind}") for target, c in per_target.items())
        rows.append(
            "<tr>"
            f'<td class="mono">{_esc(commit.sha[:10])}</td>'
            f"<td>{_esc(commit.source_branch)}</td>"
            f"<td>{_esc(commit.message)}</td>"
            f'<td><span class="badge-group">{badges}</span></td>'
            "</tr>"
        )
    if commits:
        parts.append(
            '<h3>提交与决策</h3>'
            '<div class="table-wrap"><table>'
            "<thead><tr><th>SHA</th><th>源分支</th><th>提交说明</th><th>决策</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody>"
            "</table></div>"
        )
    return "\n".join(parts)


def _action_body(report: Report) -> str:
    if not report.action_required:
        return _empty_state("本周期无 Action Required 项 — 无需人工处理")
    parts: list[str] = []
    for item in report.action_required:
        if "error" in item:
            parts.append(
                '<div class="callout">'
                f'<strong>{_esc(item["node"])} 节点错误</strong>'
                f'<div>{_esc(item["error"])}</div>'
                "</div>"
            )
            continue
        evidence = "".join(f"<li>{_esc(e)}</li>" for e in item.get("evidence") or [])
        parts.append(
            '<div class="callout pending">'
            f'<strong>{_badge(item.get("kind", "ManualReview"))} '
            f'{_esc(item.get("sha", ""))} → {_esc(item.get("branch", ""))}</strong>'
            f'{"<ul class=\"compact\">" + evidence + "</ul>" if evidence else ""}'
            "</div>"
        )
    return "\n".join(parts)


def _failure_stage(branch: Any) -> str | None:
    """Which stage failed first on a branch: cherry_pick / conflict / build."""
    for commit in branch.commits:
        if commit.cherry_pick == "FAILED":
            return "cherry_pick"
    for commit in branch.commits:
        if commit.cherry_pick == "CONFLICT":
            return "conflict"
    for commit in branch.commits:
        if any(outcome.status == "FAILED" for outcome in commit.build.values()):
            return "build"
    return None


def _agent_attempts(branch: Any) -> int:
    return sum(
        outcome.agent_attempts
        for commit in branch.commits
        for outcome in commit.build.values()
    )


def _recommendation(branch: Any, stage: str | None) -> str:
    if branch.status == "SUCCESS":
        return "人工推送"
    if branch.status == "MANUAL":
        return "人工处理"
    if branch.status == "PARTIAL":
        return "部分成功，剩余停批人工审核"
    if stage == "conflict":
        return "停批，人工解决冲突"
    if stage == "cherry_pick":
        return "停批，人工处理（基础设施失败）"
    return "停批，人工审核"


def _commit_status_badges(commit: Any) -> str:
    cherry = commit.cherry_pick
    cherry_class = {
        "OK": "badge-ok",
        "EMPTY": "badge-ok",
        "CONFLICT": "badge-review",
        "FAILED": "badge-need",
    }.get(cherry, "badge-skip")
    badges = [f'<span class="badge {cherry_class}">cherry-pick {cherry}</span>']
    for model, outcome in commit.build.items():
        kind = {"OK": "badge-ok", "FAILED": "badge-need", "SKIPPED": "badge-skip"}.get(
            outcome.status, "badge-skip"
        )
        badges.append(f'<span class="badge {kind}">{model} {outcome.status}</span>')
    return '<span class="badge-group">' + "".join(badges) + "</span>"


def _sync_body(state: dict) -> str:
    results = state.get("branch_results") or {}
    if not results:
        return _empty_state("本周期无可同步的目标分支 — 空检")
    parts: list[str] = []
    for target in sorted(results):
        branch = results[target]
        status_class = {
            "SUCCESS": "badge-ok",
            "PARTIAL": "badge-review",
            "FAILED": "badge-need",
            "MANUAL": "badge-pending",
        }.get(branch.status, "badge-skip")
        patch = (
            f'<div class="hint">patch: {_esc(branch.patch_path)}</div>'
            if branch.patch_path
            else ""
        )
        push = (
            f'<div class="hint">git checkout {_esc(target)}'
            f" && git am {_esc(branch.patch_path)}</div>"
            if branch.patch_path and branch.status == "SUCCESS"
            else ""
        )
        stop = (
            f'<div class="callout">{_esc(branch.stop_reason)}</div>'
            if branch.stop_reason
            else ""
        )
        failed = branch.status != "SUCCESS"
        if failed:
            stage = _failure_stage(branch)
            detail = "".join(
                [
                    _info_item("失败阶段", stage or branch.stop_reason or branch.status),
                    _info_item("Agent 尝试次数", str(_agent_attempts(branch))),
                    _info_item("建议", _recommendation(branch, stage)),
                ]
            )
            fail_detail = '<div class="info-grid">' + detail + "</div>"
        else:
            fail_detail = ""
        rows = []
        for commit in branch.commits:
            rows.append(
                "<tr>"
                f'<td class="mono">{_esc(commit.sha[:10])}</td>'
                f"<td>{_commit_status_badges(commit)}</td>"
                "</tr>"
            )
        table = (
            '<div class="table-wrap"><table>'
            "<thead><tr><th>SHA</th><th>状态</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody>"
            "</table></div>"
        ) if rows else ""
        parts.append(
            "<article class=\"target-card\">"
            f"<h3>{_esc(target)} <span class=\"badge {status_class}\">{branch.status}</span></h3>"
            '<div class="panel-body">'
            f"{patch}{push}{stop}{fail_detail}{table}"
            "</div></article>"
        )
    return "\n".join(parts)


def _load_template() -> str:
    resource = importlib.resources.files("bsa.report") / "templates" / "report.html"
    if resource.is_file():
        return resource.read_text(encoding="utf-8")
    fallback = Path(__file__).resolve().parent / "templates" / "report.html"
    return fallback.read_text(encoding="utf-8")


def render_html_report(state: dict, report: Report) -> Path:
    """Render the end-of-cycle HTML report into ``report.html_path``."""
    report.html_path.parent.mkdir(parents=True, exist_ok=True)
    tokens = {
        "CYCLE_ID": report.cycle_id,
        "SCAN_WINDOW": _window_str(state),
        "GENERATED_AT": _now_cst(),
        "DETECTION_BODY": _detection_body(state, report),
        "ACTION_BODY": _action_body(report),
        "SYNC_BODY": _sync_body(state),
    }
    html = _load_template()
    for key, value in tokens.items():
        html = html.replace("{{" + key + "}}", value)
    report.html_path.write_text(html, encoding="utf-8")
    return report.html_path


def write_decisions_json(state: dict, report: Report) -> Path:
    """Persist the frozen decisions dict to ``report.decisions_json_path``."""
    report.decisions_json_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        sha: {target: c.model_dump() for target, c in per_target.items()}
        for sha, per_target in (state.get("decisions") or {}).items()
    }
    report.decisions_json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report.decisions_json_path


def write_agent_diffs(state: dict, report: Report) -> list[Path]:
    """Persist agent-produced diffs for audit (决策 18 闸门5) under
    ``<cycle>/audit/<branch>/<sha>_conflict.diff`` and ``<sha>_<model>_fix.diff``."""
    audit_root = report.decisions_json_path.parent / "audit"
    written: list[Path] = []
    for target, branch in sorted((state.get("branch_results") or {}).items()):
        for commit in branch.commits:
            if commit.conflict_resolution is not None and commit.conflict_resolution.diff:
                path = audit_root / target / f"{commit.sha}_conflict.diff"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(commit.conflict_resolution.diff, encoding="utf-8")
                written.append(path)
            for model, outcome in commit.build.items():
                if outcome.fix_diff:
                    path = audit_root / target / f"{commit.sha}_{model}_fix.diff"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(outcome.fix_diff, encoding="utf-8")
                    written.append(path)
    return written


def _branch_failure_lines(state: dict) -> list[str]:
    """非 SUCCESS 分支的失败明细（停批 commit / 失败型号 / 首条错误）。

    cron 精简流程「失败不重试直接发邮件」：让收件人不点开 report.html 也能直读
    哪个分支/commit/型号停批。逐行缩进组织，mail 正文纯文本。
    """
    lines: list[str] = []
    for target in sorted(state.get("branch_results") or {}):
        branch = state["branch_results"][target]
        if branch.status == "SUCCESS":
            continue
        header = f"- [{target}] {branch.status}"
        if branch.stop_reason:
            header += f"（{branch.stop_reason}）"
        lines.append(header)
        for commit in branch.commits:
            if commit.cherry_pick not in ("OK", "EMPTY"):
                lines.append(f"    {commit.sha[:10]} cherry-pick {commit.cherry_pick}")
            for model, outcome in (commit.build or {}).items():
                if outcome.status != "FAILED":
                    continue
                err = (outcome.errors or [""])[0]
                lines.append(f"    {commit.sha[:10]} {model} 编译失败: {err[:200]}")
    return lines


def build_email_body(report: Report, state: dict) -> str:
    """Email body summary (决策 11): window / branch count / commit count / conclusions."""
    counts = _conclusion_counts(state)
    branches = sorted(state.get("branch_results") or {})
    commits = state.get("detected_commits") or []
    errors = len(state.get("errors") or {})
    lines = [
        f"Branch Sync Agent 周期报告 {report.cycle_id}",
        f"状态: {state.get('status', '') or report.summary.get('status', '')}",
        f"扫描窗口: {_window_str(state)}",
        f"检测 commit: {len(commits)}",
        f"目标分支: {len(branches)}",
        "结论: "
        f"NeedSync {counts['NeedSync']} / AlreadyIncluded {counts['AlreadyIncluded']} / "
        f"ManualReview {counts['ManualReview']} / OutOfScope {counts['OutOfScope']}",
    ]
    if report.action_required:
        lines.append(f"Action Required: {len(report.action_required)} 项")
    if errors:
        lines.append(f"警告: {errors} 条节点错误记录")
    failures = _branch_failure_lines(state)
    if failures:
        lines.append("")
        lines.append("失败/停批明细:")
        lines.extend(failures)
    lines.append("")
    lines.append("详细报告: " + str(report.html_path))
    return "\n".join(lines)
