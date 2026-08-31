"""build 矩阵展示辅助：型号 → 实际编译脚本标签，按需读日志预览。

web 侧复用 V1 的 build_rules（型号 → script/product）给 build 矩阵打标签，
让操作者一眼看出"这次编的是哪个型号/脚本"，并收敛默认信息量（仅 FAILED
结果展示最终错误摘要，OK 只显示状态与次数）。
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path

from bsa.build.runner import _resolve_build_script
from bsa.rules.build_rules import load_build_rules

_BUILD_RULES_PATH = None
_rules_cache = None


def _bundled_build_rules_path() -> Path:
    global _BUILD_RULES_PATH
    if _BUILD_RULES_PATH is None:
        try:
            resource = importlib.resources.files("bsa.rules")
            if resource.is_dir():
                _BUILD_RULES_PATH = Path(str(resource)) / "build_rules.yaml"
                return _BUILD_RULES_PATH
        except (ModuleNotFoundError, FileNotFoundError):
            pass
        _BUILD_RULES_PATH = (
            Path(__file__).resolve().parent.parent.parent / "bsa" / "rules" / "build_rules.yaml"
        )
    return _BUILD_RULES_PATH


def _build_types() -> dict:
    global _rules_cache
    if _rules_cache is None:
        try:
            _rules_cache = load_build_rules(_bundled_build_rules_path()).build_types
        except Exception:
            _rules_cache = {}
    return _rules_cache


def model_script_label(model: str) -> str:
    """型号 → 'script product' 标签；显式 build_types 优先，旧格式推断兜底。"""
    bt = _build_types().get(model)
    if bt is not None:
        return f"{bt.script} {bt.product}"
    script, product = _resolve_build_script(model)
    return f"{script} {product}"


def _read_build_log(log_dir: str, log_path: str | None, max_lines: int = 100):
    """读 build 日志尾部（失败原因在日志末尾）；缺失/越界返回 None。"""
    if not log_path:
        return None
    root = Path(log_dir).resolve()
    path = Path(log_path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    truncated = len(lines) > max_lines
    return "\n".join(lines[-max_lines:]), truncated


def _enrich_outcomes(builds: dict | None, log_dir: str) -> None:
    """就地补充一组 ``model -> outcome`` 的展示字段（commit build 与 baseline 共用）。"""
    for model, outcome in (builds or {}).items():
        outcome["model_label"] = model_script_label(model)
        outcome["log_preview"] = None
        outcome["log_truncated"] = False
        if outcome.get("status") == "FAILED":
            preview = _read_build_log(log_dir, outcome.get("log_path"))
            outcome["log_preview"] = preview[0] if preview else None
            outcome["log_truncated"] = preview[1] if preview else False


def enrich_build_outcomes(branch: dict, log_dir: str) -> None:
    """就地补充每个 build outcome 的展示字段：log_preview/truncated/model_label。

    log_preview 只在 outcome 为 FAILED（最终失败）时读——OK 结果不需要拖全量日志。
    同时覆盖 commit build 与 baseline（基线编译失败也要在详情页透出日志/错误）。
    """
    for cr in branch.get("commits") or []:
        _enrich_outcomes(cr.get("build"), log_dir)
    _enrich_outcomes(branch.get("baseline"), log_dir)
