# reliability Specification (delta)

## ADDED Requirements

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
