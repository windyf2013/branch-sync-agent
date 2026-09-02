# Proposal: fix-cycle-enumeration-and-archive-boundary

## Why

重扫 `cycle-2026-09-01` 窗口（`scan-*` 周期）走查暴露出两个独立缺陷，都指向「周期枚举 + 时间语义」这一块，导致 web 无法如实呈现「重跑的 cron 任务去哪了」：

1. **`list_cycle_records` 只 glob `cycle-*`**（`src/bsa/scheduler/cycle.py:179`），漏掉 `scan-*` 周期。后果连锁：
   - `scan-*` 周期明明 `cycle.json` 写 `status: SUCCESS`，但 `latest_completed_cycle`/`list_cycles` 都读不到它 → web 上「隐身」，工作台仍显示旧的 `cycle-2026-09-01`。
   - `_cycle_terminal_status`（`cli.py:189`）靠 `list_cycle_records` 找 `scan-*` 读终态，找不到 → 返回 `UNKNOWN` → `register_finish` 把 tasks 行记成 `failed`（实际 SUCCESS）。
   - `_cmd_status` / `bsa report` 的 `_cycle_status` 同样读不到 `scan-*` 终态。

2. **归档边界「最近完成周期把自己归档」**：历史页归档规则是 `tasks.created_at < latest_cycle_start`（`history.py:113`）。cron 调度器写 tasks 行的 `created_at`（本地 0:00:03）比引擎开跑写 `cycle.json started_at`（0:00:04）早 1 秒 → 最近完成周期自己的 tasks 行被归档进历史页，同时它又是 `latest_completed_cycle`，于是同一周期同时出现在工作台和历史页，「两个完全看不出区别」。

## What Changes

- **`list_cycle_records` 统一 glob 全部有 cycle.json 的周期**：`cycle-*` + `scan-*`（`run_cycle` 全家桶落盘的两类；`manual-*`/`rerun-*` 不写 cycle.json，本就不该枚举）。所有调用点（投影 `latest_completed_cycle`/`list_cycles`/`latest_cycle_start`、`_cycle_terminal_status`、`_cmd_status`、`_cycle_status`、`rerun._locate_cycle`）自然覆盖 `scan-*`。
- **归档排除最近完成周期本身**：`_archived_tasks` 归档判断排除 `latest_completed_cycle` 的 cycle_id，根治「当前周期进历史页」，不再依赖 1 秒级时间戳对齐。

明确**不做**：不改 cron 调度器写行时刻与引擎写 cycle.json 时刻的对齐（1 秒差是进程边界固有，用「排除当前周期」语义根治，不追时间戳）；不改 `manual-*`/`rerun-*` 的 cycle record 落盘行为（它们按设计不写 cycle.json）。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `reliability` / `reporting`: 周期记录枚举覆盖 `scan-*`（含 `:`/`+` 的目录名），终态读取不再误判。
- `web-platform`: 历史页归档边界排除最近完成周期本身，当前周期不再误归档。

## Impact

- 引擎 `src/bsa/scheduler/cycle.py`：`list_cycle_records` glob 改为 `cycle-*` + `scan-*`。
- 平台 `src/bsa_web/views/history.py`：`_archived_tasks` 归档判断排除 `latest_completed_cycle`。
- 测试：`tests/test_projection.py`（或 scheduler 相关）新增 scan-* 枚举单测；`tests/web/test_detail_pages.py` 新增「当前周期不归档」单测。

## 待定项（设计阶段敲定）

1. `list_cycle_records` glob 的精确写法：`root.glob("cycle-*/cycle.json") + root.glob("scan-*/cycle.json")`，还是统一 `root.glob("*/cycle.json")`（后者会把任何含 cycle.json 的目录都算进来，含未来的 manual/rerun 若未来改落盘）。
2. 归档排除的实现位置：在 `_archived_tasks` 内排除，还是下沉到 `latest_cycle_start` 语义（后者会改工作台手动任务边界，影响面更大，倾向前者）。
