# Design: fix-cycle-enumeration-and-archive-boundary

## 架构总览

两处独立修复，都落在「周期枚举 + 归档边界」这一语义层，不引入新节点、不改路由、不改周期 id 规则。

```
① list_cycle_records 统一 glob cycle-* + scan-* → scan 周期不再隐身/误标 failed
② 归档排除 latest_completed_cycle 本身 → 当前周期不再误归档进历史页
```

## 改动 1：list_cycle_records 统一 glob cycle-* + scan-*

- **文件**：`src/bsa/scheduler/cycle.py` `list_cycle_records`。
- **缺陷**：只 `root.glob("cycle-*/cycle.json")`，漏 `scan-*`（manual-scan 落盘）。
- **语义**：有 `cycle.json` 的周期 = `run_cycle` 全家桶（cron 每日 + manual-scan）落盘的周期，即 `cycle-*` 与 `scan-*` 两类。`manual-*`（手动同步）与 `rerun-*`（重跑）按设计**不写** `cycle.json`，本就不该被枚举。
- **实现**：显式列举两类前缀（而非 `*/cycle.json` 宽匹配），避免未来任何含 `cycle.json` 的目录被误纳入；`scan-*` 目录名含 `:`/`+`（如 `scan-2026-08-30T22:00:00+08:00-...`），`Path.glob("scan-*/cycle.json")` 正常匹配。
- **连锁修复**：所有调用点自然覆盖 `scan-*`——
  - `latest_completed_cycle` / `list_cycles` / `latest_cycle_start`（投影层）
  - `_cycle_terminal_status`（CLI 折叠终态，不再把 SUCCESS 的 scan 标 failed）
  - `_cmd_status`（`bsa status` 能读到 scan 周期）
  - `bsa report` 的 `_cycle_status`（scan 周期不再误判 running）
  - `rerun._locate_cycle`（scan 周期含 batch，可作源周期定位）

## 改动 2：归档排除最近完成周期本身

- **文件**：`src/bsa_web/views/history.py` `_archived_tasks`。
- **缺陷**：归档规则 `created < latest_cycle_start`，而 cron 调度器写 tasks 行的 `created_at`（本地 0:00:03）比引擎写 `cycle.json started_at`（0:00:04）早 1 秒，导致最近完成周期自己的 tasks 行被归档，同一周期同时出现在工作台和历史页。
- **实现**：在 `_archived_tasks` 内，对 `kind=cycle` 且 `cycle_id == latest_completed_cycle` 的任务行直接跳过归档（保留在工作台）。**不动 `latest_cycle_start` 语义**——它同时供工作台手动任务边界使用，改动会扩大影响面。
- **边界**：只排除「最近完成周期」这一条，其余历史周期仍按 `created < start` 归档，语义不变。

## 数据流与边界

- 不新增周期类型、不改 `manual-*`/`rerun-*` 落盘行为（它们不写 cycle.json 是设计约束，见 CLAUDE.md 不变量 #15）。
- `list_cycle_records` 排序 `_started_sort_key` 不变；`scan-*` 与 `cycle-*` 混排按 `started_at` 升序。
- 归档排除只作用于 cycle 任务，不触碰手动 sync/rerun 的归档边界。

## 测试策略

- `tests/test_scheduler_cycle.py`（或等价）：`list_cycle_records` 同时枚举 `cycle-*` 与 `scan-*`，按 `started_at` 升序；`manual-*`/`rerun-*` 目录（无 cycle.json）不被枚举。
- `tests/test_projection.py`：`latest_completed_cycle` 能取到 `scan-*` 周期。
- `tests/web/test_detail_pages.py`（或 history 相关）：`_archived_tasks` 排除 `latest_completed_cycle` 本身，该周期不进归档列表。
- 全量 `uv run pytest` + `uv run ruff check src` 通过。

## 验收标准（结果导向）

1. 重扫 `cycle-2026-09-01` 窗口（`scan-*`）后：工作台/`bsa status` 能显示 `scan-*` 周期，且 `register_finish` 记 `succeeded`（不再 `failed`）。
2. `bsa report scan-... --json` 返回非 `{status: running}`，而是完整终态投影。
3. 历史页不再出现 `cycle-2026-09-01`（最近完成周期），它只留在工作台。
4. `uv run pytest` 全绿、`uv run ruff check src` 零告警。
