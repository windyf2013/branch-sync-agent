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


def enrich_steps(steps: list[dict], branch: dict | None) -> None:
    """把投影里对应 commit 的处理产物挂到步骤的 ``detail`` 上（就地）。"""
    if not branch:
        return
    by_sha = {
        cr.get("sha"): cr for cr in branch.get("commits") or [] if cr.get("sha")
    }
    for s in steps:
        if s.get("running") or not s.get("sha"):
            continue
        cr = by_sha.get(s["sha"])
        if cr is None:
            continue
        node = s["node"]
        detail: dict = {}
        if node in ("cherry_pick", "resolve_conflict"):
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
            o = ((cr.get("build") or {}).get(s.get("model")) if cr.get("build")
                 and s.get("model") else None)
            if o is None and node == "baseline_build":
                o = ((branch.get("baseline") or {}).get(s.get("model"))
                     if branch.get("baseline") else None)
            if o and o.get("status") in ("FAILED", "SKIPPED"):
                _attach(detail,
                        title="编译失败" if o.get("status") == "FAILED" else "编译跳过",
                        reason=o.get("reason"))
                if o.get("errors"):
                    _attach(detail, lines=list(o["errors"])[:5])
                if o.get("fix_diff"):
                    _attach(detail, diff=o["fix_diff"],
                            diff_caption=f"智能体修复 diff（{o.get('agent_attempts') or 0} 轮）")
                if o.get("log_preview"):
                    _attach(detail, log=o["log_preview"],
                            log_truncated=o.get("log_truncated", False))
        if detail:
            s["detail"] = detail
