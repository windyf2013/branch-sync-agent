from __future__ import annotations

import re
from pathlib import Path

from branch_maintenance.branch_md import DocBug
from branch_maintenance.conclude import Conclusion
from branch_maintenance.html_report import (
    ReportModel,
    ReportStats,
    ScanSourceInfo,
    TargetConclusionRow,
    TargetRollup,
    TargetRollupItem,
    build_report_model,
    default_report_path,
    render_report,
    write_report,
)
from branch_maintenance.html_report import CommitReport


def sample_model(*, out_of_scope_pairs: int = 0) -> ReportModel:
    target_rows = [
        TargetConclusionRow(
            target_branch="br_v4_LineA_release_20260101",
            lifecycle="active",
            conclusion=Conclusion(
                kind="NeedSync",
                evidence=["Target branch lacks the source fix with clear file/function anchors."],
                confidence="high",
            ),
            cherry_pick_hint="git cherry-pick abc1234",
        ),
        TargetConclusionRow(
            target_branch="br_v4_LineA_develop_b_20260101",
            lifecycle="active",
            conclusion=Conclusion(
                kind="ManualReview",
                evidence=["Similarity 0.65 is in gray zone between 0.50 and 0.90."],
                confidence="medium",
            ),
        ),
        TargetConclusionRow(
            target_branch="br_v4_LineA_feature_x_20260101",
            lifecycle="active",
            conclusion=Conclusion(
                kind="AlreadyIncluded",
                evidence=["Target branch has commit with same issue id CQ-12345."],
                confidence="high",
            ),
        ),
    ]
    for _ in range(out_of_scope_pairs):
        target_rows.append(
            TargetConclusionRow(
                target_branch="br_v4_LineA_personal_dev_20260101",
                lifecycle="active",
                conclusion=Conclusion(
                    kind="OutOfScope",
                    evidence=["Target branch type personal is not a Need Sync target."],
                    confidence="high",
                ),
            )
        )

    stats = ReportStats(
        commits_scanned=1,
        bug_fix_count=1,
        heuristic_count=0,
        not_included_count=0,
        need_sync_count=1,
        already_included_count=1,
        manual_review_count=1,
        out_of_scope_count=out_of_scope_pairs,
        need_sync_commits=1,
        already_included_commits=1,
        manual_review_commits=1,
        out_of_scope_commits=1 if out_of_scope_pairs else 0,
        target_debt_total=2,
    )

    commit = CommitReport(
        sha="abc1234567890abcdef1234567890abcdef1234",
        author="dev@example.com",
        committed_at="2026-07-29T12:00:00Z",
        message="[BUG] CQ-12345 fix null deref in dhcp",
        recognition_source="machine:[BUG]",
        changed_files=["plat/dhcp/dhcp_core.c"],
        symbols=["dhcp_validate_pool"],
        source_branch="br_v4_LineA_develop_a_20260101",
        homologous_section="LineA",
        issue_ids=["CQ-12345"],
        target_rows=target_rows,
    )

    by_target = [
        TargetRollup(
            target_branch="br_v4_LineA_release_20260101",
            lifecycle="active",
            need_sync_items=[
                TargetRollupItem(
                    sha=commit.sha,
                    message=commit.message,
                    source_branch=commit.source_branch,
                    confidence="high",
                )
            ],
            manual_review_items=[],
        ),
        TargetRollup(
            target_branch="br_v4_LineA_develop_b_20260101",
            lifecycle="active",
            need_sync_items=[],
            manual_review_items=[
                TargetRollupItem(
                    sha=commit.sha,
                    message=commit.message,
                    source_branch=commit.source_branch,
                    confidence="medium",
                )
            ],
        ),
    ]

    return ReportModel(
        repo_id="demo-repo",
        repo_path="/tmp/demo-repo",
        generated_at="2026-07-29 21:00:00",
        branch_file=".cursor/scripts/branch.md",
        branch_md_parsed_at="2026-07-29 21:00:00",
        scan_sources=[
            ScanSourceInfo(
                branch="br_v4_LineA_develop_a_20260101",
                last_scan_ref="deadbeef",
                new_tip="abc12345",
                commits_scanned=3,
            )
        ],
        document_bugs=[
            DocBug(
                name="br_v4_dup_branch",
                count=2,
                sections=["LineA", "LineB"],
            )
        ],
        unknown_branches=["br_v4_unknown_branch"],
        stats=stats,
        commits=[commit],
        by_target=by_target,
        eval_time_range="2026-07-29 00:00:00 ~ 2026-07-31 23:59:59",
    )


def test_html_structure_markers():
    html = render_report(sample_model())
    assert 'id="bma-home"' in html
    assert 'id="bma-doc-bugs"' in html
    assert 'id="bma-commits"' in html
    assert 'id="bma-by-target"' in html
    assert 'id="bma-back-to-top"' in html
    assert "返回顶部" in html
    assert "ENTERPRISE DELIVERY" not in html
    assert "评估时间范围" in html
    assert "2026-07-29 00:00:00 ~ 2026-07-31 23:59:59" in html
    assert "报告生成时间" in html
    assert "2026-07-29 21:00:00" in html


def test_format_report_time_second_precision():
    from branch_maintenance.html_report import format_eval_time_range, format_report_time

    assert format_report_time("2026-07-29T00:00:00+08:00") == "2026-07-29 00:00:00"
    assert format_report_time("2026-07-31T23:59:59.123456+08:00") == "2026-07-31 23:59:59"
    assert (
        format_eval_time_range("2026-07-29T00:00:00+08:00", "2026-07-31T23:59:59+08:00")
        == "2026-07-29 00:00:00 ~ 2026-07-31 23:59:59"
    )


def test_home_section_titles():
    html = render_report(sample_model())
    assert "扫描信息" in html
    assert "风险汇总" in html
    assert "统计信息" in html
    assert "需要同步" in html
    assert "人工确认" in html
    assert "目标欠债" not in html
    assert "文档解析时间" not in html
    assert "次（涉及" in html
    assert "个提交" in html
    assert "评估次数" in html
    assert "实际扫描引用" not in html
    assert "扫描起点 SHA" in html
    assert "扫描终点 SHA" in html
    assert "扫描提交数" in html
    assert "不计入欠债" not in html
    assert ">3</td>" in html  # per-branch commits_scanned from sample_model


def test_non_bug_fix_commit_still_listed_in_details():
    model = sample_model()
    model.commits.append(
        CommitReport(
            sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            author="dev",
            committed_at="2026-07-29T13:00:00Z",
            message="build version 4.34.346",
            recognition_source="not-included",
            changed_files=["build/version"],
            symbols=[],
            source_branch="br_v4_LineA_develop_a_20260101",
            homologous_section="LineA",
            issue_ids=[],
            target_rows=[],
            is_bug_fix=False,
            skip_sync_reason="提交说明与补丁均未命中 Bug Fix 识别规则。",
        )
    )
    html = render_report(model)
    assert "build version 4.34.346" in html
    assert "未做跨分支同步评估" in html
    assert "扫描提交数" in html
    assert "时间窗内扫描到的全部提交" in html
    assert "关联符号" not in html
    assert "全部（" in html
    assert "可信度说明" in html
    assert "相同 commit SHA" in html
    assert 'id="bma-commit-search"' in html


def test_pending_agent_commit_rendered():
    model = sample_model()
    model.commits.append(
        CommitReport(
            sha="b3e31641cdcfabc0000000000000000000000000",
            author="dev",
            committed_at="2026-07-29T14:00:00Z",
            message="raisecom-equipment.yang add uuid",
            recognition_source="pending:claude-agent",
            changed_files=["a.c"],
            symbols=[],
            source_branch="br_v4_LineA_develop_a_20260101",
            homologous_section="LineA",
            issue_ids=[],
            target_rows=[],
            is_bug_fix=False,
            needs_agent=True,
            judge_reason="无机器可读修复标记；交由 Claude 主 Agent 阅读 diff 后判定。",
        )
    )
    html = render_report(model)
    assert "待 Claude Agent 判定" in html
    assert "raisecom-equipment.yang add uuid" in html


def test_document_bugs_rendered():
    html = render_report(sample_model())
    assert "br_v4_dup_branch" in html
    assert "LineA" in html
    assert "LineB" in html


def test_conclusion_kinds_rendered():
    html = render_report(sample_model())
    assert "需要同步" in html
    assert "已包含" in html
    assert "人工确认" in html
    assert "不在范围" in html
    assert 'class="badge badge-need"' in html
    assert 'class="badge badge-review"' in html
    assert 'class="badge badge-ok"' in html
    assert 'class="badge badge-scope"' in html
    assert 'data-filter="all"' in html
    assert 'data-filter="bugfix"' in html
    assert 'data-filter="NeedSync"' in html
    assert 'data-filter="ManualReview"' in html
    assert 'data-filter="AlreadyIncluded"' in html
    assert 'data-filter="OutOfScope"' in html
    assert 'data-filter="skip"' in html
    assert 'data-kinds="NeedSync ManualReview AlreadyIncluded"' in html
    assert 'class="badge badge-included"' in html
    assert "人工确认 ×" in html
    assert "目标分支结论" in html
    assert "待 Agent 判定" not in html


def test_out_of_scope_excluded_from_debt_totals():
    model = sample_model(out_of_scope_pairs=2)
    html = render_report(model)
    assert model.stats.target_debt_total == 2
    assert 'data-bma-debt-total="2"' in html
    assert model.stats.out_of_scope_count == 2
    assert 'data-bma-out-of-scope-count="2"' in html


def test_build_report_model_computes_debt_excluding_out_of_scope():
    model = build_report_model(
        repo_id="demo",
        repo_path="/tmp/demo",
        generated_at="2026-07-29T21:00:00Z",
        branch_file=".cursor/scripts/branch.md",
        branch_md_parsed_at="2026-07-29T21:00:00Z",
        scan_sources=[],
        document_bugs=[],
        unknown_branches=[],
        commits=[
            CommitReport(
                sha="sha1",
                author="a",
                committed_at="2026-07-29",
                message="fix",
                recognition_source="heuristic:files+functions",
                changed_files=["a.c"],
                symbols=[],
                source_branch="src",
                homologous_section="S",
                issue_ids=[],
                target_rows=[
                    TargetConclusionRow(
                        target_branch="t1",
                        lifecycle="active",
                        conclusion=Conclusion(kind="NeedSync", evidence=["e"], confidence="high"),
                    ),
                    TargetConclusionRow(
                        target_branch="t2",
                        lifecycle="active",
                        conclusion=Conclusion(kind="OutOfScope", evidence=["e"], confidence="high"),
                    ),
                    TargetConclusionRow(
                        target_branch="t3",
                        lifecycle="active",
                        conclusion=Conclusion(
                            kind="AlreadyIncluded",
                            evidence=["e"],
                            confidence="high",
                        ),
                    ),
                ],
            )
        ],
    )
    assert model.stats.need_sync_count == 1
    assert model.stats.out_of_scope_count == 1
    assert model.stats.already_included_count == 1
    assert model.stats.need_sync_commits == 1
    assert model.stats.out_of_scope_commits == 1
    assert model.stats.already_included_commits == 1
    assert model.stats.manual_review_commits == 0
    assert model.stats.target_debt_total == 1


def test_dual_metric_counts_distinct_commits_and_pairs():
    """One commit with 3 ManualReview targets → 3 pairs, 1 commit."""
    rows = [
        TargetConclusionRow(
            target_branch=f"t{i}",
            lifecycle="active",
            conclusion=Conclusion(kind="ManualReview", evidence=["e"], confidence="medium"),
        )
        for i in range(3)
    ]
    model = build_report_model(
        repo_id="demo",
        repo_path="/tmp/demo",
        generated_at="2026-07-29T21:00:00Z",
        branch_file=".cursor/scripts/branch.md",
        branch_md_parsed_at="2026-07-29T21:00:00Z",
        scan_sources=[],
        document_bugs=[],
        unknown_branches=[],
        commits=[
            CommitReport(
                sha="sha-multi",
                author="a",
                committed_at="2026-07-29",
                message="fix: multi target",
                recognition_source="machine:[BUG]",
                changed_files=["a.c"],
                symbols=[],
                source_branch="src",
                homologous_section="S",
                issue_ids=[],
                target_rows=rows,
                is_bug_fix=True,
            )
        ],
    )
    assert model.stats.manual_review_count == 3
    assert model.stats.manual_review_commits == 1
    html = render_report(model)
    assert "3 次（涉及 1 个提交）" in html
    assert "3 次 · 1 提交" in html


def test_by_target_rollup_lists_debt_items_only():
    html = render_report(sample_model())
    assert "br_v4_LineA_release_20260101" in html
    assert "br_v4_LineA_develop_b_20260101" in html
    assert "br_v4_LineA_personal_dev_20260101" not in html
    assert "是否合入" not in html
    assert "操作" in html
    assert "合入</button>" in html
    assert 'class="merge-check"' not in html
    assert "bma-merge-modal" in html


def test_cherry_pick_hint_optional():
    html = render_report(sample_model())
    # Action recipe lives on by-target merge control, not in「依据」.
    assert "data-cherry=" in html
    assert "git cherry-pick" in html
    assert "依据" in html
    # Evidence cell must not append the git recipe as if it were rationale.
    assert re.search(
        r'class="evidence">[^<]*git cherry-pick',
        html,
    ) is None


def test_html_escape_special_chars():
    model = sample_model()
    model.commits[0].message = 'fix <script>alert("x")</script> & "quotes"'
    html = render_report(model)
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
    assert "&amp;" in html


def test_write_report_creates_file(tmp_path: Path):
    html = render_report(sample_model())
    out = tmp_path / "reports" / "branch_maintenance_demo.html"
    written = write_report(html, out)
    assert written == out.resolve()
    assert out.read_text(encoding="utf-8") == html


def test_default_report_path():
    path = default_report_path("my_repo", "ai_reports")
    assert path.parent.as_posix().endswith("ai_reports")
    assert path.name.startswith("branch_maintenance_my_repo")
    assert path.suffix == ".html"
