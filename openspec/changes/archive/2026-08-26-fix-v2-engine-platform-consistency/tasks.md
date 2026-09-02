# Tasks: fix-v2-engine-platform-consistency

（本变更已完成实现并通过验证，此处记录任务清单与验证结果。）

## 引擎侧

- [x] P0-2/G10：`report` 节点收敛可区分终态（`bsa/graph/nodes.py`）；`_cmd_run_cycle`/
  `_cmd_manual_scan` 按 fold 规则折叠 `tasks.state`（`bsa/cli.py`）。
- [x] G9：`detect_commits` 加载 judgments.json 传入 classify（`bsa/graph/nodes.py`）。
- [x] G11：`write_state_json`/`read_projection_payload`（`bsa/report/projection.py`）；
  周期/sync/rerun 结束写 state.json（`bsa/scheduler/cycle.py`、`bsa/cli.py`）。
- [x] P2-1：conclude 目标类型由 yaml 驱动（`bsa/rules/conclude.py`、
  `decision_rules.{py,yaml}`、GraphContext 注入）。
- [x] P2-3：rerun 单一线程 id（`bsa/commands/rerun.py`、`bsa/cli.py`）。
- [x] P2-6：`register_start` 捕获 IntegrityError 返回 None（`bsa/commands/task_reporter.py`）。
- [x] P2-7：`manual_scan_cycle_id` + manual-scan 登记（`bsa/scheduler/cycle.py`、
  `bsa/cli.py`）。

## 平台侧

- [x] P0-1：executor 对账/兜底收敛 `cycle.json` 僵尸 running（`bsa_web/executor.py`）；
  工作台 interrupted 周期续跑入口 + `/api/cycle/resume`（`bsa_web/api/operations.py`、
  `bsa_web/views/workbench.py`、`bsa_web/templates/workbench.html`）。
- [x] P2-2：executor 预生成 sync/fresh-rerun cycle_id 并注入 env（`bsa_web/executor.py`）。
- [x] P2-5：首页 commit 数从任务行取，去掉 N 子进程（`bsa_web/views/workbench.py`）。
- [x] PID 探活：记录 child PID + `kill(pid,0)` 对账探活（`bsa_web/executor.py`）。
- [x] `tasks` 表新增 `commits`/`pid` 列（幂等迁移，`bsa_web/db.py`、
  `bsa/commands/task_reporter.py`）。

## 文档

- [x] P2-4：spec 放弃措辞改为"周期内有效"（`openspec/specs/web-platform/spec.md`）。

## 验证

- [x] `uv run pytest`：888 passed, 3 skipped。
- [x] `uv run ruff check`：改动文件全部通过（9 处 pre-existing 错误未在本次改动范围内）。
- 回归：既有关键测试断言（REPORTED → 终态）已同步更新。
