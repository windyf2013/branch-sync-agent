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
    for key, models in rules.build_models_by_section.items():
        if key in section or section in key:
            return list(models)
    raise BuildConfigError(
        f"产品线「{section}」未配置编译型号，停止任务（请在 build_rules.yaml 补充）"
    )
