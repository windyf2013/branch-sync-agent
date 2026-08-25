# Design: add-web-maintenance-workbench

## 架构总览

两层结构，平台与 V1 Agent 解耦但共享同一仓库与运行环境：

```
浏览器
  │ HTTPS
  ▼
nginx（反代 + TLS 终结 + 登录限速）
  │
  ▼
平台进程（FastAPI + Jinja2 SSR，独立 systemd 单元）
  ├─ 平台本地 SQLite（会话 / 操作日志 / 任务注册表）
  ├─ V1 投影 CLI 读取（bsa report <cycle> --json）→ 只读
  └─ V1 执行 CLI 写入（bsa sync/rerun/override）→ 子进程，持全局 flock
          │
          ▼
V1 Agent（既有 cron + CLI + state.sqlite3 + worktree）
```

- 平台进程崩溃不影响 V1 cron 运行；V1 数据不被平台直接写。
- 平台对 V1 的一切读取经投影 CLI、写入经 V1 CLI 子进程。

## V1 侧扩展

### CLI 新命令（`src/bsa/cli.py` 扩展）

| 命令 | 行为 |
|---|---|
| `bsa sync <src> <target> [--sha <sha>...]` | 单目标分支同步；有 `--sha` 走直同步，否则源+目标走完整决策链路 |
| `bsa rerun <target> [--cycle <id>] [--fresh]` | 分支级重跑；默认沿用当前活 worktree 按 git 状态重入；`--fresh` 重建+重判 |
| `bsa override <sha> [--is-bug-fix] [--risk]` | 写 judgments.json（人工覆盖优先），持锁 |
| `bsa report <cycle> --json` | 投影：输出周期结构化状态 JSON；运行中只输出 `{status: running}` |

### 分支级执行（核心重构）

现有 graph 为整周期执行（thread_id = cycle_id）。分支级 sync/rerun 需支持**单 target 子执行**：

- **方案**：新增"分支子流程"入口，复用既有节点函数（`prepare_worktree` / `cherry_pick` / `resolve_conflict` / `build` / `fix_build` / `generate_patch`）构造**单 target 的 LangGraph 子图**，或复用主图但初始 state 仅含单个 target 批次。
- 推荐：**构造单 target 子图**（不重走 detect/sync_decision 主链），`bsa sync --sha` 时手工构造该 commit 的批次；`bsa rerun --fresh` 时先对该 target 重判结论（复用 `conclude_pair`，target 快照取自当前远端），NeedSync 才继续。
- 结果写回既有 `branch_results[target]` 结构，供投影 CLI 读取；手动任务用独立 `cycle_id`（如 `manual-<时间戳>`）命名 checkpoint thread，不与每日周期冲突。

### 全局 flock

- 加文件锁 `logs/bsa.lock`（`fcntl.flock`），包住：`run-cycle` 整周期执行、`bsa sync`、`bsa rerun`、`bsa override`、worktree 清理、平台推送执行。
- 锁由进程持有，进程退出自动释放；阻塞等待 + 超时（如 30min）上限。
- 推送/override 也持锁，避免与周期清理删 worktree、judgments 全量写竞争。

### 运行中标记

- `run_cycle` 开始即写 `cycle.json`（`status=running` + started_at），结束/失败覆盖为终态（保留现有字段语义）。`list_cycle_records` 兼容。

### WAL

- `open_checkpointer` 打开 `state.sqlite3` 时 `PRAGMA journal_mode=WAL` + `busy_timeout`，支持投影并发读。

### 投影 CLI 输出结构

`bsa report <cycle> --json` 输出（基于 `TaskState` 领域模型 `model_dump()`）：

```json
{
  "cycle_id": "...",
  "status": "COMPLETED",          // 或 {status: "running"}
  "scan_window": ["...", "..."],
  "detected_commits": [...],       // CommitInfo
  "decisions": {sha: {target: Conclusion4}},
  "branch_results": {
    "target": {
      "status": "SUCCESS|PARTIAL|FAILED|MANUAL",
      "worktree_path": "...",
      "patch_path": "...",
      "commits": [{"sha", "cherry_pick", "conflict_resolution", "build": {model: BuildOutcome}}]
    }
  },
  "action_required": [...]
}
```

- 读取路径：从 checkpoint 取该 thread 最终 state；也可直接读 `decisions.json` + 周期目录，但 commit 详情/build 结果必须在 checkpoint，故走 checkpoint 读取（只读连接）。

## 平台侧设计

### 模块划分（`src/bsa_web/`）

| 模块 | 职责 |
|---|---|
| `app.py` | FastAPI 应用装配、中间件、路由注册 |
| `auth.py` | 认证后端接口 + 简单账号实现 + 会话（服务端会话表） |
| `rbac.py` | 角色校验依赖（操作者操作需 role 校验） |
| `views/` | Jinja2 路由：工作台首页、历史报告、详情、操作日志、设置 |
| `api/` | 任务触发/轮询、推送执行等 JSON 端点（供 JS 轮询） |
| `projection.py` | 封装 `bsa report <cycle> --json` 子进程调用 |
| `runner.py` | 异步任务执行：V1 CLI 子进程管理、状态机、排队、超时、结果捕获 |
| `push.py` | 推送执行链：四道闸校验 → 确认 → 受限 git push |
| `audit.py` | 操作日志写入（append-only） |
| `db.py` | 平台 SQLite（schema_version 迁移） |

### 平台 DB schema（`schema_version` 1）

- `sessions(id, user, role, created_at, expires_at, csrf_token)`
- `audit_log(id, ts, user, action, cycle_id, target, sha, detail_json, result)`
- `tasks(id, kind, user, cycle_id, target, shas, state, error, created_at, started_at, finished_at)`

### 任务执行（`runner.py`）

- 状态机 `queued→running→succeeded/failed`；`queued` 表示正在等 flock（周期运行中）。
- 子进程超时兜底 + 退出后清理（`Popen` + `communicate(timeout)`），不残留僵尸。
- 同 target 已有 running/queued 任务则拒绝重复触发。
- 进度：任务表状态轮询 + 可选 `run.log` tail。

### 推送执行链（`push.py`）

1. 读投影取 `branch_results[target]`：校验 status=SUCCESS、worktree 存在。
2. 读 `safety_rules.yaml` 的 `forbidden_branches`，校验 target 不在其中。
3. 持 flock 下执行 `git status --porcelain`，非空拒绝。
4. 确认框提交后，持 flock 执行受限 git：`git push origin HEAD:<target>`（`cwd=worktree`），禁 `--force`。
5. 结果回显 + 写 `audit_log`（含 commit 范围 = `branch_results[target].commits[].sha`）。

### 受限推送执行器

- 平台侧独立 executor（不复用 V1 `WhitelistExecutor`，其不含 push）；白名单仅 `["push"]`，参数强校验为 `origin HEAD:<target>` 形态，`target` 限定在"当前周期 SUCCESS 分支集合"内。

### 前端

- Jinja2 SSR 为主；少量 JS：任务轮询、状态刷新、推送确认框。
- 详情独立路由（`/cycle/<id>/target/<target>`、`/cycle/<id>/commit/<sha>`），patch/日志按需加载。
- 首页/详情每次请求实时经投影读取，不缓存。

### 认证

- `BSA_USERS` env 静态配置：`用户名:bcrypt哈希:角色`。认证后端抽象接口 `Authenticator`，LDAP/SSO 就绪后换实现。
- CSRF：表单 POST token 校验；会话 HttpOnly/Secure/SameSite=Lax；登录限速由 nginx。

### 数据保留

- L1 审计永久（平台库只增不删；V1 cycle.json/decisions/audit/patch 不动）。
- L2 体积日志清理：平台每日 job 清理超过 N 天的 `build.log`/`run.log`（默认 30 天，env 可配）。
- L3 worktree 由 V1 cron 清理（现状不变）。

### 备份

- 每日 cron job：打包平台 DB + `state.sqlite3` + `judgments.json` + `cycle.json` 至 `logs/backup/`，保留 7 份。

### 部署

- 平台独立 systemd 单元（`bsa-web`），nginx 反代 + TLS；配置全走 env。
- 平台监听端口 **8888**（env `BSA_WEB_PORT` 可配），nginx 对外 443 → 反代到 127.0.0.1:8888；不直接用特权端口。
- 平台 DB `schema_version` 迁移；V1 cron 不动。

## 测试策略

- V1 新 CLI：`bsa sync/rerun/override/report --json` 单测（复用 `tests/fixtures/mini_repo.py`、fake executor）+ flock 并发测试 + 分支级重入测试。
- 平台：pytest + FastAPI TestClient——认证/角色/CSRF、任务状态机与排队、推送四道闸（mock git push）、审计写入、投影解析。
- ruff 全程。
