from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel


class ConcludeThresholds(BaseModel):
    similarity_high: float = 0.90
    similarity_low: float = 0.50
    need_sync_target_types: list[str] = ["release", "fix"]


class DecisionRules(BaseModel):
    classify: dict[str, Any]
    conclude: ConcludeThresholds
    branch_mapping: dict[str, str] = {}


def load_decision_rules(path: Path) -> DecisionRules:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return DecisionRules(**data)
