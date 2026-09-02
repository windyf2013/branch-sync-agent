from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import BaseModel


class BuildConfigError(Exception):
    """产品线/型号配置缺失或无法解析：任务应报错停止，不得静默用错编译脚本。"""


class BuildType(BaseModel):
    script: str
    product: str


class BuildRules(BaseModel):
    build_types: dict[str, BuildType]
    build_models_by_section: dict[str, list[str]]
    build_modules: dict[str, str] = {}


def load_build_rules(path: Path) -> BuildRules:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return BuildRules(**data)


def resolve_build_models(
    section_of: Mapping[str, str], target: str, rules: BuildRules
) -> list[str]:
    """目标分支所在 branch.md section 的编译型号列表；查不到则抛配置错误。

    ``section_of`` 为分支名 → section 标题映射（来自 branch.md 解析或同源矩阵）。
    section 匹配：配置 key（如「组网产品分支」）按子串匹配 section 标题（如
    「1.1 组网产品分支」，容忍编号前缀）。分支不在 branch.md、或产品线未配置
    型号，均报错停止——避免用错编译脚本。
    """
    section = section_of.get(target)
    if section is None:
        raise BuildConfigError(
            f"目标分支 {target} 不在 branch.md，无法确定产品线/编译型号，停止任务"
        )
    # 双向子串可能同时命中多个 key（如 section "4.34" 同时命中「4.34 主分支」与
    # 「4.34产品主线分支」）。按书写顺序取首个 = 静默用错编译脚本，故收集全部匹配，
    # 多于一个即报错停止。
    matched = [
        (key, models)
        for key, models in rules.build_models_by_section.items()
        if key in section or section in key
    ]
    if len(matched) > 1:
        keys = "、".join(f"「{key}」" for key, _ in matched)
        raise BuildConfigError(
            f"产品线「{section}」同时命中多个编译型号配置：{keys}，"
            "无法确定型号，停止任务（请修改 build_rules.yaml 使各 key 互不为子串）"
        )
    if matched:
        return list(matched[0][1])
    raise BuildConfigError(
        f"产品线「{section}」未配置编译型号，停止任务（请在 build_rules.yaml 补充）"
    )


def resolve_build_modules(changed_files: list[str], rules: BuildRules) -> str | None:
    """改动文件 → 编译脚本模块参数；无法确定时返回 None（全量编译兜底）。

    按最长前缀匹配每个改动文件的模块 token；所有文件命中**同一** token 才返回该
    token（如 ``"component wlan"``），任一文件未命中、或命中多个不同 token 均返回
    None——绝不静默猜测模块（与 resolve_build_models 的「多命中即停」同一精神）。
    """
    if not changed_files or not rules.build_modules:
        return None
    token: str | None = None
    for path in changed_files:
        matched = None
        for prefix, value in rules.build_modules.items():
            if path.startswith(prefix) and (matched is None or len(prefix) > len(matched[0])):
                matched = (prefix, value)
        if matched is None:
            return None
        if token is None:
            token = matched[1]
        elif token != matched[1]:
            return None
    return token
