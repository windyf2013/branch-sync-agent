from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from bsa.executor.lock import flock_acquire

_RISK = Literal["low", "medium", "high"]


def apply_override(
    log_dir: str | Path,
    sha: str,
    *,
    is_bug_fix: bool | None = None,
    risk: _RISK | None = None,
    timeout: float = 1800.0,
) -> dict:
    """写入 judgments.json 人工覆盖条目，持全局锁避免与周期写入竞争。"""
    log_dir = Path(log_dir)
    path = log_dir / "judgments.json"
    with flock_acquire(log_dir / "bsa.lock", timeout=timeout):
        data: dict = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        entry = data.get(sha, {})
        if is_bug_fix is not None:
            entry["is_bug_fix"] = bool(is_bug_fix)
        if risk is not None:
            entry["risk"] = risk
        entry["recognition_source"] = "manual-override"
        data[sha] = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data[sha]
