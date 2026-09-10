"""面向人类的枚举 → 中文映射（引擎与平台共用的单一事实源）。

为什么在 `bsa` 而不是 `bsa_web`：报告与邮件由引擎渲染（`bsa.report.renderer`），
它们同样需要中文文案，而分层方向是 ``bsa_web → bsa`` —— 引擎 import 平台会形成
反向依赖。故映射收敛到这个叶子模块（只依赖 ``re``），平台侧原地 re-export，
既有符号名与过滤器行为逐字不变。

注意 ``STEP_ZH``（进度步骤态）**刻意不在此处**：它只服务平台步骤清单，键空间是
``CHERRY_PICK_OK`` / ``BASELINE_OK`` 这类进度态；报告渲染的是 ``cherry_pick`` 的
``OK/CONFLICT/FAILED/EMPTY``，键空间不同，合并只会造出一张永不命中的表。
"""

from __future__ import annotations

import re

# 面向人类的状态值/四态 → 中文映射（统一收敛，避免散落各页的硬编码英→中 if/else）。
# 值不在表内则原样返回（兜底绝不吞信息）。
STATUS_ZH: dict[str, str] = {
    "SUCCESS": "成功",
    "FAILED": "失败",
    # PARTIAL = 这批没走完，「停批」专属 FAILFAST_STOP。
    "PARTIAL": "未完成",
    "MANUAL": "待处理",
    "UNKNOWN": "未知",
    "RUNNING": "进行中",
    "REPORTED": "已报告",
    "OK": "通过",
    "EMPTY": "已应用",
    "CONFLICT": "冲突",
    "SKIPPED": "已跳过",
}

# 报告/邮件专用超集。周期级与节点级终态（COMPLETED / DETECTED / BASELINE_* /
# CHERRY_PICK_* / BUILD_* …）只出现在报告里，**不能并进 STATUS_ZH** —— 那会静默改变
# 平台 status_zh 过滤器对 6+ 个消费点的显示输出。派生关系保证同键两边取值一致。
REPORT_STATUS_ZH: dict[str, str] = {
    **STATUS_ZH,
    "NEW": "新周期",
    "COMPLETED": "已完成",
    "DETECTED": "已检测",
    "DECIDED": "已判定",
    "PREPARED": "已就绪",
    "CHERRY_PICKED": "已 cherry-pick",
    "BASELINE_OK": "基线通过",
    "BASELINE_FAILED": "基线失败",
    "CHERRY_PICK_CONFLICT": "cherry-pick 冲突",
    "CHERRY_PICK_FAILED": "cherry-pick 中断",
    "BUILD_FAILED": "编译失败",
    "FAILFAST_STOP": "停批",
    "PATCHED": "已出补丁",
}

# 四态客观结论（唯一出自确定性规则层，此处只做展示映射）。
FOUR_STATE_ZH: dict[str, str] = {
    "NeedSync": "待同步",
    "AlreadyIncluded": "已包含",
    "OutOfScope": "不适用",
    "ManualReview": "待人工",
}

# 节点名 → 中文标签（报告与平台共用）。与引擎 progress.py 的 NODE_LABELS 语义一致，
# 但这里覆盖更全（含 decide / branch_matrix 等只在错误路径出现的节点），
# 未知节点原样返回兜底，绝不吞掉信息。
NODE_ZH: dict[str, str] = {
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
STOP_REASON_PATTERNS: list[tuple[re.Pattern, object]] = [
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

# 报告「失败阶段」的展示文案。键是 _failure_stage() 的机器判定值（英文 token 保留
# 在括号里作副标）：报告页脚已写明「结论代码保留英文枚举便于自动化对接」，且
# 保留 token 让机器可读与人工可读同时成立。
FAILURE_STAGE_ZH: dict[str, str] = {
    "cherry_pick": "cherry-pick 未完成 (cherry_pick)",
    "conflict": "冲突 (conflict)",
    "build": "编译失败 (build)",
}


def status_label(value: object) -> str:
    """平台用状态映射；未知值原样返回。"""
    return STATUS_ZH.get(str(value), str(value))


def report_status_label(value: object) -> str:
    """报告用状态映射（超集）；未知值原样返回。"""
    return REPORT_STATUS_ZH.get(str(value), str(value))


def kind_label(value: object) -> str:
    """四态结论 → 中文；未知值原样返回。"""
    return FOUR_STATE_ZH.get(str(value), str(value))


def failure_stage_label(value: object) -> str:
    """失败阶段 → 「中文 (英文 token)」；未知值原样返回。"""
    return FAILURE_STAGE_ZH.get(str(value), str(value))


def node_label(node: str | None) -> str:
    """节点名 → 中文标签；未知节点原样返回（兜底不吞）。"""
    return NODE_ZH.get(str(node or ""), node or "")


def humanize_stop_reason(text: str | None) -> str | None:
    """把引擎写死的英文 stop_reason 转成自然中文；未知内容原样透传。"""
    if not isinstance(text, str) or not text.strip():
        return text
    t = text.strip()
    for pattern, fmt in STOP_REASON_PATTERNS:
        m = pattern.match(t)
        if m:
            return fmt(m)
    return t
