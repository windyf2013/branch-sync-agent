#!/usr/bin/env python3
"""清 BSA 周期 checkpoint，供「手动重跑同一天」前使用。

背景：``bsa run-cycle --date <YYYY-MM-DD>``（含 executor 对 kind='cycle' 的调度）
对同一天是 resume 语义——只要 ``logs/state.sqlite3`` 里该 thread_id（== cycle_id）
的 checkpoint 还在（即便上一轮已终态 FAILED），重跑只会 ``resume existing
checkpoint`` 秒退，并重发收尾报告邮件，看起来像「没跑」。要真正重跑同一天，
必须先删该 thread 的 checkpoint。

用法（仓库根下执行）：
    python scripts/clear-checkpoint.py                 # --dry-run 只打印将删
    python scripts/clear-checkpoint.py --yes           # 真删
    python scripts/clear-checkpoint.py 2026-09-09      # 指定日期（默认今天）
    python scripts/clear-checkpoint.py 2026-09-09 --yes

安全：只删目标 thread（checkpoints + 残留 writes），其余 scan/cycle 线程保留；
有平台活动周期任务（tasks 表 queued/running）在跑时不删，提示先等它结束。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date as _date
from pathlib import Path


def _find_log_dir() -> Path:
    """log_dir：脚本所在 scripts/ 的上一级（仓库根）下的 logs。

    引擎 .env 的 LOG_DIR 默认 'logs'（相对仓库根）；这里直接取仓库根/logs，
    与本仓部署一致。若实际部署指向别的 LOG_DIR，可在命令行用 --log-dir 覆盖。
    """
    root = Path(__file__).resolve().parent.parent
    return root / "logs"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cycle_date", nargs="?", default=_date.today().isoformat(),
                    help="周期日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--yes", action="store_true", help="真正删除（默认只 dry-run 打印）")
    ap.add_argument("--log-dir", default=None, help="覆盖 log_dir（默认 <仓库根>/logs）")
    args = ap.parse_args()

    log_dir = Path(args.log_dir) if args.log_dir else _find_log_dir()
    cycle_id = f"cycle-{args.cycle_date}"
    db_path = log_dir / "state.sqlite3"
    platform_db = log_dir / "platform.sqlite3"

    if not db_path.exists():
        print(f"[clear-checkpoint] 无 state.sqlite3：{db_path}（无需清理）")
        return 0

    # 平台有活动周期任务在跑则拒绝，避免清到正在执行的周期
    if platform_db.exists():
        try:
            con = sqlite3.connect(f"file:{platform_db}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT id, state FROM tasks WHERE kind='cycle' "
                "AND state IN ('queued','running') ORDER BY id"
            ).fetchone()
            con.close()
        except sqlite3.Error:
            row = None
        if row:
            print(f"[clear-checkpoint] 平台有活动周期任务 id={row['id']} "
                  f"state={row['state']} —— 先等其结束再清，已中止")
            return 1

    con = sqlite3.connect(db_path)
    n_cp = con.execute("DELETE FROM checkpoints WHERE thread_id=?",
                       (cycle_id,)).rowcount
    n_w = 0
    cols = [r[1] for r in con.execute("PRAGMA table_info(writes)").fetchall()]
    if "thread_id" in cols:
        n_w = con.execute("DELETE FROM writes WHERE thread_id=?",
                          (cycle_id,)).rowcount
    con.commit()
    con.close()

    verb = "已删除" if args.yes else "将删除（--dry-run，加 --yes 生效）"
    print(f"[clear-checkpoint] {verb} thread={cycle_id} "
          f"checkpoints={n_cp} writes={n_w}")
    # dry-run 未真删时提示真实命令
    if not args.yes:
        print(f"[clear-checkpoint] 确认清理请执行："
              f"python scripts/clear-checkpoint.py {args.cycle_date} --yes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
