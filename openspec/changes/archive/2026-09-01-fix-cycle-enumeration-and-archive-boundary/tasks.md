# Tasks: fix-cycle-enumeration-and-archive-boundary

## 1. 引擎：list_cycle_records 统一 glob cycle-* + scan-*

- 文件：`src/bsa/scheduler/cycle.py`（`list_cycle_records`）
- 改动：glob 改为同时枚举 `cycle-*/cycle.json` 与 `scan-*/cycle.json`，仍按 `_started_sort_key` 升序。
- 验证：`tests/test_scheduler_cycle.py`（或等价）新增——同时造 `cycle-*` 与 `scan-*` 目录，`list_cycle_records` 返回两类、按 started_at 升序、不含无 cycle.json 的 `manual-*` 目录。

## 2. 平台：归档排除最近完成周期本身

- 文件：`src/bsa_web/views/history.py`（`_archived_tasks`）
- 改动：对 `kind=cycle` 且 `cycle_id == latest_completed_cycle` 的任务行跳过归档（保留工作台），其余周期仍按 `created < start` 归档。
- 验证：`tests/web/test_detail_pages.py`（或 history 相关）新增——`latest_completed_cycle` 的 cycle 任务不进归档列表。

## 3. 回滚说明

- 改动不改变周期 id 规则、不改变 `manual-*`/`rerun-*` 落盘行为、不动 `latest_cycle_start` 语义。
- 回滚 = revert 两处提交；无数据迁移。

## 验证命令

```bash
uv run pytest tests/test_scheduler_cycle.py tests/test_projection.py tests/web/test_detail_pages.py
uv run ruff check src
uv run pytest
```

## 验收标准（结果导向）

1. 重扫 `cycle-2026-09-01` 窗口（`scan-*`）后：`bsa status`/工作台能显示 `scan-*` 周期，`register_finish` 记 `succeeded`（不再 failed）。
2. `bsa report scan-... --json` 返回完整终态，非 `{status: running}`。
3. 历史页不再出现 `cycle-2026-09-01`，它只留在工作台。
4. `uv run pytest` 全绿、`uv run ruff check src` 零告警。
