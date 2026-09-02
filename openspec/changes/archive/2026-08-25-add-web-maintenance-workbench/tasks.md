# Tasks: add-web-maintenance-workbench

## 阶段 A：V1 基础设施（依赖基础）

### A1 全局 flock 基础设施

- [x] 新增 `src/bsa/executor/lock.py`：`flock_acquire(path, timeout)` 上下文管理器（`fcntl.flock`，阻塞+超时）
- [x] 在 `src/bsa/scheduler/cycle.py` 的 `_execute` 入口包全局锁（`logs/bsa.lock`）
- [x] 测试 `tests/test_lock.py`：互斥、超时、进程退出自动释放
- [x] 验证：`uv run pytest tests/test_lock.py && uv run ruff check src/bsa/executor/lock.py`
- [x] 回滚：删除 `src/bsa/executor/lock.py` 与 cycle.py 引用

### A2 周期运行中标记

- [x] 修改 `src/bsa/scheduler/cycle.py`：周期开始时立即写 `cycle.json`（`status=running` + started_at），结束覆盖终态
- [x] 确认 `list_cycle_records` 兼容运行中记录
- [x] 测试：运行中/完成/失败三种 cycle.json 时序
- [x] 验证：`uv run pytest tests/test_scheduler_cycle.py`
- [x] 回滚：还原 cycle.json 写入点

### A3 state.sqlite3 WAL

- [x] 修改 `src/bsa/graph/workflow.py` `open_checkpointer`：`PRAGMA journal_mode=WAL` + `busy_timeout`
- [x] 测试：并发写读不锁死
- [x] 验证：`uv run pytest tests/test_workflow.py tests/test_snapshot.py`

### A4 投影 CLI（`bsa report <cycle> --json`）

- [x] 新增 `src/bsa/report/projection.py`：从 checkpoint 读 thread 最终 state，`model_dump()` 结构化输出
- [x] 运行中周期仅输出 `{status: "running"}`（读 cycle.json 判定）
- [x] CLI 注册 `report` 子命令（`src/bsa/cli.py`）
- [x] 测试 `tests/test_projection.py`：完成/运行中/不存在周期、字段完整性（branch_results/decisions/action_required）
- [x] 验证：`uv run pytest tests/test_projection.py && uv run ruff check src/bsa/`
- [x] 回滚：删除 projection.py 与 CLI 子命令

### A5 override CLI（`bsa override`）

- [x] 新增 `src/bsa/commands/override.py`：写 `logs/judgments.json`（持全局锁）
- [x] 支持 `--is-bug-fix` / `--risk` 参数
- [x] CLI 注册 `override` 子命令
- [x] 测试 `tests/test_override.py`：写入格式、持锁、judgments 文件可被 SyncDecisionAgent 读取
- [x] 验证：`uv run pytest tests/test_override.py`

### A6 分支级同步 CLI（`bsa sync`）

- [x] 新增 `src/bsa/commands/sync.py`：
  - `--sha` 模式：构造单 commit 批次，走单 target 子图
  - 源+目标模式：检测窗口 commit → `conclude_pair` 重判 → 只同步 NeedSync
- [x] 单 target 子图：复用 `prepare_worktree/cherry_pick/build/fix_build/generate_patch` 节点（`src/bsa/graph/single_target.py`）
- [x] 结果写 `branch_results[target]`，独立 `cycle_id`（`manual-*`）
- [x] CLI 注册 `sync` 子命令
- [x] 测试 `tests/test_sync_command.py`：--sha 直同步、源+目标决策、结论非 NeedSync 停止
- [x] 验证：`uv run pytest tests/test_sync_command.py`

### A7 分支级重跑 CLI（`bsa rerun`）

- [x] 新增 `src/bsa/commands/rerun.py`：
  - 默认：沿用当前活 worktree，按 git 状态重入（未完成 cherry-pick 继续、已应用跳过），全量 build 验证后更新 patch
  - `--fresh`：重建现场，先重判结论（AlreadyIncluded/OutOfScope 停止）
  - 现场 `git status --porcelain` 非空拦截
- [x] CLI 注册 `rerun` 子命令
- [x] 测试 `tests/test_rerun_command.py`：保留现场续跑、--fresh 重判、dirty 拦截
- [x] 验证：`uv run pytest tests/test_rerun_command.py`

## 阶段 B：平台骨架

### B1 平台工程骨架

- [x] 新增 `src/bsa_web/` 包：`app.py`（FastAPI 装配）、`settings.py`（env 配置）、`db.py`（SQLite + schema_version 迁移）
- [x] `pyproject.toml` 注册 `bsa-web` 入口（uvicorn 启动）
- [x] 测试：`tests/web/test_app.py` 健康检查端点
- [x] 验证：`uv run pytest tests/web/ && uv run ruff check src/bsa_web/`

### B2 认证、会话与角色

- [x] `auth.py`：`Authenticator` 接口 + env 静态实现（`BSA_USERS`：用户名/密码哈希/角色）；服务端会话表 + CSRF token
- [x] `rbac.py`：操作者角色依赖（校验敏感操作）
- [x] 登录/登出/会话过期中间件；cookie HttpOnly/Secure/SameSite
- [x] 测试 `tests/web/test_auth.py`：登录、未登录重定向、会话过期、CSRF 拒绝、角色校验（查看者操作被拒）
- [x] 验证：`uv run pytest tests/web/test_auth.py`

## 阶段 C：平台读取与展示

### C1 投影封装与工作台首页

- [x] `projection.py`：封装 `bsa report <cycle> --json` 子进程调用 + 解析
- [x] 工作台首页路由：待办区（可推送/ManualReview/失败停批）、当前周期实时总览、快速操作、Agent 状态（运行中/失败标记）
- [x] 运行中周期仅显示"进行中"徽章
- [x] 测试 `tests/web/test_workbench.py`：首页渲染、运行中周期处理、实时读取（不缓存）
- [x] 验证：`uv run pytest tests/web/test_workbench.py`

### C2 历史报告与详情页

- [x] 历史报告页：按日期列周期，三区块 + 结论筛选
- [x] 详情页：`/cycle/<id>/target/<target>` 与 `/cycle/<id>/commit/<sha>`（patch/diff/编译日志/Agent 修复记录），按需加载
- [x] 测试 `tests/web/test_detail_pages.py`
- [x] 验证：`uv run pytest tests/web/test_detail_pages.py`

## 阶段 D：平台操作

### D1 异步任务执行 runner

- [x] `runner.py`：任务状态机 `queued→running→succeeded/failed`、子进程管理（超时+清理）、同 target 并发拒绝、排队显示
- [x] 任务表 `tasks` schema 落地
- [x] 测试 `tests/web/test_runner.py`：状态机、超时、并发拒绝、排队
- [x] 验证：`uv run pytest tests/web/test_runner.py`

### D2 触发同步与重跑 API

- [x] 触发同步端点（源+目标 / `--sha`）→ `bsa sync`；重跑端点 → `bsa rerun`
- [x] 任务进度轮询端点
- [x] 测试 `tests/web/test_operations.py`：触发、轮询、失败回显
- [x] 验证：`uv run pytest tests/web/test_operations.py`

### D3 人工项处理

- [x] 改判定端点 → `bsa override` + 操作日志
- [x] 确认继续端点 → 复用 `--sha` 直同步
- [x] 放弃端点 → 标记 + 操作日志
- [x] 测试 `tests/web/test_manual_review.py`
- [x] 验证：`uv run pytest tests/web/test_manual_review.py`

### D4 推送执行链

- [x] `push.py`：四道闸校验（SUCCESS/worktree 存在/非 forbidden_branches/status 干净）→ 确认框 → 受限 git push（`origin HEAD:<target>`，禁 `--force`）→ 结果回显
- [x] 受限推送执行器（白名单仅 push，参数强校验）
- [x] 审计写入（操作人/时间/分支/commit 范围/结果）
- [x] 测试 `tests/web/test_push.py`：四道闸各失败分支、dirty 拦截、push 成功/非 fast-forward 失败、审计记录
- [x] 验证：`uv run pytest tests/web/test_push.py`

### D5 操作日志与审计

- [x] `audit.py`：append-only 写入（应用内无更新/删除入口）
- [x] 操作日志页
- [x] 测试 `tests/web/test_audit.py`
- [x] 验证：`uv run pytest tests/web/test_audit.py`

## 阶段 E：运维与收尾

### E1 数据保留与备份

- [x] L2 体积日志清理 job（默认 30 天可配）
- [x] 每日备份 job（平台 DB + state.sqlite3 + judgments.json + cycle.json → `logs/backup/`，保留 7 份）
- [x] 测试 `tests/web/test_retention.py`
- [x] 验证：`uv run pytest tests/web/test_retention.py`

### E2 可观测性与部署配置

- [x] 健康检查端点完善 + 结构化请求日志（logrotate 配置）
- [x] systemd 单元 + nginx 反代 + env 示例（`.env.example`）+ TLS 配置文档
- [x] 验证：`uv run ruff check src/bsa_web/ && uv run pytest tests/web/`

### E3 端到端验证

- [x] 全量测试：`uv run pytest && uv run ruff check src/`
- [x] 手动链路冒烟：登录→工作台→触发同步→轮询→（mock）推送→审计页可见
- [x] 代码审查确认：平台无自动推送逻辑、无 `--force`
