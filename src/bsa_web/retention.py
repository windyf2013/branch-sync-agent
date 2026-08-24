"""数据保留分级清理：只删超龄 L2 体积日志，绝不碰 L1 审计产物。

L2 = ``**/build/**/*.log`` 与任意层级 ``run.log``（按 mtime 超 ``keep_days``）；
L1 = cycle.json / decisions.json / report.html / patch / audit diff 一律保留。
run_maintenance 为组合入口（清理 + 滚动备份），供 cron 每日调用。
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from bsa_web.backup import backup_now


def _is_l2(path: Path) -> bool:
    """判断是否属于 L2 体积日志（build/run.log）。"""
    if path.name == "run.log":
        return True
    return path.name.endswith(".log") and "build" in path.parts


def _prune_empty_dirs(root: Path, start: Path) -> None:
    """best-effort 清掉因删除而变空的父目录（不含 root 本身）。"""
    current = start.parent
    while current != root and current != current.parent:
        try:
            if not current.is_dir() or any(current.iterdir()):
                return
            current.rmdir()
        except OSError:
            return
        current = current.parent


def clean_logs(log_dir: str | Path, keep_days: int = 30) -> int:
    """清理超过 ``keep_days`` 的 L2 体积日志，返回删除的文件数。

    只匹配 ``**/build/**/*.log`` 与任意层级 ``run.log``；cycle.json/
    decisions.json/report.html/patch/audit diff 等 L1 审计产物永不删除。
    """
    root = Path(log_dir)
    if not root.is_dir():
        return 0
    cutoff = time.time() - keep_days * 86400
    deleted = 0
    for path in root.rglob("*"):
        if not path.is_file() or not _is_l2(path):
            continue
        if path.stat().st_mtime >= cutoff:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        deleted += 1
        _prune_empty_dirs(root, path)
    return deleted


def run_maintenance(
    log_dir: str | Path,
    backup_dir: str | Path,
    keep_days: int = 30,
    keep: int = 7,
) -> dict:
    """组合入口：先 L2 分级清理，再做每日滚动备份，返回统计。"""
    deleted = clean_logs(log_dir, keep_days=keep_days)
    archive = backup_now(log_dir, backup_dir, keep=keep)
    return {"deleted": deleted, "backup": str(archive)}


def main(argv: list[str] | None = None) -> int:
    """cron 可执行入口：``python -m bsa_web.retention --log-dir ... --backup-dir ...``。"""
    parser = argparse.ArgumentParser(description="BSA 数据保留与备份（每日维护）")
    parser.add_argument("--log-dir", required=True, help="V1/平台日志根目录")
    parser.add_argument("--backup-dir", required=True, help="备份输出目录")
    parser.add_argument("--keep-days", type=int, default=30, help="L2 日志保留天数")
    parser.add_argument("--keep", type=int, default=7, help="滚动备份保留份数")
    args = parser.parse_args(argv)
    result = run_maintenance(args.log_dir, args.backup_dir, args.keep_days, args.keep)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
