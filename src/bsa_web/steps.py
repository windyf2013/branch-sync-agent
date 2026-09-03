"""把投影 commit 记录富化成步骤详情附件，供任务详情页与周期概览页共用。

投影 commit 记录 → 按 node 分派可展示的「错误/原因/日志/diff」。build/fix_build
的每次尝试都覆盖投影同一 build[model]（引擎只存终态），无法从投影还原
「失败→修复→通过」的过程——那由 progress.jsonl 逐次 start/end 忠实记录。
本模块把投影里的失败归因与处理产物挂到对应的最后一次尝试步骤上供展开。
"""
from __future__ import annotations


def _attach(
    detail: dict, *, title: str = None, reason: str = None, lines: list | None = None,
    log: str = None, log_truncated: bool = False, diff: str = None,
    diff_caption: str = None,
) -> dict:
    """就地累积 detail 附件字段；重复调用追加（build 每次失败都挂一块）。"""
    if title and not detail.get("title"):
        detail["title"] = title
    if reason and not detail.get("reason"):
        detail["reason"] = reason
    if lines:
        detail.setdefault("lines", []).extend(lines)
    if log:
        detail.setdefault("log", log)
        detail["log_truncated"] = log_truncated
    if diff:
        detail.setdefault("diff", diff)
        detail.setdefault("diff_caption", diff_caption or "智能体 diff")
    return detail


def enrich_steps(
    steps: list[dict],
    branch: dict | None,
    *,
    cycle_id: str | None = None,
) -> None:
    """把投影里对应 commit / 基线编译的处理产物挂到步骤的 ``detail`` 上（就地）。

    ``branch`` 提供该目标分支的投影（commits + baseline）；``cycle_id`` 可选，
    提供时给失败步骤附完整日志下载链接（/cycle/.../log）。基线编译步骤无 sha
    （节点级，非 commit 级），按 ``node == "baseline_build"`` 从
    ``branch.baseline[model]`` 取结果富化，否则其失败会只剩一个红徽章而看不到
    原因与日志（周期概览/任务详情时间线都能展开）。
    """
    if not branch:
        return
    by_sha = {
        cr.get("sha"): cr for cr in branch.get("commits") or [] if cr.get("sha")
    }
    baseline = branch.get("baseline") or {}
    target_branch = branch.get("target_branch")
    for s in steps:
        if s.get("running"):
            continue
        node = s["node"]
        sha = s.get("sha")
        model = s.get("model")
        cr = by_sha.get(sha) if sha else None
        detail: dict = {}
        if node in ("cherry_pick", "resolve_conflict"):
            if cr is None:
                continue
            if cr.get("resolution_error"):
                _attach(detail, reason=cr["resolution_error"])
            if node == "resolve_conflict":
                res = cr.get("conflict_resolution")
                if res:
                    if res.get("files"):
                        _attach(detail, lines=[f"冲突文件：{', '.join(res['files'])}"])
                    if res.get("agent_reason"):
                        _attach(detail, lines=[f"智能体说明：{res['agent_reason']}"])
                    if res.get("diff"):
                        _attach(detail, diff=res["diff"], diff_caption="冲突解决 diff")
        elif node in ("build", "fix_build", "baseline_build"):
            if node == "baseline_build":
                if cr is not None:
                    continue  # 防御：基线步骤不应带 commit
                o = (baseline.get(model) if model else None)
            else:
                if cr is None:
                    continue
                o = ((cr.get("build") or {}).get(model)
                     if cr.get("build") and model else None)
            if o is None or o.get("status") not in ("FAILED", "SKIPPED"):
                continue
            _attach_outcome_failure(
                detail, o, node=node, model=model, sha=sha,
                cycle_id=cycle_id, target_branch=target_branch,
            )
        if detail:
            s["detail"] = detail


def _attach_outcome_failure(
    detail: dict,
    o: dict,
    *,
    node: str,
    model: str | None,
    sha: str | None,
    cycle_id: str | None,
    target_branch: str | None,
) -> None:
    """把一次失败/跳过的 build outcome 富化到步骤 detail：标题/原因/错误定位/日志。

    错误定位优先用 ``enrich_build_outcomes``（build_labels）从日志尾部提炼的
    ``error_lines``；缺失时退回投影原始 ``errors``（截断到 5 条、每条 300 字符，
    引擎的 errors 可能是整段编译输出的大块文本）。日志预览在 FAILED 时由
    build_labels 读好挂到 outcome；此处一并转进 detail 供 _steps.html 折叠。
    """
    _attach(
        detail,
        title="编译失败" if o.get("status") == "FAILED" else "编译跳过",
        reason=(o.get("reason") or "").strip() or None,
    )
    error_lines = list(o.get("error_lines") or [])
    if not error_lines:
        error_lines = [(str(e)[:300]) for e in (o.get("errors") or [])[:5]]
    if error_lines:
        _attach(detail, lines=error_lines)
    if o.get("fix_diff"):
        _attach(detail, diff=o["fix_diff"],
                diff_caption=f"智能体修复 diff（{o.get('agent_attempts') or 0} 轮）")
    if o.get("log_preview"):
        _attach(detail, log=o["log_preview"],
                log_truncated=o.get("log_truncated", False))
    if cycle_id and target_branch and o.get("log_path"):
        if node == "baseline_build" and model:
            detail["log_href"] = (
                f"/cycle/{cycle_id}/target/{target_branch}/baseline/{model}/log"
            )
        elif node in ("build", "fix_build") and sha and model:
            detail["log_href"] = (
                f"/cycle/{cycle_id}/target/{target_branch}/commit/{sha}/build/{model}/log"
            )
