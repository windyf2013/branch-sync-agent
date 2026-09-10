#!/usr/bin/env bash
# =============================================================================
# run-cycle.sh — BSA 手动 cron 周期「入队」启动脚本
#
# 目的：手动触发一次与 cron 每日周期「任务流完全一致」的完整同步周期，并让它
#       **在 Web 平台可见**。做法是往 tasks 表插一行 kind='cycle' 的 queued 任务
#       （与 systemd bsa-executor 内置 0:00 调度用的是同一个入队函数 enqueue_task），
#       由运行中的 bsa-executor 认领执行 —— 平台「任务中心」可见、可 tail 日志、
#       可跟踪，且引擎全局 bsa.lock 保证与 0:00 周期互斥不双跑。
#
# 为何不直接 bsa run-cycle：直跑不经 executor 入队，平台看不到该任务；且对同
#     一天（同一 cycle_id）是 resume 语义——若当天已有（即便已终态的）checkpoint，
#     直跑只会 resume 秒退并重发报告邮件，看起来像「没跑」。入队后由 executor
#     调度，行为与 cron 完全一致；若确实要重跑同一天，请先清 checkpoint（见
#     scripts/clear-checkpoint.py，默认 --dry-run 只打印不删）。
#
# 用法：
#   scripts/run-cycle.sh                      # 入队今天（cycle-$(date +%F)）
#   scripts/run-cycle.sh 2026-09-09           # 入队指定日期
#   scripts/run-cycle.sh --check-now          # 先检查是否已有活动周期任务/冲突
#
# 前置依赖：
#   - bsa-executor 服务在运行（systemctl --user status bsa-executor）。
#   - 仓库根有 .env（REPO_PATH / CRON_BRANCH_FILE / MAIL_* / LOG_DIR …）；本脚本
#     从自身位置推导仓库根并 cd 过去，保证读对 .env。
#   - .venv 已就绪（uv sync）。
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"

usage() {
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-1}"
}

# --- 解析参数 -------------------------------------------------------------
# 首个参数若是 YYYY-MM-DD 则作为日期消费；其余仅支持已知透传旗标。
DATE="$(date +%F)"
CHECK_ONLY=0
EXTRA=()
for a in "$@"; do
    case "$a" in
        -h|--help) usage 0 ;;
        --check-now) CHECK_ONLY=1 ;;
        --*) echo "[run-cycle] 未知选项: $a"; usage 1 ;;
        *)
            if [[ "$a" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
                DATE="$a"
            else
                echo "[run-cycle] 无法识别的参数: $a"; usage 1
            fi
            ;;
    esac
done

[[ -x "$PY" ]] || { echo "[run-cycle] 缺少 $PY —— 请先执行 uv sync"; exit 1; }

# --- 入队（复用 executor.enqueue_task，与 cron 调度同链路）----------------
# WebSettings.log_dir 默认 'logs'（相对仓库根），DB 在 logs/platform.sqlite3。
# 引擎日志 dir 取引擎 .env 的 LOG_DIR：默认同为 logs；二者一致时入队即引擎可见。
LOG_DIR="$ROOT/logs"

echo "[run-cycle] 手动 cron 周期入队 cycle-$DATE → 平台 tasks 表"
echo "[run-cycle] platform DB: $LOG_DIR/platform.sqlite3"

"$PY" - "$LOG_DIR" "$DATE" "$CHECK_ONLY" <<'PY'
import sys
from pathlib import Path

log_dir, date, check_only = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
cycle_id = f"cycle-{date}"
db_path = log_dir / "platform.sqlite3"

from bsa_web.db import init_db
con = init_db(db_path)

# 已有活动周期任务（queued/running）则拒绝，避免与 0:00 调度/在跑任务冲突
row = con.execute(
    "SELECT id, state, source, cycle_id, created_at FROM tasks "
    "WHERE kind='cycle' AND state IN ('queued','running') ORDER BY id"
).fetchone()
if check_only:
    if row:
        print(f"[run-cycle] 检查：有活动周期任务 id={row['id']} state={row['state']} "
              f"source={row['source']}（cycle_id={row['cycle_id']}）")
        sys.exit(0)
    print(f"[run-cycle] 检查：无活动周期任务，可安全入队 {cycle_id}")
    sys.exit(0)
if row:
    print(f"[run-cycle] 已有活动周期任务 id={row['id']} state={row['state']} "
          f"source={row['source']} —— 跳过入队（唯一索引也会拒绝）")
    sys.exit(1)

from bsa_web.executor import enqueue_task
task_id = enqueue_task(con, "cycle", "system", None, source="manual")
if task_id is None:
    print("[run-cycle] 入队被拒（唯一索引/并发）：可能刚有周期任务入队，稍后再试")
    sys.exit(1)
con.execute("UPDATE tasks SET cycle_id=? WHERE id=?", (cycle_id, task_id))
con.commit()
print(f"[run-cycle] 已入队 kind=cycle cycle_id={cycle_id} task_id={task_id} source=manual")
print(f"[run-cycle] 由运行中的 bsa-executor 认领执行；任务日志 logs/tasks/task-{task_id}.log")
PY
rc=$?
exit "$rc"
