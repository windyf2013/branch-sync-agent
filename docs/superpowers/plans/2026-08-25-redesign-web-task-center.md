# 任务中心与新建同步（redesign-web-task-center）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** Web 平台重构为"B 新建同步（纯发起）/ A 任务中心（唯一状态与处理端）"，并为失败/停批任务提供受限 WebSSH（ttyd）在线处理。

**架构：** B 区引导式发起（源分支→`bsa commits` 候选多选→目标分支，用户手动=无需决策）并自动衔接进 A；A 区按分支任务面板展示最近一周期自动+手动任务，任务详情页聚合重跑/推送/放弃·恢复/人工项，失败任务经平台反代的 ttyd 受限终端（免二次认证）在线处理。

**技术栈：** Python 3.13 + uv；FastAPI + Jinja2 SSR；LangGraph（V1）；pydantic；pytest + ruff；ttyd（WebSSH，单二进制）。

**规格：** `openspec/changes/redesign-web-task-center/plan-ready.md`（源），`openspec/changes/redesign-web-task-center/specs/` 与 `design.md`。

## 项目规则

- 语言：人类可读文案中文；代码标识符、路径、命令、OpenSpec 标题保持原文。
- TDD 铁律：先写失败测试 → 确认失败 → 最小实现 → 确认通过 → commit。每个任务一个 commit。
- 测试：`uv run pytest`；静态：`uv run ruff check src/`（select E/F/I/UP/B，line-length 100）。
- 不破坏 V1 既有行为与五道闸门；推送禁 `--force`；无自动推送。
- 读经投影/CLI、写经 V1 CLI 子进程；WebSSH 例外（直接操作 worktree，受限+审计）。
- 平台端口 `BSA_WEB_PORT`（默认 8888）；路径/密钥走 env。
- WebSSH：ttyd 每会话实例，免二次认证（复用平台会话），会话级审计必记 + 原始日志可选本地落盘。
- commit 消息：`feat: ...` / `fix: ...` / `docs(openspec): ...`，中文说明。

---

## 任务 1：V1 `bsa commits <src>` 只读 CLI

**文件：**
- Create: `src/bsa/commands/commits.py`, `tests/test_commits_command.py`
- Modify: `src/bsa/cli.py`

**接口：**
- Produces: `bsa.commands.commits.list_candidate_commits(git, src, limit=50) -> list[dict]`（每项 `{"sha","message","committed_at"}`）；CLI `bsa commits <src> [--limit N]` 输出 JSON。
- Consumes: `bsa.git.service.GitService`（`branch_tip`、`log`）、`bsa.config.settings.load_settings`。

- [ ] **步骤 1：写失败测试**

```python
# tests/test_commits_command.py
import json
from bsa.commands.commits import list_candidate_commits
from bsa.executor import CompletedProcess, FakeExecutor


def _ok(stdout="", rc=0): return CompletedProcess(returncode=rc, stdout=stdout, stderr="")

def _fake_git(executor, repo):
    from bsa.git.service import GitService
    return GitService(executor, repo)


def test_lists_recent_commits(tmp_path):
    log_out = "abc123|fix the bug|2026-08-20T10:00:00+08:00\n" \
              "def456|chore docs|2026-08-19T09:00:00+08:00\n"
    ex = FakeExecutor([_ok("origin/x\nsha"), _ok(log_out)])
    commits = list_candidate_commits(_fake_git(ex, tmp_path), "x", limit=50)
    assert commits[0] == {"sha": "abc123", "message": "fix the bug", "committed_at": "2026-08-20T10:00:00+08:00"}
    assert len(commits) == 2


def test_limit_passed_to_git(tmp_path):
    ex = FakeExecutor([_ok("origin/x\nsha"), _ok("a|m|t\n")])
    list_candidate_commits(_fake_git(ex, tmp_path), "x", limit=10)
    args = ex.calls[1][0]
    assert args[1] == "log" and "10" in args  # --max-count 10


def test_branch_missing_raises(tmp_path):
    ex = FakeExecutor([_ok(rc=1), _ok(rc=1)])
    import pytest
    from bsa.executor import InfrastructureError
    with pytest.raises(InfrastructureError):
        list_candidate_commits(_fake_git(ex, tmp_path), "nope", limit=50)
```

- [ ] **步骤 2：运行确认失败**

Run: `uv run pytest tests/test_commits_command.py -v`
Expected: FAIL（`ModuleNotFoundError: bsa.commands.commits`）

- [ ] **步骤 3：最小实现**

```python
# src/bsa/commands/commits.py
import json
from typing import Any


def list_candidate_commits(git, src: str, limit: int = 50) -> list[dict[str, str]]:
    """源分支最近 N 条候选 commit（sha/message/committed_at），只读。"""
    ref, _ = git.branch_tip(src)
    fmt = "--format=%H|%s|%aI"
    result = git._run(["log", "-n", str(limit), fmt, ref],
                      error_msg=f"cannot list commits for {src}")
    commits: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            commits.append({"sha": parts[0], "message": parts[1], "committed_at": parts[2]})
    return commits
```

> 注意：`git._run` 是私有方法——实现时若不用私有，用 `git.executor.run` 自行判 rc 并抛 `InfrastructureError`。以最小且不碰私有的方式实现。

- [ ] **步骤 4：注册 CLI**

```python
# src/bsa/cli.py
commits_p = sub.add_parser("commits", help="列出源分支候选 commit（JSON）")
commits_p.add_argument("src", help="源分支")
commits_p.add_argument("--limit", type=int, default=50)
commits_p.set_defaults(handler=_cmd_commits)

def _cmd_commits(args):
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"配置错误: {exc}", file=sys.stderr); return 2
    from bsa.commands.commits import list_candidate_commits
    from bsa.graph.factory import build_graph_context
    ctx = build_graph_context(settings)
    try:
        commits = list_candidate_commits(ctx.git, args.src, limit=args.limit)
    except Exception as exc:
        print(f"候选 commit 加载失败: {exc}", file=sys.stderr); return 1
    print(json.dumps(commits, ensure_ascii=False, indent=2)); return 0
```

- [ ] **步骤 5：运行确认通过 + 提交**

Run: `uv run pytest tests/test_commits_command.py tests/test_cli_smoke.py -v && uv run ruff check src/bsa/`
Expected: PASS

Commit: `git add -A && git commit -m "feat: bsa commits 只读候选 commit CLI"`

## 任务 2：放弃/恢复

**文件：**
- Modify: `src/bsa_web/db.py`
- Create: `src/bsa_web/api/abandon.py`, `tests/web/test_abandon.py`
- Modify: `src/bsa_web/views/workbench.py`（任务中心生成时过滤）

**接口：**
- Produces: `abandons` 表（id/cycle_id/target/sha?/user/created_at，唯一 `(cycle_id,target,sha)`）；`bsa_web.api.abandon.router`：`POST /api/abandon`、`POST /api/restore`（require_operator + CSRF + 审计）；`bsa_web.db.is_abandoned(conn, cycle_id, target, sha=None) -> bool`、`abandoned_keys(conn, cycle_id) -> set[tuple]`。
- Consumes: `bsa_web.audit.record`。

- [ ] **步骤 1：写失败测试**

```python
# tests/web/test_abandon.py
def test_abandon_then_restore(tmp_path):
    app = _make_app(tmp_path); client = _client(app); _login(client)
    r = _post(client, "/api/abandon", {"cycle_id": "c1", "target": "release/2.4"})
    assert r.status_code == 200
    assert is_abandoned(app.state.db, "c1", "release/2.4") is True
    r = _post(client, "/api/restore", {"cycle_id": "c1", "target": "release/2.4"})
    assert r.status_code == 200
    assert is_abandoned(app.state.db, "c1", "release/2.4") is False


def test_abandon_unique_commit_level(tmp_path):
    # 同 (cycle,target,sha) 重复放弃幂等；不同 sha 各自独立
    ...

def test_abandon_requires_operator(tmp_path):
    # viewer → 403
    ...
```

（`_make_app`/`_login`/`_post` 复用 tests/web 既有 helper 模式，CSRF JSON body 传 `_csrf`。）

- [ ] **步骤 2：运行确认失败**

Run: `uv run pytest tests/web/test_abandon.py -v`
Expected: FAIL

- [ ] **步骤 3：实现**

```python
# src/bsa_web/db.py（init_db 后追加迁移）
conn.execute("""CREATE TABLE IF NOT EXISTS abandons(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id TEXT NOT NULL, target TEXT NOT NULL, sha TEXT,
  user TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(cycle_id, target, sha))""")
```

```python
# src/bsa_web/api/abandon.py
import sqlite3
import time
from datetime import datetime, timezone

def _iso(): return datetime.now(timezone.utc).isoformat(timespec="seconds")

def is_abandoned(db, cycle_id, target, sha=None):
    return db.execute("SELECT 1 FROM abandons WHERE cycle_id=? AND target=? AND sha IS ?",
                      (cycle_id, target, sha)).fetchone() is not None

def abandoned_keys(db, cycle_id) -> set[tuple[str, str | None]]:
    return {(r["target"], r["sha"]) for r in db.execute(
        "SELECT target, sha FROM abandons WHERE cycle_id=?", (cycle_id,))}
```

路由：`POST /api/abandon`（body cycle_id/target/sha?）INSERT OR IGNORE + 审计 action=abandon；`POST /api/restore` DELETE + 审计 action=restore；require_operator + CSRF；返回 `{"ok": True}`。

- [ ] **步骤 4：过滤联动 + 提交**

`views/workbench.py`：任务中心/待处理生成时用 `abandoned_keys` 过滤（branch 级 sha=None 匹配该分支所有；commit 级匹配具体 sha）；面板显示"已放弃"+ 恢复入口。

Run: `uv run pytest tests/web/test_abandon.py -v && uv run ruff check src/bsa_web/`
Expected: PASS

Commit: `git add -A && git commit -m "feat: 放弃/恢复（abandons 表 + API + 任务列表过滤联动）"`

## 任务 3：B 区新建同步 + 自动衔接

**文件：**
- Modify: `src/bsa_web/api/operations.py`（`GET /api/commits?src=`；`POST /api/sync` 支持 `shas`）
- Modify: `src/bsa_web/views/workbench.py`, `src/bsa_web/templates/workbench.html`（三步表单 + JS）
- Create: `tests/web/test_new_sync.py`

**接口：**
- Produces: `GET /api/commits?src=<branch>`（require_login）→ 子进程 `bsa commits <src>` 返回列表；`POST /api/sync` body 增 `shas: list[str]`（runner submit sync，`bsa sync <target> --sha ...` 直同步）。
- Consumes: `bsa_web.projection`（subprocess 模式）、`bsa_web.runner.TaskRunner`、`bsa_web.auth`。

- [ ] **步骤 1：写失败测试**

```python
# tests/web/test_new_sync.py
def test_commits_endpoint_returns_list(tmp_path, monkeypatch):
    monkeypatch.setattr("bsa_web.api.operations._load_commits", lambda src: [{"sha": "a", "message": "m", "committed_at": "t"}])
    client = _client(_make_app(tmp_path)); _login(client)
    r = client.get("/api/commits?src=br_x")
    assert r.status_code == 200 and r.json()[0]["sha"] == "a"

def test_commits_endpoint_failure_hints(tmp_path, monkeypatch):
    monkeypatch.setattr("bsa_web.api.operations._load_commits", lambda src: [])
    r = client.get("/api/commits?src=br_x")
    assert r.status_code == 200  # 空列表，前端提示加载失败/无候选

def test_sync_submit_with_shas(tmp_path, monkeypatch):
    # POST /api/sync {src,target,shas} → runner.submit 收到 shas，返回 task_id
    ...
```

- [ ] **步骤 2：运行确认失败** → FAIL

- [ ] **步骤 3：实现**

`operations.py`：
```python
def _load_commits(log_dir, src, limit=50) -> list[dict]:
    # 子进程 sys.executable -m bsa.cli commits <src> --limit N，注入 LOG_DIR=log_dir
    # 返回解析 JSON；失败返回 []
```

`workbench.html` 三步表单（JS）：
- 源分支 select onchange → fetch `/api/commits?src=...` → 渲染多选 commit 列表（sha 短 + message + 时间）
- 勾选计数；目标分支 select；提交 → `POST /api/sync {src,target,shas}` → 成功返回 task_id → `window.location = /task/<cycle>/<target>`（或刷新手动区块）
- 候选为空/加载失败 → 提示"候选 commit 加载失败/无候选"

`POST /api/sync`：body 增 `shas`（可选列表）；`runner.submit("sync", user, target, shas, src=src)`；直同步语义（`bsa sync <target> --sha ...`）。CSRF。

- [ ] **步骤 4：运行确认通过 + 提交**

Run: `uv run pytest tests/web/test_new_sync.py tests/web/test_operations.py -v && uv run ruff check src/bsa_web/`
Expected: PASS
Commit: `git add -A && git commit -m "feat: B 区引导式新建同步（源分支→commit 多选→目标分支）"`

## 任务 4：A 区任务中心 + 任务详情页聚合

**文件：**
- Modify: `src/bsa_web/views/workbench.py`, `src/bsa_web/templates/workbench.html`
- Create: `src/bsa_web/views/task_detail.py`, `src/bsa_web/templates/task_detail.html`, `tests/web/test_task_center.py`
- Modify: `src/bsa_web/app.py`（挂 task_detail 路由）

**接口：**
- Produces: `bsa_web.views.task_detail.router`（`/task/{cycle_id}/{target}`）；任务中心视图聚合 `auto_tasks` / `manual_tasks`。
- Consumes: `bsa_web.projection`、`bsa_web.db`（tasks/abandons）、`bsa_web.auth`。

- [ ] **步骤 1：写失败测试**

```python
# tests/web/test_task_center.py
def test_center_splits_auto_manual(tmp_path, monkeypatch):
    # monkeypatch projection: 最新周期展开为自动任务；tasks 表造手动任务
    # 断言首页含两区块标题与对应分支
    ...

def test_center_filters_abandoned(tmp_path, monkeypatch):
    # abandons 中分支不出现在待处理/可推送，显示已放弃徽章
    ...

def test_task_detail_shows_actions_by_status(tmp_path, monkeypatch):
    # SUCCESS → 推送键；FAILED+worktree → SSH+重跑；已放弃 → 恢复
    ...
```

- [ ] **步骤 2：运行确认失败** → FAIL

- [ ] **步骤 3：实现**

`workbench.py`：
- `auto_tasks`：最新周期投影 `branch_results` 展开（分支粒度，含 status/patch/commits/abandon 过滤）。
- `manual_tasks`：tasks 表（kind=sync/rerun）+ 对应 manual/rerun cycle 投影分支结果合并。
- 模板：两区块面板卡片（分支、状态徽章、commit 数、时间、已放弃标记）。

`task_detail.py`：`/task/{cycle_id}/{target}` 读投影 branch_results[target] + tasks 表状态 + abandons；复用 detail 渲染逻辑（patch/日志/Agent 修复记录）+ 操作按钮（重跑 POST /api/rerun、推送确认框、放弃/恢复、人工项、SSH 入口按状态显示）。

- [ ] **步骤 4：运行确认通过 + 提交**

Run: `uv run pytest tests/web/test_task_center.py tests/web/test_workbench.py tests/web/test_detail_pages.py -v && uv run ruff check src/bsa_web/`
Expected: PASS
Commit: `git add -A && git commit -m "feat: A 区任务中心（自动/手动分区）与任务详情页聚合操作"`

## 任务 5：WebSSH（ttyd，最大新项）

**文件：**
- Create: `src/bsa_web/ssh.py`, `tests/web/test_webssh.py`
- Create: `src/bsa_web/views/ssh.py` + `templates/ssh.html`
- Modify: `src/bsa_web/db.py`（ssh_sessions 表）、`src/bsa_web/app.py`（路由 + WS 反代）、`deploy/`（ttyd 部署说明）

**接口：**
- Produces: `POST /api/ssh/open`（require_operator，校验任务 FAILED/PARTIAL/MANUAL 且 worktree 存在）→ spawn `ttyd -o -p 0 -W -i 127.0.0.1 -- timeout 1800 bash -c "cd <worktree> && exec bash"`，读端口，存 `ssh_sessions(token→cycle/target/worktree/port/user)`，返回 token；`GET /ssh/<token>` 页面；WS `/ssh/ws/<token>` 反代到 `ws://127.0.0.1:<port>/`；`POST /api/ssh/close`。
- Consumes: `bsa_web.auth`（免二次认证=复用会话）、`bsa_web.audit`（会话级审计）、`bsa_web.db`。

- [ ] **步骤 1：确认 ttyd 就绪**

Run: `which ttyd || echo "需安装 ttyd（单二进制，apt install ttyd 或官方 release）"`
（若本机无 ttyd：测试用假 spawn（monkeypatch），真实冒烟在部署机验证。）

- [ ] **步骤 2：写失败测试**

```python
# tests/web/test_webssh.py
def test_ssh_open_only_for_failed_task_with_worktree(tmp_path, monkeypatch):
    # 任务非失败 → 403/400；失败且有 worktree → spawn（monkeypatch spawn）返回 token
    ...

def test_ssh_session_requires_login(tmp_path, monkeypatch):
    # 未登录访问 /ssh/<token> → 302
    ...

def test_ssh_audit_written(tmp_path, monkeypatch):
    # open/close 写审计（user/worktree/时长），命令原始日志可选落盘
    ...

def test_ssh_open_records_session(tmp_path, monkeypatch):
    # ssh_sessions 表存 token/port/worktree；close 清理
    ...
```

- [ ] **步骤 3：运行确认失败** → FAIL

- [ ] **步骤 4：实现**

`ssh.py`：
- `open_session(db, auth, cycle, target, worktree)`：spawn `ttyd -o -p 0 -W -i 127.0.0.1 -- timeout 1800 bash -c "cd <worktree> && exec bash"`（`-o` 断开即退出）；解析 stdout "Listening on port N" 拿端口；写 `ssh_sessions(token, cycle, target, worktree, port, user, created_at)`；审计 record(action=ssh_open)。
- `close_session`：kill 进程、清行、审计 action=ssh_close（时长）。
- WS 反代（app.py）：`/ssh/ws/<token>` 校验会话 + 未过期 → 用 `websockets` 客户端连接 ttyd 并双向转发。
- 免二次认证：页面/WS 均走平台会话。

`deploy/`：ttyd 安装与 systemd/nginx 说明（ttyd 仅本机监听，经平台反代暴露）。

- [ ] **步骤 5：运行确认通过 + 提交**

Run: `uv run pytest tests/web/test_webssh.py -v && uv run ruff check src/bsa_web/`
Expected: PASS（spawn 已 monkeypatch；真实 ttyd 在部署机冒烟）
Commit: `git add -A && git commit -m "feat: WebSSH 受限终端（ttyd，免二次认证，会话级审计）"`

## 任务 6：推送对手动 SUCCESS 开放 + 端到端

**文件：**
- Modify: `tests/web/test_push.py`（手动 SUCCESS 推送键）、`tests/web/test_task_center.py`
- 修改点视实现（推送键渲染已支持任何 SUCCESS 投影）

- [ ] **步骤 1：写失败测试**

```python
def test_push_button_on_manual_success(tmp_path, monkeypatch):
    # 手动（manual cycle）SUCCESS 分支在任务详情页有推送键；四道闸通过
    ...
```

- [ ] **步骤 2：运行确认失败** → FAIL（若推送键未渲染）

- [ ] **步骤 3：确认四道闸对 manual 投影生效**

核对 `bsa_web/push.py` 读 `branch_results[target].status` 与 worktree 存在——对 manual cycle 投影同样成立；补齐渲染与测试。

- [ ] **步骤 4：全量验证 + 端到端冒烟**

Run: `uv run pytest && uv run ruff check src/`
手动冒烟（部署机）：登录 → B 区选源加载 commit → 勾选 → 目标 → 发起 → 自动进 A 手动区块 → 打开任务详情 → 失败任务见 WebSSH（ttyd 终端可输入）→（mock）重跑/推送/放弃恢复。
代码审查确认：无自动推送、无 --force、WebSSH 受限（仅失败任务、仅 worktree）。

- [ ] **步骤 5：提交**

Commit: `git add -A && git commit -m "feat: 推送对手动 SUCCESS 开放 + 端到端验证"`

---

## 验证计划

- 单元/集成：`uv run pytest`（V1 `tests/` + 平台 `tests/web/`），覆盖 commits CLI / 放弃恢复 / 新建同步 / 任务中心 / 任务详情聚合 / WebSSH 授权鉴权审计 / 推送开放。
- 静态：`uv run ruff check src/`
- 手动：登录→B 区引导发起→自动进 A→任务详情→失败任务 WebSSH（ttyd）→（mock）重跑/推送/放弃恢复；代码审查确认无自动推送、无 --force、WebSSH 受限。

## 自检

- 每任务有失败测试→实现→通过→commit；无占位话术；文件/接口/验证命令明确。
- Source Coverage 覆盖 plan-ready 全部 14 个验收点。
- WebSSH 选型已确认（ttyd，G6）；任务 5 真实 ttyd 冒烟在部署机执行（本机无则 monkeypatch 测试）。
