"""TaskRunner 单测：状态机、同 target 并发拒绝、子进程生命周期。

run_func 注入替换真实 CLI 子进程调用，便于无副作用的单测。
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from bsa_web.db import init_db
from bsa_web.runner import TaskRunner, cleanup_task, enqueue_task


def _make_runner(tmp_path, *, run_func=None, timeout_sec=3600):
    db = init_db(tmp_path / "logs" / "platform.sqlite3")
    return TaskRunner(db, str(tmp_path / "logs"), run_func=run_func, timeout_sec=timeout_sec)


def _engine_finish(env, *, state, cycle_id=None, error=None):
    """模拟 V1 引擎通过 task_reporter 写终态（executor 注入 BSA_TASK_ID/LOG_DIR）。"""
    from bsa.commands.task_reporter import register_finish

    register_finish(
        env["LOG_DIR"], int(env["BSA_TASK_ID"]), state=state,
        cycle_id=cycle_id or "cycle-test", error=error,
    )


def _ok_run(cmd, env):
    _engine_finish(env, state="succeeded")
    return 0, "ok", ""


def _submit_sync(runner, target="feat/x", src="main", shas=None):
    return runner.submit("sync", "alice", target, shas=shas, src=src)


def _wait_state(db, task_id: int, wanted: str, timeout: float = 5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is not None and row["state"] == wanted:
            return True
        time.sleep(0.02)
    return False


class TestStateMachine:
    def test_submit_then_running_then_succeeded(self, tmp_path):
        release = threading.Event()

        def fake_run(cmd, env):
            release.wait(5)
            _engine_finish(env, state="succeeded")
            return 0, "同步完成", ""

        runner = _make_runner(tmp_path, run_func=fake_run)
        runner.start()
        task_id = _submit_sync(runner)
        assert task_id is not None
        assert _wait_state(runner.db, task_id, "running")
        release.set()
        runner.join(timeout=5)
        task = runner.get(task_id)
        assert task["state"] == "succeeded"
        assert task["finished_at"] is not None

    def test_submit_rerun_queued_then_succeeded(self, tmp_path):
        runner = _make_runner(tmp_path, run_func=_ok_run)
        runner.start()
        task_id = runner.submit("rerun", "alice", "feat/x", fresh=True)
        assert task_id is not None
        runner.join(timeout=5)
        assert runner.get(task_id)["state"] == "succeeded"

    def test_engine_registers_cycle_id_on_success(self, tmp_path):
        # 引擎（task_reporter）写终态时携带 cycle_id → tasks.cycle_id 落库
        def fake_run(cmd, env):
            _engine_finish(env, state="succeeded", cycle_id="manual-20260825-101010-12345")
            return 0, "同步完成", ""

        runner = _make_runner(tmp_path, run_func=fake_run)
        runner.start()
        task_id = _submit_sync(runner)
        runner.join(timeout=5)
        task = runner.get(task_id)
        assert task["state"] == "succeeded"
        assert task["cycle_id"] == "manual-20260825-101010-12345"

    def test_engine_registers_failed_with_error(self, tmp_path):
        # 引擎写 failed 终态（含 error）→ executor 不覆盖
        def fake_run(cmd, env):
            _engine_finish(env, state="failed", cycle_id="manual-20260825-111111-9999",
                           error="build error")
            return 1, "stdout", "build error"

        runner = _make_runner(tmp_path, run_func=fake_run)
        runner.start()
        task_id = _submit_sync(runner)
        runner.join(timeout=5)
        task = runner.get(task_id)
        assert task["state"] == "failed"
        assert task["cycle_id"] == "manual-20260825-111111-9999"
        assert "build error" in task["error"]

    def test_engine_writes_final_state_executor_does_not_override(self, tmp_path):
        # 引擎已写终态时，executor 不重复写（不覆盖引擎的状态/cycle_id）
        def fake_run(cmd, env):
            _engine_finish(env, state="succeeded", cycle_id="rerun-feat/x-20260825-093000-4242")
            return 1, "引擎已写终态，返回码非零也不覆盖", ""

        runner = _make_runner(tmp_path, run_func=fake_run)
        runner.start()
        task_id = runner.submit("rerun", "alice", "feat/x")
        runner.join(timeout=5)
        task = runner.get(task_id)
        assert task["state"] == "succeeded"
        assert task["cycle_id"] == "rerun-feat/x-20260825-093000-4242"

    def test_executor_fallback_failed_when_engine_did_not_finish(self, tmp_path):
        # 引擎未写终态（如配置错误早退）→ executor 兜底标 failed
        def fake_run(cmd, env):
            return 2, "", "配置错误"

        runner = _make_runner(tmp_path, run_func=fake_run)
        runner.start()
        task_id = _submit_sync(runner)
        runner.join(timeout=5)
        task = runner.get(task_id)
        assert task["state"] == "failed"
        assert "配置错误" in task["error"]
        assert task["finished_at"] is not None

    def test_timeout_marks_failed(self, tmp_path):
        def fake_run(cmd, env):
            raise subprocess.TimeoutExpired(cmd, timeout=3600)

        runner = _make_runner(tmp_path, run_func=fake_run)
        runner.start()
        task_id = _submit_sync(runner)
        runner.join(timeout=5)
        task = runner.get(task_id)
        assert task["state"] == "failed"
        assert "超时" in task["error"]

    def test_get_missing_returns_none(self, tmp_path):
        runner = _make_runner(tmp_path)
        assert runner.get(999) is None

    def test_unknown_kind_raises(self, tmp_path):
        runner = _make_runner(tmp_path)
        with pytest.raises(ValueError):
            runner.submit("bogus", "alice", "feat/x")

    def test_queued_reports_wait_reason(self, tmp_path):
        runner = _make_runner(tmp_path, run_func=_ok_run)
        task_id = runner.submit("sync", "alice", "feat/x", src="main")
        task = runner.get(task_id)
        assert task["state"] == "queued"
        assert "等待" in task["wait_reason"]


class TestConcurrency:
    def test_same_target_queued_submit_rejected(self, tmp_path):
        runner = _make_runner(tmp_path, run_func=_ok_run)
        first = _submit_sync(runner)
        assert first is not None
        assert _submit_sync(runner) is None
        # 不同 target 不互相阻塞
        assert _submit_sync(runner, target="feat/y") is not None

    def test_same_target_running_submit_rejected(self, tmp_path):
        db = init_db(tmp_path / "platform.sqlite3")
        db.execute(
            "INSERT INTO tasks(kind, user, target, state, created_at) "
            "VALUES ('sync', 'alice', 'feat/x', 'running', 't')"
        )
        db.commit()
        runner = TaskRunner(db, str(tmp_path / "logs"), run_func=_ok_run)
        assert runner.submit("sync", "alice", "feat/x", src="main") is None

    def test_unique_index_blocks_duplicate_submit(self, tmp_path):
        """同一 queued/running target 重复提交命中唯一索引返回 None。"""
        db = init_db(tmp_path / "platform.sqlite3")
        db.execute(
            "INSERT INTO tasks(kind, user, target, state, created_at) "
            "VALUES ('sync', 'alice', 'feat/x', 'queued', 't')"
        )
        db.commit()
        runner = TaskRunner(db, str(tmp_path / "logs"), run_func=_ok_run)
        assert runner.submit("sync", "alice", "feat/x", src="main") is None
        assert db.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 1

    def test_active_target_index_exists(self, tmp_path):
        db = init_db(tmp_path / "platform.sqlite3")
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
            ("idx_tasks_active_target",),
        ).fetchall()
        assert len(rows) == 1


class TestStaleRecovery:
    def test_start_reconciles_running_without_progress_failed_keeps_queued(self, tmp_path):
        # executor 重启对账：running 无进度现场 → failed（不误杀为平台重启中断）；
        # queued → 保留待重新认领（不直接对账，交给认领循环）。
        db = init_db(tmp_path / "platform.sqlite3")
        db.execute(
            "INSERT INTO tasks(kind,user,target,state,created_at) "
            "VALUES ('sync','alice','feat/x','running','t')"
        )
        db.execute(
            "INSERT INTO tasks(kind,user,target,state,created_at) "
            "VALUES ('sync','alice','feat/y','queued','t')"
        )
        db.commit()
        runner = TaskRunner(
            db, str(tmp_path / "logs"), run_func=_ok_run, cycle_probe=lambda cid: False
        )
        runner._reconcile_on_start()  # 直接测对账，不启动认领线程
        rows = dict(
            (row["target"], dict(row))
            for row in db.execute("SELECT * FROM tasks").fetchall()
        )
        assert rows["feat/x"]["state"] == "failed"
        assert "中断" in rows["feat/x"]["error"]
        assert rows["feat/x"]["finished_at"] is not None
        # queued 保留，等待重新认领
        assert rows["feat/y"]["state"] == "queued"

    def test_start_marks_running_with_progress_interrupted(self, tmp_path):
        # running 且 state.sqlite3 有 checkpoint 进度 → interrupted（可续跑）
        db = init_db(tmp_path / "platform.sqlite3")
        db.execute(
            "INSERT INTO tasks(kind,user,target,cycle_id,state,created_at) "
            "VALUES ('sync','alice','feat/x','manual-20260825-120000-1','running','t')"
        )
        db.commit()
        runner = TaskRunner(
            db, str(tmp_path / "logs"), run_func=_ok_run, cycle_probe=lambda cid: True
        )
        runner._reconcile_on_start()
        row = db.execute("SELECT * FROM tasks").fetchone()
        assert row["state"] == "interrupted"
        assert "可续跑" in row["error"]

    def test_stale_cleared_target_submittable_after_start(self, tmp_path):
        db = init_db(tmp_path / "platform.sqlite3")
        db.execute(
            "INSERT INTO tasks(kind,user,target,state,created_at) "
            "VALUES ('sync','alice','feat/x','running','t')"
        )
        db.commit()
        runner = TaskRunner(
            db, str(tmp_path / "logs"), run_func=_ok_run, cycle_probe=lambda cid: False
        )
        assert runner.submit("sync", "alice", "feat/x", src="main") is None
        runner.start()
        assert runner.submit("sync", "alice", "feat/x", src="main") is not None
        runner.join(timeout=5)

    def test_reconcile_converges_cycle_record_zombie_running(self, tmp_path):
        # P0-1：对账把 running cycle 任务标 interrupted 时，收敛 cycle.json 僵尸 running
        import json

        log_dir = tmp_path / "logs"
        db = init_db(log_dir / "platform.sqlite3")
        db.execute(
            "INSERT INTO tasks(kind,user,target,cycle_id,state,created_at) "
            "VALUES ('cycle','system',NULL,'cycle-2026-08-25','running','t')"
        )
        db.commit()
        cycle_dir = log_dir / "cycle-2026-08-25"
        cycle_dir.mkdir(parents=True)
        (cycle_dir / "cycle.json").write_text(
            json.dumps(
                {
                    "cycle_id": "cycle-2026-08-25",
                    "status": "running",
                    "started_at": "t",
                    "finished_at": "t",
                }
            )
        )
        runner = TaskRunner(
            db, str(log_dir), run_func=_ok_run, cycle_probe=lambda cid: True
        )
        runner._reconcile_on_start()

        row = db.execute("SELECT * FROM tasks").fetchone()
        assert row["state"] == "interrupted"
        record = json.loads((cycle_dir / "cycle.json").read_text(encoding="utf-8"))
        assert record["status"] == "interrupted"


    def test_reconcile_skips_alive_child(self, tmp_path, monkeypatch):
        # PID 探活：孤儿子进程仍在跑 → 不标 interrupted/failed，等其 register_finish 自愈
        db = init_db(tmp_path / "platform.sqlite3")
        db.execute(
            "INSERT INTO tasks(kind,user,target,cycle_id,state,created_at,pid) "
            "VALUES ('sync','alice','feat/x','manual-1','running','t', 99999)"
        )
        db.commit()
        runner = TaskRunner(
            db, str(tmp_path / "logs"), run_func=_ok_run, cycle_probe=lambda cid: True
        )
        monkeypatch.setattr("bsa_web.executor.os.kill", lambda pid, sig: None)
        runner._reconcile_on_start()
        row = db.execute("SELECT * FROM tasks").fetchone()
        assert row["state"] == "running"

    def test_reconcile_marks_dead_child(self, tmp_path):
        # 子进程已死（pid 不存在）→ 按有无进度标 interrupted/failed
        db = init_db(tmp_path / "platform.sqlite3")
        db.execute(
            "INSERT INTO tasks(kind,user,target,cycle_id,state,created_at,pid) "
            "VALUES ('sync','alice','feat/x','manual-1','running','t', 999999999)"
        )
        db.commit()
        runner = TaskRunner(
            db, str(tmp_path / "logs"), run_func=_ok_run, cycle_probe=lambda cid: True
        )
        runner._reconcile_on_start()
        row = db.execute("SELECT * FROM tasks").fetchone()
        assert row["state"] == "interrupted"


class TestSubprocessLifecycle:
    def test_default_cli_kills_process_on_timeout(self, tmp_path, monkeypatch):
        created = []

        class FakeProc:
            def __init__(self, *args, **kwargs):
                self.killed = False
                created.append(self)

            def communicate(self, timeout=None):
                raise subprocess.TimeoutExpired("cmd", timeout or 0)

            def kill(self):
                self.killed = True

        monkeypatch.setattr("bsa_web.executor.subprocess.Popen", FakeProc)
        runner = _make_runner(tmp_path, timeout_sec=0.01)
        with pytest.raises(subprocess.TimeoutExpired):
            runner._run_cli(
                [sys.executable, "-m", "bsa.cli", "sync", "main", "feat/x"],
                {"LOG_DIR": str(tmp_path / "logs")},
            )
        assert created and created[0].killed

    def test_default_cli_injects_log_dir(self, tmp_path, monkeypatch):
        seen = {}

        class FakeProc:
            def __init__(self, *args, **kwargs):
                seen["cmd"] = args[0]
                seen["env"] = kwargs.get("env", {})

            def communicate(self, timeout=None):
                return "out", "err"

            @property
            def returncode(self):
                return 0

        monkeypatch.setattr("bsa_web.executor.subprocess.Popen", FakeProc)
        runner = _make_runner(tmp_path)
        rc, out, err = runner._run_cli(
            [sys.executable, "-m", "bsa.cli", "sync", "main", "feat/x"],
            {"LOG_DIR": str(tmp_path / "logs")},
        )
        assert rc == 0
        assert seen["cmd"][-3:] == ["sync", "main", "feat/x"]
        assert seen["env"]["LOG_DIR"] == str(tmp_path / "logs")

    def test_run_cli_streams_output_to_task_log(self, tmp_path):
        from datetime import UTC, datetime

        from bsa_web.executor import task_log_tail

        runner = _make_runner(tmp_path)
        db = runner.db
        db.execute(
            "INSERT INTO tasks(kind, user, target, state, created_at, source) "
            "VALUES ('sync','alice','feat/x','running',?, 'web')",
            (datetime.now(UTC).isoformat(),),
        )
        db.commit()
        task_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        runner._last_task_id = task_id
        rc, out, err = runner._run_cli(
            [sys.executable, "-c", "import sys; print('hello task')"],
            {"LOG_DIR": str(tmp_path / "logs")},
        )
        assert rc == 0
        assert out == "" and err == ""
        assert "hello task" in task_log_tail(str(tmp_path / "logs"), task_id)


class TestCmdBuilding:
    def test_sync_cmd_with_src_target_sha(self, tmp_path):
        calls = []
        runner = _make_runner(
            tmp_path, run_func=lambda cmd, env: (calls.append(cmd) or (0, "", ""))
        )
        runner.start()
        _submit_sync(runner, src="main", target="feat/x", shas=["abc123", "def456"])
        runner.join(timeout=5)
        assert calls[0] == [
            sys.executable,
            "-m",
            "bsa.cli",
            "sync",
            "main",
            "feat/x",
            "--sha",
            "abc123",
            "def456",
        ]

    def test_sync_preassigns_cycle_id_and_injects_env(self, tmp_path, monkeypatch):
        # P2-2：executor 预生成 sync cycle_id 并注入 BSA_MANUAL_CYCLE_ID（运行期即知）
        seen = {}

        def fake_run(cmd, env):
            seen["cycle_id"] = env.get("BSA_MANUAL_CYCLE_ID")
            _engine_finish(env, state="succeeded", cycle_id=env.get("BSA_MANUAL_CYCLE_ID"))
            return 0, "ok", ""

        monkeypatch.setattr("bsa_web.executor.manual_cycle_id", lambda: "manual-preassigned-1")
        runner = _make_runner(tmp_path, run_func=fake_run)
        runner.start()
        task_id = _submit_sync(runner)
        runner.join(timeout=5)
        assert seen["cycle_id"] == "manual-preassigned-1"
        assert runner.get(task_id)["cycle_id"] == "manual-preassigned-1"

    def test_rerun_fresh_preassigns_cycle_id_and_injects_env(self, tmp_path, monkeypatch):
        # P2-2：fresh 重跑预生成 manual cycle_id 并注入 BSA_MANUAL_CYCLE_ID
        seen = {}

        def fake_run(cmd, env):
            seen["cycle_id"] = env.get("BSA_MANUAL_CYCLE_ID")
            _engine_finish(env, state="succeeded", cycle_id=env.get("BSA_MANUAL_CYCLE_ID"))
            return 0, "ok", ""

        monkeypatch.setattr("bsa_web.executor.manual_cycle_id", lambda: "manual-fresh-1")
        runner = _make_runner(tmp_path, run_func=fake_run)
        runner.start()
        task_id = runner.submit("rerun", "alice", "feat/x", fresh=True)
        runner.join(timeout=5)
        assert seen["cycle_id"] == "manual-fresh-1"
        assert runner.get(task_id)["cycle_id"] == "manual-fresh-1"

    def test_sync_cmd_without_sha(self, tmp_path):
        calls = []
        runner = _make_runner(
            tmp_path, run_func=lambda cmd, env: (calls.append(cmd) or (0, "", ""))
        )
        runner.start()
        _submit_sync(runner, src="main", target="feat/x")
        runner.join(timeout=5)
        assert calls[0] == [sys.executable, "-m", "bsa.cli", "sync", "main", "feat/x"]

    def test_rerun_cmd_with_fresh(self, tmp_path):
        calls = []
        runner = _make_runner(
            tmp_path, run_func=lambda cmd, env: (calls.append(cmd) or (0, "", ""))
        )
        runner.start()
        runner.submit("rerun", "alice", "feat/x", fresh=True)
        runner.join(timeout=5)
        assert calls[0] == [
            sys.executable,
            "-m",
            "bsa.cli",
            "rerun",
            "feat/x",
            "--fresh",
        ]

    def test_rerun_cmd_without_fresh(self, tmp_path):
        calls = []
        runner = _make_runner(
            tmp_path, run_func=lambda cmd, env: (calls.append(cmd) or (0, "", ""))
        )
        runner.start()
        runner.submit("rerun", "alice", "feat/x")
        runner.join(timeout=5)
        assert calls[0] == [sys.executable, "-m", "bsa.cli", "rerun", "feat/x"]


class TestTaskReporter:
    def test_register_start_cli_direct_inserts_running(self, tmp_path):
        # CLI 直启（无 BSA_TASK_ID）：register_start INSERT running source=cli
        from bsa.commands.task_reporter import register_finish, register_start

        task_id = register_start(
            str(tmp_path), kind="sync", target="feat/cli", cycle_id="manual-1",
            src="main",
        )
        conn = sqlite3.connect(str(tmp_path / "platform.sqlite3"))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert row["source"] == "cli"
        assert row["kind"] == "sync"
        assert row["cycle_id"] == "manual-1"
        assert row["target"] == "feat/cli"
        assert row["state"] == "running"
        register_finish(str(tmp_path), task_id, state="succeeded", cycle_id="manual-1")
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert row["state"] == "succeeded"
        assert row["finished_at"] is not None

    def test_register_start_executor_reuses_task_id(self, tmp_path, monkeypatch):
        # executor 触发（BSA_TASK_ID 存在）：register_start 回填 cycle_id 不 INSERT
        from bsa.commands.task_reporter import register_start

        db = init_db(tmp_path / "platform.sqlite3")
        tid = enqueue_task(
            db, "sync", "alice", "feat/x", src="main",
            cycle_id=None,
        )
        monkeypatch.setenv("BSA_TASK_ID", str(tid))
        got = register_start(
            str(tmp_path), kind="sync", target="feat/x", cycle_id="manual-1", src="main",
        )
        assert got == tid
        row = db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["cycle_id"] == "manual-1"
        # 未 INSERT 新行
        assert db.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 1

    def test_register_finish_writes_error(self, tmp_path):
        from bsa.commands.task_reporter import register_finish, register_start

        task_id = register_start(str(tmp_path), kind="sync", target="x", cycle_id="c1")
        register_finish(str(tmp_path), task_id, state="failed", cycle_id="c1", error="boom")
        conn = sqlite3.connect(str(tmp_path / "platform.sqlite3"))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert row["state"] == "failed"
        assert row["error"] == "boom"

    def test_register_start_cli_direct_conflict_returns_none(self, tmp_path):
        # P2-6：CLI 直启 INSERT 与 active 同 target 冲突 → 返回 None，不抛 IntegrityError
        from bsa.commands.task_reporter import register_start

        db = init_db(tmp_path / "platform.sqlite3")
        enqueue_task(db, "sync", "alice", "feat/x", src="main")
        got = register_start(
            str(tmp_path), kind="sync", target="feat/x", cycle_id="manual-1", src="main",
        )
        assert got is None


class TestCleanupTask:
    def test_cleanup_deletes_task_checkpoint_and_calls_worktree_cleaner(
        self, tmp_path
    ):
        from pathlib import Path

        db = init_db(tmp_path / "platform.sqlite3")
        tid = enqueue_task(
            db, "sync", "alice", "feat/x", shas=["a"], src="main",
            cycle_id="manual-20260825-120000-99",
        )
        # 造 state.sqlite3 的 checkpoint + writes
        state_db = Path(str(tmp_path)) / "state.sqlite3"
        conn = sqlite3.connect(str(state_db))
        conn.execute(
            "CREATE TABLE checkpoints(thread_id TEXT, checkpoint_ns TEXT, "
            "checkpoint_id TEXT, parent_checkpoint_id TEXT, type TEXT, "
            "checkpoint BLOB, metadata BLOB)"
        )
        conn.execute(
            "CREATE TABLE writes(thread_id TEXT, task_id TEXT, idx INTEGER, "
            "channel TEXT, type TEXT, value BLOB)"
        )
        conn.execute(
            "INSERT INTO checkpoints(thread_id, checkpoint_ns, checkpoint_id, type, checkpoint) "
            "VALUES ('manual-20260825-120000-99', 'x', 'c1', 'write', '{}')"
        )
        conn.execute(
            "INSERT INTO writes(thread_id, task_id, idx, channel, type, value) "
            "VALUES ('manual-20260825-120000-99', 't1', 0, 'status', 'str', 'x')"
        )
        conn.commit()
        conn.close()

        seen = {}

        def fake_cleaner(log_dir, target, cycle_id):
            seen["target"] = target
            seen["cycle_id"] = cycle_id
            return {"removed": True}

        result = cleanup_task(
            db, tid, str(tmp_path), worktree_cleaner=fake_cleaner
        )
        assert result["deleted"] is True
        assert result["checkpoint_deleted"] is True
        assert result["worktree_removed"] is True
        assert seen == {"target": "feat/x", "cycle_id": "manual-20260825-120000-99"}
        assert db.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 0
        # checkpoint + writes 已删
        conn = sqlite3.connect(str(state_db))
        assert conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0] == 0
        conn.close()

    def test_cleanup_missing_task_returns_not_deleted(self, tmp_path):
        db = init_db(tmp_path / "platform.sqlite3")
        result = cleanup_task(db, 999, str(tmp_path))
        assert result["deleted"] is False

    def test_cleanup_without_cycle_skips_checkpoint_and_worktree(self, tmp_path):
        db = init_db(tmp_path / "platform.sqlite3")
        tid = enqueue_task(db, "sync", "alice", "feat/x", src="main")
        result = cleanup_task(db, tid, str(tmp_path))
        assert result["deleted"] is True
        assert result["checkpoint_deleted"] is False
        assert result["worktree_removed"] is False


class TestCronScheduler:
    def test_schedule_enqueues_daily_cycle_task(self, tmp_path):
        # 调度器触发时插入 kind=cycle 任务（cycle_id 预生成 cycle-<date>）
        from datetime import datetime

        from bsa_web.runner import TaskExecutor

        db = init_db(tmp_path / "platform.sqlite3")
        now = datetime(2026, 8, 25, 0, 0, 0)
        runner = TaskExecutor(
            db,
            str(tmp_path / "logs"),
            run_func=_ok_run,
            now=lambda: now,
            poll_sec=0.01,
        )
        runner._enqueue_daily_cycle()
        row = db.execute("SELECT * FROM tasks").fetchone()
        assert row["kind"] == "cycle"
        assert row["cycle_id"] == "cycle-2026-08-25"
        assert row["state"] == "queued"
        assert row["source"] == "cron"

    def test_schedule_skips_when_active_cycle_exists(self, tmp_path):
        from datetime import datetime

        from bsa_web.runner import TaskExecutor

        db = init_db(tmp_path / "platform.sqlite3")
        runner = TaskExecutor(
            db,
            str(tmp_path / "logs"),
            run_func=_ok_run,
            now=lambda: datetime(2026, 8, 25, 0, 0, 0),
            poll_sec=0.01,
        )
        runner._enqueue_daily_cycle()
        runner._enqueue_daily_cycle()  # 同一时刻再触发：唯一索引兜底，不重复插入
        rows = db.execute("SELECT COUNT(*) AS n FROM tasks").fetchall()
        assert rows[0]["n"] == 1

    def test_schedule_does_not_fire_off_hour(self, tmp_path):
        from datetime import datetime

        from bsa_web.runner import TaskExecutor

        db = init_db(tmp_path / "platform.sqlite3")
        runner = TaskExecutor(
            db,
            str(tmp_path / "logs"),
            run_func=_ok_run,
            now=lambda: datetime(2026, 8, 25, 9, 30, 0),
            poll_sec=0.01,
        )
        fired = runner._schedule_tick(datetime(2026, 8, 25, 9, 30, 0), None)
        assert fired is False
        assert db.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 0

    def test_schedule_tick_fires_once_at_midnight(self, tmp_path):
        from datetime import datetime

        from bsa_web.runner import TaskExecutor

        db = init_db(tmp_path / "platform.sqlite3")
        runner = TaskExecutor(
            db,
            str(tmp_path / "logs"),
            run_func=_ok_run,
            now=lambda: datetime(2026, 8, 25, 0, 0, 0),
            poll_sec=0.01,
        )
        midnight = datetime(2026, 8, 25, 0, 0, 0)
        assert runner._schedule_tick(midnight, None) is True
        assert runner._schedule_tick(midnight, "2026-8-25") is False  # 当天已触发
        assert db.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 1

    def test_cycle_task_builds_run_cycle_cmd_with_env(self, tmp_path):
        from datetime import datetime

        from bsa_web.runner import TaskExecutor

        calls = []
        db = init_db(tmp_path / "platform.sqlite3")
        runner = TaskExecutor(
            db,
            str(tmp_path / "logs"),
            run_func=lambda cmd, env: (calls.append(cmd) or (0, "ok", "")),
            now=lambda: datetime(2026, 8, 25, 0, 0, 0),
            poll_sec=0.01,
        )
        runner._enqueue_daily_cycle()
        runner._claim_one()
        assert calls
        assert calls[0][3] == "run-cycle"
        assert "--date" in calls[0]
        assert "2026-08-25" in calls[0]

    def test_active_cycle_unique_index_exists(self, tmp_path):
        db = init_db(tmp_path / "platform.sqlite3")
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
            ("idx_tasks_active_cycle",),
        ).fetchall()
        assert len(rows) == 1

    def test_executor_single_instance_lock(self, tmp_path):
        from bsa_web.runner import TaskExecutor

        db = init_db(tmp_path / "platform.sqlite3")
        first = TaskExecutor(db, str(tmp_path / "logs"), run_func=_ok_run)
        with pytest.raises(RuntimeError, match="单实例冲突"):
            TaskExecutor(db, str(tmp_path / "logs"), run_func=_ok_run)
        first._lock.close()
