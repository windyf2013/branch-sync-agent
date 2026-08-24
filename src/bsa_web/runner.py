"""异步任务 runner：queued→running→succeeded/failed，后台单 worker 串行消费。

Web 平台运维操作（触发同步/重跑）经任务队列异步执行：``submit`` 写 tasks 表
（state=queued）并入队，daemon worker 逐个取出置 running，子进程调用 V1 CLI
（``bsa sync``/``bsa rerun``），终态置 succeeded/failed（失败捕获 CLI 返回码
与 stderr）。同 target 存在 running/queued 任务时拒绝新提交（返回 None）；
子进程超时（默认 3600s）kill 并标 failed。CLI 环境注入 ``LOG_DIR``（承任务 10
结论），其余 BSA env 从父进程继承。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime

SUPPORTED_KINDS = ("sync", "rerun")
DEFAULT_TIMEOUT_SEC = 3600

QUEUED_WAIT_REASON = "排队等待执行（同 target 串行，等待前序任务完成）"

_ERROR_MAX_LEN = 2000


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class TaskRunner:
    """任务队列 + 后台 worker。``run_func`` 可注入替换 CLI 子进程便于单测。"""

    def __init__(self, db, log_dir, *, run_func=None, timeout_sec=DEFAULT_TIMEOUT_SEC):
        self.db = db
        self.log_dir = str(log_dir)
        self._timeout_sec = timeout_sec
        self._run = run_func if run_func is not None else self._run_cli
        self._queue: queue.Queue = queue.Queue()
        self._cond = threading.Condition()
        self._active = 0
        self._started = False
        self._worker = threading.Thread(
            target=self._consume, daemon=True, name="bsa-task-runner"
        )

    def start(self) -> None:
        """启动 daemon worker（幂等）。"""
        if not self._started:
            self._started = True
            self._worker.start()

    def join(self, timeout: float | None = None) -> None:
        """等待已提交任务全部处理完（支持超时）。"""
        with self._cond:
            deadline = None if timeout is None else time.monotonic() + timeout
            while self._active > 0:
                if deadline is None:
                    self._cond.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._cond.wait(remaining)

    def submit(self, kind, user, target, shas=None, src=None, fresh=False) -> int | None:
        """入队新任务；同 target 有 running/queued 任务则拒绝，返回 None。"""
        if kind not in SUPPORTED_KINDS:
            raise ValueError(f"未知任务类型: {kind}")
        if self._has_active(target):
            return None
        shas_json = json.dumps(shas) if shas else None
        cur = self.db.execute(
            "INSERT INTO tasks(kind, user, target, src, shas, fresh, state, created_at) "
            "VALUES (?,?,?,?,?,?, 'queued', ?)",
            (kind, user, target, src, shas_json, 1 if fresh else 0, _now_iso()),
        )
        self.db.commit()
        task_id = cur.lastrowid
        with self._cond:
            self._active += 1
        self._queue.put(task_id)
        return task_id

    def _has_active(self, target) -> bool:
        row = self.db.execute(
            "SELECT id FROM tasks WHERE target=? AND state IN ('queued','running') LIMIT 1",
            (target,),
        ).fetchone()
        return row is not None

    def get(self, task_id) -> dict | None:
        """任务详情；queued 状态附带等待原因。"""
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return None
        task = dict(row)
        if task["state"] == "queued":
            task["wait_reason"] = QUEUED_WAIT_REASON
        return task

    def _consume(self) -> None:
        while True:
            task_id = self._queue.get()
            try:
                self._run_one(task_id)
            except Exception as exc:  # 兜底：未知异常也收敛为 failed
                self._mark(task_id, "failed", error=f"执行异常: {exc}")
            finally:
                self._queue.task_done()
                with self._cond:
                    self._active = max(self._active - 1, 0)
                    if self._active == 0:
                        self._cond.notify_all()

    def _run_one(self, task_id: int) -> None:
        row = self.get(task_id)
        if row is None:
            return
        self._mark(task_id, "running", started_at=_now_iso())
        cmd = self._build_cmd(row)
        try:
            returncode, stdout, stderr = self._run(cmd)
        except subprocess.TimeoutExpired:
            self._mark(
                task_id,
                "failed",
                error=f"执行超时（>{self._timeout_sec}s），已终止",
                finished_at=_now_iso(),
            )
            return
        if returncode == 0:
            self._mark(task_id, "succeeded", finished_at=_now_iso())
        else:
            error = (stderr or stdout or "执行失败").strip()[:_ERROR_MAX_LEN]
            self._mark(task_id, "failed", error=error, finished_at=_now_iso())

    def _build_cmd(self, row: dict) -> list[str]:
        """按任务行构造 CLI 命令（注入 sys.executable 保证同解释器）。"""
        cmd = [sys.executable, "-m", "bsa.cli", row["kind"]]
        if row["kind"] == "sync":
            cmd.append(row["src"] or "")
            cmd.append(row["target"])
            shas = json.loads(row["shas"]) if row["shas"] else None
            if shas:
                cmd.extend(["--sha", *shas])
        elif row["kind"] == "rerun":
            cmd.append(row["target"])
            if row["fresh"]:
                cmd.append("--fresh")
        return cmd

    def _mark(self, task_id, state, *, error=None, started_at=None, finished_at=None) -> None:
        fields = ["state=?"]
        params = [state]
        if error is not None:
            fields.append("error=?")
            params.append(error)
        if started_at is not None:
            fields.append("started_at=?")
            params.append(started_at)
        if finished_at is not None:
            fields.append("finished_at=?")
            params.append(finished_at)
        params.append(task_id)
        self.db.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id=?", params)
        self.db.commit()

    def _run_cli(self, cmd: list[str]) -> tuple[int, str, str]:
        """Popen 调 CLI；超时 kill。LOG_DIR 注入，其余 env 继承父进程。"""
        env = dict(os.environ)
        env["LOG_DIR"] = self.log_dir
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
        )
        try:
            stdout, stderr = proc.communicate(timeout=self._timeout_sec)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
        return proc.returncode, stdout, stderr
