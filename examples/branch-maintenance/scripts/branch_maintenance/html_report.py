from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from branch_maintenance.branch_md import DocBug
from branch_maintenance.conclude import Conclusion, ConclusionKind, Confidence

DEFAULT_TEMPLATE = Path(__file__).resolve().parent / "templates" / "branch_maintenance_report.html"

KIND_LABELS: dict[ConclusionKind, str] = {
    "NeedSync": "需要同步",
    "AlreadyIncluded": "已包含",
    "ManualReview": "人工确认",
    "OutOfScope": "不在范围",
}

CONFIDENCE_LABELS: dict[Confidence, str] = {
    "high": "高",
    "medium": "中",
    "low": "低",
}

LIFECYCLE_LABELS: dict[str, str] = {
    "active": "在维",
    "frozen": "锁版",
    "eol": "停维",
    "preview": "预览",
}

DEBT_KINDS = frozenset({"NeedSync", "ManualReview"})


@dataclass
class ScanSourceInfo:
    branch: str
    last_scan_ref: str | None
    new_tip: str | None
    resolved_ref: str | None = None
    commits_scanned: int = 0


@dataclass
class TargetConclusionRow:
    target_branch: str
    lifecycle: str
    conclusion: Conclusion
    suggestion: str | None = None
    cherry_pick_hint: str | None = None


@dataclass
class CommitReport:
    sha: str
    author: str
    committed_at: str
    message: str
    recognition_source: str
    changed_files: list[str]
    symbols: list[str]
    source_branch: str
    homologous_section: str
    issue_ids: list[str]
    target_rows: list[TargetConclusionRow] = field(default_factory=list)
    is_bug_fix: bool = True
    skip_sync_reason: str | None = None
    judge_reason: str | None = None
    needs_agent: bool = False


@dataclass
class TargetRollupItem:
    sha: str
    message: str
    source_branch: str
    confidence: Confidence
    target_branch: str = ""
    author: str = ""
    committed_at: str = ""
    conclusion_kind: str = ""
    cherry_pick_hint: str | None = None


@dataclass
class TargetRollup:
    target_branch: str
    lifecycle: str
    need_sync_items: list[TargetRollupItem] = field(default_factory=list)
    manual_review_items: list[TargetRollupItem] = field(default_factory=list)


@dataclass
class ReportStats:
    commits_scanned: int = 0
    bug_fix_count: int = 0
    heuristic_count: int = 0
    not_included_count: int = 0
    pending_agent_count: int = 0
    need_sync_count: int = 0
    already_included_count: int = 0
    manual_review_count: int = 0
    out_of_scope_count: int = 0
    # Commit-level coverage: how many commits have ≥1 pair of each kind.
    need_sync_commits: int = 0
    already_included_commits: int = 0
    manual_review_commits: int = 0
    out_of_scope_commits: int = 0
    target_debt_total: int = 0


@dataclass
class ReportModel:
    repo_id: str
    repo_path: str
    generated_at: str
    branch_file: str
    branch_md_parsed_at: str
    scan_sources: list[ScanSourceInfo]
    document_bugs: list[DocBug]
    unknown_branches: list[str]
    stats: ReportStats
    commits: list[CommitReport] = field(default_factory=list)
    by_target: list[TargetRollup] = field(default_factory=list)
    eval_time_range: str = ""


def _kind_label(kind: ConclusionKind) -> str:
    return KIND_LABELS.get(kind, kind)


def _confidence_label(value: Confidence | str) -> str:
    return CONFIDENCE_LABELS.get(value, str(value))  # type: ignore[arg-type]


def _lifecycle_label(value: str) -> str:
    return LIFECYCLE_LABELS.get(value, value)


def _kind_css_class(kind: ConclusionKind) -> str:
    return f"kind-{kind}"


_KIND_BADGE_CLASS: dict[ConclusionKind, str] = {
    "NeedSync": "badge-need",
    "ManualReview": "badge-review",
    "AlreadyIncluded": "badge-included",
    "OutOfScope": "badge-scope",
}


def _esc(value: object) -> str:
    return escape("" if value is None else str(value))


def format_report_time(value: object | None) -> str:
    """Format a timestamp as ``YYYY-MM-DD HH:MM:SS`` (second precision, no fraction)."""
    if value is None:
        return "—"
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")

    text = str(value).strip()
    if not text or text == "-":
        return "—"

    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        # Already wall-clock like "2026-07-29 00:00:00" or git-ish strings.
        if "T" in text:
            text = text.split(".", 1)[0].replace("T", " ")
        if "+" in text[10:]:
            text = text.split("+", 1)[0].strip()
        elif text.count("-") >= 3 and text.rfind("-") > 10:
            # Keep date-time before a trailing offset like -0800 if present.
            pass
        if "." in text:
            text = text.split(".", 1)[0]
        return text[:19] if len(text) >= 19 else text

    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def format_eval_time_range(since: str | None, until: str | None) -> str:
    if since or until:
        return f"{format_report_time(since)} ~ {format_report_time(until)}"
    return "增量扫描（无显式时间窗）"


def _count_conclusions(commits: list[CommitReport]) -> ReportStats:
    stats = ReportStats(commits_scanned=len(commits))
    for commit in commits:
        if commit.needs_agent or commit.recognition_source == "pending:claude-agent":
            stats.pending_agent_count += 1
        elif commit.is_bug_fix:
            stats.bug_fix_count += 1
            if commit.recognition_source.startswith("heuristic:"):
                stats.heuristic_count += 1
        else:
            stats.not_included_count += 1

        kind_counts = _conclusion_kind_counts(commit)
        if kind_counts["NeedSync"] > 0:
            stats.need_sync_commits += 1
            stats.need_sync_count += kind_counts["NeedSync"]
        if kind_counts["AlreadyIncluded"] > 0:
            stats.already_included_commits += 1
            stats.already_included_count += kind_counts["AlreadyIncluded"]
        if kind_counts["ManualReview"] > 0:
            stats.manual_review_commits += 1
            stats.manual_review_count += kind_counts["ManualReview"]
        if kind_counts["OutOfScope"] > 0:
            stats.out_of_scope_commits += 1
            stats.out_of_scope_count += kind_counts["OutOfScope"]
        stats.target_debt_total += kind_counts["NeedSync"] + kind_counts["ManualReview"]
    return stats


def _format_pair_dual(pair_count: int, commit_count: int) -> str:
    """Dual metric: assessment pairs + commits that have ≥1 such pair."""
    return f"{pair_count} 次（涉及 {commit_count} 个提交）"


def _format_pair_dual_short(pair_count: int, commit_count: int) -> str:
    """Compact dual metric for KPI cards."""
    return f"{pair_count} 次 · {commit_count} 提交"


def _build_by_target(commits: list[CommitReport]) -> list[TargetRollup]:
    grouped: dict[str, TargetRollup] = {}
    for commit in commits:
        for row in commit.target_rows:
            kind = row.conclusion.kind
            if kind not in DEBT_KINDS:
                continue
            rollup = grouped.get(row.target_branch)
            if rollup is None:
                rollup = TargetRollup(
                    target_branch=row.target_branch,
                    lifecycle=row.lifecycle,
                )
                grouped[row.target_branch] = rollup
            item = TargetRollupItem(
                sha=commit.sha,
                message=commit.message,
                source_branch=commit.source_branch,
                confidence=row.conclusion.confidence,
                target_branch=row.target_branch,
                author=commit.author,
                committed_at=commit.committed_at,
                conclusion_kind=kind,
                cherry_pick_hint=row.cherry_pick_hint
                or f"git checkout {row.target_branch} && git cherry-pick {commit.sha[:12]}",
            )
            if kind == "NeedSync":
                rollup.need_sync_items.append(item)
            else:
                rollup.manual_review_items.append(item)
    return sorted(grouped.values(), key=lambda entry: entry.target_branch)


def build_report_model(
    *,
    repo_id: str,
    repo_path: str,
    generated_at: str,
    branch_file: str,
    branch_md_parsed_at: str,
    scan_sources: list[ScanSourceInfo],
    document_bugs: list[DocBug],
    unknown_branches: list[str],
    commits: list[CommitReport],
    not_included_count: int = 0,
    eval_time_range: str = "",
    since: str | None = None,
    until: str | None = None,
) -> ReportModel:
    stats = _count_conclusions(commits)
    # Prefer explicit counter when caller still passes it; otherwise derive from commits.
    if not_included_count:
        stats.not_included_count = not_included_count
    range_text = eval_time_range or format_eval_time_range(since, until)
    return ReportModel(
        repo_id=repo_id,
        repo_path=repo_path,
        generated_at=format_report_time(generated_at),
        branch_file=branch_file,
        branch_md_parsed_at=format_report_time(branch_md_parsed_at),
        scan_sources=scan_sources,
        document_bugs=document_bugs,
        unknown_branches=unknown_branches,
        stats=stats,
        commits=commits,
        by_target=_build_by_target(commits),
        eval_time_range=range_text,
    )


def default_report_path(repo_id: str, output_dir: str) -> Path:
    safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in repo_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return Path(output_dir) / f"branch_maintenance_{safe_id}_{stamp}.html"


def _render_scan_sources(sources: list[ScanSourceInfo]) -> str:
    if not sources:
        return "<div class=\"empty-state\">本次未记录扫描源分支。</div>"
    rows = []
    for source in sources:
        rows.append(
            "<tr>"
            f"<td class=\"mono\">{_esc(source.branch)}</td>"
            f"<td class=\"mono\">{_esc(source.last_scan_ref or '--')}</td>"
            f"<td class=\"mono\">{_esc(source.new_tip or '--')}</td>"
            f"<td>{int(source.commits_scanned)}</td>"
            "</tr>"
        )
    return (
        "<div class=\"table-wrap\"><table><thead><tr>"
        "<th>扫描源分支</th><th>扫描起点 SHA</th><th>扫描终点 SHA</th><th>扫描提交数</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _kpi(label: str, value: object, css: str, *, dual: bool = False) -> str:
    value_css = "value value-dual" if dual else "value"
    return (
        f"<div class=\"kpi {css}\">"
        f"<span class=\"label\">{_esc(label)}</span>"
        f"<span class=\"{value_css}\">{_esc(value)}</span>"
        "</div>"
    )


def _render_home(model: ReportModel) -> str:
    stats = model.stats
    risk_css = "risk-banner ok" if stats.target_debt_total == 0 else "risk-banner"
    pending_row = ""
    if stats.pending_agent_count > 0:
        # Delivery reports should re-run after Agent judgments; show only if incomplete.
        pending_row = (
            f"<tr><td>待 Claude Agent 判定</td><td>{stats.pending_agent_count}</td>"
            "<td>交付前应完成判定并重跑；当前仍有未判定提交（非正常交付态）。</td></tr>"
        )

    need_dual = _format_pair_dual(stats.need_sync_count, stats.need_sync_commits)
    included_dual = _format_pair_dual(
        stats.already_included_count, stats.already_included_commits
    )
    review_dual = _format_pair_dual(
        stats.manual_review_count, stats.manual_review_commits
    )
    scope_dual = _format_pair_dual(
        stats.out_of_scope_count, stats.out_of_scope_commits
    )
    need_kpi = _format_pair_dual_short(stats.need_sync_count, stats.need_sync_commits)
    review_kpi = _format_pair_dual_short(
        stats.manual_review_count, stats.manual_review_commits
    )

    return (
        "<div class=\"kpi-grid kpi-grid-4\">"
        f"{_kpi('扫描提交数', stats.commits_scanned, 'kpi-neutral')}"
        f"{_kpi('Bug Fix', stats.bug_fix_count, 'kpi-ok')}"
        f"{_kpi('需要同步', need_kpi, 'kpi-need', dual=True)}"
        f"{_kpi('人工确认', review_kpi, 'kpi-review', dual=True)}"
        "</div>"
        f"<div class=\"{risk_css}\" data-bma-debt-total=\"{stats.target_debt_total}\">"
        "<div><div class=\"risk-title\">风险汇总</div></div>"
        f"<div class=\"risk-note\" data-bma-out-of-scope-count=\"{stats.out_of_scope_count}\" "
        f"data-bma-out-of-scope-commits=\"{stats.out_of_scope_commits}\">"
        f"需要同步：{need_dual} · 人工确认：{review_dual} · 不在范围：{scope_dual}"
        "</div></div>"
        "<h3>扫描信息</h3>"
        "<div class=\"info-grid\">"
        f"<div class=\"info-item\"><span class=\"k\">仓库路径</span>"
        f"<span class=\"v mono\">{_esc(model.repo_path)}</span></div>"
        f"<div class=\"info-item\"><span class=\"k\">branch.md</span>"
        f"<span class=\"v mono\">{_esc(model.branch_file)}</span></div>"
        f"<div class=\"info-item\"><span class=\"k\">报告生成时间</span>"
        f"<span class=\"v\">{_esc(format_report_time(model.generated_at))}</span></div>"
        "</div>"
        f"{_render_scan_sources(model.scan_sources)}"
        "<h3>统计信息</h3>"
        "<p class=\"panel-note\">"
        "扫描漏斗（扫描提交数 / Bug Fix / 未评估）为提交个数；"
        "结论类为评估次数（提交×目标），并附带涉及提交数。"
        "</p>"
        "<div class=\"table-wrap\"><table><thead><tr>"
        "<th>指标</th><th>数值</th><th>说明</th>"
        "</tr></thead><tbody>"
        f"<tr><td>扫描提交数</td><td>{stats.commits_scanned}</td>"
        "<td>时间窗内扫描到的全部提交，详情区均列出。</td></tr>"
        f"<tr><td>Bug Fix</td><td>{stats.bug_fix_count}</td>"
        "<td>识别为缺陷修复并进入跨分支同步评估的提交数。</td></tr>"
        f"{pending_row}"
        f"<tr><td>未做同步评估</td><td>{stats.not_included_count}</td>"
        "<td>已列入详情但判定为非 Bug Fix，不做跨分支同步评估。</td></tr>"
        f"<tr><td>需要同步</td><td>{need_dual}</td>"
        "<td>NeedSync 评估次数；涉及提交 = 至少有一条 NeedSync 目标结论的提交数。</td></tr>"
        f"<tr><td>已包含</td><td>{included_dual}</td>"
        "<td>AlreadyIncluded 评估次数（相同 SHA / cherry-pick / 单号 / 等价补丁等）。</td></tr>"
        f"<tr><td>人工确认</td><td>{review_dual}</td>"
        "<td>ManualReview 评估次数；证据不足或相似度灰区。</td></tr>"
        f"<tr><td>不在范围</td><td>{scope_dual}</td>"
        "<td>OutOfScope 评估次数（生命周期 / 分支类型 / 缺锚点文件等）。</td></tr>"
        "</tbody></table></div>"
        "<h3>风险汇总</h3>"
        f"<p>需要同步：{need_dual} · 人工确认：{review_dual} · 不在范围：{scope_dual}。</p>"
        "<h3>可信度说明</h3>"
        "<div class=\"callout\">"
        "<p><strong>结论判定顺序：</strong>"
        "① 目标分支是否已含相同 commit SHA → "
        "② cherry-pick 标记 / 相同单号 / 等价 patch-id → "
        "③ 文件改动相似度（≥ 高阈值视为已包含；灰区人工确认）→ "
        "④ 锚定文件/符号与修复是否缺失 → NeedSync / ManualReview / OutOfScope。</p>"
        "<p><strong>可信度：</strong>"
        "高 = 机器标记/单号/相同 SHA/明确 cherry-pick 等强证据；"
        "中 = 符号锚定或相似度灰区等部分证据；"
        "低 = 主要依赖启发式或修复缺失依据不充分。"
        "相似度高阈值默认 0.90，灰区默认 0.50～0.90（可在配置中调整）。</p>"
        "</div>"
    )


def _render_doc_bugs(model: ReportModel) -> str:
    parts: list[str] = []
    if model.document_bugs:
        rows = []
        for bug in model.document_bugs:
            rows.append(
                "<tr>"
                f"<td class=\"mono\">{_esc(bug.name)}</td>"
                f"<td>{bug.count}</td>"
                f"<td>{_esc(', '.join(bug.sections))}</td>"
                "</tr>"
            )
        parts.append(
            "<div class=\"table-wrap\"><table><thead><tr>"
            "<th>分支名</th><th>出现次数</th><th>所在产品线</th>"
            "</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )
    else:
        parts.append("<div class=\"empty-state\">未发现同名分支重复。</div>")

    if model.unknown_branches:
        items = "".join(f"<li class=\"mono\">{_esc(name)}</li>" for name in model.unknown_branches)
        parts.append(f"<h3>未知类型 / 缺失分支</h3><ul class=\"compact\">{items}</ul>")
    return "".join(parts)


def _kind_badge(kind: ConclusionKind, *, count: int | None = None) -> str:
    css = _KIND_BADGE_CLASS.get(kind, "badge-skip")
    label = _kind_label(kind)
    if count is not None:
        label = f"{label} ×{count}"
    return (
        f"<span class=\"badge {css}\" title=\"{_esc(kind)}\">"
        f"{_esc(label)}</span>"
    )


def _conclusion_kind_counts(
    commit: CommitReport,
) -> dict[ConclusionKind, int]:
    counts: dict[ConclusionKind, int] = {
        "NeedSync": 0,
        "ManualReview": 0,
        "AlreadyIncluded": 0,
        "OutOfScope": 0,
    }
    for row in commit.target_rows:
        kind = row.conclusion.kind
        if kind in counts:
            counts[kind] += 1
    return counts


def _commit_conclusion_badges(commit: CommitReport) -> str:
    """Summary chips for conclusion kinds present on this commit (pair counts)."""
    counts = _conclusion_kind_counts(commit)
    parts: list[str] = []
    for kind in ("NeedSync", "ManualReview", "AlreadyIncluded", "OutOfScope"):
        n = counts[kind]
        if n > 0:
            parts.append(_kind_badge(kind, count=n))
    return "".join(parts)


def _render_target_table(rows: list[TargetConclusionRow]) -> str:
    if not rows:
        return "<div class=\"empty-state\">无目标分支结论。</div>"
    body_rows = []
    for row in rows:
        kind = row.conclusion.kind
        evidence_html = "<br>".join(_esc(line) for line in row.conclusion.evidence)
        # cherry_pick_hint is an action recipe, not conclusion evidence — shown only
        # in the by-target「合入」modal via data-cherry, not in this「依据」column.
        body_rows.append(
            "<tr>"
            f"<td class=\"mono\">{_esc(row.target_branch)}</td>"
            f"<td>{_esc(_lifecycle_label(row.lifecycle))}</td>"
            f"<td>{_kind_badge(kind)}</td>"
            f"<td>{_esc(_confidence_label(row.conclusion.confidence))}</td>"
            f"<td class=\"evidence\">{evidence_html}</td>"
            "</tr>"
        )
    return (
        "<div class=\"table-wrap\"><table><thead><tr>"
        "<th>目标分支</th><th>生命周期</th><th>结论</th><th>可信度</th><th>依据</th>"
        "</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table></div>"
    )


def _commit_role(commit: CommitReport) -> tuple[str, str, str, str]:
    """Return (role_key, card_css, sync_role_label, badge_html)."""
    if commit.needs_agent or commit.recognition_source == "pending:claude-agent":
        return (
            "pending",
            "is-pending",
            "待 Claude Agent 判定",
            "<span class=\"badge badge-pending\">待 Claude Agent 判定</span>",
        )
    if commit.is_bug_fix:
        counts = _conclusion_kind_counts(commit)
        if counts["NeedSync"] > 0:
            card = "is-need"
        elif counts["ManualReview"] > 0:
            card = "is-review"
        else:
            card = "is-bug"
        # Header: Bug Fix + per-kind summary chips matching stats semantics.
        badges = ["<span class=\"badge badge-ok\">Bug Fix</span>"]
        conclusion_badges = _commit_conclusion_badges(commit)
        if conclusion_badges:
            badges.append(f"<span class=\"badge-group\">{conclusion_badges}</span>")
        else:
            badges.append("<span class=\"badge badge-skip\">无目标结论</span>")
        return (
            "bugfix",
            card,
            "Bug Fix（已做同步评估）",
            "".join(badges),
        )
    return (
        "skip",
        "is-skip",
        "未做跨分支同步评估",
        "<span class=\"badge badge-skip\">未评估</span>",
    )


def _render_commits(commits: list[CommitReport]) -> str:
    if not commits:
        return "<div class=\"empty-state\">本次运行未扫描到提交。</div>"

    count_all = len(commits)
    count_bug = 0
    count_skip = 0
    count_pending = 0
    count_kinds: dict[str, int] = {
        "NeedSync": 0,
        "ManualReview": 0,
        "AlreadyIncluded": 0,
        "OutOfScope": 0,
    }
    for commit in commits:
        role, _, _, _ = _commit_role(commit)
        if role == "bugfix":
            count_bug += 1
            kind_counts = _conclusion_kind_counts(commit)
            for kind, n in kind_counts.items():
                if n > 0:
                    count_kinds[kind] += 1
        elif role == "pending":
            count_pending += 1
        else:
            count_skip += 1

    filter_btns = [
        f"<button type=\"button\" class=\"filter-btn active\" data-filter=\"all\">全部（{count_all}）</button>",
        f"<button type=\"button\" class=\"filter-btn\" data-filter=\"bugfix\">Bug Fix（{count_bug}）</button>",
        (
            f"<button type=\"button\" class=\"filter-btn\" data-filter=\"NeedSync\">"
            f"需要同步（{count_kinds['NeedSync']}）</button>"
        ),
        (
            f"<button type=\"button\" class=\"filter-btn\" data-filter=\"ManualReview\">"
            f"人工确认（{count_kinds['ManualReview']}）</button>"
        ),
        (
            f"<button type=\"button\" class=\"filter-btn\" data-filter=\"AlreadyIncluded\">"
            f"已包含（{count_kinds['AlreadyIncluded']}）</button>"
        ),
        (
            f"<button type=\"button\" class=\"filter-btn\" data-filter=\"OutOfScope\">"
            f"不在范围（{count_kinds['OutOfScope']}）</button>"
        ),
        f"<button type=\"button\" class=\"filter-btn\" data-filter=\"skip\">未评估（{count_skip}）</button>",
    ]
    if count_pending > 0:
        filter_btns.append(
            f"<button type=\"button\" class=\"filter-btn\" data-filter=\"pending\">"
            f"待 Agent（{count_pending}）</button>"
        )

    toolbar = (
        "<div class=\"toolbar\" role=\"toolbar\" aria-label=\"提交筛选\">"
        "<span class=\"tb-label\">筛选</span>"
        f"{''.join(filter_btns)}"
        "<input class=\"search-box\" id=\"bma-commit-search\" type=\"search\" "
        "placeholder=\"搜索 SHA / 说明 / 分支 / 文件…\" />"
        "</div>"
        "<div class=\"legend\">"
        "<span class=\"badge badge-need\">需要同步</span>"
        "<span class=\"badge badge-review\">人工确认</span>"
        "<span class=\"badge badge-included\">已包含</span>"
        "<span class=\"badge badge-ok\">Bug Fix</span>"
        "<span class=\"badge badge-scope\">不在范围</span>"
        "<span class=\"legend-note\">"
        "同一提交可同时含多种结论；筛选按标签命中（多标签提交会出现在多个筛选中）；"
        "标题旁为结论汇总（与统计同口径）"
        "</span>"
        "</div>"
    )

    sections: list[str] = [toolbar]
    for commit in commits:
        files = ", ".join(commit.changed_files) if commit.changed_files else "—"
        issues = ", ".join(commit.issue_ids) if commit.issue_ids else "—"
        role, card_css, sync_role, badge = _commit_role(commit)
        kind_counts = _conclusion_kind_counts(commit)
        kind_tokens = " ".join(kind for kind, n in kind_counts.items() if n > 0)
        first_line = commit.message.splitlines()[0] if commit.message.strip() else "(empty)"
        search_blob = " ".join(
            [
                commit.sha,
                first_line,
                commit.author,
                commit.source_branch,
                commit.homologous_section,
                files,
                issues,
                commit.recognition_source,
                sync_role,
                kind_tokens,
                " ".join(_kind_label(k) for k, n in kind_counts.items() if n > 0),
            ]
        )

        if commit.is_bug_fix:
            sync_body = (
                "<h4>目标分支结论</h4>"
                f"{_render_target_table(commit.target_rows)}"
            )
        elif commit.needs_agent or commit.recognition_source == "pending:claude-agent":
            reason = commit.judge_reason or commit.skip_sync_reason or "交由 Claude 主 Agent 判定。"
            sync_body = (
                "<h4>目标分支结论</h4>"
                "<div class=\"callout pending\">无机器可读修复标记，请 Claude 主 Agent 阅读本提交 diff 后判定是否 Bug Fix。"
                f"说明：{_esc(reason)}</div>"
            )
        else:
            reason = commit.skip_sync_reason or "未识别为 Bug Fix。"
            sync_body = (
                "<h4>目标分支结论</h4>"
                "<div class=\"callout\">本提交列入时间窗明细，但未做跨分支同步评估。"
                f"原因：{_esc(reason)}</div>"
            )

        judge_row = ""
        if commit.judge_reason:
            judge_row = f"<tr><td>判定说明</td><td>{_esc(commit.judge_reason)}</td></tr>"

        sections.append(
            f"<article class=\"commit-card {card_css}\" id=\"commit-{_esc(commit.sha[:12])}\" "
            f"data-role=\"{role}\" data-kinds=\"{_esc(kind_tokens)}\" "
            f"data-search=\"{_esc(search_blob)}\">"
            "<div class=\"commit-head\">"
            f"<h3><span class=\"sha\">{_esc(commit.sha[:12])}</span>{_esc(first_line)}</h3>"
            f"<div class=\"commit-badges\">{badge}</div>"
            "</div>"
            "<div class=\"commit-body\">"
            "<div class=\"table-wrap\"><table class=\"kv-table\"><tbody>"
            f"<tr><td>作者</td><td>{_esc(commit.author)}</td></tr>"
            f"<tr><td>提交时间</td><td>{_esc(commit.committed_at)}</td></tr>"
            f"<tr><td>同步评估</td><td>{_esc(sync_role)}</td></tr>"
            f"<tr><td>识别来源</td><td class=\"mono\">{_esc(commit.recognition_source)}</td></tr>"
            f"{judge_row}"
            f"<tr><td>单号</td><td>{_esc(issues)}</td></tr>"
            f"<tr><td>源分支</td><td class=\"mono\">{_esc(commit.source_branch)}</td></tr>"
            f"<tr><td>产品线</td><td>{_esc(commit.homologous_section)}</td></tr>"
            f"<tr><td>关联文件</td><td class=\"mono\">{_esc(files)}</td></tr>"
            "</tbody></table></div>"
            f"{sync_body}"
            "</div></article>"
        )
    return "".join(sections)


def _render_by_target_row(item: TargetRollupItem, *, kind_badge: str) -> str:
    first_line = item.message.splitlines()[0] if item.message.strip() else "(empty)"
    cherry = item.cherry_pick_hint or (
        f"git checkout {item.target_branch} && git cherry-pick {item.sha[:12]}"
    )
    row_id = f"merge-{_esc(item.target_branch)}-{_esc(item.sha[:12])}"
    return (
        "<tr>"
        f"<td>{kind_badge}</td>"
        f"<td class=\"mono\"><a href=\"#commit-{_esc(item.sha[:12])}\">{_esc(item.sha[:12])}</a></td>"
        f"<td class=\"mono\">{_esc(item.source_branch)}</td>"
        f"<td>{_esc(first_line)}</td>"
        f"<td>{_esc(_confidence_label(item.confidence))}</td>"
        f"<td class=\"col-action\">"
        f"<button type=\"button\" class=\"btn-merge\" data-bma-merge=\"1\" "
        f"data-row-id=\"{row_id}\" "
        f"data-sha=\"{_esc(item.sha)}\" "
        f"data-sha-short=\"{_esc(item.sha[:12])}\" "
        f"data-target=\"{_esc(item.target_branch)}\" "
        f"data-source=\"{_esc(item.source_branch)}\" "
        f"data-author=\"{_esc(item.author)}\" "
        f"data-committed-at=\"{_esc(item.committed_at)}\" "
        f"data-kind=\"{_esc(_kind_label(item.conclusion_kind) if item.conclusion_kind else '')}\" "
        f"data-confidence=\"{_esc(_confidence_label(item.confidence))}\" "
        f"data-message=\"{_esc(item.message)}\" "
        f"data-cherry=\"{_esc(cherry)}\">合入</button>"
        "</td>"
        "</tr>"
    )


def _render_by_target(rollups: list[TargetRollup]) -> str:
    if not rollups:
        return (
            "<div class=\"empty-state\">"
            "按目标分支汇总：暂无「需要同步」或「人工确认」条目。"
            "</div>"
        )
    sections = []
    for rollup in rollups:
        rows = []
        for item in rollup.need_sync_items:
            rows.append(
                _render_by_target_row(
                    item,
                    kind_badge=_kind_badge("NeedSync"),
                )
            )
        for item in rollup.manual_review_items:
            rows.append(
                _render_by_target_row(
                    item,
                    kind_badge=_kind_badge("ManualReview"),
                )
            )
        sections.append(
            f"<article class=\"target-card\" id=\"target-{_esc(rollup.target_branch)}\">"
            f"<h3>{_esc(rollup.target_branch)} "
            f"<span class=\"meta\">（{_esc(_lifecycle_label(rollup.lifecycle))}）</span></h3>"
            "<div class=\"table-wrap\"><table><thead><tr>"
            "<th>结论</th><th>提交</th><th>源分支</th><th>说明</th><th>可信度</th>"
            "<th>操作</th>"
            "</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
            "</article>"
        )
    return "".join(sections)


def _load_template(template_path: Path | None) -> str:
    path = template_path or DEFAULT_TEMPLATE
    return path.read_text(encoding="utf-8")


def render_report(model: ReportModel, template_path: Path | None = None) -> str:
    template = _load_template(template_path)
    replacements = {
        "{{REPO_ID}}": _esc(model.repo_id),
        "{{GENERATED_AT}}": _esc(format_report_time(model.generated_at)),
        "{{EVAL_TIME_RANGE}}": _esc(
            model.eval_time_range or format_eval_time_range(None, None)
        ),
        "{{HOME_BODY}}": _render_home(model),
        "{{DOC_BUGS_BODY}}": _render_doc_bugs(model),
        "{{COMMITS_BODY}}": _render_commits(model.commits),
        "{{BY_TARGET_BODY}}": _render_by_target(model.by_target),
    }
    html = template
    for key, value in replacements.items():
        html = html.replace(key, value)
    return html


def write_report(html: str, output_path: Path) -> Path:
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path
