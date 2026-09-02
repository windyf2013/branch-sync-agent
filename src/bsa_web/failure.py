"""失败归因提取：从投影 payload 提炼人类可读的中文失败原因。

纯函数、无 I/O（不 subprocess、不读库），被视图层（task_detail / workbench /
history / detail）与 executor 终态富化共用，可单测直接 import。

数据形状依据（见 logs/*/state.json 实测）：
- ``action_required`` 元素两种形状：
  - 节点失败 ``{"node": "...", "error": "..."}``
  - 人工项 ``{"sha", "branch", "kind": "ManualReview", "evidence": [...], ...}``
    注意：真实数据**没有 ``reason`` 字段**，只有 ``evidence``（列表）。
- ``branch_results[target]`` 含 ``status`` / ``stop_reason`` / ``commits[]``；
  每个 commit 含 ``cherry_pick`` / ``resolution_error`` / ``build``。
"""

from __future__ import annotations

# build 失败时引擎若无法归因，会写这个占位 reason；此时不应当作归因展示，
# 具体错误由日志定位（build_labels.extract_build_errors）另行提取。
_UNATTRIBUTABLE_BUILD_REASON = "LLM 不可用，无法归因"


def failure_summary(payload: dict, target: str | None = None) -> list[str]:
    """从投影 payload 提取失败归因摘要（中文，去重保序）。

    优先级（target 为 None 时聚合全部分支，否则只看该分支）：
    1. branch_results[target].stop_reason
    2. action_required 节点失败的 node:error
    3. action_required ManualReview 的 evidence（兼容 reason 兜底）
    4. commit 级 resolution_error（冲突解决失败）
    5. build outcome 的 o.reason / 编译失败占位标记

    返回空列表 = 无失败证据（成功或数据缺失）。
    """
    reasons: list[str] = []
    branches = payload.get("branch_results") or {}

    # 1. 分支级 stop_reason
    if target is not None:
        branch = branches.get(target)
        if branch:
            _append_if_text(reasons, branch.get("stop_reason"))
    else:
        for b in branches.values():
            if b.get("stop_reason"):
                reasons.append(f"{b.get('target_branch')}: {b['stop_reason']}")

    # 2/3. action_required 节点失败 + ManualReview
    for item in payload.get("action_required") or []:
        if item.get("node"):
            # 节点失败（周期级，无 branch）：影响所有分支，不受 target 过滤
            reasons.append(f"{item['node']} 节点错误：{item.get('error') or '未知'}")
        elif item.get("kind") == "ManualReview":
            if target is not None and item.get("branch") != target:
                continue
            _append_if_text(reasons, _manual_review_reason(item))

    # 4/5. commit 级 resolution_error + build 失败
    for branch_name, branch in branches.items():
        if target is not None and branch_name != target:
            continue
        for cr in branch.get("commits") or []:
            short = (cr.get("sha") or "")[:8]
            resolution_error = cr.get("resolution_error")
            if resolution_error:
                reasons.append(f"{short} 冲突解决失败：{resolution_error}")
                continue
            for model, outcome in (cr.get("build") or {}).items():
                if outcome.get("status") != "FAILED":
                    continue
                reason = (outcome.get("reason") or "").strip()
                if reason and reason != _UNATTRIBUTABLE_BUILD_REASON:
                    reasons.append(f"{short} {model} 编译失败：{reason}")
                else:
                    reasons.append(f"{short} {model} 编译失败")

    # 去重保序
    seen: set[str] = set()
    deduped: list[str] = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            deduped.append(r)
    return deduped


def failure_text(payload: dict, target: str | None = None) -> str:
    """``failure_summary`` 用「；」连接成单字符串，供写回 ``tasks.error``。"""
    return "；".join(failure_summary(payload, target))


def decision_breakdown(payload: dict) -> dict[str, int]:
    """周期级四态判定计数（按 commit × 目标分支）。

    供周期概览解释「检测 N · 同步 0 · SUCCESS」的矛盾：检测到的 commit
    可能全部落入 AlreadyIncluded / OutOfScope，此时同步数为 0 是合理的。
    """
    counts = {
        "NeedSync": 0,
        "AlreadyIncluded": 0,
        "OutOfScope": 0,
        "ManualReview": 0,
    }
    decisions = payload.get("decisions") or {}
    for per_target in decisions.values():
        for decision in (per_target or {}).values():
            kind = decision.get("kind")
            if kind in counts:
                counts[kind] += 1
    return counts


def commit_destinations(payload: dict) -> dict[str, int]:
    """周期级「检测 commit 去向」守恒归类（按 distinct commit）。

    每个检测到的 commit 归入且仅归入一个去向桶，故恒有
    ``detected == synced + skipped + review + unhandled``。优先级（先命中先归桶）：

    1. synced   —— 任一目标分支 cherry_pick OK/EMPTY（已实际应用）；
    2. skipped  —— 未同步，且所有判定 kind ⊆ {AlreadyIncluded, OutOfScope}；
    3. review   —— 未同步，且存在 ManualReview 判定或 action_required 人工项；
    4. unhandled—— 其余（检测到但无下落：NeedSync 未落地 / 无判定记录）。

    这是对 ``cycle_summary`` 只给「检测/同步/跳过」三个数、账对不上的补全——
    「未处理」桶正是失败无归因的缺口暴露点，历史页与周期概览共用。
    """
    decisions = payload.get("decisions") or {}
    branches = payload.get("branch_results") or {}

    synced_shas: set[str] = set()
    for branch in branches.values():
        for cr in branch.get("commits") or []:
            if cr.get("sha") and cr.get("cherry_pick") in ("OK", "EMPTY"):
                synced_shas.add(cr["sha"])

    review_shas = {
        item.get("sha")
        for item in (payload.get("action_required") or [])
        if item.get("kind") == "ManualReview" and item.get("sha")
    }

    counts = {"detected": 0, "synced": 0, "skipped": 0, "review": 0, "unhandled": 0}
    for c in payload.get("detected_commits") or []:
        sha = c.get("sha")
        if not sha:
            continue
        counts["detected"] += 1
        if sha in synced_shas:
            counts["synced"] += 1
            continue
        kinds = {(d.get("kind") or "") for d in (decisions.get(sha) or {}).values()}
        if kinds and kinds <= {"AlreadyIncluded", "OutOfScope"}:
            counts["skipped"] += 1
            continue
        if "ManualReview" in kinds or sha in review_shas:
            counts["review"] += 1
            continue
        counts["unhandled"] += 1
    return counts


def _manual_review_reason(item: dict) -> str | None:
    """ManualReview 项的归因：优先 ``reason``（若引擎某处写了），否则取 ``evidence`` 首条。"""
    reason = item.get("reason")
    if reason:
        return reason
    evidence = item.get("evidence") or []
    return evidence[0] if evidence else None


def _append_if_text(reasons: list[str], value: object) -> None:
    """value 非空则追加（strip 后仍非空）。"""
    if isinstance(value, str) and value.strip():
        reasons.append(value.strip())
