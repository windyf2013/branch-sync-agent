"""TaskRunner 单测：状态机、同 target 并发拒绝、子进程生命周期。

run_func 注入替换真实 CLI 子进程调用，便于无副作用的单测。
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from bsa_web.db import init_db
from bsa_web.runner import TaskRunner


def _make_runner(tmp_path, *, run_func=None, timeout_sec=3600):
    db = init_db(tmp_path / "platform.sqlite3")
    return TaskRunner(db, str(tmp_path / "logs"), run_func=run_func, timeout_sec=timeout_sec)


def _ok_run(cmd):
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

        def fake_run(cmd):
            release.wait(5)
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

    def test_failed_captures_cli_stderr(self, tmp_path):
        def fake_run(cmd):
            return 1, "stdout", "同步失败: build error"

        runner = _make_runner(tmp_path, run_func=fake_run)
        runner.start()
        task_id = _submit_sync(runner)
        runner.join(timeout=5)
        task = runner.get(task_id)
        assert task["state"] == "failed"
        assert "build error" in task["error"]
        assert task["finished_at"] is not None

    def test_timeout_marks_failed(self, tmp_path):
        def fake_run(cmd):
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

    def test_unique_index_blocks_submit_when_precheck_bypassed(self, tmp_path, monkeypatch):
        """模拟 TOCTOU：预检通过后另一提交先插入，INSERT 命中唯一索引返回 None。"""
        db = init_db(tmp_path / "platform.sqlite3")
        db.execute(
            "INSERT INTO tasks(kind, user, target, state, created_at) "
            "VALUES ('sync', 'alice', 'feat/x', 'queued', 't')"
        )
        db.commit()
        runner = TaskRunner(db, str(tmp_path / "logs"), run_func=_ok_run)
        monkeypatch.setattr(runner, "_has_active", lambda target: False)
        assert runner.submit("sync", "alice", "feat/x", src="main") is None
        assert db.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 1

    def test_active_target_index_exists(self, tmp_path):
        db = init_db(tmp_path / "platform.sqlite3")
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
            ("idx_tasks_active_target",),
        ).fetchall()
        assert len(rows) == 1


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

        monkeypatch.setattr("bsa_web.runner.subprocess.Popen", FakeProc)
        runner = _make_runner(tmp_path, timeout_sec=0.01)
        with pytest.raises(subprocess.TimeoutExpired):
            runner._run_cli([sys.executable, "-m", "bsa.cli", "sync", "main", "feat/x"])
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

        monkeypatch.setattr("bsa_web.runner.subprocess.Popen", FakeProc)
        runner = _make_runner(tmp_path)
        rc, out, err = runner._run_cli(
            [sys.executable, "-m", "bsa.cli", "sync", "main", "feat/x"]
        )
        assert rc == 0
        assert seen["cmd"][-3:] == ["sync", "main", "feat/x"]
        assert seen["env"]["LOG_DIR"] == str(tmp_path / "logs")


class TestCmdBuilding:
    def test_sync_cmd_with_src_target_sha(self, tmp_path):
        calls = []
        runner = _make_runner(
            tmp_path, run_func=lambda cmd: (calls.append(cmd) or (0, "", ""))
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

    def test_sync_cmd_without_sha(self, tmp_path):
        calls = []
        runner = _make_runner(
            tmp_path, run_func=lambda cmd: (calls.append(cmd) or (0, "", ""))
        )
        runner.start()
        _submit_sync(runner, src="main", target="feat/x")
        runner.join(timeout=5)
        assert calls[0] == [sys.executable, "-m", "bsa.cli", "sync", "main", "feat/x"]

    def test_rerun_cmd_with_fresh(self, tmp_path):
        calls = []
        runner = _make_runner(
            tmp_path, run_func=lambda cmd: (calls.append(cmd) or (0, "", ""))
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
            tmp_path, run_func=lambda cmd: (calls.append(cmd) or (0, "", ""))
        )
        runner.start()
        runner.submit("rerun", "alice", "feat/x")
        runner.join(timeout=5)
        assert calls[0] == [sys.executable, "-m", "bsa.cli", "rerun", "feat/x"]
