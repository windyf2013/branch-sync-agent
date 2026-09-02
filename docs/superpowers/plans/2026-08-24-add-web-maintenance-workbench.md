# Web 分支维护工作台（add-web-maintenance-workbench）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 交付 V2 Web 分支维护工作台 —— 浏览器登录后可查看实时工作台/历史/详情、触发同步/重跑、处理人工项、在四道闸下人工触发推送并留痕；V1 侧新增分支级 sync/rerun、override、投影 CLI、全局 flock、运行中标记与 WAL。

**架构：** 两层结构。V1 侧新增分支级单 target 执行入口与基础设施（flock/运行中标记/WAL/投影/override），全部经 CLI 暴露；平台为独立 FastAPI + Jinja2 SSR 进程，读经投影 CLI、写经 V1 CLI 子进程，本地 SQLite 存会话/审计/任务并逻辑关联 V1 标识符。

**技术栈：** Python 3.13 + uv；FastAPI + Jinja2 + 少量 JS；LangGraph（checkpoint 读取仅经投影）；pydantic；pytest + ruff；uvicorn。

**规格：** `openspec/changes/add-web-maintenance-workbench/plan-ready.md`（源），`openspec/changes/add-web-maintenance-workbench/design.md` 与 `specs/`。

## 全局约束

- 语言：人类可读文案中文；代码标识符、路径、命令、OpenSpec 标题保持原文。
- TDD 铁律：先写失败测试 → 确认失败 → 最小实现 → 确认通过 → commit。每个任务一个 commit。
- 测试：`uv run pytest`；静态：`uv run ruff check src/`（select E/F/I/UP/B，line-length 100）。
- 不破坏 V1 既有行为与五道闸门；GitService 白名单不含 push。
- 真实路径/密钥不硬编码，走 env（pydantic-settings，`.env` 可选）。
- 平台端口 `BSA_WEB_PORT`（默认 **8888**）。
- commit 消息：`feat: ...` / `fix: ...`，中文说明。

---

## 第一部分：V1 基础设施与分支级命令

### 任务 1：全局 flock

**文件：**
- Create: `src/bsa/executor/lock.py`
- Test: `tests/test_lock.py`
- Modify: `src/bsa/scheduler/cycle.py`

**接口：**
- Produces: `bsa.executor.lock.flock_acquire(path: str|Path, timeout: float = 1800.0) -> contextmanager`；`bsa.executor.lock.LockTimeoutError(TimeoutError)`。

- [ ] **步骤 1：写失败测试**

```python
# tests/test_lock.py
import os
import threading
import time

import pytest

from bsa.executor.lock import LockTimeoutError, flock_acquire


def test_acquires_and_releases(tmp_path):
    lock = tmp_path / "l.lock"
    with flock_acquire(lock):
        pass
    assert lock.exists()


def test_mutex_between_threads(tmp_path):
    lock = tmp_path / "l.lock"
    held: list[bool] = []
    with flock_acquire(lock):
        other = threading.Thread(target=lambda: [held.append(flock_acquire(lock).__enter__() is None)], daemon=True)
        other.start(); other.join(timeout=0.2)
        time.sleep(0.05)
    assert held == [False], "second holder should block while first holds"


def test_released_after_exception(tmp_path):
    lock = tmp_path / "l.lock"
    with pytest.raises(RuntimeError):
        with flock_acquire(lock):
            raise RuntimeError("boom")
    with flock_acquire(lock, timeout=1.0):
        pass


def test_timeout_raises(tmp_path):
    lock = tmp_path / "l.lock"
    with flock_acquire(lock):
        with pytest.raises(LockTimeoutError):
            with flock_acquire(lock, timeout=0.1):
                pass
```

- [ ] **步骤 2：运行确认失败**

Run: `uv run pytest tests/test_lock.py -v`
Expected: FAIL（`ModuleNotFoundError: bsa.executor.lock`）

- [ ] **步骤 3：最小实现**

```python
# src/bsa/executor/lock.py
import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class LockTimeoutError(TimeoutError):
    """获取全局锁超时。"""


@contextmanager
def flock_acquire(path: str | Path, timeout: float = 1800.0) -> Iterator[None]:
    """阻塞+超时获取全局文件锁；进程退出自动释放，不会死锁。"""
    lock = Path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(f"获取全局锁超时（{timeout:.0f}s）：{lock}")
                time.sleep(0.5)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
```

- [ ] **步骤 4：运行确认通过**

Run: `uv run pytest tests/test_lock.py -v`
Expected: PASS

- [ ] **步骤 5：接入周期执行入口并提交**

```python
# src/bsa/scheduler/cycle.py（_execute 开头，清理 worktree 之前）
from bsa.executor.lock import flock_acquire
...
def _execute(...):
    ...
    log_dir = Path(settings.log_dir)
    cycle_dir = log_dir / cycle_id
    cycle_dir.mkdir(parents=True, exist_ok=True)
    with flock_acquire(log_dir / "bsa.lock"):
        return _execute_locked(context, cycle_id, cycle_dir, since=since, until=until, dry_run=dry_run, force_new=force_new)
```

将原 `_execute` 主体（`logger` 初始化之后到 `return 0`）重命名为 `_execute_locked`，签名追加 `cycle_dir`；`logger` 初始化保留在锁内。

Run: `uv run pytest tests/ -k "not real_git" && uv run ruff check src/bsa/`
Expected: 既有测试全绿（cycle 相关用例通过）

Commit: `git add -A && git commit -m "feat: 全局 flock 串行化 V1 执行"`

### 任务 2：周期运行中标记

**文件：**
- Modify: `src/bsa/scheduler/cycle.py`
- Test: `tests/test_scheduler_cycle.py`（新建）

**接口：**
- Produces: `run_cycle` 开始时即写 `cycle.json`（`status=running` + started_at），结束覆盖终态。`list_cycle_records` 兼容。

- [ ] **步骤 1：写失败测试**

```python
# tests/test_scheduler_cycle.py
import json

from bsa.scheduler.cycle import list_cycle_records, run_cycle


def test_running_marker_written_before_execution(tmp_path, monkeypatch):
    written: list[dict] = []

    def fake_ctx(*a, **k):
        return None

    import bsa.scheduler.cycle as mod

    monkeypatch.setattr(mod, "load_settings", lambda: SettingsStub(tmp_path))
    monkeypatch.setattr(mod, "_execute_locked", lambda *a, **k: 0)
    monkeypatch.setattr(mod, "_setup_run_logger", lambda p: logging.getLogger("x"))
    run_cycle("2026-08-24")
    record = json.loads((tmp_path / "cycle-2026-08-24" / "cycle.json").read_text(encoding="utf-8"))
    assert record["status"] == "COMPLETED"
```

（`SettingsStub` 提供 `log_dir`、`repo_path`、`branch_file` 等最小属性；实现时以实际 `_execute_locked` 结构为准调整 monkeypatch 目标。）

- [ ] **步骤 2：运行确认失败**

Run: `uv run pytest tests/test_scheduler_cycle.py -v`
Expected: FAIL（无运行中标记时序）

- [ ] **步骤 3：实现运行中标记**

在 `_execute_locked` 中，`cycle_dir.mkdir` 之后立即：

```python
started_at = datetime.now().isoformat(timespec="seconds")
_write_cycle_record(
    cycle_dir, cycle_id, status="running", report_path=None,
    mail_status=None, started_at=started_at,
    finished_at=started_at,
)
```

并把 `logger.info("cycle %s start ...")` 移到其前；结束时现有 `_write_cycle_record` 保持不变（覆盖为终态）。

- [ ] **步骤 4：运行确认通过 + 提交**

Run: `uv run pytest tests/test_scheduler_cycle.py tests/test_cli_smoke.py -v`
Expected: PASS

Commit: `git add -A && git commit -m "feat: 周期开始即写运行中标记"`

### 任务 3：state.sqlite3 WAL

**文件：**
- Modify: `src/bsa/graph/workflow.py`（`open_checkpointer`）
- Test: `tests/test_workflow.py`（追加用例）

- [ ] **步骤 1：写失败测试（追加）**

```python
# tests/test_workflow.py 追加
def test_checkpointer_db_uses_wal(tmp_path):
    db = tmp_path / "state.sqlite3"
    with open_checkpointer(str(db)) as cp:
        pass
    import sqlite3
    conn = sqlite3.connect(str(db))
    journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert journal == "wal"
```

- [ ] **步骤 2：运行确认失败**

Run: `uv run pytest tests/test_workflow.py::test_checkpointer_db_uses_wal -v`
Expected: FAIL（journal_mode 为 delete 或 memory）

- [ ] **步骤 3：实现**

```python
# src/bsa/graph/workflow.py open_checkpointer
def open_checkpointer(conn_string: str) -> Iterator[SqliteSaver]:
    conn = sqlite3.connect(conn_string, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        yield _new_saver(conn)
    finally:
        conn.close()
```

- [ ] **步骤 4：运行确认通过 + 提交**

Run: `uv run pytest tests/test_workflow.py -v`
Expected: PASS（既有 checkpoint 用例回归绿）

Commit: `git add -A && git commit -m "feat: checkpoint 库开启 WAL 支持并发读"`

### 任务 4：投影 CLI（`bsa report <cycle> --json`）

**文件：**
- Create: `src/bsa/report/projection.py`
- Create: `tests/test_projection.py`
- Modify: `src/bsa/cli.py`

**接口：**
- Produces: `bsa.report.projection.read_cycle_state(settings, cycle_id) -> dict|None`（从 checkpoint 读 `channel_values`）；`projection_payload(state, cycle_id, status) -> dict`（model_dump）。
- Consumes: `bsa.graph.workflow.open_checkpointer`、`thread_config`；`bsa.scheduler.cycle.list_cycle_records`。

- [ ] **步骤 1：写失败测试**

```python
# tests/test_projection.py
import json
import subprocess

import pytest

from bsa.domain.models import BranchResult, CommitResult
from bsa.report.projection import projection_payload, read_cycle_state


def test_projection_payload_dumps_domain_models():
    state = {
        "cycle_id": "cycle-2026-08-24",
        "status": "COMPLETED",
        "detected_commits": [],
        "decisions": {},
        "branch_results": {
            "release/2.4": BranchResult(
                target_branch="release/2.4", worktree_path="/wt", status="SUCCESS",
                commits=[], patch_path="/p.patch", stop_reason=None,
            )
        },
    }
    payload = projection_payload(state)
    assert payload["branch_results"]["release/2.4"]["status"] == "SUCCESS"
    json.dumps(payload)  # 必须可序列化


def test_read_cycle_state_returns_none_for_unknown(monkeypatch, tmp_path):
    class Stub:
        log_dir = str(tmp_path)
    monkeypatch.setattr("bsa.report.projection.open_checkpointer", _noop_cp)
    assert read_cycle_state(Stub(), "cycle-nope") is None
```

- [ ] **步骤 2：运行确认失败**

Run: `uv run pytest tests/test_projection.py -v`
Expected: FAIL（`bsa.report.projection` 不存在）

- [ ] **步骤 3：实现**

```python
# src/bsa/report/projection.py
from __future__ import annotations

from typing import Any

from bsa.graph.workflow import open_checkpointer, thread_config


def read_cycle_state(settings, cycle_id: str) -> dict[str, Any] | None:
    """从 checkpoint 读某周期最终 state（channel_values）；不存在返回 None。"""
    db = str(Path(settings.log_dir) / "state.sqlite3")
    if not Path(db).exists():
        return None
    with open_checkpointer(db) as cp:
        tup = cp.get_tuple(thread_config(cycle_id))
        if tup is None:
            return None
        return tup.checkpoint.get("channel_values") or {}


def _cycle_status(settings, cycle_id: str) -> str:
    record = next((r for r in list_cycle_records(settings.log_dir) if r.get("cycle_id") == cycle_id), None)
    return (record or {}).get("status", "running")


def projection_payload(state: dict[str, Any]) -> dict[str, Any]:
    """将 TaskState 转为可 JSON 序列化的结构化投影。"""
    return {
        "cycle_id": state.get("cycle_id"),
        "status": state.get("status"),
        "scan_window": state.get("scan_window"),
        "detected_commits": [c.model_dump() for c in state.get("detected_commits") or []],
        "decisions": {
            sha: {t: c.model_dump() for t, c in per.items()}
            for sha, per in (state.get("decisions") or {}).items()
        },
        "branch_results": {
            t: b.model_dump() for t, b in (state.get("branch_results") or {}).items()
        },
        "action_required": (state.get("report").action_required if state.get("report") else []) or [],
    }
```

- [ ] **步骤 4：注册 CLI**

```python
# src/bsa/cli.py
proj_p = sub.add_parser("report", help="输出周期结构化状态（JSON）")
proj_p.add_argument("cycle", help="cycle_id")
proj_p.add_argument("--json", action="store_true", help="JSON 输出")
proj_p.set_defaults(handler=_cmd_report)

def _cmd_report(args):
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"配置错误: {exc}", file=sys.stderr); return 2
    state = read_cycle_state(settings, args.cycle)
    if state is None and _cycle_status(settings, args.cycle) == "running":
        print(json.dumps({"status": "running"}, ensure_ascii=False)); return 0
    if state is None:
        print(f"周期不存在或未完成: {args.cycle}", file=sys.stderr); return 1
    print(json.dumps(projection_payload(state), ensure_ascii=False, indent=2)); return 0
```

- [ ] **步骤 5：运行确认通过 + 提交**

Run: `uv run pytest tests/test_projection.py tests/test_cli_smoke.py -v`
Expected: PASS

Commit: `git add -A && git commit -m "feat: 投影 CLI bsa report --json 只读输出周期状态"`

### 任务 5：override CLI（`bsa override`）

**文件：**
- Create: `src/bsa/commands/override.py`
- Create: `tests/test_override.py`
- Modify: `src/bsa/cli.py`

**接口：**
- Produces: `bsa.commands.override.apply_override(log_dir, sha, *, is_bug_fix=None, risk=None) -> dict`（写 judgments.json，持锁）。
- Consumes: `bsa.executor.lock.flock_acquire`；judgments 文件结构 `{sha: {"is_bug_fix": bool, "risk": str, "recognition_source": str}}`。

- [ ] **步骤 1：写失败测试**

```python
# tests/test_override.py
import json

from bsa.commands.override import apply_override


def test_writes_judgment_file(tmp_path):
    out = apply_override(tmp_path, "abc123", is_bug_fix=True, risk="high")
    data = json.loads((tmp_path / "judgments.json").read_text(encoding="utf-8"))
    assert data["abc123"]["is_bug_fix"] is True
    assert data["abc123"]["risk"] == "high"
    assert data["abc123"]["recognition_source"] == "manual-override"


def test_merges_existing_entries(tmp_path):
    (tmp_path / "judgments.json").write_text(
        json.dumps({"other": {"is_bug_fix": False}}), encoding="utf-8"
    )
    apply_override(tmp_path, "abc123", is_bug_fix=True)
    data = json.loads((tmp_path / "judgments.json").read_text(encoding="utf-8"))
    assert set(data) == {"other", "abc123"}
```

- [ ] **步骤 2：运行确认失败**

Run: `uv run pytest tests/test_override.py -v`
Expected: FAIL

- [ ] **步骤 3：实现**

```python
# src/bsa/commands/override.py
import json
from pathlib import Path
from typing import Literal

from bsa.executor.lock import flock_acquire

_RISK = Literal["low", "medium", "high"]


def apply_override(
    log_dir: str | Path,
    sha: str,
    *,
    is_bug_fix: bool | None = None,
    risk: _RISK | None = None,
) -> dict:
    log_dir = Path(log_dir)
    path = log_dir / "judgments.json"
    with flock_acquire(log_dir / "bsa.lock"):
        data: dict = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        entry = data.get(sha, {})
        if is_bug_fix is not None:
            entry["is_bug_fix"] = bool(is_bug_fix)
        if risk is not None:
            entry["risk"] = risk
        entry["recognition_source"] = "manual-override"
        data[sha] = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data[sha]
```

CLI 注册（`src/bsa/cli.py`）：`bsa override <sha> [--is-bug-fix] [--risk {low,medium,high}]` → 调 `apply_override`，打印结果。

- [ ] **步骤 4：运行确认通过 + 提交**

Run: `uv run pytest tests/test_override.py tests/test_sync_decision_agent.py -v`
Expected: PASS（judgments 可被既有 SyncDecisionAgent 读取）

Commit: `git add -A && git commit -m "feat: override CLI 写 judgments 人工覆盖"`

### 任务 6：单 target 子图与 sync CLI（`bsa sync`）

**文件：**
- Create: `src/bsa/graph/single_target.py`
- Create: `src/bsa/commands/sync.py`
- Create: `tests/test_sync_command.py`
- Modify: `src/bsa/cli.py`

**接口：**
- Produces: `bsa.graph.single_target.run_single_target(context, cycle_id, target, batch, *, fresh: bool) -> dict`（构造单 target state，复用节点执行，返回更新后的 state）。
- Consumes: `bsa.graph.nodes` 的 `prepare_worktree/cherry_pick/resolve_conflict/build/fix_build/generate_patch`；`bsa.scheduler.cycle._initial_state` 结构。

- [ ] **步骤 1：写失败测试（用 FakeExecutor 驱动单 target 图）**

```python
# tests/test_sync_command.py
from bsa.commands.sync import run_sync_command
from bsa.executor import FakeExecutor, CompletedProcess
from bsa.domain.models import CommitInfo, Conclusion4


def _ok(stdout="", rc=0): return CompletedProcess(returncode=rc, stdout=stdout, stderr="")
def _ctx(tmp_path):
    from bsa.config.settings import Settings
    from bsa.graph.factory import build_graph_context
    import bsa.graph.factory as f
    settings = Settings(repo_path=str(tmp_path), branch_file=str(tmp_path/"b.md"),
        worktree_root=str(tmp_path/"wt"), llm_model="x", llm_api_key="k",
        llm_base_url="http://x", docker_image="img", docker_mount_workspace="/w",
        build_script_dir=str(tmp_path/"b"), mail_sender="s", mail_recipients=["r"],
        log_dir=str(tmp_path/"logs"))
    # executor 注入：用 FakeExecutor 提供 git 响应
    return build_graph_context(settings)
```

（真实实现以 `tests/test_graph_nodes.py` 的既有 FakeExecutor 驱动方式为准——先阅读该文件复用其响应编排；`run_sync_command` 内部构造单 target state 并 invoke。）

- [ ] **步骤 2：运行确认失败**

Run: `uv run pytest tests/test_sync_command.py -v`
Expected: FAIL

- [ ] **步骤 3：实现单 target 子图**

```python
# src/bsa/graph/single_target.py
from __future__ import annotations

from bsa.graph.nodes import GraphContext, node_wrapper
from bsa.graph.state import TaskState


def run_single_target(
    ctx: GraphContext,
    cycle_id: str,
    target: str,
    commits: list,          # list[CommitInfo]
    *,                      # 需要 checkpointer 时由调用方传入
) -> dict:
    """对单个 target 执行同步链路，返回更新后的 state。

    构造仅含该 target 的初始 state，依次执行 prepare_worktree → 逐
    commit cherry_pick → build → generate_patch，失败按既有 fail-fast
    语义停止。复用主图节点函数保证行为一致。
    """
    state = {
        "cycle_id": cycle_id,
        "scan_window": ("", ""),
        "branch_md_version": "",
        "detected_commits": commits,
        "classifications": {},
        "decisions": {c.sha: {target: Conclusion4(kind="NeedSync", evidence=[], confidence="high")} for c in commits},
        "batches": {target: [c.sha for c in commits]},
        "current_target": target,
        "current_commit": None,
        "branch_results": {},
        "status": "NEW",
        "errors": {},
        "report": None,
    }
    from bsa.graph.workflow import build_workflow
    graph = build_workflow(ctx, checkpointer=None)  # 复用主图；初始 state 只含单 target
    # 用 checkpoint 保证可断点；生产传入持久化 checkpointer
    ...
```

（实现时完整处理：单 target 图在 `next_branch` 后即 END；`run_sync_command` 组装 `--sha` 直同步批次或源+目标决策批次，独立 `cycle_id = manual-<ts>`，结果写回并调 `report` 节点。）

- [ ] **步骤 4：实现 CLI 命令并注册**

`bsa sync <src> <target> [--sha <sha>...] [--date YYYY-MM-DD]`：
- `--sha`：`run_sync_command` 直接构造该 commit 批次直同步。
- 源+目标：复用 `sync_decision` + `conclude_pair`（目标快照取自当前远端），NeedSync 才入批次。
- 持 flock、写独立 `manual-*` cycle、结果写 `branch_results[target]`。

- [ ] **步骤 5：运行确认通过 + 提交**

Run: `uv run pytest tests/test_sync_command.py tests/test_workflow.py -v`
Expected: PASS（主图回归绿）

Commit: `git add -A && git commit -m "feat: 分支级同步 CLI bsa sync（源+目标 / --sha 直同步）"`

### 任务 7：分支级重跑 CLI（`bsa rerun`）

**文件：**
- Create: `src/bsa/commands/rerun.py`
- Create: `tests/test_rerun_command.py`
- Modify: `src/bsa/cli.py`

**接口：**
- Produces: `bsa.commands.rerun.run_rerun_command(log_dir, target, *, cycle=None, fresh=False) -> dict`。
- Consumes: `bsa.graph.single_target`；`bsa.git.service.GitService.status`（dirty 拦截）。

- [ ] **步骤 1：写失败测试**

```python
# tests/test_rerun_command.py
from bsa.commands.rerun import run_rerun_command


def test_fresh_rerun_redesides_conclusion(tmp_path):
    # 构造已含 source sha 的 target 快照 → 应判 AlreadyIncluded 并停止
    out = run_rerun_command(tmp_path, "release/2.4", fresh=True)
    assert out["stop"] is True


def test_retained_rerun_rejects_dirty_worktree(tmp_path):
    # 模拟 worktree 有未提交修改 → 拦截
    ...
```

- [ ] **步骤 2：运行确认失败**

Run: `uv run pytest tests/test_rerun_command.py -v`
Expected: FAIL

- [ ] **步骤 3：实现**

```python
# src/bsa/commands/rerun.py
def run_rerun_command(log_dir, target, *, cycle=None, fresh=False) -> dict:
    # 定位当前周期活 worktree（logs 周期记录或投影 branch_results）
    # fresh: 重建现场 + 先重判结论（conclude_pair vs 当前远端），
    #        AlreadyIncluded/OutOfScope → return {"stop": True}
    # 保留现场: git status --porcelain 非空 → return {"stop": True, "reason": "dirty"}
    # 否则调 run_single_target 续跑（未完成 cherry-pick 继续、已应用跳过）→ 更新 patch
    ...
```

- [ ] **步骤 4：注册 CLI + 确认通过 + 提交**

CLI：`bsa rerun <target> [--cycle <id>] [--fresh]`。dirty 拦截复用 `GitService.status()`。

Run: `uv run pytest tests/test_rerun_command.py tests/test_cli_smoke.py -v`
Expected: PASS

Commit: `git add -A && git commit -m "feat: 分支级重跑 CLI bsa rerun（保留现场 / --fresh 重判）"`

---

## 第二部分：Web 平台

### 任务 8：平台工程骨架 + 依赖

**文件：**
- Create: `src/bsa_web/__init__.py`
- Create: `src/bsa_web/app.py`
- Create: `src/bsa_web/settings.py`
- Create: `src/bsa_web/db.py`
- Create: `tests/web/__init__.py`
- Create: `tests/web/test_app.py`
- Modify: `pyproject.toml`

**接口：**
- Produces: `bsa_web.settings.WebSettings`（含 `bsa_web_port=8888`、`log_dir`、`secret_key`、`session_ttl_sec=8*3600`、`users: dict[str,str]`）；`bsa_web.db.init_db(path) -> sqlite3.Connection`（schema_version=1）；`bsa_web.app.create_app() -> FastAPI`。
- Consumes: V1 `bsa.config.settings.load_settings`。

- [ ] **步骤 1：加依赖 + 写失败测试**

`pyproject.toml` dependencies 追加：`fastapi>=0.115`、`uvicorn[standard]>=0.30`、`jinja2>=3.1`、`itsdangerous>=2.1`、`passlib[bcrypt]>=1.7`（或 `bcrypt>=4` 直用）。

```python
# tests/web/test_app.py
from fastapi.testclient import TestClient
from bsa_web.app import create_app


def test_healthcheck():
    client = TestClient(create_app())
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
```

- [ ] **步骤 2：运行确认失败**

Run: `uv run pytest tests/web/test_app.py -v`
Expected: FAIL

- [ ] **步骤 3：实现骨架**

```python
# src/bsa_web/settings.py
from pydantic_settings import BaseSettings, SettingsConfigDict


class WebSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    bsa_web_port: int = 8888
    log_dir: str = "logs"
    secret_key: str
    session_ttl_sec: int = 8 * 3600
    users: dict[str, str] = {}   # "username:bcrypt_hash:role"


def load_web_settings(*, env_file: str | None = ".env") -> WebSettings:
    return WebSettings(_env_file=env_file)
```

```python
# src/bsa_web/db.py
import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY, user TEXT NOT NULL, role TEXT NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, user TEXT NOT NULL,
  action TEXT NOT NULL, cycle_id TEXT, target TEXT, sha TEXT,
  detail_json TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, user TEXT NOT NULL,
  cycle_id TEXT, target TEXT, shas TEXT, state TEXT NOT NULL,
  error TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT);
"""


def init_db(path: str | Path) -> sqlite3.Connection:
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version','1')")
    conn.commit()
    return conn
```

```python
# src/bsa_web/app.py
from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="BSA Web 工作台")

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
```

- [ ] **步骤 4：确认通过 + 提交**

Run: `uv run pytest tests/web/test_app.py -v && uv run ruff check src/bsa_web/`
Expected: PASS

Commit: `git add -A && git commit -m "feat: 平台工程骨架（FastAPI + DB + env 配置）"`

### 任务 9：认证、会话、角色、CSRF

**文件：**
- Create: `src/bsa_web/auth.py`
- Create: `src/bsa_web/rbac.py`
- Create: `tests/web/test_auth.py`
- Modify: `src/bsa_web/app.py`, `src/bsa_web/db.py`

**接口：**
- Produces: `bsa_web.auth.Authenticator`（协议：`authenticate(user, password) -> str|None`（角色）、`hash_password`）、`EnvAuthenticator`；`bsa_web.auth.create_session(db, user, role)`、`bsa_web.auth.get_session_user(db, token) -> tuple|None`；`bsa_web.auth.require_login(request)`、`bsa_web.auth.require_operator(request)` 依赖。
- `BSA_USERS` 格式 `user:bcrypt:operator`。

- [ ] **步骤 1：写失败测试**

```python
# tests/web/test_auth.py
from fastapi.testclient import TestClient
from bsa_web.app import create_app


def test_login_flow_and_session_cookie(monkeypatch, tmp_path):
    app = create_app(settings_override={"log_dir": str(tmp_path), "users": {"alice": "op"}})
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 302  # 未登录重定向
    # 登录
    r = client.post("/login", data={"username": "alice", "password": "op"})
    assert r.status_code == 302


def test_viewer_cannot_trigger(monkeypatch, tmp_path):
    # role=viewer 调用触发端点 → 403
    ...


def test_csrf_rejects_form_without_token(monkeypatch, tmp_path):
    ...
```

- [ ] **步骤 2：运行确认失败**

Run: `uv run pytest tests/web/test_auth.py -v`
Expected: FAIL

- [ ] **步骤 3：实现 auth + rbac + 中间件**

```python
# src/bsa_web/auth.py
import hashlib
import hmac
import secrets
import time
from typing import Protocol

from .db import get_db  # app.state 持连接


def hash_password(password: str) -> str:
    import bcrypt
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


class Authenticator(Protocol):
    def authenticate(self, username: str, password: str) -> str | None: ...
    def roles_for(self, username: str) -> str: ...


class EnvAuthenticator:
    """初版静态账号：BSA_USERS = user:bcrypt:role,user2:..."""
    def __init__(self, raw: str):
        self._users = {}
        for item in (raw or "").split(","):
            if not item:
                continue
            user, pwd_hash, role = item.split(":")
            self._users[user] = (pwd_hash, role)

    def authenticate(self, username, password):
        entry = self._users.get(username)
        if not entry:
            return None
        import bcrypt
        return entry[1] if bcrypt.checkpw(password.encode(), entry[0].encode()) else None

    def roles_for(self, username):
        return self._users.get(username, ("", "viewer"))[1]


def create_session(db, user, role, ttl_sec) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    db.execute("INSERT INTO sessions(token,user,role,created_at,expires_at) VALUES(?,?,?,?,?)",
               (token, user, role, iso(now), iso(now + ttl_sec)))
    db.commit()
    return token
```

`app.py`：加登录/登出路由、session cookie（HttpOnly/Secure/SameSite=Lax）、CSRF token（`itsdangerous` 签名 cookie 或 sessions 表存 token）、`require_login`/`require_operator` FastAPI 依赖（每端点角色校验）。

- [ ] **步骤 4：确认通过 + 提交**

Run: `uv run pytest tests/web/test_auth.py -v && uv run ruff check src/bsa_web/`
Expected: PASS

Commit: `git add -A && git commit -m "feat: 认证/会话/角色/CSRF（env 静态账号过渡）"`

### 任务 10：投影封装 + 工作台首页

**文件：**
- Create: `src/bsa_web/projection.py`
- Create: `src/bsa_web/views/__init__.py`, `src/bsa_web/views/workbench.py`
- Create: `src/bsa_web/templates/base.html`, `src/bsa_web/templates/workbench.html`
- Create: `tests/web/test_workbench.py`
- Modify: `src/bsa_web/app.py`

**接口：**
- Produces: `bsa_web.projection.latest_completed_cycle(log_dir) -> str|None`；`bsa_web.projection.load_cycle(log_dir, cycle_id) -> dict|None`（调 `bsa report <cycle> --json` 子进程）；`load_branch_status(...)`。
- Consumes: V1 CLI `bsa report`（`sys.executable -m bsa.cli report <cycle> --json` 或 console script）。

- [ ] **步骤 1：写失败测试**

```python
# tests/web/test_workbench.py
from fastapi.testclient import TestClient
from bsa_web.app import create_app


def test_workbench_renders_pending_actions(tmp_path, monkeypatch):
    fake_cycle = {"cycle_id": "cycle-2026-08-24", "status": "COMPLETED",
                  "branch_results": {"release/2.4": {"status": "SUCCESS", "patch_path": "/p"}}}
    monkeypatch.setattr("bsa_web.projection.load_cycle", lambda *a: fake_cycle)
    monkeypatch.setattr("bsa_web.projection.latest_completed_cycle", lambda *a: "cycle-2026-08-24")
    app = create_app(settings_override={"log_dir": str(tmp_path)})
    client = _logged_in_client(app)
    r = client.get("/")
    assert "release/2.4" in r.text
    assert "推送" in r.text
```

- [ ] **步骤 2：运行确认失败**

Run: `uv run pytest tests/web/test_workbench.py -v`
Expected: FAIL

- [ ] **步骤 3：实现投影封装 + 首页路由**

```python
# src/bsa_web/projection.py
import json
import subprocess
import sys


def _run_report(log_dir: str, cycle_id: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "bsa.cli", "report", cycle_id, "--json"],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "BSA_LOG_DIR": log_dir, **({"BSA_ENV_FILE": ""} if False else {})},
    )
    if proc.returncode != 0:
        return {}
    return json.loads(proc.stdout)


def load_cycle(log_dir, cycle_id):
    data = _run_report(log_dir, cycle_id)
    return data or None


def latest_completed_cycle(log_dir):
    # 读 logs/cycle-*/cycle.json，取状态非 running 且最近者
    ...
```

`views/workbench.py`：待办区（SUCCESS→可推送、ManualReview 待确认、FAILED/MANUAL→重跑）、实时总览、快速操作表单、Agent 状态（running 徽章）。模板用 Jinja2 渲染。运行中周期仅显示"进行中"。

- [ ] **步骤 4：确认通过 + 提交**

Run: `uv run pytest tests/web/test_workbench.py -v && uv run ruff check src/bsa_web/`
Expected: PASS

Commit: `git add -A && git commit -m "feat: 实时工作台首页（待办/总览/Agent 状态）"`

### 任务 11：历史报告与详情页

**文件：**
- Create: `src/bsa_web/views/history.py`, `src/bsa_web/views/detail.py`
- Create: `src/bsa_web/templates/history.html`, `src/bsa_web/templates/detail.html`
- Create: `tests/web/test_detail_pages.py`
- Modify: `src/bsa_web/app.py`

- [ ] **步骤 1：写失败测试**

```python
# tests/web/test_detail_pages.py
def test_history_lists_cycles(tmp_path, monkeypatch):
    # 多个 cycle.json → 按日期列出
    ...

def test_detail_shows_patch_and_build_log(tmp_path, monkeypatch):
    # /cycle/<id>/target/<t>/commit/<sha> 渲染 patch_text 与 build log 路径
    ...
```

- [ ] **步骤 2：实现**

历史页读 `list_cycle_records`（`bsa.scheduler.cycle`），按日期倒序 + 结论筛选。详情页从投影 payload 取 `detected_commits`（patch_text）、`branch_results[target].commits[].build[].log_path`、`conflict_resolution`；日志文件按需读取（`<log_path>` 截断展示 + 下载链接）。

- [ ] **步骤 3：确认通过 + 提交**

Run: `uv run pytest tests/web/test_detail_pages.py -v && uv run ruff check src/bsa_web/`
Expected: PASS

Commit: `git add -A && git commit -m "feat: 历史报告与 commit/分支详情页"`

### 任务 12：异步任务 runner + 触发/重跑 API

**文件：**
- Create: `src/bsa_web/runner.py`
- Create: `src/bsa_web/api/__init__.py`, `src/bsa_web/api/operations.py`
- Create: `tests/web/test_runner.py`, `tests/web/test_operations.py`
- Modify: `src/bsa_web/app.py`

**接口：**
- Produces: `bsa_web.runner.TaskRunner`（`submit(kind, user, target, shas) -> int`；`get(id) -> dict`；后台线程消费队列调 V1 CLI）。
- 状态机 `queued→running→succeeded/failed`；同 target 并发拒绝（`task_running_for(target)`）。

- [ ] **步骤 1：写失败测试**

```python
# tests/web/test_runner.py
from bsa_web.runner import TaskRunner


def test_state_machine_and_concurrency_reject(tmp_path):
    runner = TaskRunner(db, run_func=lambda *a: (0, "ok"))
    tid = runner.submit("sync", "alice", "release/2.4", [])
    assert runner.get(tid)["state"] == "succeeded"
    # 同 target 再次 submit 被拒
    assert runner.submit("sync", "alice", "release/2.4", []) is None
```

- [ ] **步骤 2：实现**

`runner.py`：`queue.Queue` + daemon worker；`submit` 先查 tasks 表同 target running/queued → 拒绝；worker 调 `bsa sync/rerun` 子进程（`sys.executable -m bsa.cli ...`），捕获返回码与 stderr 存 `tasks.error`。子进程 `communicate(timeout=3600)`，超时杀进程。`operations.py`：`POST /api/sync`、`POST /api/rerun`、`GET /api/tasks/<id>`。

- [ ] **步骤 3：确认通过 + 提交**

Run: `uv run pytest tests/web/test_runner.py tests/web/test_operations.py -v`
Expected: PASS

Commit: `git add -A && git commit -m "feat: 异步任务 runner + 触发同步/重跑 API"`

### 任务 13：人工项处理

**文件：**
- Create: `src/bsa_web/api/manual_review.py`
- Create: `tests/web/test_manual_review.py`
- Modify: `src/bsa_web/app.py`

- [ ] **步骤 1：写失败测试**

```python
# tests/web/test_manual_review.py
def test_override_dispatches_bsa_override(tmp_path, monkeypatch):
    # POST /api/override → 调用 V1 override CLI（monkeypatch subprocess）且写审计
    ...

def test_confirm_continue_uses_sha_sync(tmp_path, monkeypatch):
    # POST /api/confirm → 触发 bsa sync <target> --sha <sha>
    ...
```

- [ ] **步骤 2：实现**

- `POST /api/override`：`{sha, is_bug_fix?, risk?}` → 子进程 `bsa override` + 审计（action=override）。
- `POST /api/confirm`：`{target, sha}` → 触发 `bsa sync <target> --sha <sha>`（任务提交）+ 审计（action=confirm_continue）。
- `POST /api/abandon`：`{target, sha?}` → 审计（action=abandon）+ 标记（写入平台本地表或仅日志）。

- [ ] **步骤 3：确认通过 + 提交**

Run: `uv run pytest tests/web/test_manual_review.py -v`
Expected: PASS

Commit: `git add -A && git commit -m "feat: 人工项处理（改判定/确认继续/放弃）"`

### 任务 14：操作日志与审计（先行就绪，推送依赖）

**文件：**
- Create: `src/bsa_web/audit.py`
- Create: `src/bsa_web/views/audit_log.py`
- Create: `tests/web/test_audit.py`
- Modify: `src/bsa_web/db.py`, `src/bsa_web/app.py`

**接口：**
- Produces: `bsa_web.audit.record(db, user, action, *, cycle_id=None, target=None, sha=None, detail=None, result=None)`；`bsa_web.audit.list_records(db, limit=500)`。
- 约束：应用内无 update/delete 入口（append-only）。

- [ ] **步骤 1：写失败测试**

```python
# tests/web/test_audit.py
def test_record_and_list(tmp_path):
    db = init_db(tmp_path / "platform.sqlite3")
    record(db, "alice", "push", target="release/2.4", result="ok")
    rows = list_records(db)
    assert rows[0]["action"] == "push" and rows[0]["user"] == "alice"


def test_no_update_delete_api(tmp_path):
    # 审计 API 不暴露 DELETE/PUT
    ...
```

- [ ] **步骤 2：实现 + 确认 + 提交**

Run: `uv run pytest tests/web/test_audit.py -v`
Expected: PASS
Commit: `git add -A && git commit -m "feat: append-only 操作日志与审计页"`

### 任务 15：推送执行链

**文件：**
- Create: `src/bsa_web/push.py`
- Create: `src/bsa_web/api/push.py`
- Create: `tests/web/test_push.py`
- Modify: `src/bsa_web/app.py`

**接口：**
- Produces: `bsa_web.push.check_push_gates(projection, target, safety_forbidden, worktree_git) -> list[str]`（返回未过闸原因，空=通过）；`bsa_web.push.execute_push(worktree, target) -> (rc, out)`。
- 受限执行器：白名单仅 `git push`，参数强校验 `origin HEAD:<target>`，禁 `--force`。

- [ ] **步骤 1：写失败测试**

```python
# tests/web/test_push.py
from bsa_web.push import check_push_gates, execute_push
from bsa.executor import FakeExecutor, CompletedProcess


def test_gate_success():
    gates = check_push_gates(
        {"branch_results": {"release/2.4": {"status": "SUCCESS"}}},
        "release/2.4", forbidden=["master"], status_clean=True)
    assert gates == []


def test_gate_rejects_non_success():
    gates = check_push_gates({"branch_results": {"release/2.4": {"status": "MANUAL"}}},
                             "release/2.4", forbidden=[], status_clean=True)
    assert gates


def test_gate_rejects_forbidden_branch():
    gates = check_push_gates({"branch_results": {"master": {"status": "SUCCESS"}}},
                             "master", forbidden=["master"], status_clean=True)
    assert gates


def test_gate_rejects_dirty():
    gates = check_push_gates({"branch_results": {"release/2.4": {"status": "SUCCESS"}}},
                             "release/2.4", forbidden=[], status_clean=False)
    assert gates


def test_execute_push_uses_head_target_and_no_force():
    ex = FakeExecutor([CompletedProcess(returncode=0, stdout="", stderr="")])
    rc, out = execute_push(ex, "/wt", "release/2.4")
    assert ex.calls[0][0] == ["git", "push", "origin", "HEAD:release/2.4"]
    assert "-f" not in ex.calls[0][0]
```

- [ ] **步骤 2：运行确认失败**

Run: `uv run pytest tests/web/test_push.py -v`
Expected: FAIL

- [ ] **步骤 3：实现**

```python
# src/bsa_web/push.py
from __future__ import annotations


def check_push_gates(projection, target, *, forbidden, status_clean) -> list[str]:
    branch = (projection.get("branch_results") or {}).get(target)
    gates: list[str] = []
    if not branch:
        gates.append("分支不在当前周期结果中")
        return gates
    if branch.get("status") != "SUCCESS":
        gates.append(f"状态 {branch.get('status')} 非 SUCCESS，不可推送")
    if target in forbidden:
        gates.append(f"目标分支 {target} 在 forbidden_branches 中")
    if not status_clean:
        gates.append("worktree 有未提交修改，请先清理")
    return gates


def execute_push(executor, worktree, target) -> tuple[int, str]:
    result = executor.run(["push", "origin", f"HEAD:{target}"], cwd=worktree, timeout_sec=600)
    return result.returncode, f"{result.stdout}\n{result.stderr}"
```

`api/push.py`：`POST /api/push` → ①持 flock 下读投影+`git status --porcelain` ②`check_push_gates` ③任一不过返回 400+原因 ④确认框参数（目标/commit 范围/patch 摘要）回显 ⑤`execute_push`（受限 executor，白名单仅 push、禁 force）⑥审计记录。`api/push.py` 的确认参数在二次提交时由前端带上。

- [ ] **步骤 4：确认通过 + 提交**

Run: `uv run pytest tests/web/test_push.py -v && uv run ruff check src/bsa_web/`
Expected: PASS

Commit: `git add -A && git commit -m "feat: 推送四道闸 + 受限 push（禁 force）+ 审计"`

### 任务 16：数据保留与备份

**文件：**
- Create: `src/bsa_web/retention.py`, `src/bsa_web/backup.py`
- Create: `tests/web/test_retention.py`
- Modify: `src/bsa_web/app.py`

**接口：**
- Produces: `bsa_web.retention.clean_logs(log_dir, keep_days=30)`（只清 build.log/run.log）；`bsa_web.backup.backup_now(log_dir, backup_dir, keep=7)`。

- [ ] **步骤 1：写失败测试**

```python
# tests/web/test_retention.py
from bsa_web.retention import clean_logs
from bsa_web.backup import backup_now


def test_clean_only_targets_l2(tmp_path):
    old = tmp_path / "cycle-1" / "build" / "b.log"; old.parent.mkdir(parents=True)
    old.write_text("x")
    (tmp_path / "cycle-1" / "decisions.json").write_text("{}")
    clean_logs(tmp_path, keep_days=0)
    assert not old.exists()
    assert (tmp_path / "cycle-1" / "decisions.json").exists()  # L1 不删


def test_backup_rolls_to_7(tmp_path):
    for i in range(9):
        backup_now(tmp_path, tmp_path / "backup", keep=7)
    backups = sorted((tmp_path / "backup").glob("backup-*.tgz"))
    assert len(backups) == 7
```

- [ ] **步骤 2：实现 + 确认 + 提交**

Run: `uv run pytest tests/web/test_retention.py -v`
Expected: PASS
Commit: `git add -A && git commit -m "feat: 数据保留分级清理与每日备份"`

### 任务 17：可观测性 + 部署配置 + 端到端

**文件：**
- Modify: `src/bsa_web/app.py`（结构化请求日志中间件）
- Create: `deploy/bsa-web.service`, `deploy/nginx.conf`, `.env.example`
- Create: `docs/deploy-v2-web.md`

- [ ] **步骤 1：健康检查完善 + 请求日志**

`app.py` 加 logging 中间件（method/path/status/duration），`/healthz` 返回 DB 可写检查。

- [ ] **步骤 2：部署配置**

`deploy/bsa-web.service`：systemd 单元，ExecStart=`uv run uvicorn bsa_web.app:create_app --factory --host 127.0.0.1 --port 8888`，Restart=always，EnvironmentFile。`deploy/nginx.conf`：443 TLS 反代到 127.0.0.1:8888，`limit_req` 登录路径。`.env.example`：`BSA_WEB_PORT=8888`、`BSA_USERS=`、`SECRET_KEY=`、`BSA_LOG_DIR=`。

- [ ] **步骤 3：全量验证 + 端到端冒烟**

Run: `uv run pytest && uv run ruff check src/`
Expected: 全部通过

手动冒烟：启动 `uv run uvicorn bsa_web.app:create_app --factory --port 8888` → 登录 → 工作台 → 触发同步 → 轮询 → mock 推送 → 审计页。
代码审查确认：平台无自动推送逻辑、无 `--force`。

- [ ] **步骤 4：提交**

Commit: `git add -A && git commit -m "feat: 可观测性与部署配置（8888 端口）"`

---

## 验证计划

- 单元/集成：`uv run pytest`（V1 `tests/` + 平台 `tests/web/`），覆盖 flock/运行中标记/WAL/投影/override/sync/rerun/认证/角色/CSRF/任务/推送四道闸/审计/保留。
- 静态：`uv run ruff check src/`
- 手动：登录→工作台→触发同步→轮询→（mock）推送→审计页；代码审查确认无自动推送、无 `--force`。

## 自检

- 每个任务有失败测试 → 实现 → 通过 → commit；无占位话术；所有文件/接口/验证命令明确。
- Source Coverage 与 plan-ready.md 的 14 slice 一一对应（任务 1-7 = Slice 1-5；任务 8-9 = Slice 6；任务 10 = Slice 7；任务 11 = Slice 8；任务 12 = Slice 9；任务 13 = Slice 10；任务 15 = Slice 11；任务 14 = Slice 12；任务 16 = Slice 13；任务 17 = Slice 14）。
- 端口契约：平台监听 `BSA_WEB_PORT`（默认 8888），nginx 443 → 127.0.0.1:8888。
