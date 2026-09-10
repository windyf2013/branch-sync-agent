from __future__ import annotations

import importlib.resources
import json
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any

from bsa.domain.labels import (
    failure_stage_label,
    humanize_stop_reason,
    kind_label,
    report_status_label,
)
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
    # 中文主 + 英文副标：人一眼读懂，机器仍可按枚举对接（与页脚既有约定一致）。
    return f'<span class="badge kind-{kind}">{_esc(label or f"{kind_label(kind)} ({kind})")}</span>'


def _status_badge(status: str, css_class: str) -> str:
    """分支/commit/build 状态徽章：`成功 (SUCCESS)` 形态。"""
    return (
        f'<span class="badge {css_class}">'
        f"{_esc(f'{report_status_label(status)} ({status})')}</span>"
    )


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


def _first_line(text: object) -> str:
    """提交说明的首行（subject）。正文多行会把表格撑爆，且表格只需要主题。"""
    return str(text or "").strip().splitlines()[0] if str(text or "").strip() else ""


def _topology_pairs(state: dict) -> list[dict]:
    """源 → 目标配对（来自 detect_commits 写进 state 的 topology 投影）。

    旧周期 / 手动链路无该键 → 空列表，调用方各自降级；绝不从分支名反推配对。
    """
    return state.get("topology") or []


def _sources_by_target(state: dict) -> dict[str, list[str]]:
    """目标分支 → 喂它的源分支列表（保序去重）。

    报告要说清「这个主分支的改动从哪来」，只能靠 topology；sources/targets 两个
    扁平列表给不出配对关系。
    """
    mapping: dict[str, list[str]] = {}
    for entry in _topology_pairs(state):
        sources = entry.get("sources") or []
        for target in entry.get("targets") or []:
            bucket = mapping.setdefault(target, [])
            bucket.extend(src for src in sources if src not in bucket)
    return mapping


def _topology_body(state: dict) -> str:
    """同步拓扑区块：有配对就成对呈现，没有就退回扁平清单（不伪造）。"""
    pairs = _topology_pairs(state)
    if not pairs:
        sources = sorted(state.get("sources") or [])
        targets = sorted(state.get("targets") or [])
        if not sources and not targets:
            return ""
        return (
            "<h3>同步拓扑</h3>"
            '<div class="info-grid">'
            + _info_item("源分支（业务分支）", "、".join(sources) or "—")
            + _info_item("目标分支（主分支）", "、".join(targets) or "—")
            + "</div>"
        )
    rows = []
    for entry in pairs:
        rows.append(
            "<tr>"
            f'<td class="mono">{_esc(entry.get("section") or "—")}</td>'
            f'<td>{"、".join(_esc(s) for s in entry.get("sources") or []) or "—"}</td>'
            f'<td class="arrow" aria-hidden="true">→</td>'
            f'<td>{"、".join(_esc(t) for t in entry.get("targets") or []) or "—"}</td>'
            "</tr>"
        )
    return (
        "<h3>同步拓扑（业务分支 → 主分支）</h3>"
        '<div class="table-wrap"><table>'
        "<thead><tr><th>产品线</th><th>业务分支（源）</th><th></th>"
        "<th>主分支（目标）</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table></div>"
    )


def _detection_body(state: dict, report: Report) -> str:
    counts = _conclusion_counts(state)
    commits = state.get("detected_commits") or []
    branches = sorted(state.get("branch_results") or {})
    status = state.get("status", "") or report.summary.get("status", "")
    actions = len(report.action_required)

    kpis = [
        _kpi("状态", report_status_label(status), "neutral"),
        _kpi("检测 commit", str(len(commits))),
        _kpi("结论数", str(_conclusion_total(state))),
        _kpi("待同步", str(counts["NeedSync"]), "need"),
        _kpi("待人工", str(counts["ManualReview"]), "review"),
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
    topology = _topology_body(state)
    if topology:
        parts.append(topology)

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
        badges = "".join(
            _badge(c.kind, f"{target}: {kind_label(c.kind)} ({c.kind})")
            for target, c in per_target.items()
        )
        rows.append(
            "<tr>"
            f'<td class="mono">{_esc(commit.sha[:10])}</td>'
            f"<td>{_esc(commit.source_branch)}</td>"
            f"<td>{_esc(_first_line(commit.message))}</td>"
            f'<td class="muted">{_esc(commit.author)}</td>'
            f'<td><span class="badge-group">{badges}</span></td>'
            "</tr>"
        )
    if commits:
        parts.append(
            '<h3>提交与决策</h3>'
            '<div class="table-wrap"><table>'
            # 「作者原文」是刻意标注：提交说明是元数据，不是引擎结论。
            # cycle-2026-09-10 的一句「编译错误待yuanhuaili修改」被当成系统诊断，
            # 因为这一列与引擎判定视觉同级。
            "<thead><tr><th>SHA</th><th>源分支</th><th>提交说明（作者原文）</th>"
            "<th>作者</th><th>决策</th></tr></thead>"
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
    badges = [
        f'<span class="badge {cherry_class}">'
        f"cherry-pick {report_status_label(cherry)} ({cherry})</span>"
    ]
    for model, outcome in commit.build.items():
        kind = {"OK": "badge-ok", "FAILED": "badge-need", "SKIPPED": "badge-skip"}.get(
            outcome.status, "badge-skip"
        )
        badges.append(
            f'<span class="badge {kind}">'
            f"{_esc(model)} {report_status_label(outcome.status)} ({outcome.status})</span>"
        )
    return '<span class="badge-group">' + "".join(badges) + "</span>"


def _sync_body(state: dict) -> str:
    results = state.get("branch_results") or {}
    if not results:
        return _empty_state("本周期无可同步的目标分支 — 空检")
    # sha → CommitInfo：报告要说清「这个 commit 是什么、从哪来」，只需说明/作者/源分支。
    commits_by_sha = {c.sha: c for c in (state.get("detected_commits") or [])}
    sources_by_target = _sources_by_target(state)
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
            f'<div class="callout">{_esc(humanize_stop_reason(branch.stop_reason))}</div>'
            if branch.stop_reason
            else ""
        )
        # 来源分支：报告此前只有目标分支名，收件人无从知道改动从哪条业务分支来。
        source_list = sources_by_target.get(target) or []
        origin = (
            _info_item("来源分支", "、".join(source_list))
            if source_list
            else _info_item("来源分支", "—")
        )
        failed = branch.status != "SUCCESS"
        if failed:
            stage = _failure_stage(branch)
            detail = "".join(
                [
                    _info_item(
                        "失败阶段",
                        failure_stage_label(stage)
                        if stage
                        else humanize_stop_reason(branch.stop_reason or branch.status),
                    ),
                    _info_item("Agent 尝试次数", str(_agent_attempts(branch))),
                    _info_item("建议", _recommendation(branch, stage)),
                ]
            )
            fail_detail = '<div class="info-grid">' + origin + detail + "</div>"
        else:
            fail_detail = '<div class="info-grid">' + origin + "</div>"
        rows = []
        for commit in branch.commits:
            info = commits_by_sha.get(commit.sha)
            subject = _first_line(info.message) if info is not None else "—"
            author = info.author if info is not None else "—"
            rows.append(
                "<tr>"
                f'<td class="mono">{_esc(commit.sha[:10])}</td>'
                f"<td>{_esc(subject)}</td>"
                f'<td class="muted">{_esc(author)}</td>'
                f"<td>{_commit_status_badges(commit)}</td>"
                "</tr>"
            )
        table = (
            '<div class="table-wrap"><table>'
            "<thead><tr><th>SHA</th><th>提交说明（作者原文）</th><th>作者</th>"
            "<th>状态</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody>"
            "</table></div>"
        ) if rows else ""
        parts.append(
            "<article class=\"target-card\">"
            f"<h3>{_esc(target)} "
            f'<span class="badge {status_class}">'
            f"{_esc(f'{report_status_label(branch.status)} ({branch.status})')}</span></h3>"
            '<div class="panel-body">'
            f"{patch}{push}{stop}{fail_detail}{table}"
            "</div></article>"
        )
    return "\n".join(parts)


def _load_template(name: str = "report.html") -> str:
    resource = importlib.resources.files("bsa.report") / "templates" / name
    if resource.is_file():
        return resource.read_text(encoding="utf-8")
    fallback = Path(__file__).resolve().parent / "templates" / name
    return fallback.read_text(encoding="utf-8")


# 周期终态 → 平台同款徽章类（web .badge: success/partial/failed/running/neutral）。
_STATUS_BADGE_CLASS = {
    "SUCCESS": "badge-ok",
    "REPORTED": "badge-ok",
    "COMPLETED": "badge-ok",
    "PARTIAL": "badge-review",
    "RUNNING": "badge-pending",
    "FAILED": "badge-need",
    "MANUAL": "badge-scope",
}


def _hero_stats(state: dict, report: Report) -> str:
    """周期条内联统计：检测 / 同步 / 跳过 / 待确认。"""
    counts = _conclusion_counts(state)
    results = state.get("branch_results") or {}
    synced = sum(
        1 for b in results.values() for c in b.commits if c.cherry_pick in ("OK", "EMPTY")
    )
    items = [
        ("检测 commit", len(state.get("detected_commits") or []), "neutral"),
        ("同步", synced, "ok"),
        ("跳过", counts["AlreadyIncluded"] + counts["OutOfScope"], "skip"),
        ("待确认", counts["ManualReview"], "review"),
    ]
    return "".join(
        f'<span class="stat stat-{kind}">'
        f'<b class="stat-num">{value}</b>'
        f'<span class="stat-label">{_esc(label)}</span></span>'
        for label, value, kind in items
    )


def _pair_html(sources: object, targets: object, section: str = "") -> str:
    """一组「源 → 目标」的行内表达（周期条与空检态共用）。"""
    srcs = "、".join(_esc(s) for s in (sources or [])) or "—"
    tgts = "、".join(_esc(t) for t in (targets or [])) or "—"
    prefix = f'<span class="mono topo-section">{_esc(section)}</span>' if section else ""
    return f'{prefix}<b>{srcs}</b><span class="arrow">→</span><b>{tgts}</b>'


def _hero_topology(state: dict) -> str:
    """周期条下方的「源 → 目标」一行式拓扑；无配对数据时退回扁平清单。"""
    pairs = _topology_pairs(state)
    if pairs:
        chunks = [
            _pair_html(entry.get("sources"), entry.get("targets"), entry.get("section") or "")
            for entry in pairs
        ]
        return '<span class="topo-pair">' + " · ".join(chunks) + "</span>"
    sources = sorted(state.get("sources") or [])
    targets = sorted(state.get("targets") or [])
    if not sources and not targets:
        return ""
    return '<span class="topo-pair">' + _pair_html(sources, targets) + "</span>"


def render_html_report(state: dict, report: Report) -> Path:
    """Render the end-of-cycle HTML report into ``report.html_path``."""
    report.html_path.parent.mkdir(parents=True, exist_ok=True)
    status = state.get("status", "") or report.summary.get("status", "")
    tokens = {
        "CYCLE_ID": _esc(report.cycle_id),
        "SCAN_WINDOW": _esc(_window_str(state)),
        "GENERATED_AT": _esc(_now_cst()),
        "STATUS_CLASS": _STATUS_BADGE_CLASS.get(str(status), "badge-scope"),
        "STATUS_LABEL": _esc(f"{report_status_label(status)} ({status})"),
        "HERO_STATS": _hero_stats(state, report),
        "HERO_TOPOLOGY": _hero_topology(state),
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
        header = f"- [{target}] {report_status_label(branch.status)} ({branch.status})"
        if branch.stop_reason:
            header += f"（{humanize_stop_reason(branch.stop_reason)}）"
        lines.append(header)
        for commit in branch.commits:
            if commit.cherry_pick not in ("OK", "EMPTY"):
                lines.append(
                    f"    {commit.sha[:10]} cherry-pick "
                    f"{report_status_label(commit.cherry_pick)} ({commit.cherry_pick})"
                )
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
    status = state.get("status", "") or report.summary.get("status", "")
    lines = [
        f"Branch Sync Agent 周期报告 {report.cycle_id}",
        f"状态: {report_status_label(status)} ({status})",
        f"扫描窗口: {_window_str(state)}",
        f"检测 commit: {len(commits)}",
        f"目标分支: {len(branches)}",
        "结论: "
        f"NeedSync {counts['NeedSync']} / AlreadyIncluded {counts['AlreadyIncluded']} / "
        f"ManualReview {counts['ManualReview']} / OutOfScope {counts['OutOfScope']}",
    ]
    # 源 → 目标配对：纯文本兜底也要能看出「哪个业务分支同步到哪个主分支」。
    pairs = _topology_pairs(state)
    if pairs:
        lines.append("同步拓扑:")
        for entry in pairs:
            lines.append(
                f"  [{entry.get('section') or '—'}] "
                f"{'、'.join(entry.get('sources') or []) or '—'}"
                f" → {'、'.join(entry.get('targets') or []) or '—'}"
            )
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




# ---------------------------------------------------------------- 邮件正文 HTML 摘要

# 邮件里失败明细的条数上限：多分支多失败时正文会爆长，且 Gmail 对超长 HTML 会折叠。
_EMAIL_FAILURE_LIMIT = 10

# 状态 → (前景, 背景, 描边)，与平台 .badge 的语义色同值（邮件里必须字面 hex）。
_BADGE_COLORS: dict[str, tuple[str, str, str]] = {
    "SUCCESS": ("#16a34a", "#f0fdf4", "#bbf7d0"),
    "REPORTED": ("#16a34a", "#f0fdf4", "#bbf7d0"),
    "COMPLETED": ("#16a34a", "#f0fdf4", "#bbf7d0"),
    "PARTIAL": ("#d97706", "#fffbeb", "#fde68a"),
    "FAILED": ("#dc2626", "#fef2f2", "#fecaca"),
    "MANUAL": ("#0284c7", "#f0f9ff", "#bae6fd"),
    "RUNNING": ("#0284c7", "#f0f9ff", "#bae6fd"),
}
_BADGE_FALLBACK = ("#64748b", "#f8fafc", "#e9edf4")

_FONT = "'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif"


def _email_badge(text: str, status: str) -> str:
    """邮件版徽章。

    平台的 ``.badge::before`` 圆点在邮件里必须换成字面 ``span`` —— Outlook 桌面与
    Gmail（非 Gmail 账户）都不渲染伪元素。
    """
    color, bg, bd = _BADGE_COLORS.get(status, _BADGE_FALLBACK)
    return (
        '<span style="display:inline-block;padding:2px 9px;border-radius:999px;'
        f"font-family:{_FONT};font-size:12px;font-weight:600;color:{color};"
        f'background:{bg};border:1px solid {bd};white-space:nowrap;">'
        f'<span style="color:{color};">&#9679;</span> {text}</span>'
    )


def _email_heading(text: str, color: str = "#334155") -> str:
    return (
        '<div style="font-family:'
        + _FONT
        + f';font-size:13px;font-weight:700;color:{color};margin-bottom:8px;">{text}</div>'
    )


def _email_stat_row(state: dict) -> str:
    """统计条：检测 / 同步 / 跳过 / 待确认（与平台工作台周期条同指标）。"""
    counts = _conclusion_counts(state)
    results = state.get("branch_results") or {}
    synced = sum(
        1 for b in results.values() for c in b.commits if c.cherry_pick in ("OK", "EMPTY")
    )
    cells = [
        ("检测 commit", len(state.get("detected_commits") or []), "#0f172a"),
        ("同步", synced, "#16a34a"),
        ("跳过", counts["AlreadyIncluded"] + counts["OutOfScope"], "#94a3b8"),
        ("待确认", counts["ManualReview"], "#d97706"),
    ]
    html = []
    for idx, (label, value, color) in enumerate(cells):
        border = "" if idx == 0 else "border-left:1px solid #e9edf4;"
        html.append(
            f'<td align="center" style="padding:12px 8px;{border}">'
            f'<div style="font-family:{_FONT};font-size:20px;font-weight:700;'
            f'color:{color};">{value}</div>'
            f'<div style="font-family:{_FONT};font-size:12px;color:#64748b;'
            f'margin-top:2px;">{label}</div></td>'
        )
    return "".join(html)


def _email_topology(state: dict) -> str:
    """源 → 目标一行式拓扑。无配对数据时退回扁平清单（不伪造配对）。"""
    pairs = _topology_pairs(state)
    mono = "font-family:Consolas,'Cascadia Code',monospace;font-size:12.5px;color:#334155;"
    arrow = '<span style="color:#2f6bfb;font-weight:700;">&rarr;</span>'
    if pairs:
        rows = []
        for entry in pairs:
            srcs = "、".join(_esc(s) for s in entry.get("sources") or []) or "—"
            tgts = "、".join(_esc(t) for t in entry.get("targets") or []) or "—"
            section = _esc(entry.get("section") or "")
            rows.append(
                f'<div style="{mono}margin-top:4px;word-break:break-all;">'
                f"{srcs} {arrow} {tgts}"
                f'<span style="color:#94a3b8;font-size:11.5px;"> · {section}</span></div>'
            )
        return (
            '<tr><td style="padding:16px 24px 0;">'
            + _email_heading("同步拓扑（业务分支 → 主分支）")
            + "".join(rows)
            + "</td></tr>"
        )
    sources = sorted(state.get("sources") or [])
    targets = sorted(state.get("targets") or [])
    if not sources and not targets:
        return ""
    srcs = "、".join(_esc(s) for s in sources) or "—"
    tgts = "、".join(_esc(t) for t in targets) or "—"
    return (
        f'<tr><td style="padding:16px 24px 0;{mono}word-break:break-all;">'
        f"{srcs} {arrow} {tgts}</td></tr>"
    )


def _email_conclusions(state: dict) -> str:
    """四态结论计数一行。保留英文枚举便于自动化对接（与报告页脚约定一致）。"""
    counts = _conclusion_counts(state)
    return (
        f'<tr><td style="padding:10px 24px 0;font-family:{_FONT};font-size:12.5px;'
        "color:#64748b;\">结论："
        f"待同步 {counts['NeedSync']} · 已包含 {counts['AlreadyIncluded']} · "
        f"待人工 {counts['ManualReview']} · 不适用 {counts['OutOfScope']}</td></tr>"
    )


def _email_attention(report: Report, state: dict) -> str:
    """Action Required / 节点错误提示（有才显示）。"""
    actions = len(report.action_required)
    errors = len(state.get("errors") or {})
    if not actions and not errors:
        return ""
    bits = []
    if actions:
        bits.append(f"Action Required {actions} 项")
    if errors:
        bits.append(f"节点错误记录 {errors} 条")
    return (
        '<tr><td style="padding:12px 24px 0;">'
        '<div style="padding:10px 14px;background:#fffbeb;border:1px solid #fde68a;'
        f'border-radius:8px;font-family:{_FONT};font-size:12.5px;color:#a16207;">'
        f'需人工关注：{" · ".join(bits)}</div></td></tr>'
    )


def _email_branches(state: dict) -> str:
    """各目标分支执行结果：目标分支 + 状态 + 来源分支（+ 成功时的合入命令）。"""
    results = state.get("branch_results") or {}
    if not results:
        return ""
    sources_by_target = _sources_by_target(state)
    rows = []
    for target in sorted(results):
        branch = results[target]
        badge_text = f"{report_status_label(branch.status)} ({branch.status})"
        parts = [
            f'<div style="font-family:Consolas,monospace;font-size:13px;font-weight:600;'
            f'color:#0f172a;word-break:break-all;">{_esc(target)}</div>',
            '<div style="margin-top:6px;">'
            f"{_email_badge(badge_text, branch.status)}</div>",
        ]
        srcs = sources_by_target.get(target) or []
        if srcs:
            # 「来源分支」是本次改造的核心：此前目标卡片只有目标分支名。
            parts.append(
                f'<div style="font-family:{_FONT};font-size:12px;color:#64748b;'
                f'margin-top:4px;">来源：{"、".join(_esc(s) for s in srcs)}</div>'
            )
        if branch.patch_path and branch.status == "SUCCESS":
            parts.append(
                '<div style="margin-top:8px;padding:6px 10px;background:#eef4ff;'
                "border:1px solid #d7e4ff;border-radius:8px;font-family:Consolas,monospace;"
                f'font-size:12px;color:#1d4ed8;word-break:break-all;">'
                f"git checkout {_esc(target)} &amp;&amp; git am {_esc(branch.patch_path)}</div>"
            )
        rows.append(
            '<tr><td style="padding:12px 24px;border-top:1px solid #e9edf4;">'
            + "".join(parts)
            + "</td></tr>"
        )
    return (
        '<tr><td style="padding:20px 24px 0;">'
        + _email_heading("同步执行结果", "#334155")
        + '</td></tr><tr><td style="padding:0;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">'
        + "".join(rows)
        + "</table></td></tr>"
    )


def _email_failures(state: dict) -> str:
    """失败/停批明细：不点开附件也能直读哪个分支/commit/型号停批。

    超过上限时明确告知「还有 N 条」—— 静默截断会让收件人以为已经看全。
    """
    lines = _branch_failure_lines(state)
    if not lines:
        return ""
    shown = lines[:_EMAIL_FAILURE_LIMIT]
    truncated = len(lines) - len(shown)
    items = "".join(
        '<div style="font-family:Consolas,monospace;font-size:12px;color:#334155;'
        f'line-height:1.9;word-break:break-all;">{_esc(line.strip())}</div>'
        for line in shown
    )
    more = (
        f'<div style="font-family:{_FONT};font-size:12px;color:#64748b;margin-top:6px;">'
        f"…还有 {truncated} 条，见附件 report.html</div>"
        if truncated
        else ""
    )
    return (
        '<tr><td style="padding:20px 24px 0;">'
        + _email_heading("失败 / 停批明细", "#dc2626")
        + '<div style="padding:12px 14px;background:#fef2f2;border:1px solid #fecaca;'
        f'border-radius:8px;">{items}{more}</div></td></tr>'
    )


def build_email_html(state: dict, report: Report, subject: str = "") -> str:
    """邮件正文的 HTML 摘要（完整报告作附件）。

    收件人应在**不点开附件**的情况下判断本周期成没成、哪些分支同步了、哪里停批；
    完整报告的逐 commit 明细、编译日志与审计 diff 留在附件里。

    邮件客户端硬约束：零 ``<style>`` 块、零 CSS 变量、零伪元素、零 flex/grid ——
    Gmail（非 Gmail 账户）会剥 ``<style>``，Outlook 桌面不认 ``var(--x)`` 与
    ``::before``。故此处一律内联样式 + ``<table role="presentation">``。
    """
    status = state.get("status", "") or report.summary.get("status", "")
    cycle_line = (
        f'<code style="font-family:Consolas,monospace;">{_esc(report.cycle_id)}</code>'
        f" &nbsp; {_email_badge(f'{report_status_label(status)} ({status})', status)}"
        f"<br>窗口 {_esc(_window_str(state))}"
    )
    sections = (
        _email_conclusions(state)
        + _email_attention(report, state)
        + _email_branches(state)
        + _email_failures(state)
    )
    tokens = {
        "SUBJECT": _esc(subject or f"Branch Sync Agent 周期报告 {report.cycle_id}"),
        "TITLE": _esc(f"分支同步周期报告 · {report_status_label(status)}"),
        "CYCLE_LINE": cycle_line,
        "TOPOLOGY_ROWS": _email_topology(state),
        "STAT_ROWS": _email_stat_row(state),
        "SECTIONS": sections,
    }
    html = _load_template("email_summary.html")
    for key, value in tokens.items():
        html = html.replace("{{" + key + "}}", value)
    return html
