# Proposal: fix-v2-engine-platform-consistency

## Why

V2 阶段整体方案经多轮变更（add-web-maintenance-workbench → redesign-web-task-center →
引擎主动登记收敛），评审发现多处"承诺—实现"脱钩：周期终态不可区分、cron 周期中断无法续跑、
人工覆盖判定未贯通全量 commit、投影直接解析 LangGraph checkpoint 内部结构、任务关联键
cycle_id 不唯一。这些问题会导致平台"失败醒目标记"失效、中断现场无法重新识别、手动判定被
机器规则覆盖，构成管理与识别的隐患。

## What Changes

- **周期终态可区分（P0-2/G10）**：`report` 节点按 branch_results 收敛出 `SUCCESS` /
  `PARTIAL` / `FAILED` 终态；`_cmd_run_cycle`/`_cmd_manual_scan` 按 fold 规则折叠到
  `tasks.state`（SUCCESS 才记 succeeded）；工作台据此醒目标记周期失败。
- **周期中断续跑 + 僵尸收敛（P0-1）**：executor 对账/兜底把 running 的 cycle 任务标
  interrupted/failed 时同步收敛 `cycle.json` 僵尸 running；工作台对 interrupted 周期提供
  "续跑"按钮（同一 cycle_id 重排入 `run-cycle`，checkpoint 自动 resume）。
- **人工覆盖贯通全量 commit（G9）**：`detect_commits` 加载 `judgments.json` 传入
  `classify`，使 override 对机器已判定 commit 也生效（人工判定优先级最高）。
- **投影读结构化 state.json（G11）**：周期/sync/rerun 结束写 `state.json`（model_dump），
  `bsa report --json` 优先读 state.json（回退 checkpoint 兜底），平台不再解析 checkpoint
  内部结构。
- **规则层单一权威源（P2-1）**：conclude 目标类型（need_sync/ineligible）由
  `decision_rules.yaml` 单一驱动，去除 `conclude.py` 硬编码常量。
- **任务关联键唯一化（P2-2/P2-3/P2-7）**：executor 预生成 sync/fresh-rerun 的 cycle_id
  并注入环境变量；rerun 单一线程 id 生成（register_start 与实际 checkpoint 线程一致）；
  manual-scan 登记任务且 cycle_id 与 `run_cycle` 同源。
- **CLI 直启登记容错（P2-6）**：`register_start` 撞唯一索引返回 None，CLI 按"忙碌"友好
  退出，不再抛 `IntegrityError` 崩溃。
- **放弃周期内有效（P2-4）**：spec 措辞由"跨周期有效"更正为"周期内有效"（超期即弃）。
- **首页性能（P2-5）**：手动任务 commit 数从任务行取（sync 用 shas 长度 / rerun 落库），
  去掉首页每任务一次 `bsa report` 子进程。
- **对账 PID 探活**：executor 记录子进程 PID，对账时 `kill(pid,0)` 探活，孤儿仍在跑则不
  误标 interrupted/failed。

## Capabilities

### Modified Capabilities

- `decision`: 人工覆盖判定贯通全量 commit（G9）；conclude 目标类型由
  `decision_rules.yaml` 单一驱动（P2-1）。
- `reporting`: 投影数据源改为结构化 `state.json`（回退 checkpoint），平台只消费 JSON（G11）。
- `reliability`: 周期终态可区分与折叠（P0-2）；周期中断续跑 + `cycle.json` 僵尸收敛（P0-1）；
  任务关联键 cycle_id 唯一化（P2-2/P2-3）；CLI 直启登记容错（P2-6）；对账 PID 探活。
- `sync-execution`: rerun 单一线程 id；manual-scan 登记任务且 cycle_id 一致（P2-7）。
- `web-platform`: 工作台失败醒目标记；周期续跑入口；放弃周期内有效（P2-4）；首页 commit
  数从任务行取（P2-5）。

## Impact

- 引擎：`bsa/cli.py`、`bsa/scheduler/cycle.py`、`bsa/report/projection.py`、
  `bsa/graph/nodes.py`、`bsa/rules/conclude.py`、`bsa/rules/decision_rules.{py,yaml}`、
  `bsa/commands/{sync,rerun,task_reporter}.py`。
- 平台：`bsa_web/executor.py`、`bsa_web/db.py`、`bsa_web/api/operations.py`、
  `bsa_web/views/workbench.py`、`bsa_web/templates/workbench.html`。
- 数据：`tasks` 表新增 `commits`、`pid` 列（幂等迁移）；周期目录新增 `state.json`。
- 测试：新增约 28 个测试覆盖上述行为；全量 888 passed + ruff 通过。
