from __future__ import annotations

import contextlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from bsa.cli import main
from bsa.domain.models import (
    BranchResult,
    BuildOutcome,
    CommitResult,
    Conclusion4,
    Report,
)
from bsa.report.renderer import build_email_body, render_html_report
from bsa.rules import Classification
from bsa.scheduler.cycle import (
    _derive_cycle_id,
    cleanup_worktrees,
    list_cycle_records,
    run_cycle,
)
from tests.test_config import valid_env
from tests.test_graph_nodes import (
    DEVELOP,
    TARGET,
    base_state,
    make_ctx,
    write_branch_md,
)
from tests.test_workflow import FakeWorktreeGit, inject_worktree_git


def _populate(ctx, shas: list[str]) -> None:
    git = ctx.git
    git.window_shas = {f"origin/{DEVELOP}": list(shas)}
    for sha in shas:
        git.changed[sha] = ["plat/demo.c"]
        git.patches[sha] = "+x"
        git.patch_ids[sha] = f"pid-{sha}"
        ctx.classify.results[sha] = Classification(
            is_bug_fix=True,
            recognition_source="machine:[BUG]",
            needs_agent=False,
        )
        ctx.conclude.results[(sha, TARGET)] = Conclusion4(
            kind="NeedSync", evidence=[], confidence="high"
        )
    inject_worktree_git(ctx, FakeWorktreeGit(), TARGET, cycle_id="cycle-2026-08-20")


def test_validate_config_exits_zero_with_env(monkeypatch, tmp_path):
    env = valid_env()
    env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr("bsa.config.settings.os.environ", env)

    assert main(["validate-config"]) == 0


def test_validate_config_missing_env_exits_two(monkeypatch, tmp_path):
    monkeypatch.setattr("bsa.config.settings.os.environ", {})
    monkeypatch.chdir(tmp_path)  # 隔离 cwd 的 .env，确保配置缺失

    assert main(["validate-config"]) == 2


def test_cleanup_worktrees_removes_stale_only(tmp_path):
    ctx = make_ctx(tmp_path)
    wt_root = Path(ctx.settings.worktree_root)
    ctx.git.worktrees = [
        wt_root / f"{TARGET}-cycle-2026-08-19",
        wt_root / f"{TARGET}-cycle-2026-08-20",
        tmp_path / "repo",
    ]

    cleanup_worktrees(ctx, "cycle-2026-08-20")

    removed = [args[0] for name, args in ctx.git.calls if name == "remove_worktree"]
    assert removed == [wt_root / f"{TARGET}-cycle-2026-08-19"]


def test_cleanup_worktrees_ignores_failed_removal(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.git.worktrees = [
        Path(ctx.settings.worktree_root) / f"{TARGET}-cycle-2026-08-19",
        Path(ctx.settings.worktree_root) / f"{TARGET}-cycle-2026-08-18",
    ]
    ctx.git.fail_remove_worktree = True
    cleanup_worktrees(ctx, "cycle-2026-08-20")
    removed = [args[0] for name, args in ctx.git.calls if name == "remove_worktree"]
    assert len(removed) == 2


def test_run_cycle_invokes_worktree_cleanup(tmp_path):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    _populate(ctx, ["a1"])
    stale = Path(ctx.settings.worktree_root) / f"{TARGET}-cycle-2026-08-19"
    ctx.git.worktrees = [stale]

    assert run_cycle("2026-08-20", context=ctx) == 0

    assert any(name == "list_worktrees" for name, args in ctx.git.calls)
    assert ("remove_worktree", (stale,)) in ctx.git.calls


def test_run_cycle_dry_run_full_cycle_produces_artifacts(tmp_path):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    _populate(ctx, ["a1"])

    code = run_cycle("2026-08-20", dry_run=True, context=ctx)

    assert code == 0
    cycle_dir = Path(ctx.settings.log_dir) / "cycle-2026-08-20"
    assert (cycle_dir / "report.html").exists()
    assert (cycle_dir / "decisions.json").exists()
    assert (cycle_dir / "run.log").exists()
    # G11：投影数据源落盘结构化 state.json
    assert (cycle_dir / "state.json").exists()
    state_json = json.loads((cycle_dir / "state.json").read_text(encoding="utf-8"))
    assert state_json["status"] == "SUCCESS"
    assert state_json["cycle_id"] == "cycle-2026-08-20"
    record = json.loads((cycle_dir / "cycle.json").read_text(encoding="utf-8"))
    assert record["status"] == "SUCCESS"
    assert record["mail_status"] == "skipped"
    html = (cycle_dir / "report.html").read_text(encoding="utf-8")
    assert "检测信息" in html
    assert "Action Required" in html
    assert "同步执行结果" in html


def test_run_cycle_window_override_honored(tmp_path):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    _populate(ctx, ["a1"])

    code = run_cycle(
        "2026-08-20",
        since="2026-08-18T22:00:00+08:00",
        until="2026-08-19T22:00:00+08:00",
        context=ctx,
    )

    assert code == 0
    recorded = [args for name, args in ctx.git.calls if name == "commits_in_window"]
    assert recorded[0][0] == "2026-08-18T22:00:00+08:00"
    assert recorded[0][1] == "2026-08-19T22:00:00+08:00"


def test_manual_scan_passes_window_override_to_run_cycle(monkeypatch, tmp_path):
    seen: dict = {}

    def fake_run_cycle(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return 0

    env = valid_env()
    env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr("bsa.config.settings.os.environ", env)
    monkeypatch.setattr("bsa.cli.run_cycle", fake_run_cycle)

    code = main(
        [
            "manual-scan",
            "--since",
            "2026-08-18T22:00:00+08:00",
            "--until",
            "2026-08-19T22:00:00+08:00",
        ]
    )

    assert code == 0
    assert seen["kwargs"]["since"] == "2026-08-18T22:00:00+08:00"
    assert seen["kwargs"]["until"] == "2026-08-19T22:00:00+08:00"
    # P2-7：登记/执行用同一 scan-* 周期 id
    assert seen["kwargs"]["cycle_id"] == (
        "scan-2026-08-18T22:00:00+08:00-2026-08-19T22:00:00+08:00"
    )


def test_run_cycle_exception_records_failed_with_error(monkeypatch, tmp_path):
    """run_cycle 抛异常时必须写终态，不能留 running 僵尸行。

    异常逃出 _cmd_run_cycle 后若无人写终态，任务行永久停在 register_start 写的
    running（finished_at/error 皆 NULL），平台一直显示「运行中」；且 cycle 任务
    target=NULL，不受 idx_tasks_active_target 约束，会静默累积。
    """
    import sqlite3

    env = valid_env()
    env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr("bsa.config.settings.os.environ", env)

    def boom(*args, **kwargs):
        raise RuntimeError("矩阵解析为空")

    monkeypatch.setattr("bsa.cli.run_cycle", boom)

    assert main(["run-cycle", "--date", "2026-08-20"]) == 1

    conn = sqlite3.connect(str(tmp_path / "logs" / "platform.sqlite3"))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 1").fetchone()
    assert row["state"] == "failed"
    assert "矩阵解析为空" in (row["error"] or "")
    assert row["finished_at"] is not None


def test_run_cycle_settings_failure_leaves_no_running_row(monkeypatch, tmp_path):
    """load_settings 在 register_start 之前就炸时，不许留 running 行。

    settings/log_dir/task_id 都在 try 内赋值，except 里引用会 NameError —— 必须
    先绑定再守卫，否则「修僵尸行」的收尾自己会把 CLI 变成崩溃。
    """
    import sqlite3

    monkeypatch.setattr("bsa.config.settings.os.environ", {})
    monkeypatch.chdir(tmp_path)  # 隔离 cwd 的 .env，确保配置缺失

    assert main(["run-cycle", "--date", "2026-08-20"]) == 1

    db = tmp_path / "logs" / "platform.sqlite3"
    if db.exists():
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        stuck = conn.execute(
            "SELECT COUNT(*) c FROM tasks WHERE state='running'"
        ).fetchone()
        assert stuck["c"] == 0


def test_manual_scan_exception_records_failed_with_error(monkeypatch, tmp_path):
    """manual-scan 与 run-cycle 同构：异常必须落终态 + 原因。"""
    import sqlite3

    env = valid_env()
    env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr("bsa.config.settings.os.environ", env)

    def boom(*args, **kwargs):
        raise RuntimeError("扫描窗口非法")

    monkeypatch.setattr("bsa.cli.run_cycle", boom)

    assert main(
        ["manual-scan", "--since", "2026-08-18T22:00:00+08:00",
         "--until", "2026-08-19T22:00:00+08:00"]
    ) == 1

    conn = sqlite3.connect(str(tmp_path / "logs" / "platform.sqlite3"))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 1").fetchone()
    assert row["state"] == "failed"
    assert "扫描窗口非法" in (row["error"] or "")


def test_manual_scan_registers_task_source_cli(monkeypatch, tmp_path):
    # P2-7：manual-scan CLI 直启登记 kind=cycle 任务（source=cli）
    import sqlite3

    env = valid_env()
    env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr("bsa.config.settings.os.environ", env)
    monkeypatch.setattr("bsa.cli.run_cycle", lambda *args, **kw: 0)

    assert main(
        ["manual-scan", "--since", "2026-08-18T22:00:00+08:00",
         "--until", "2026-08-19T22:00:00+08:00"]
    ) == 0

    conn = sqlite3.connect(str(tmp_path / "logs" / "platform.sqlite3"))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM tasks").fetchone()
    assert row is not None
    assert row["kind"] == "cycle"
    assert row["source"] == "cli"
    assert row["cycle_id"].startswith("scan-")


def test_run_cycle_cli_passes_dry_run(monkeypatch):
    seen: dict = {}

    def fake_run_cycle(*args, **kwargs):
        seen["kwargs"] = kwargs
        return 0

    monkeypatch.setattr("bsa.cli.run_cycle", fake_run_cycle)

    assert main(["run-cycle", "--date", "2026-01-01", "--dry-run"]) == 0
    assert seen["kwargs"]["dry_run"] is True


def test_run_cycle_missing_env_exits_one(monkeypatch, tmp_path):
    monkeypatch.setattr("bsa.config.settings.os.environ", {})
    monkeypatch.chdir(tmp_path)  # 隔离 cwd 的 .env，确保配置缺失

    assert main(["run-cycle"]) == 1


@pytest.mark.parametrize(
    ("cycle_status", "expected_state"),
    [("SUCCESS", "succeeded"), ("FAILED", "failed"), ("PARTIAL", "failed")],
)
def test_run_cycle_folds_terminal_status_into_task(
    monkeypatch, tmp_path, cycle_status, expected_state
):
    # P0-2/G10：cycle 终态按 fold 规则折叠到 tasks.state，SUCCESS 才记 succeeded。
    import sqlite3

    env = valid_env()
    env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr("bsa.config.settings.os.environ", env)
    monkeypatch.setenv("BSA_CYCLE_ID", "cycle-2026-08-20")

    def fake_run_cycle(date, **kwargs):
        cid = kwargs.get("cycle_id") or f"cycle-{date}"
        d = Path(tmp_path / "logs") / cid
        d.mkdir(parents=True, exist_ok=True)
        (d / "cycle.json").write_text(
            json.dumps(
                {
                    "cycle_id": cid,
                    "status": cycle_status,
                    "report_path": None,
                    "mail_status": None,
                    "started_at": "2026-08-20T00:00:00",
                    "finished_at": "2026-08-20T00:00:01",
                }
            ),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr("bsa.cli.run_cycle", fake_run_cycle)

    assert main(["run-cycle", "--date", "2026-08-20"]) == 0

    conn = sqlite3.connect(str(tmp_path / "logs" / "platform.sqlite3"))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM tasks").fetchone()
    assert row["state"] == expected_state
    assert row["cycle_id"] == "cycle-2026-08-20"
    # 非 SUCCESS 时必须留下原因：平台只读 tasks.error，cycle.json 不存原因。
    if expected_state == "failed":
        assert (row["error"] or "").strip()
    else:
        assert not row["error"]


def test_run_cycle_failed_folds_node_reason_into_task_error(monkeypatch, tmp_path):
    """折叠为 failed 时，tasks.error 要带上节点级失败原文。

    只写状态不写原因，平台就只剩一个无因的「失败」——正是本次复盘的痛点。
    """
    import sqlite3

    env = valid_env()
    env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr("bsa.config.settings.os.environ", env)
    monkeypatch.setenv("BSA_CYCLE_ID", "cycle-2026-08-20")

    def fake_run_cycle(date, **kwargs):
        cid = kwargs.get("cycle_id") or f"cycle-{date}"
        d = Path(tmp_path / "logs") / cid
        d.mkdir(parents=True, exist_ok=True)
        (d / "cycle.json").write_text(
            json.dumps({"cycle_id": cid, "status": "FAILED", "started_at": "x",
                        "finished_at": "y", "report_path": None, "mail_status": None}),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr("bsa.cli.run_cycle", fake_run_cycle)
    monkeypatch.setattr(
        "bsa.cli.cycle_failure_text",
        lambda settings, cycle_id, **kw: "周期失败：branch_matrix: 矩阵解析为空",
    )

    assert main(["run-cycle", "--date", "2026-08-20"]) == 0

    conn = sqlite3.connect(str(tmp_path / "logs" / "platform.sqlite3"))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM tasks").fetchone()
    assert row["state"] == "failed"
    assert "branch_matrix" in (row["error"] or "")
    assert "矩阵解析为空" in (row["error"] or "")


def test_run_cycle_checkpoint_resume_reuses_state(tmp_path):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    _populate(ctx, ["a1"])

    assert run_cycle("2026-08-20", context=ctx) == 0
    runner_calls = len(ctx.runner.build_calls)
    fetch_calls = [c for c in ctx.git.calls if c[0] == "fetch_all"]

    assert run_cycle("2026-08-20", context=ctx) == 0

    assert len(ctx.runner.build_calls) == runner_calls
    assert len([c for c in ctx.git.calls if c[0] == "fetch_all"]) == len(fetch_calls)


def test_status_shows_latest_cycle(monkeypatch, tmp_path, capsys):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    _populate(ctx, ["a1"])
    assert run_cycle("2026-08-20", context=ctx) == 0

    env = valid_env()
    env["LOG_DIR"] = str(ctx.settings.log_dir)
    monkeypatch.setattr("bsa.config.settings.os.environ", env)

    assert main(["status"]) == 0
    captured = capsys.readouterr().out
    assert "cycle-2026-08-20" in captured
    assert "SUCCESS" in captured


def test_render_html_report_three_sections(tmp_path):
    state = base_state()
    state["decisions"] = {
        "abc123": {TARGET: Conclusion4(kind="NeedSync", evidence=["ev"], confidence="high")}
    }
    state["branch_results"] = {
        TARGET: BranchResult(
            target_branch=TARGET,
            worktree_path=str(tmp_path),
            status="SUCCESS",
            commits=[
                CommitResult(
                    sha="abc123",
                    cherry_pick="OK",
                    conflict_resolution=None,
                    build={
                        "RTL9617C": BuildOutcome(
                            model="RTL9617C",
                            status="OK",
                            log_path="/l",
                            errors=[],
                            agent_attempts=0,
                            fix_diff=None,
                        )
                    },
                )
            ],
            patch_path=str(tmp_path / "p.patch"),
            stop_reason=None,
        )
    }
    report = Report(
        cycle_id="cycle-2026-08-20",
        html_path=tmp_path / "report.html",
        summary={"status": "REPORTED", "commits_detected": 1},
        action_required=[],
        decisions_json_path=tmp_path / "decisions.json",
    )

    path = render_html_report(state, report)

    assert path == tmp_path / "report.html"
    html = path.read_text(encoding="utf-8")
    assert "检测信息" in html
    assert "Action Required" in html
    assert "同步执行结果" in html
    assert "abc123" in html
    assert TARGET in html

    body = build_email_body(report, state)
    assert "cycle-2026-08-20" in body
    assert "NeedSync 1" in body
    assert "检测 commit" in body


class _FixedClock:
    _now = datetime(2026, 8, 20, 10, 0, 0)

    @classmethod
    def now(cls, tz=None):
        return cls._now

    @classmethod
    def strptime(cls, value, fmt):
        return datetime.strptime(value, fmt)


def test_derive_cycle_id_same_physical_date_always_same(monkeypatch):
    monkeypatch.setattr("bsa.scheduler.cycle.datetime", _FixedClock)

    assert _derive_cycle_id(None) == "cycle-2026-08-20"
    assert _derive_cycle_id("2026-08-20") == "cycle-2026-08-20"
    assert _derive_cycle_id("20260820") == "cycle-2026-08-20"


def test_list_cycle_records_orders_by_started_at(tmp_path):
    log_dir = tmp_path / "logs"
    for cid, ts in [
        ("cycle-2026-08-21", "2026-08-20T09:00:00"),
        ("cycle-2026-08-20", "2026-08-21T09:00:00"),
    ]:
        d = log_dir / cid
        d.mkdir(parents=True)
        (d / "cycle.json").write_text(
            json.dumps(
                {
                    "cycle_id": cid,
                    "status": "REPORTED",
                    "report_path": None,
                    "mail_status": None,
                    "started_at": ts,
                    "finished_at": ts,
                }
            ),
            encoding="utf-8",
        )

    records = list_cycle_records(log_dir)

    assert [r["cycle_id"] for r in records] == [
        "cycle-2026-08-21",
        "cycle-2026-08-20",
    ]


def test_status_picks_latest_by_started_at(monkeypatch, tmp_path, capsys):
    log_dir = tmp_path / "logs"
    for cid, ts in [
        ("cycle-2026-08-21", "2026-08-20T09:00:00"),
        ("cycle-2026-08-20", "2026-08-21T09:00:00"),
    ]:
        d = log_dir / cid
        d.mkdir(parents=True)
        (d / "cycle.json").write_text(
            json.dumps(
                {
                    "cycle_id": cid,
                    "status": "REPORTED",
                    "report_path": None,
                    "mail_status": None,
                    "started_at": ts,
                    "finished_at": ts,
                }
            ),
            encoding="utf-8",
        )

    env = valid_env()
    env["LOG_DIR"] = str(log_dir)
    monkeypatch.setattr("bsa.config.settings.os.environ", env)

    assert main(["status"]) == 0
    captured = capsys.readouterr().out
    assert "cycle-2026-08-20" in captured
    assert "cycle-2026-08-21" not in captured


def _sync_branch_result(status: str, target: str = "feat/x") -> BranchResult:
    return BranchResult(
        target_branch=target,
        worktree_path="/wt",
        status=status,
        commits=[],
        patch_path=None,
        stop_reason=None,
    )


def _sync_env(monkeypatch, tmp_path, status: str):
    """让 ``bsa sync`` 走到 run_sync_command 后按给定 status 收尾的 env。"""
    env = valid_env()
    env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr("bsa.config.settings.os.environ", env)
    monkeypatch.setattr("bsa.cli.manual_cycle_id", lambda: "cycle-2026-08-24")
    monkeypatch.setattr("bsa.cli.build_graph_context", lambda settings, cycle_id=None: object())
    monkeypatch.setattr(
        "bsa.cli.build_source_target_batch",
        lambda ctx, src, target, since, until: (["abc123"], {}),
    )
    monkeypatch.setattr("bsa.cli.open_checkpointer", contextlib.nullcontext)
    monkeypatch.setattr(
        "bsa.cli.run_sync_command",
        lambda ctx, cycle_id=None, target=None, batch=None, checkpointer=None: {
            "branch_results": {"feat/x": _sync_branch_result(status)}
        },
    )


def test_sync_success_returns_zero_on_stdout(monkeypatch, tmp_path, capsys):
    _sync_env(monkeypatch, tmp_path, "SUCCESS")
    assert main(["sync", "main", "feat/x"]) == 0
    assert "SUCCESS" in capsys.readouterr().out


@pytest.mark.parametrize("status", ["FAILED", "PARTIAL", "MANUAL"])
def test_sync_bad_status_returns_nonzero_on_stderr(monkeypatch, tmp_path, capsys, status):
    # I2：FAILED/PARTIAL/MANUAL 同步不得伪装成功，平台 runner 按非零返回码标记 failed
    _sync_env(monkeypatch, tmp_path, status)
    assert main(["sync", "main", "feat/x"]) == 1
    captured = capsys.readouterr()
    assert status in captured.err


def test_sync_cli_direct_registers_task_source_cli(monkeypatch, tmp_path):
    # CLI 直启（无 BSA_TASK_ID）→ 引擎 task_reporter 登记 source=cli 任务
    import sqlite3

    _sync_env(monkeypatch, tmp_path, "SUCCESS")
    assert main(["sync", "main", "feat/x"]) == 0
    conn = sqlite3.connect(str(tmp_path / "logs" / "platform.sqlite3"))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM tasks").fetchone()
    assert row is not None
    assert row["source"] == "cli"
    assert row["kind"] == "sync"
    assert row["target"] == "feat/x"
    assert row["state"] == "succeeded"
    assert row["cycle_id"] == "cycle-2026-08-24"


def test_sync_executor_triggered_reuses_task_id(monkeypatch, tmp_path):
    # executor 触发（BSA_TASK_ID 存在）→ 引擎复用该行回填 cycle_id，不 INSERT
    import sqlite3

    from bsa_web.db import init_db
    from bsa_web.runner import enqueue_task

    db = init_db(tmp_path / "logs" / "platform.sqlite3")
    tid = enqueue_task(db, "sync", "alice", "feat/x", src="main")
    _sync_env(monkeypatch, tmp_path, "SUCCESS")
    monkeypatch.setenv("BSA_TASK_ID", str(tid))
    assert main(["sync", "main", "feat/x"]) == 0
    conn = sqlite3.connect(str(tmp_path / "logs" / "platform.sqlite3"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM tasks").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == tid
    assert rows[0]["source"] == "web"
    assert rows[0]["cycle_id"] == "cycle-2026-08-24"


def test_sync_failed_cli_direct_registers_failed(monkeypatch, tmp_path):
    import sqlite3

    _sync_env(monkeypatch, tmp_path, "FAILED")
    assert main(["sync", "main", "feat/x"]) == 1
    conn = sqlite3.connect(str(tmp_path / "logs" / "platform.sqlite3"))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM tasks").fetchone()
    assert row["state"] == "failed"
    assert row["source"] == "cli"
    # 分支级失败必须写原因：平台任务表只显示 tasks.error。
    assert "FAILED" in (row["error"] or "")


@pytest.mark.parametrize("status", ["PARTIAL", "MANUAL"])
def test_sync_bad_status_records_error_with_status(
    monkeypatch, tmp_path, capsys, status
):
    import sqlite3

    _sync_env(monkeypatch, tmp_path, status)
    assert main(["sync", "main", "feat/x"]) == 1
    capsys.readouterr()
    conn = sqlite3.connect(str(tmp_path / "logs" / "platform.sqlite3"))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM tasks").fetchone()
    assert row["state"] == "failed"
    assert status in (row["error"] or "")
    assert "feat/x" in (row["error"] or "")
