# Tasks: redesign-web-task-center

## 阶段 A：V1 只读 CLI

### A1 `bsa commits <src>` 候选 commit 只读 CLI

- [x] 新增 `src/bsa/commands/commits.py`：`bsa commits <src> [--limit N]`（默认 50），来源 `git log`（origin/<src>），输出结构化 JSON（sha/message/committed_at 列表）
- [x] CLI 注册（`src/bsa/cli.py`）；只读，不写状态
- [x] 测试 `tests/test_commits_command.py`：返回列表结构、limit、src 不存在/失败容错（非零退出+错误信息）、只读
- [x] 验证：`uv run pytest tests/test_commits_command.py && uv run ruff check src/bsa/`
- [x] 回滚：删除 commands/commits.py 与 CLI 子命令

## 阶段 B：放弃/恢复

### B1 abandons 数据表与 API

- [x] `src/bsa_web/db.py` 新增 `abandons` 表（id/cycle_id/target/sha?/user/created_at），唯一约束 `(cycle_id, target, sha)`，schema_version 迁移
- [x] `src/bsa_web/api/abandon.py`：`POST /api/abandon`（{cycle_id,target,sha?}）、`POST /api/restore`（{cycle_id,target,sha?}），require_operator + CSRF，写审计
- [x] 测试 `tests/web/test_abandon.py`：放弃/恢复、唯一约束、重复放弃幂等、权限、审计
- [x] 验证：`uv run pytest tests/web/test_abandon.py`

### B2 任务列表放弃过滤联动

- [x] 任务中心列表/待处理生成时 LEFT JOIN abandons 过滤已放弃项（显示"已放弃"徽章 + 恢复入口）
- [x] 测试：放弃项不在待处理/可推送中，恢复后重新出现
- [x] 验证：`uv run pytest tests/web/`

## 阶段 C：B 区新建同步

### C1 引导式表单（源分支→commit 多选→目标分支）

- [x] `src/bsa_web/api/operations.py` 新增候选 commit 端点 `GET /api/commits?src=<branch>`（子进程调 `bsa commits <src>`，解析返回）
- [x] 工作台 B 区表单改三步交互：源分支 select → commit 多选列表（异步加载 `/api/commits`）→ 目标分支 select → 提交
- [x] 提交调 `POST /api/sync` 传 `{src, target, shas}`（复用 runner 直同步）；CSRF
- [x] 模板/JS：候选加载失败提示、勾选计数、禁用未选提交
- [x] 测试 `tests/web/test_new_sync.py`：commit 加载端点、提交参数、加载失败容错、权限
- [x] 验证：`uv run pytest tests/web/test_new_sync.py`

### C2 发起后自动衔接进 A 区

- [x] B 区提交成功后：前端等 `POST` 返回 task_id，自动跳转 A 区任务详情/刷新手动区块
- [x] 测试：提交成功返回 task_id 且前端行为断言（重定向位置）
- [x] 验证：`uv run pytest tests/web/`

## 阶段 D：A 区任务中心重构

### D1 任务中心分区与面板

- [x] `src/bsa_web/views/workbench.py` 重构首页：自动/手动两区块，分支任务面板列表
- [x] 任务聚合：手动 = tasks 表 + manual cycle 投影；自动 = 最新周期投影展开；合并排序（最近一个周期窗口）
- [x] 面板字段：分支、状态徽章、commit 数、发起时间/周期、已放弃标记
- [x] 模板 `workbench.html` 重构为面板卡片
- [x] 测试 `tests/web/test_task_center.py`：自动/手动聚合、窗口过滤、状态徽章、已放弃过滤
- [x] 验证：`uv run pytest tests/web/test_task_center.py`

### D2 任务详情页聚合操作

- [x] 新增/改造任务详情路由 `/task/<cycle_id>/<target>`：复用现有分支详情渲染 + 操作按钮
- [x] 聚合操作：重跑、推送（四道闸+确认框）、放弃/恢复、人工项（改判定/确认）、WebSSH 入口（按授权显示）
- [x] 测试：详情页各操作入口按状态/权限显示（SUCCESS→推送、FAILED+现场→SSH+重跑、已放弃→恢复）
- [x] 验证：`uv run pytest tests/web/`

## 阶段 E：WebSSH（最大新项，独立评审）

### E1 受限终端

- [x] 选型并接入 WebSSH：ttyd 或 xterm.js+websocket+pty（选型决定依赖）
- [x] 终端入口固定 `cd <worktree> && exec bash`；平台运行账号无 sudo 密码权限（部署约束）；shell 启动清 PATH 中 sudo
- [x] 授权：仅"有 worktree 且 FAILED/PARTIAL/MANUAL"任务渲染 SSH 入口；连接需平台会话鉴权（短期 token 换连接）
- [x] 审计：会话开始/结束写审计（user/worktree/时长）；命令脱敏记录（过滤 password=/token=/私钥行），粒度可配
- [x] 生命周期：空闲超时自动关闭；不残留 pty
- [x] 测试 `tests/web/test_webssh.py`：授权（非失败任务无入口）、鉴权拒绝、审计写、脱敏
- [x] 验证：`uv run pytest tests/web/test_webssh.py && uv run ruff check src/bsa_web/`

## 阶段 F：推送与收尾

### F1 推送对手动 SUCCESS 开放

- [x] 确认四道闸对 manual cycle 投影（`branch_results[target].status==SUCCESS` + worktree 存在）生效；推送键在手/自动 SUCCESS 任务详情页均渲染
- [x] 测试：手动同步 SUCCESS 任务的推送键可用（mock push 链）
- [x] 验证：`uv run pytest tests/web/test_push.py tests/web/test_task_center.py`

### F2 端到端验证

- [x] 全量测试：`uv run pytest && uv run ruff check src/`
- [x] 手动冒烟：登录 → B 区选源分支加载 commit → 勾选 → 目标 → 发起 → 自动进 A 手动区块 → 打开任务详情 → 失败任务见 WebSSH →（mock）重跑/推送/放弃恢复
- [x] 代码审查：无自动推送、无 --force、WebSSH 受限

## 备注

- WebSSH 组件选型（ttyd vs 自研）在 E1 实施前与用户确认。
- 任务中心窗口（一个周期）过滤逻辑与历史页衔接；操作日志页保持不变。
