# reliability Specification (delta)

## MODIFIED Requirements

### Requirement: 周期级失败语义

单分支失败不拖垮整周期，周期末必出报告。

#### Scenario 单分支失败不拖垮整周期。

- **当** 某目标分支内某 commit 失败/停批（fail-fast 或 forbidden_paths 转人工）
- **则** 分支内后续 commits 按决策 3 三层方案逐个判断关联性，相关停、无关继续（不整批一刀切）
- **且** 其他目标分支继续各自的批次（跨分支隔离）
- **且** 无论成败，周期结束必出报告 + 邮件（含失败明细、Failure Stage、Agent Attempts、Recommendation）

#### Scenario: 周期终态可区分（P0-2）

- **当** 周期结束
- **则** report 节点按 branch_results 收敛出可区分终态：全成功 `SUCCESS` / 部分失败
  `PARTIAL` / 有错误或失败 `FAILED`
- **且** 该终态写入 cycle.json 与 state.json
- **且** 平台任务登记按 fold 规则折叠：`SUCCESS` 记 succeeded，其余记 failed，供醒目标记

## ADDED Requirements

### Requirement: 周期中断续跑

周期执行被中断后，现场保留且可经平台续跑。

#### Scenario: 中断可续跑 + 僵尸收敛（P0-1）

- **当** executor 对账/兜底把 running 的 cycle 任务标 interrupted/failed
- **则** 同步把该周期 `cycle.json` 的僵尸 running 收敛为终态（interrupted/FAILED）
- **且** 平台对 interrupted 周期提供"续跑"入口，以同一 cycle_id 重排入 `run-cycle`，
  checkpoint 自动 resume，不自动重放

### Requirement: 任务关联键唯一化

任务行 cycle_id 与 checkpoint 线程 id 全程唯一且一致。

#### Scenario: cycle_id 运行期即知（P2-2/P2-3）

- **当** executor 认领 sync / fresh-rerun 任务
- **则** executor 预生成 cycle_id 落库并注入环境变量（`BSA_MANUAL_CYCLE_ID`）
- **且** rerun 只生成一个线程 id，register_start 与实际 checkpoint 线程一致
- **且** retained 续跑沿用来源周期 cycle_id 定位现场，另起 rerun 线程

### Requirement: CLI 直启登记容错

引擎 CLI 直启登记不因并发冲突崩溃。

#### Scenario: 同 target 忙碌友好退出（P2-6）

- **当** CLI 直启 sync/rerun/run-cycle 且该 target/周期已有活动任务
- **则** `register_start` 撞唯一索引返回 None，CLI 打印"忙碌"提示并以非零码退出
- **且** 不抛未捕获的 `IntegrityError`

### Requirement: 对账 PID 探活

executor 重启对账以子进程存活状态区分"孤儿仍在跑"与"真死"。

#### Scenario: 孤儿仍在跑不误标

- **当** executor 重启对账遇到 running 任务
- **则** 若记录的子进程 PID 仍存活（`kill(pid,0)`），跳过标记，等待其 register_finish 自愈
- **且** 子进程已死才按有无 checkpoint 进度标 interrupted/failed
