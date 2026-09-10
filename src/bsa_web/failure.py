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

import re

# 旧版引擎写死的「LLM 不可用」占位 reason（无真实失败信息）：仅兼容旧记录，
# 命中时改由日志定位（build_labels.extract_build_errors）提取真实错误。新引擎
# 已让 reason 携带真实失败原因（「LLM 调用失败：…」），会作为正常归因展示。
_UNATTRIBUTABLE_BUILD_REASON = "LLM 不可用，无法归因"

# 节点名 → 中文标签（页面展示用）。与引擎 progress.py 的 NODE_LABELS 语义一致，
# 但这里覆盖更全（含 decide / branch_matrix 等只在错误路径出现的节点），
# 未知节点原样返回兜底，绝不吞掉信息。
_NODE_ZH: dict[str, str] = {
    "detect_commits": "代码迁出",
    "sync_decision": "同步判定",
    "decide": "同步判定",
    "branch_matrix": "分支拓扑解析",
    "prepare_worktree": "建立 worktree",
    "baseline_build": "基线编译",
    "cherry_pick": "cherry-pick",
    "resolve_conflict": "解决冲突",
    "build": "编译",
    "fix_build": "修复重编译",
    "generate_patch": "生成 patch",
    "report": "生成报告",
    "fail_fast": "失败关联判定",
    "next_branch": "切换目标分支",
    "next_commit": "切换提交",
}

# 引擎写死的英文 stop_reason → 自然中文。按 (正则, 格式化函数) 顺序匹配，
# 命中即替换；未命中原样透传（历史数据或未来新增文案不会因此丢失）。
_STOP_REASON_PATTERNS: list[tuple[re.Pattern, object]] = [
    (
        re.compile(r"^baseline build failed on (.+)$"),
        lambda m: f"型号 {m.group(1)} 基线编译失败",
    ),
    # 同步中断 ≠ 冲突：前者是引擎/工具链没能完成 cherry-pick（无需人工解代码），
    # 后者才需要人工裁决。措辞刻意避开「冲突」二字，避免误导响应方向。
    (
        re.compile(r"^cherry-pick failed on (\S+)$"),
        lambda m: f"提交 {m.group(1)} 同步中断（cherry-pick 未能完成）",
    ),
    (
        re.compile(r"^fail-fast: (\S+) failed; subsequent commits judged related$"),
        lambda m: f"{m.group(1)} 失败，后续关联提交已一并停批",
    ),
    (
        re.compile(r"^fail-fast: (\S+) failed$"),
        lambda m: f"{m.group(1)} 失败",
    ),
]


def node_label(node: str | None) -> str:
    """节点名 → 中文标签；未知节点原样返回（兜底不吞）。"""
    return _NODE_ZH.get(str(node or ""), node or "")


def humanize_stop_reason(text: str | None) -> str | None:
    """把引擎写死的英文 stop_reason 转成自然中文；未知内容原样透传。"""
    if not isinstance(text, str) or not text.strip():
        return text
    t = text.strip()
    for pattern, fmt in _STOP_REASON_PATTERNS:
        m = pattern.match(t)
        if m:
            return fmt(m)
    return t


def failure_summary(payload: dict, target: str | None = None) -> list[str]:
    """从投影 payload 提取执行失败归因摘要（中文，去重保序）。

    **不含 ManualReview 人工项**——那不是失败，是待人工裁决，由各页
    独立的「需人工处理 / 人工项」板块承载（周期概览、任务详情、工作台待办）。
    混进「失败原因」会让待确认 commit 被误读为周期执行失败。

    优先级（target 为 None 时聚合全部分支，否则只看该分支）：
    1. branch_results[target].stop_reason
    2. action_required 节点失败的 node:error（带 branch 的节点失败按分支过滤，
       周期级失败在 target=None 时始终透出）
    3. commit 级 reason（cherry-pick 未能完成，多为引擎/工具链问题）
    4. commit 级 resolution_error（冲突解决失败，需人工裁决）
    5. 周期级 errors 兜底（节点失败原文）
    6. build outcome 的 o.reason / 编译失败占位标记

    返回空列表 = 无执行失败（周期可能因 MANUAL/待人工而终态非 SUCCESS，
    此时由调用方据 action_required 给出「无执行失败」的旁注，见 detail.py）。
    """
    reasons: list[str] = []
    branches = payload.get("branch_results") or {}

    # 1. 分支级 stop_reason（引擎写死的英文 → 自然中文）
    if target is not None:
        branch = branches.get(target)
        if branch:
            _append_if_text(reasons, humanize_stop_reason(branch.get("stop_reason")))
    else:
        for b in branches.values():
            if b.get("stop_reason"):
                reasons.append(
                    f"{b.get('target_branch')}: {humanize_stop_reason(b['stop_reason'])}"
                )

    # 2. action_required 节点失败（无 ManualReview 分支）。
    # 节点失败分两种：
    # - 带 branch（如 prepare_worktree 绑定具体目标）：只在该分支展示，
    #   否则会把 A 分支的编译失败复制到每个成功分支的「失败原因」上；
    # - 无 branch（周期级，如 detect/branch_matrix 早退）：target 过滤时
    #   照常透出（target=None 的周期概览始终展示），由周期级失败承载。
    for item in payload.get("action_required") or []:
        if not item.get("node"):
            continue
        node_branch = item.get("branch")
        if node_branch and target is not None and node_branch != target:
            continue
        reason = f"{node_label(item['node'])} 节点错误：{item.get('error') or '未知'}"
        if node_branch and target is None:
            reason = f"{node_branch}: {reason}"
        reasons.append(reason)

    # 3. commit 级 cherry-pick 失败 / resolution_error + build 失败
    for branch_name, branch in branches.items():
        if target is not None and branch_name != target:
            continue
        for cr in branch.get("commits") or []:
            short = (cr.get("sha") or "")[:8]
            if cr.get("cherry_pick") == "FAILED":
                detail = _first_line(cr.get("reason"))
                reasons.append(
                    f"{short} 同步中断（cherry-pick 未能完成）：{detail}"
                    if detail
                    else f"{short} 同步中断（cherry-pick 未能完成，引擎未记录原因）"
                )
                continue
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

    # 4. 周期级 errors 兜底：即便上面的字段都缺（历史数据 / 未覆盖的节点），
    #    也不能让「失败原因」安静地空着 —— 空着会把人推向 commit message 之类的
    #    无关文本（cycle-2026-09-10 的误读正是这么发生的）。
    if not reasons:
        for node, rec in sorted((payload.get("errors") or {}).items()):
            message = _first_line((rec or {}).get("error"))
            if message:
                reasons.append(f"{node_label(node)} 节点错误：{message}")

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


def commit_bucket(payload: dict, sha: str) -> str:
    """单 commit 的去向桶（synced/skipped/review/unhandled），幂等、每个恰好一个。

    与 ``commit_destinations`` 同规则（它逐 commit 复用本函数），供 commit 表
    「判定结果」列展示：同步 / 跳过 / 待确认 / 未处理。优先级（先命中先归）：
    1. synced   —— 任一目标分支 cherry_pick OK/EMPTY（已实际应用）；
    2. skipped  —— 未同步，且所有判定 kind ⊆ {AlreadyIncluded, OutOfScope}；
    3. review   —— 未同步，且存在 ManualReview 判定或 action_required 人工项；
    4. unhandled—— 其余（检测到但无下落：NeedSync 未落地 / 无判定记录）。
    """
    branches = payload.get("branch_results") or {}
    for branch in branches.values():
        for cr in branch.get("commits") or []:
            if cr.get("sha") == sha and cr.get("cherry_pick") in ("OK", "EMPTY"):
                return "synced"
    decisions = payload.get("decisions") or {}
    kinds = {(d.get("kind") or "") for d in (decisions.get(sha) or {}).values()}
    if kinds and kinds <= {"AlreadyIncluded", "OutOfScope"}:
        return "skipped"
    if "ManualReview" in kinds:
        return "review"
    if any(
        item.get("kind") == "ManualReview" and item.get("sha") == sha
        for item in (payload.get("action_required") or [])
    ):
        return "review"
    return "unhandled"


def commit_rationale(payload: dict, sha: str) -> str:
    """单 commit 的判定说明（结果列的同源缘由），与 ``commit_bucket`` 对齐。

    取「决定该 commit 去向」的那条判定的 evidence 原文，缺失时给桶级兜底文案：
    - synced    —— 实际同步到的那条目标分支的 NeedSync 判定缘由（为什么需同步）；
    - skipped   —— AlreadyIncluded / OutOfScope 判定缘由；
    - review    —— ManualReview 判定缘由；
    - unhandled —— 判定待同步但未同步落地；无判定则提示排查归因。
    """
    decisions = (payload.get("decisions") or {}).get(sha) or {}
    bucket = commit_bucket(payload, sha)

    def _first_evidence_of(*kinds: str) -> str | None:
        for d in decisions.values():
            if not isinstance(d, dict) or d.get("kind") not in kinds:
                continue
            ev = _decision_evidence(d)
            if ev:
                return ev
        return None

    if bucket == "synced":
        # 精确到实际同步到的目标分支上的判定，拿不到再退回任意 NeedSync
        applied_targets = [
            branch.get("target_branch")
            for branch in (payload.get("branch_results") or {}).values()
            for cr in (branch.get("commits") or [])
            if cr.get("sha") == sha and cr.get("cherry_pick") in ("OK", "EMPTY")
            and branch.get("target_branch")
        ]
        for target in applied_targets:
            d = decisions.get(target)
            if isinstance(d, dict) and d.get("kind") == "NeedSync":
                ev = _decision_evidence(d)
                if ev:
                    return ev
        ev = _first_evidence_of("NeedSync")
        if ev:
            return ev
        return "已同步应用到目标分支"
    if bucket == "skipped":
        ev = _first_evidence_of("AlreadyIncluded", "OutOfScope")
        if ev:
            return ev
        return "目标分支已包含该修复，或本产品线不适用"
    if bucket == "review":
        ev = _first_evidence_of("ManualReview")
        if ev:
            return ev
        return "需人工裁决"
    # unhandled
    ev = _first_evidence_of("NeedSync")
    if ev:
        return f"判定待同步但未同步落地：{ev}"
    return "无判定记录，需排查归因"


def _decision_evidence(decision: dict) -> str | None:
    """一条判定里最像「缘由」的文本：优先 evidence 首条，回退 reason 兜底。"""
    for e in decision.get("evidence") or []:
        if isinstance(e, str) and e.strip():
            return e.strip()
    reason = decision.get("reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return None


def commit_destinations(payload: dict) -> dict[str, int]:
    """周期级「检测 commit 去向」守恒归类（按 distinct commit）。

    每个检测到的 commit 归入且仅归入一个去向桶，故恒有
    ``detected == synced + skipped + review + unhandled``——直接复用
    ``commit_bucket`` 逐 commit 归类，两处永不漂移。这是对 ``cycle_summary``
    只给「检测/同步/跳过」三个数、账对不上的补全——「未处理」桶正是失败无归因
    的缺口暴露点，历史页与周期概览共用。
    """
    counts = {"detected": 0, "synced": 0, "skipped": 0, "review": 0, "unhandled": 0}
    for c in payload.get("detected_commits") or []:
        sha = c.get("sha")
        if not sha:
            continue
        counts["detected"] += 1
        counts[commit_bucket(payload, sha)] += 1
    return counts


def _append_if_text(reasons: list[str], value: object) -> None:
    """value 非空则追加（strip 后仍非空）。"""
    if isinstance(value, str) and value.strip():
        reasons.append(value.strip())


def _first_line(value: object) -> str:
    """取多行诊断的首个非空行。

    git 的诊断常是多行（``error: …`` + ``fatal: cherry-pick failed``）；摘要里
    只留第一行，完整原文仍在 commit 详情/errors 里可查。
    """
    if not isinstance(value, str):
        return ""
    for line in value.splitlines():
        if line.strip():
            return line.strip()
    return ""
