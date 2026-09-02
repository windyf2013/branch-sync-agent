# web-platform Specification Delta

## MODIFIED Requirements

### Requirement: 人工项处理

平台提供失败现场查看与人工决策能力，失败/停批任务可经受限 WebSSH 在线处理。

#### Scenario: 查看失败现场

- **当** 存在停批/失败分支
- **则** 展示冲突现场、编译错误、Agent 修复记录（来自投影与日志文件）

#### Scenario: WebSSH 在线处理

- **当** 任务为失败/停批且有 worktree 现场
- **则** 任务详情页提供受限 WebSSH 终端，映射到该任务 worktree
- **且** 终端仅限该 worktree 目录、禁 sudo、禁出目录（三层防御 + 部署约束）
- **且** 仅"有现场且失败/停批"的必要任务开放；ManualReview 待确认项不走 SSH
- **且** 终端操作审计（会话级必记 + 命令脱敏记录）

#### Scenario: 改判定

- **当** 操作者修改某 commit 的 is_bug_fix / risk 判定
- **则** 平台经 V1 CLI 写入 judgments.json（人工覆盖优先）
- **且** 生效于下一次决策执行（cron 周期或重跑重判），不追溯改写已冻结的 decisions.json
- **且** 提供"立即重跑"入口让新判定尽快生效（本变更落地：override 成功后前端渲染"立即重跑该分支"入口，复用 rerun）
- **且** 改判定行为记录操作日志（操作人/时间/sha/新值）

#### Scenario: 按成因给对 override 控件

- **当** 操作者对 ManualReview 项改判定
- **则** 平台按该结论的 `cause` 字段精确渲染 override 控件：
  - `cause=pending` → 只给「标记 bug fix」（`is_bug_fix=true`）
  - `cause∈{severity_gate, fix_missing}` → 只给「风险」下拉（risk）
  - 其余成因（`function_renamed`/`similarity_gray`/`symbols_missing`/`unknown_branch_type`）→ 不显示 override 控件，仅提供「确认继续 / 放弃」
- **且** 不再提供 `is_bug_fix=false` 入口（流程无法阻止同步，「排除」一律走「放弃」）

#### Scenario: 清除 override

- **当** 操作者清除某 commit 的人工覆盖
- **则** 平台调用 `bsa override <sha> --clear` 删除该 sha 及 `fp:` 键，回到「未判定」
- **且** 清除后提供「立即重跑该分支」入口让 LLM 重判生效
- **且** 清除行为记录操作日志

#### Scenario: 确认继续与放弃

- **当** 操作者确认某 ManualReview 项允许同步
- **则** 复用直同步路径（指定 sha 强制同步，单个 commit）执行
- **且** 操作者可放弃某 commit/分支，标记并记录操作日志

#### Scenario: decision 层 ManualReview 操作可达

- **当** 周期存在 ManualReview 项、但对应目标分支无 `branch_results`（决策层判定、未进入执行）
- **则** 任务详情页 `/task/{cycle}/{target}` 不再 404，而是降级渲染「仅人工项」视图（无 worktree/build/patch，但有人工项操作区）
- **且** 工作台「待确认」KPI 可点，落到可操作页
- **且** 周期概览 ManualReview 项可点，落到可操作页
- **且** 操作者可在该页完成「确认继续 / 改判定 / 放弃」全套人工项操作

#### Scenario: 确认继续语义透明化

- **当** 操作者对 ManualReview 项点击「确认继续」
- **则** 平台在操作前明确提示「将发起一条独立手动同步任务（指定 sha 直同步），不在原周期内收敛」
- **且** 提交后走现有直同步路径（`bsa sync <target> --sha <sha>`），与原 cron 周期分裂的关系对用户透明

### Requirement: 实时工作台首页

首页为任务中心，按分支粒度分区展示最近一个周期内的所有任务，所有操作在任务详情页完成。

#### Scenario: 工作台待办与总览

- **当** 操作者/查看者进入首页
- **则** 首页分两区块：自动（Agent cron 周期展开的分支任务）与手动（用户发起的分支任务）
- **且** 任务按分支粒度展示为面板列表，含状态徽章（进行中/排队/成功/失败/停批/已放弃）
- **且** 仅列出最近一个周期窗口内的任务；超出窗口的历史收敛进历史报告页
- **且** 展示 Agent 运行状态（V1 周期运行中/失败醒目标记）
- **且** 运行中周期仅显示"进行中"徽章，不展示未完成周期的详情
- **且** 周期终态为 `FAILED`/`PARTIAL` 时显示"周期失败"醒目标记（不再一律"完成"）
- **且** 周期任务为 interrupted 时，操作者可见"续跑"入口（同一 cycle_id 重跑）
- **且** 手动任务 commit 数从任务行读取（sync 用 shas 长度 / rerun 用引擎落库值），
  不再为每个任务 spawn `bsa report` 子进程（保障首页 <3 秒）

#### Scenario: 页面状态实时性

- **当** 用户长时间停留后操作
- **则** 每次请求从 V1 投影读取最新状态，不缓存过期数据

#### Scenario: 任务详情页承载操作

- **当** 用户点击某分支任务
- **则** 跳转任务详情页，所有操作在该页完成：查看现场证据、WebSSH（失败/停批且现场存在）、重跑、推送（SUCCESS）、放弃/恢复、人工项处理
- **且** 任务详情页复用现有分支/commit 详情渲染，patch/编译日志按需加载

#### Scenario: 达成度醒目标记

- **当** 周期 `status=SUCCESS` 但存在待人工项（`review_pending > 0`）
- **则** period-bar 与状态徽章显示「完成 · N 待人工」，不只显示纯「完成」
- **且** 「N」与「待确认」KPI 数值一致，可直接点击进入处理

## ADDED Requirements

### Requirement: 周期拓扑对照

工作台与周期概览展示完整源/目标清单（含零检出源），区分「零检出」与「漏扫」。

#### Scenario: 拓扑对照展示

- **当** 查看当前周期
- **则** 展示完整源分支全集与目标分支全集（优先 `payload.sources`/`payload.targets`）
- **且** 对零检出源标注「本期无新 commit（正常）」，与检出过 commit 的源视觉区分
- **且** `sources`/`targets` 字段缺失时，降级从 `detected_commits`/`decisions` 推导，并标注「拓扑字段缺失，仅展示检出过 commit 的分支」
