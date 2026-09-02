# reliability Specification

## Purpose
TBD - created by archiving change add-branch-sync-agent. Update Purpose after archive.

## Requirements

### Requirement: 节点级容错

node 异常由 wrapper 捕获写入 state，不崩溃。
#### Scenario 单个 node 出错不崩溃整个 workflow。
- **当** 任何 node 抛异常
- **则** node_wrapper 捕获，异常写入 state.errors，不冒泡出 graph
- **且** workflow 继续执行到报告节点
- **且** 失败节点状态记录可审计

### Requirement: LLM 容错与降级

LLM 失败重试后降级安全默认，确定性流程仍可运行。
#### Scenario LLM 调用失败或不可用。
- **当** LLM 调用超时/失败
- **则** 指数退避重试（max_retries 可配置）
- **且** 重试仍失败则降级安全默认：sync_decision → ManualReview、conflict → 转人工、build → 归因无法修复
- **且** LLM 完全不可用时确定性流程（检测/判定/报告）仍可运行，降级全人工审核

### Requirement: 外部命令容错

外部命令超时遵循 timeout≠未发生，先查真实状态。
#### Scenario git/docker 命令超时或失败。
- **当** 外部命令超时
- **则** 遵循"timeout ≠ 操作一定没有发生"，先查真实状态（git 状态/产物/容器状态）再判定，不盲目重试
- **且** fetch 失败指数退避重试（次数/间隔可配置）
- **且** 编译超时三查后判定（产物/日志标志/容器状态）

### Requirement: checkpoint 与恢复

周期中断通过 SQLite checkpoint 恢复。
#### Scenario 周期中断恢复。
- **当** 进程崩溃/退出后重启
- **则** 通过 SQLite checkpoint（thread_id = cycle_id）恢复执行
- **且** 决策先行固化的批次（决策后 state）在恢复时直接沿用，不重算
- **且** 恢复后以 git 真实状态为准，不盲目重跑已应用操作

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

### Requirement: 异常分层

异常按 BsaError 分层处理。
#### Scenario 异常分类处理。
- **当** 异常发生
- **则** 按 BsaError 分层：DomainError（业务状态走状态转移，不抛异常）/ InfrastructureError（节点边界捕获写 state.errors）/ SafetyViolation（强制拒绝转人工）

### Requirement: 全局执行串行化

所有 V1 执行（cron 周期、平台触发的 sync/rerun、worktree 清理、override）通过全局文件锁串行化。

#### Scenario: 并发执行串行

- **当** 多个 V1 执行并发请求
- **则** 全局 `flock` 保证同一时刻仅一个执行持有锁，其余阻塞等待
- **且** 锁由进程持有，进程异常退出自动释放，不产生死锁
- **且** 推送操作也在持锁下执行，避免与周期清理删除 worktree 竞争

### Requirement: 周期运行中标记

周期开始即写运行中标记，结束后覆盖为终态。

#### Scenario: 运行中可见

- **当** 周期开始执行
- **则** 立即写入 `cycle.json`（status=running），供平台感知 Agent 正在运行
- **且** 周期结束/失败时覆盖为终态（含报告/邮件状态），不影响原有周期记录语义

### Requirement: checkpoint 库并发读安全

V1 state.sqlite3 开启 WAL 模式，避免平台投影读取与周期写入撞锁。

#### Scenario: 投影并发读

- **当** 平台经投影 CLI 读取 state.sqlite3 而周期正在写入
- **则** 读取不触发 "database is locked"，读到一致快照

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
