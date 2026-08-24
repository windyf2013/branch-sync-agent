from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

from bsa.executor.exceptions import SafetyViolation


class SafetyRules(BaseModel):
    forbidden_paths: list[str]
    required_models: list[str]
    forbidden_branches: list[str]
    max_single_edit_lines: int


def load_safety_rules(path: Path) -> SafetyRules:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return SafetyRules(**data)


class SafetyEnforcer:
    def __init__(self, safety_rules: SafetyRules) -> None:
        self._rules = safety_rules

    def check_editable(self, paths: list[str]) -> None:
        for path in paths:
            for forbidden in self._rules.forbidden_paths:
                if forbidden.endswith("/"):
                    hit = path.startswith(forbidden)
                else:
                    hit = path == forbidden
                if hit:
                    raise SafetyViolation(
                        f"路径 {path} 命中禁止修改清单 {forbidden}，强制拒绝转人工。"
                    )

    def check_sync_branch(self, branch: str) -> bool:
        return branch not in self._rules.forbidden_branches

    def required_models(self) -> list[str]:
        return list(self._rules.required_models)

    def max_single_edit_lines(self) -> int:
        return self._rules.max_single_edit_lines

    def check_edit_scale(self, diff: str) -> None:
        """Reject edits whose total added/removed lines exceed max_single_edit_lines."""
        added = removed = 0
        for line in diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
        if added + removed > self._rules.max_single_edit_lines:
            raise SafetyViolation(
                f"单次修改 {added + removed} 行超过上限 {self._rules.max_single_edit_lines}"
            )
