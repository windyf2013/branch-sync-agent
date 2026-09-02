# Design: redesign-web-task-center

## 架构总览

平台信息架构重构为"B 纯发起 / A 唯一处理端"：

```
B 新建同步（纯发起，无需决策）
  源分支 select → bsa commits <src> 加载候选 commit（sha+message+time 多选列表）
  → 用户勾选 → 目标分支 select → 发起
  → 后台确认 → 自动刷新 → 任务进 A 手动区块

A 任务中心（唯一状态与处理端，最近一个周期窗口）
  ├ 自动区块：Agent cron 周期 → 分支任务面板
  └ 手动区块：用户发起 → 分支任务面板
      点击任务 → 详情页：
        现场证据 / WebSSH（失败·停批且有现场）/ 重跑 / 推送(SUCCESS) / 放弃·恢复 / 人工项处理
```

## 任务模型

- **任务身份** = `(cycle_id, target_branch)`，分支粒度。
  - 自动任务：`cycle-<date>` 周期投影展开为多个分支任务。
  - 手动任务：`manual-*` / `rerun-*` cycle 投影；同时记录在平台 `tasks` 表（状态机）。
- **任务中心范围**：最近一个周期窗口内的自动 + 手动任务；超窗口收敛进历史页。
- **状态来源**：
  - 进行中/排队：周期 running 标记 或 `tasks` 表 queued/running。
  - 成功/失败/停批：投影 `branch_results[target].status`。
  - 已放弃：平台本地 `abandons` 表（分支/commit 粒度），任务列表过滤。
- **任务列表聚合**：手动任务 = `tasks` 表 join 手动 cycle 投影；自动任务 = 最新周期投影展开；合并排序展示。

## V1 侧改动

- 新增只读 CLI `bsa commits <src> [--limit N]`（默认 50）：
  - 来源 `git log -N --format=<sha|%s|%aI>`（origin/<src>），返回 sha/message/time JSON 行或结构化输出。
  - 只读，不写状态；平台子进程调用（读经 CLI 边界）。

## 平台侧改动

### B 区 · 新建同步

- 三步交互：源分支 select（branch.md RCIOS）→ 异步加载 `bsa commits` → commit 多选 → 目标分支 select → 提交。
- 提交调 `POST /api/sync`（复用 runner），参数 `{src, target, shas: [...]}`。
- 发起语义：用户手动 = 人工审核通过，直接同步勾选 commits（`bsa sync <target> --sha ...` 直同步）。
- 发起后：web 等 `POST` 返回 task_id → `location` 跳转到 A 区任务详情/自动刷新手动区块。

### A 区 · 任务中心

- 首页重构：自动/手动两区块，分支任务面板卡片列表。
- 面板字段：分支、状态徽章、commit 数、发起时间（手动）/周期（自动）、操作摘要（推送/待处理/已放弃）。
- 点击 → 任务详情页 `/task/<cycle_id>/<target>`：
  - 复用现有 `/cycle/{id}/target/{target}` 渲染逻辑 + 聚合操作按钮。
  - 操作：重跑（`POST /api/rerun`）、推送（`POST /api/push` 四道闸 + 确认框）、放弃/恢复、人工项（改判定/确认）、WebSSH 入口。
- 历史报告页/操作日志页保持独立，任务中心收敛窗口外的历史。

### WebSSH

- 技术：ttyd（成熟 WebSSH，`ttyd -p <port> --writable <shell>`）或自研 xterm.js + websocket + pty。
- 入口命令固定：`cd <worktree> && exec bash`；平台运行账号无 sudo 密码权限（部署约束）；shell 启动清 PATH 中 sudo。
- 授权：仅失败/停批且有 worktree 的任务详情页渲染 SSH 入口；连接需平台会话鉴权（复用 cookie → 短期 token 换 WebSSH 连接）。
- 审计：会话开始/结束写审计表（user/worktree/时长）；命令经脱敏后记录（过滤 `password=`/`token=`/私钥行），粒度可配。
- 进程生命周期：会话空闲超时自动关闭；不残留 pty。

### 放弃/恢复

- 平台 DB 新增 `abandons(id, cycle_id, target, sha?, user, created_at)`，唯一约束 `(cycle_id, target, sha)`（sha 可空=整分支）。
- A 区任务列表/待处理生成时 LEFT JOIN abandons 过滤已放弃项（显示"已放弃"徽章 + 恢复入口）。
- 恢复 = 删除 abandons 记录；操作审计。

### 推送

- 四道闸逻辑不变；推送键对自动 + 手动 SUCCESS 现场开放（`branch_results[target].status == SUCCESS` 且 worktree 存在校验通过）。
- 键在任务详情页。

## 数据流与边界

- 读经投影/CLI（`bsa report`、`bsa commits`），写经 V1 CLI 子进程（`bsa sync`/`rerun`/`override`），WebSSH 例外（直接操作 worktree，受限+审计）。
- 全局 flock 串行化不变；runner 任务状态机不变（queued→running→succeeded/failed + 部分唯一索引并发拒绝 + stale 恢复）。
- 审计 append-only；放弃/恢复写审计。

## 测试策略

- V1：`bsa commits <src>` 单测（sha/message/time、limit、失败容错、只读）。
- 平台：任务中心聚合（mock 投影+任务表）、B 区引导表单（commit 加载/多选/提交）、任务详情页操作聚合（重跑/推送/放弃/恢复/人工项）、WebSSH 授权（仅失败任务可见入口、鉴权、审计写）、放弃过滤联动。
- 复用现有 tests/web 基建（TestClient、fake projection、FakeExecutor）。
