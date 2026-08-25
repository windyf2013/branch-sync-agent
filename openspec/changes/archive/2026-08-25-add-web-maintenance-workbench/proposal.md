# Proposal: add-web-maintenance-workbench

## 变更名称

`add-web-maintenance-workbench` — Branch Sync Agent V2 Web 分支维护工作台

## 背景

V1 已交付：每日检测 RCIOS 分支合入的 bug-fix commit，在独立 worktree 完成 cherry-pick、冲突解决、多型号编译验证、diff-patch 生成，并发送 HTML 报告邮件。V1 为纯 CLI + 邮件，无交互入口。

V2 将平台定位为**完整的分支维护工作台**：用户通过浏览器登录，集中查看、审阅、决策、执行合入同步与推送操作。邮件退化为"通知入口"，平台才是日常操作的地方。

能力边界（硬约束，贯穿 V1/V2）：**Agent 永不推送分支**。推送由"人"在平台触发，Agent 不自动推、平台不自动推。

## 目标与成功标准

1. 维护人员浏览器登录工作台，查看当前周期实时状态、历史报告、patch、编译日志。
2. 对需同步分支执行合入同步操作（触发同步、处理人工项、推送）。
3. 推送由人工触发，Agent/平台不自动推送；超期分支只读 + 重新同步。
4. 以商用交付标准要求安全、可靠性、可运维性（认证、授权、审计、备份、可观测性）。

## 核心需求

### FR1 用户认证与登录
- 浏览器访问平台，登录后使用。
- 初版简单账号过渡（env 配置 + 表单认证 + CSRF），认证层抽接口，后续接公司 LDAP/SSO。
- 会话保持（过期可配，默认 8h + 闲置超时）；未登录不可访问；退出登录。

### FR2 工作台首页（实时）+ 历史报告页
- **首页 = 实时工作台**：待办区（可推送 / ManualReview 待确认 / 失败停批重跑）、当前周期实时总览（各 target 状态徽章，可刷新）、快速操作（触发同步 / 重跑）、历史报告入口、Agent 状态（V1 周期失败醒目标记）。
- **历史报告页**：按日期只读查看历史报告，三区块（Action Required / 已成功同步 / 检测信息），按结论筛选。
- commit/分支详情：patch、diff、编译日志、Agent 修复记录，独立路由页面、按需加载。

### FR3 手动触发同步
- 默认：源分支 + 目标分支，走完整链路（检测→重判结论→同步 NeedSync）。
- 兼容：指定 commit SHA 直同步（`bsa sync <target> --sha <sha>`），跳过决策，仍走五道闸门。
- 异步执行：任务状态机 queued→running→succeeded/failed，轮询进度，结果回平台。

### FR4 处理人工项
- 查看失败现场（冲突、编译错误、Agent 修复记录）。
- 改判定（is_bug_fix / risk）：写 V1 `judgments.json`（已有"人工覆盖优先"钩子），重跑决策。
- 确认允许同步（ManualReview）：复用直同步路径强制同步。
- 重跑：`bsa rerun <target>`（保留现场续跑）/ `--fresh`（重建重同步，先重判结论）。
- 放弃：标记并记录操作日志。

### FR5 推送操作
- 仅**当前周期活 worktree + 状态 SUCCESS** 可推送。
- **严格四道闸**：①状态 SUCCESS ②worktree 存在 ③目标非 `forbidden_branches` ④`git status --porcelain` 干净。
- 二次确认框（目标分支、commit 范围、patch 摘要）→ `git push origin HEAD:<target>`，**禁 `--force`**。
- 结果回显；非 fast-forward 失败提示重新同步。
- 推送留痕（操作人、时间、分支、commit 范围、结果）。

### FR6 操作日志与审计
- 记录敏感操作：登录/登出、触发同步、重跑、改判定、确认继续、推送、放弃。
- 审计数据永久保留。

### FR7 数据保留（分级）
- L1 审计（cycle.json / decisions.json / report.html / audit·diff / patch / 操作日志）：永久。
- L2 体积日志（build log / run.log）：默认 30 天可配，每日清理。
- L3 worktree：仅当前周期，V1 cron 清理。

### FR8 导出（可选）
- 预留导出接口，初版可不含。

## 设计方向（已定框架决策）

1. **超期即弃**：worktree 只保留当前周期（V1 cleanup 行为不变）；超期分支只读查看 + 重新同步（`rerun --fresh`）。不做旧现场重建/重放。
2. **推送语义**：仅当前周期活 worktree + SUCCESS 可推；四道闸校验；`git push origin HEAD:<target>`；禁 `--force`（远端前进时干净失败，提示重同步）。
3. **续做语义**：`bsa rerun <target>` 默认沿用当前活 worktree、按 git 状态重入、全量 build 验证后更新 patch；`--fresh` 重建并先重判结论。
4. **并发模型**：全局 `flock` 串行化所有 V1 执行（cron / 平台触发 / 清理）；平台任务用独立 cycle_id 命名空间；同 target 并发触发拒绝。
5. **对接方式（方案 B，独立进程）**：读走投影 CLI（`bsa report <cycle> --json`），写经 V1 CLI 子进程（`bsa sync/rerun/override`）；平台本地库（会话/操作日志/任务注册表）用 V1 标识符逻辑关联，渲染时聚合 V1 状态 + 平台活动。
6. **实时性**：平台实时读状态不缓存；长闲置后操作前刷新。
7. **认证授权**：初版简单账号过渡，认证接口化；初版即分查看者/操作者两角色，每端点校验角色。
8. **商用基线**：CSRF、会话加固、登录限速、HTTPS 强制、密钥 env、每日备份（平台 DB + V1 DB + judgments/cycle.json）、健康检查、结构化日志 + logrotate、平台 DB schema_version 迁移、V1 `state.sqlite3` 开 WAL。

## 边界（V2 不做）

- 不自动推送（硬约束）；不实现 Agent 的冲突/编译自动修复（V1 职责）。
- 不做旧现场重建/重放；不做审批链；不做多仓库管理；不提供平台内文件编辑（人工处理走 SSH 改活 worktree）。

## V1 新增改动清单（框架引出的硬改动）

| 类别 | 改动 |
|---|---|
| CLI | 新增 `bsa sync <src> <target> [--sha ...]`、`bsa rerun <target> [--cycle] [--fresh]`、`bsa override <sha>`、`bsa report <cycle> --json` |
| 执行 | 全局 `flock`；分支级重入子图（单 target）；手动任务独立 cycle_id 命名空间 |
| 数据 | 平台写 `judgments.json`（已有钩子）；`state.sqlite3` 开 WAL |
| 推送 | 平台受限执行器白名单 `push`（仅 `origin HEAD:<target>`，禁 `--force`） |

## grill-me 决策记录

### G1 人工修改现场的语义（2026-08-24）

- **问题**：人工修改现场后是否要求先 commit 才能 rerun/推送？patch 只含已 commit 内容，未提交修改会丢失。
- **用户回答**：人工处理（无法判定 commit 被用户单独合入、冲突被用户自行解决）一律视为用户自己的合入 = 远端分支更新变化，与平台合入是两件事，平台无需特殊考虑。
- **结论**：
  - 平台不提供"在用户修改后的现场上继续"的路径。人工处理按远端更新对待，由正常决策链路（目标含相同 sha → `AlreadyIncluded`）自然吸收。
  - rerun 用途收窄为"重新同步"（无人工修改的失败重试 / 超期重同步）。
  - 保留廉价防御：rerun / 推送入口查 `git status --porcelain` 非空则拦截提示（防 Agent/平台自身残留，不承载"继续人工修改"语义）。

### G2 平台任务在 cron 持锁时的表现（2026-08-24）

- **问题**：全局 flock 串行化下，cron 运行中用户触发同步/重跑，平台子进程阻塞等锁，前端如何表现？
- **用户回答**：可以（采用推荐方案）。
- **结论**：任务状态机扩展为 `queued→running→succeeded/failed`；flock 阻塞等待（带超时上限，如 30min）；触发后立即显示"排队中（等待当前周期完成）"，轮询可见；超时后任务失败并提示重试。

### G3 运行中周期的展示与投影一致性（2026-08-24）

- **问题**：`cycle.json` 只在周期结束/失败时写，运行中周期无标记；运行中 checkpoint 是中间态。
- **用户回答**：对（采用推荐方案）。
- **结论**：
  - V1 周期开始时即写 `cycle.json`（`status=running`，结束时覆盖为终态）——新增 V1 改动项。
  - 投影 CLI 对运行中周期只返回 `{status: running}`，不提供详情。
  - 平台首页对运行中周期只显示"进行中"徽章；详情只对已完成周期保证一致。

### G4 改判定（override）的生效时机（2026-08-24）

- **问题**：judgments.json 写入后何时生效？是否追溯改写已冻结的 decisions.json？
- **用户回答**：对（采用推荐方案）。
- **结论**：override 写 `judgments.json`，生效于**下一次决策执行**（cron 周期或 `rerun --fresh` 的重判）；不追溯改写已冻结的 `decisions.json`（保持审计一致性）；平台改判定后提供"立即重跑"入口；审计留痕靠平台操作日志（谁/何时/sha/新值），不依赖 judgments 文件历史。

### G5 审计日志防篡改程度（2026-08-24）

- **问题**：商用标准下审计防篡改做到什么程度？
- **用户回答**：对（采用推荐方案）。
- **结论**：append-only（应用内无 UPDATE/DELETE 入口）+ 平台 DB 文件属主为专用服务账号（操作者无文件级写权限）+ 每日备份兜底；不做哈希链/签名链（威胁模型为操作者越权，哈希链防不了 DB 级篡改且属过度工程）。

### G6 初版账号与角色分配来源（2026-08-24）

- **问题**：简单账号过渡期账号与"查看者/操作者"角色从哪来？
- **用户回答**：局域网内部使用，env 静态配置即可。
- **结论**：`BSA_USERS` 环境变量静态配置 `用户名:密码哈希:角色`，重启生效；无管理 UI；LDAP 就绪后换认证后端、角色改从 LDAP 组映射。

### G7 推送与 override 纳入全局 flock（2026-08-24）

- **问题**：推送/override 若不持锁，可能与周期清理（删非当前周期 worktree）和 judgments 全量写竞争。
- **用户回答**：对（采用推荐方案）。
- **结论**：推送与 override 也纳入全局 flock；`bsa override` 内部持锁；平台推送在持锁下执行（秒级操作，串行成本可忽略）。

### G8 备份介质与 RPO（2026-08-24）

- **问题**：备份介质与恢复点目标。
- **用户回答**：可以（采用推荐方案）。
- **结论**：同机独立目录（`logs/backup/`），每日 cron 打包平台 DB + V1 `state.sqlite3` + `judgments.json` + `cycle.json` 为 tgz，保留最近 7 份滚动；RPO ≤ 1 天；worktree 与 patch 可再生，不备份。

## 参考文档

- `docs/v2-web-platform-requirements.md`（v0.3，完整需求与技术方案）
- 归档变更 `add-branch-sync-agent`（V1 规格：decision / detection / reliability / reporting / safety / sync-execution）
