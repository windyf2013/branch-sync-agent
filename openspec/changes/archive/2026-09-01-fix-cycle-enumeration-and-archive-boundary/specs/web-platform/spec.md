# web-platform Specification Delta

## MODIFIED Requirements

### Requirement: 历史报告与详情查看

平台提供按日期查看历史报告与 commit/分支详情的能力。

#### Scenario: 历史报告查看

- **当** 用户查看历史报告
- **则** 列出所有超期归档任务（手动 sync/rerun + cron 周期，统一来自 tasks 表）
- **且** 归档分界 = 最近完成周期启动时刻：超期前任务留在工作台，超期后收敛进历史
- **且** 最近完成周期（`latest_completed_cycle`）本身**不**被归档，只留在工作台——即便其 tasks 行的 `created_at` 早于 `cycle.json started_at`（进程边界固有 1 秒差）
- **且** 历史只做归档显示，不提供「待同步/已包含」等操作型结论筛选

#### Scenario: 详情展开

- **当** 用户展开某 commit/分支详情
- **则** 可查看 patch、diff、编译日志、Agent 修复记录、冲突解决记录
- **且** 详情独立路由页面，patch/编译日志按需加载（满足首页 <3 秒、10 用户并发）
