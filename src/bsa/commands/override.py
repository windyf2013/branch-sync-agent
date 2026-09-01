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
    message: str | None = None,
    patch_id: str | None = None,
    clear: bool = False,
    timeout: float = 1800.0,
) -> dict:
    """写入 judgments.json 人工覆盖条目，持全局锁避免与周期写入竞争。

    ``message`` + ``patch_id`` 提供时，同时写 ``fp:<fingerprint>`` 键（决策 5.1）：
    内容不变时 rebase 后 sha 漂移仍命中人工判定；内容改动自动失效。

    ``clear=True`` 删除该 sha 条目，并用 ``message``+``patch_id`` 现算 ``fp:``
    键一并删除（fp 是派生别名键，无一一对应映射）；sha 不可达、patch_id 查不出
    时降级只删 sha 键、接受 fp 残留。清除后回到「未判定」，下次决策 LLM 重判。
    """
    from bsa.rules.classify import compute_fingerprint

    log_dir = Path(log_dir)
    path = log_dir / "judgments.json"
    with flock_acquire(log_dir / "bsa.lock", timeout=timeout):
        data: dict = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        if clear:
            data.pop(sha, None)
            if message is not None and patch_id:
                fp_key = f"fp:{compute_fingerprint(message, patch_id)}"
                data.pop(fp_key, None)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return {}
        entry = dict(data.get(sha, {}))
        if is_bug_fix is not None:
            entry["is_bug_fix"] = bool(is_bug_fix)
        if risk is not None:
            entry["risk"] = risk
        entry["recognition_source"] = "manual-override"
        data[sha] = entry
        if message is not None and patch_id:
            fp_key = f"fp:{compute_fingerprint(message, patch_id)}"
            data[fp_key] = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return entry
