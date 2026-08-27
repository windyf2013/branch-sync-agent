"""同步台账 ledger（append-only，决策 5.2）。

记录每条已同步的 ``(patch_id, target_branch)``，供检测阶段短路 AlreadyIncluded：
同一业务 commit（patch-id 稳定）对同一主分支再次出现时，直接判定已含，
跳过快照构建与相似度/LLM 判定（跨天/跨周期幂等）。

文件：``{log_dir}/ledger.json``，append-only 追加；损坏/缺失视为空（节点级容错）。
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

_LEDGER_FILE = "ledger.json"
_lock = threading.Lock()


def _ledger_path(log_dir: str | Path) -> Path:
    return Path(log_dir) / _LEDGER_FILE


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def record_synced(
    log_dir: str | Path,
    patch_id: str,
    target_branch: str,
    source_sha: str,
) -> None:
    """追加一条 synced 记录（幂等：重复调用不报错，可能重复写但 is_synced 仍 True）。"""
    entry = {
        "patch_id": patch_id,
        "target_branch": target_branch,
        "source_sha": source_sha,
        "status": "synced",
        "ts": _now_iso(),
    }
    path = _ledger_path(log_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        lines = []
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8")
                if text.strip():
                    lines = text.splitlines()
            except OSError:
                lines = []
        with path.open("a", encoding="utf-8") as fh:
            if lines and not lines[-1].endswith("\n"):
                fh.write("\n")
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def is_synced(log_dir: str | Path, patch_id: str, target_branch: str) -> bool:
    """``(patch_id, target_branch)`` 是否已有 synced 记录。损坏/缺失 → False。"""
    path = _ledger_path(log_dir)
    if not path.exists():
        return False
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(entry, dict)
                    and entry.get("status") == "synced"
                    and entry.get("patch_id") == patch_id
                    and entry.get("target_branch") == target_branch
                ):
                    return True
    except OSError:
        return False
    return False
