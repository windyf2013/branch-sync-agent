"""任务执行守护进程：与 Web 平台进程完全解耦的独立 executor。

Web 平台（uvicorn）是纯管理/UI 层：只写任务意图（tasks 表 queued）、只读
状态；**不 spawn CLI、不持有执行线程**。本模块是唯一执行者：

- **认领循环**：轮询 tasks 表 queued → 原子 UPDATE 认领（running）→ 子进程调
  V1 CLI（``bsa sync``/``bsa rerun``/``bsa run-cycle``）→ 终态置
  succeeded/failed。同 target/周期并发被部分唯一索引兜底拒绝。
- **内置调度器**：每天 0:00 插入 ``kind='cycle'`` 的 cron 周期任务（统一进
  tasks 表，与手动任务同一生命周期/状态机）。
- **启动对账（打标记，不误杀）**：executor 重启时——
  - ``running`` 且 state.sqlite3 有该 cycle 的 checkpoint 进度 → 标
    ``interrupted``（保留 cycle_id/worktree，可续跑）
  - ``running`` 且无任何进度 → 标 ``failed``（真实原因）
  - ``queued`` → 保持 queued（重新认领）
  平台重启不影响任务执行（executor 独立进程）；executor 自身重启只打标记、
  由操作者手动续跑（rerun retained），不自动重放。
- **单实例守卫**：独立 ``logs/executor.lock``（不能锁 platform.sqlite3，
  web 已独占 flock 该文件）。

入口：``python -m bsa_web.executor``（deploy/bsa-executor.service）。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from bsa.commands.sync import manual_cycle_id
from bsa_web import failure, projection
from bsa_web.db import InstanceLock

DEFAULT_TIMEOUT_SEC = 3 * 3600  # 3 小时：RCIOS 单次全量编译约 27 分钟，多 commit 任务易超 1 小时

QUEUED_WAIT_REASON = "排队等待执行（同 target 串行，等待前序任务完成）"

_ERROR_MAX_LEN = 2000

# 调度器默认每天执行时间（本地时区）
_CRON_HOUR = 0
_CRON_MINUTE = 0
_SCHEDULE_POLL_SEC = 5

_logger = logging.getLogger("bsa_web.executor")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def task_detail_url(db, task_id: int) -> str | None:
    """任务对应任务详情页 URL；cycle_id 未回写（排队/执行中）返回 None。

    手动/重跑任务完成后 cycle_id 才落库，此函数供提交重定向与任务状态页判断
    "cycle_id 可用"时跳 /task/{cycle}/{target}，否则回退 /tasks/{id}。
    """
    row = db.execute(
        "SELECT cycle_id, target FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if row is None or not row["cycle_id"] or not row["target"]:
        return None
    return f"/task/{row['cycle_id']}/{row['target']}"


def enqueue_task(
    db,
    kind: str,
    user: str,
    target: str | None,
    *,
    shas: list[str] | None = None,
    src: str | None = None,
    fresh: bool = False,
    source: str = "web",
    cycle_id: str | None = None,
) -> int | None:
    """写入任务意图（queued）。由部分唯一索引兜底并发拒绝，返回 task_id 或 None。

    ``kind='cycle'`` 时 target 为 None（整周期），由 ``idx_tasks_active_cycle``
    保证同一时刻只有一个活动周期任务。``cycle_id`` 用于续跑：interrupted 任务
    重跑时带上来源周期 id，executor 据此 ``--cycle`` 保留现场续跑。
    """
    if kind not in ("sync", "rerun", "cycle"):
        raise ValueError(f"未知任务类型: {kind}")
    shas_json = json.dumps(shas) if shas else None
    try:
        cur = db.execute(
            "INSERT INTO tasks(kind, user, target, src, shas, fresh, state, "
            "created_at, source, cycle_id) VALUES (?,?,?,?,?,?, 'queued', ?, ?, ?)",
            (kind, user, target, src, shas_json, 1 if fresh else 0, _now_iso(), source, cycle_id),
        )
    except sqlite3.IntegrityError:
        db.rollback()
        return None
    db.commit()
    return cur.lastrowid


def get_task(db, task_id: int) -> dict | None:
    """任务详情；queued 状态附带等待原因。"""
    row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return None
    task = dict(row)
    if task["state"] == "queued":
        task["wait_reason"] = QUEUED_WAIT_REASON
    return task


def cleanup_task(
    db,
    task_id: int,
    log_dir: str,
    *,
    worktree_cleaner=None,
) -> dict:
    """联动删除任务记录：tasks 行 + checkpoint 线程 + 关联 worktree。

    删除任务 = 该工作生命周期终结，关联数据一并清理（用户决策）：
    - tasks 行（平台库）
    - state.sqlite3 中该 cycle 的 checkpoints + writes（平台与 V1 共享此库读）
    - worktree：经 V1 CLI 子进程 ``bsa cleanup-worktree``（持 flock，
      与周期清理串行；worktree 不存在幂等成功）

    ``worktree_cleaner`` 可注入替换子进程调用便于单测。返回清理结果 dict：
    ``task_id``、``deleted``（tasks 行是否删除）、``checkpoint_deleted``、
    ``worktree_removed``。
    """
    row = db.execute(
        "SELECT id, cycle_id, target FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if row is None:
        return {"task_id": task_id, "deleted": False}
    cycle_id = row["cycle_id"]
    target = row["target"]
    db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    db.commit()

    checkpoint_deleted = _delete_checkpoint(log_dir, cycle_id)

    worktree_removed = False
    if cycle_id and target:
        cleaner = worktree_cleaner if worktree_cleaner is not None else _cleanup_worktree_cli
        try:
            result = cleaner(log_dir, target, cycle_id)
            worktree_removed = bool(result.get("removed"))
        except (OSError, subprocess.SubprocessError):
            worktree_removed = False

    return {
        "task_id": task_id,
        "deleted": True,
        "checkpoint_deleted": checkpoint_deleted,
        "worktree_removed": worktree_removed,
    }


def _delete_checkpoint(log_dir: str, cycle_id: str | None) -> bool:
    """删除 state.sqlite3 中该 cycle 的 checkpoints/writes（幂等，失败吞掉）。"""
    if not cycle_id:
        return False
    state_db = Path(log_dir) / "state.sqlite3"
    if not state_db.is_file():
        return False
    try:
        conn = sqlite3.connect(str(state_db))
        conn.execute("DELETE FROM checkpoints WHERE thread_id=?", (cycle_id,))
        conn.execute("DELETE FROM writes WHERE thread_id=?", (cycle_id,))
        conn.commit()
        conn.close()
        return True
    except sqlite3.Error:
        return False


def _cleanup_worktree_cli(log_dir: str, target: str, cycle_id: str) -> dict:
    """经 V1 CLI 子进程删除 worktree（持 flock，与周期清理串行）。"""
    env = dict(os.environ)
    env["LOG_DIR"] = str(log_dir)
    proc = subprocess.run(
        [sys.executable, "-m", "bsa.cli", "cleanup-worktree", target, cycle_id],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    if proc.returncode != 0:
        raise subprocess.SubprocessError(proc.stderr or "cleanup-worktree failed")
    return {"removed": "removed=True" in proc.stdout or "removed=True" in proc.stderr}


def _cleanup_docker_cli(log_dir: str, cycle_id: str) -> None:
    """经 V1 CLI 子进程回收该周期 keep-alive 编译容器（docker rm -f，best-effort）。"""
    env = dict(os.environ)
    env["LOG_DIR"] = str(log_dir)
    subprocess.run(
        [sys.executable, "-m", "bsa.cli", "cleanup-task", cycle_id],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def cancel_task(
    db,
    log_dir: str,
    task_id: int,
    *,
    docker_cleaner=None,
    worktree_cleaner=None,
) -> dict:
    """取消一个任务：标 cancelled 终态 + 杀子进程 + 清 docker 容器 + 删 checkpoint/worktree。

    ``queued`` 只标终态（executor 认领时 ``WHERE state='queued'`` rowcount=0 不会执行）；
    ``running`` 先标终态（保证 executor 兜底 ``_run_one`` 见 ``state != 'running'`` 不再
    覆盖），再 SIGKILL 子进程（pid 在 tasks 行），清 docker 容器（真正停编译），最后删
    checkpoint 与 worktree。返回 ``{"cancelled": True, "task_id": ...}``；任务不存在返回
    ``{"cancelled": False}``。
    """
    row = db.execute(
        "SELECT id, state, pid, cycle_id, target FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if row is None:
        return {"cancelled": False, "task_id": task_id}
    now = _now_iso()
    db.execute(
        "UPDATE tasks SET state='cancelled', error='已取消（用户放弃）', finished_at=? WHERE id=?",
        (now, task_id),
    )
    db.commit()

    if row["state"] == "running":
        pid = row["pid"]
        if pid:
            try:
                os.kill(pid, 9)  # SIGKILL：终止 executor Popen 的 CLI 子进程，释放 bsa.lock
            except (OSError, ProcessLookupError):
                pass
        cycle_id = row["cycle_id"]
        if cycle_id:
            cleaner = docker_cleaner if docker_cleaner is not None else _cleanup_docker_cli
            try:
                cleaner(log_dir, cycle_id)
            except (OSError, subprocess.SubprocessError):
                pass
            _delete_checkpoint(log_dir, cycle_id)
            target = row["target"]
            if target:
                wt_cleaner = (
                    worktree_cleaner if worktree_cleaner is not None else _cleanup_worktree_cli
                )
                try:
                    wt_cleaner(log_dir, target, cycle_id)
                except (OSError, subprocess.SubprocessError):
                    pass
    return {"cancelled": True, "task_id": task_id}


def _converge_cycle_record(log_dir: str, cycle_id: str | None, status: str) -> None:
    """收敛 cycle.json 僵尸 running（P0-1）。

    周期执行被中断（引擎 CLI 崩溃 / 机器重启）时，``cycle.json`` 停留在
    running，工作台会一直卡"进行中"。executor 对账/兜底把 cycle 任务标
    interrupted/failed 时，同步把 cycle.json 收敛为终态，解除僵尸。
    """
    if not cycle_id:
        return
    path = Path(log_dir) / cycle_id / "cycle.json"
    if not path.is_file():
        return
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(record, dict):
        return
    record["status"] = status
    if not record.get("finished_at"):
        record["finished_at"] = _now_iso()
    try:
        path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def task_log_path(log_dir: str, task_id: int) -> Path:
    """任务执行日志文件：logs/tasks/task-<id>.log（流式实时写盘）。"""
    return Path(log_dir) / "tasks" / f"task-{task_id}.log"


def task_log_tail(log_dir: str, task_id: int, lines: int = 30) -> str:
    """任务执行日志尾部（供任务页实时展示/失败兜底错误摘要）。"""
    path = task_log_path(log_dir, task_id)
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


class TaskExecutor:
    """独立执行守护：DB 轮询认领 + worker + 调度器 + 启动对账。

    ``run_func`` 可注入替换 CLI 子进程便于单测；``now`` 可注入固定时间供
    调度器单测；``cycle_probe`` 可注入替换 checkpoint 探测便于单测。
    """

    def __init__(
        self,
        db,
        log_dir: str,
        *,
        run_func=None,
        timeout_sec: int = DEFAULT_TIMEOUT_SEC,
        poll_sec: float = 0.2,
        now=None,
        cycle_probe=None,
    ):
        self.db = db
        self.log_dir = str(log_dir)
        self._timeout_sec = timeout_sec
        self._poll_sec = poll_sec
        self._run = run_func
        self._now = now or datetime.now
        self._probe = cycle_probe
        self._stop = threading.Event()
        self._claim_thread: threading.Thread | None = None
        self._sched_thread: threading.Thread | None = None
        self._started = False
        self._last_task_id: int | None = None
        try:
            self._lock = InstanceLock(Path(log_dir) / "executor.lock")
        except RuntimeError:
            raise RuntimeError(
                f"executor 单实例冲突：另一个 bsa-executor 实例正占用 {log_dir}/executor.lock。"
                "请勿同时启动两个 executor 进程共享同一 LOG_DIR。"
            ) from None

    # ---- 生命周期 ----

    def start(self) -> None:
        """启动认领 worker 与调度线程（幂等），并先执行启动对账。"""
        if not self._started:
            self._started = True
            self._reconcile_on_start()
            self._claim_thread = threading.Thread(
                target=self._claim_loop, daemon=True, name="bsa-executor-claim"
            )
            self._claim_thread.start()
            self._sched_thread = threading.Thread(
                target=self._schedule_loop, daemon=True, name="bsa-executor-sched"
            )
            self._sched_thread.start()

    def stop(self, timeout: float = 5) -> None:
        """停止轮询/调度线程（不杀已认领的执行中的 CLI，随进程退出处理）。"""
        self._stop.set()
        for thread in (self._claim_thread, self._sched_thread):
            if thread is not None:
                thread.join(timeout=timeout)

    def join(self, timeout: float | None = None) -> None:
        """等待当前已认领任务处理完（单测用）。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            row = self.db.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE state IN ('queued','running')"
            ).fetchone()
            if row is None or row["n"] == 0:
                return
            if deadline is not None and time.monotonic() > deadline:
                return
            time.sleep(self._poll_sec)

    # ---- 启动对账（打标记，不误杀） ----

    def _reconcile_on_start(self) -> None:
        """executor 重启对账：running 按有无 checkpoint 进度标记 interrupted/failed。

        - running 且对应 cycle 有 checkpoint（state.sqlite3 能读到投影）→
          interrupted，保留 cycle_id/worktree，操作者可续跑。
        - running 且无任何进度 → failed（真实中断，无现场可续）。
        - queued → 保持 queued，认领循环重新处理。

        CLI 直启的执行由 V1 引擎主动登记（bsa.commands.task_reporter），无需
        在此枚举 checkpoint 补登记。
        """
        rows = self.db.execute(
            "SELECT id, cycle_id, kind, target, pid FROM tasks WHERE state='running'"
        ).fetchall()
        now = _now_iso()
        for row in rows:
            if self._child_alive(row["pid"]):
                # P0-1：孤儿子进程仍在跑 → 不标 interrupted/failed，等其 register_finish 自愈
                continue
            has_progress = self._cycle_has_progress(row["cycle_id"])
            if has_progress:
                self._mark(
                    row["id"],
                    "interrupted",
                    error="执行中断（executor 重启），现场已保留，可续跑",
                    finished_at=now,
                )
                _converge_cycle_record(self.log_dir, row["cycle_id"], "interrupted")
            else:
                self._mark(
                    row["id"],
                    "failed",
                    error="执行中断（executor 重启），无进度现场",
                    finished_at=now,
                )
                _converge_cycle_record(self.log_dir, row["cycle_id"], "FAILED")

    def _child_alive(self, pid: int | None) -> bool:
        """探测子进程是否仍存活（P0-1，kill(pid,0)）。无 pid 或已死返回 False。"""
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return False
        return True

    def _cycle_has_progress(self, cycle_id: str | None) -> bool:
        """state.sqlite3 是否已有该周期 checkpoint 进度（读投影子进程探测）。

        注入 ``cycle_probe`` 可替换探测逻辑便于单测；默认经
        ``bsa report <cycle> --json`` 子进程，返回码 0 且有非空投影即视为有进度。
        """
        if self._probe is not None:
            return self._probe(cycle_id)
        if not cycle_id:
            return False
        state_db = Path(self.log_dir) / "state.sqlite3"
        if not state_db.is_file():
            return False
        env = dict(os.environ)
        env["LOG_DIR"] = self.log_dir
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "bsa.cli", "report", cycle_id, "--json"],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if proc.returncode != 0:
            return False
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return False
        return bool(payload) and bool(payload.get("cycle_id"))

    # ---- 提交（web/tests 共用；独立进程场景由 enqueue_task 直接写库） ----

    def submit(
        self,
        kind: str,
        user: str,
        target: str | None,
        shas: list[str] | None = None,
        src: str | None = None,
        fresh: bool = False,
        source: str = "web",
        cycle_id: str | None = None,
    ) -> int | None:
        """写入任务意图（queued）并返回 task_id；并发/重复被唯一索引拒绝返回 None。

        web 侧测试用注入 executor 的入口；生产 web 直接调 ``enqueue_task``，
        由独立 executor 进程消费同一张 tasks 表。
        """
        return enqueue_task(
            self.db, kind, user, target, shas=shas, src=src, fresh=fresh,
            source=source, cycle_id=cycle_id,
        )

    def get(self, task_id: int) -> dict | None:
        """任务详情；queued 状态附带等待原因。"""
        return get_task(self.db, task_id)

    # ---- 认领循环 ----

    def _claim_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._claim_one()
            except Exception as exc:  # 兜底：单任务异常不拖垮循环
                _logger.warning("claim 异常: %s", exc)
            time.sleep(self._poll_sec)

    def _claim_one(self) -> None:
        """取最旧一条 queued 任务并原子认领；认领成功则执行。"""
        row = self.db.execute(
            "SELECT id FROM tasks WHERE state='queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return
        task_id = row["id"]
        # 原子认领：只有 state 仍为 queued 的行才会被置为 running（防并发双取）。
        cur = self.db.execute(
            "UPDATE tasks SET state='running', started_at=? WHERE id=? AND state='queued'",
            (_now_iso(), task_id),
        )
        self.db.commit()
        if cur.rowcount != 1:
            return
        try:
            self._run_one(task_id)
        except Exception as exc:  # 兜底：未知异常收敛为 failed
            self._mark(task_id, "failed", error=f"执行异常: {exc}")
            _logger.exception("任务 %s 执行异常", task_id)

    def _run_one(self, task_id: int) -> None:
        row = get_task(self.db, task_id)
        if row is None:
            return
        self._preassign_cycle_id(row)
        cmd = self._build_cmd(row)
        env = self._run_env(row)
        runner = self._run if self._run is not None else self._run_cli
        self._last_task_id = task_id
        try:
            # 终态由 V1 引擎 task_reporter 写入 tasks 表（BSA_TASK_ID 注入），
            # executor 不重复写；仅在引擎未写终态时兜底。
            # 注入 run_func（单测）与真实 _run_cli 同签名 (cmd, env)。
            returncode, stdout, stderr = runner(cmd, env)
        except subprocess.TimeoutExpired:
            self._mark(
                task_id,
                "failed",
                error=f"执行超时（>{self._timeout_sec}s），已终止",
                finished_at=_now_iso(),
            )
            _converge_cycle_record(self.log_dir, row.get("cycle_id"), "FAILED")
            return
        # 兜底：引擎未写终态（如配置错误早退，register_start 都未执行）→ 标 failed
        current = get_task(self.db, task_id)
        if current is not None and current["state"] == "running":
            error = (stderr or stdout or "").strip()
            if not error:
                error = task_log_tail(self.log_dir, task_id) or "执行失败"
            self._mark(task_id, "failed", error=error[:_ERROR_MAX_LEN], finished_at=_now_iso())
            _converge_cycle_record(self.log_dir, current.get("cycle_id"), "FAILED")
            return
        # 终态富化：引擎已写 failed 但 error 为空（投影里有结构化原因却没落到任务行），
        # 从投影聚合人类可读失败原因补写，避免「失败无归因」。仅失败且 error 空时
        # 触发一次 subprocess，成功路径零额外开销；同一次 load_cycle 顺带回填 commits。
        error_is_empty = not (current.get("error") or "").strip()
        if current is not None and current["state"] == "failed" and error_is_empty:
            self._enrich_terminal(task_id, current.get("cycle_id"), current.get("target"))

    def _enrich_terminal(self, task_id: int, cycle_id: str | None, target: str | None) -> None:
        """终态富化：一次 ``load_cycle`` 同时回填 error 与 commits（仅失败路径）。

        ``projection.load_cycle`` 是子进程调用，只在引擎写 failed 但 error 为空的
        少数路径执行；投影不可用则跳过（不覆盖已有 error）。
        """
        if not cycle_id:
            return
        payload = projection.load_cycle(self.log_dir, cycle_id)
        if payload is None:
            return
        aggregated = failure.failure_text(payload, target)
        if aggregated:
            self._mark(task_id, "failed", error=aggregated[:_ERROR_MAX_LEN])
        # commits 回填：任务行 commits 为 NULL 时，从投影取该目标分支的 commit 数。
        branch = (payload.get("branch_results") or {}).get(target) if target else None
        if branch is not None:
            n = len(branch.get("commits") or [])
            if n:
                self.db.execute(
                    "UPDATE tasks SET commits=? WHERE id=? AND commits IS NULL",
                    (n, task_id),
                )
                self.db.commit()

    def _preassign_cycle_id(self, row: dict) -> None:
        """预生成 sync / rerun-fresh 的 cycle_id 并落库（P2-2，运行期即知）。

        sync 与 fresh 重跑都走 ``manual_cycle_id()``；retained 续跑（cycle_id
        已为来源周期、用于 ``--cycle`` 定位现场）不覆盖。生成后写回任务行，
        使执行期间任务行 cycle_id 与 checkpoint 线程一致，重启对账可重挂。
        """
        if row.get("cycle_id"):
            return
        if row["kind"] == "sync":
            cid = manual_cycle_id()
        elif row["kind"] == "rerun" and row.get("fresh"):
            cid = manual_cycle_id()
        else:
            return
        self.db.execute("UPDATE tasks SET cycle_id=? WHERE id=?", (cid, row["id"]))
        self.db.commit()
        row["cycle_id"] = cid

    def _build_cmd(self, row: dict) -> list[str]:
        """按任务行构造 CLI 命令（注入 sys.executable 保证同解释器）。

        cycle 任务（kind='cycle'）跑 ``bsa run-cycle``，并注入 ``BSA_CYCLE_ID``
        让周期 id 在运行期即确定（executor 预生成，重启后可重挂）。
        sync 注入 BSA_MANUAL_CYCLE_ID；fresh rerun 注入 BSA_MANUAL_CYCLE_ID。
        """
        kind = row["kind"]
        cmd = [sys.executable, "-m", "bsa.cli"]
        if kind == "cycle":
            cmd += ["run-cycle"]
            if row["cycle_id"]:
                cmd += ["--date", row["cycle_id"].removeprefix("cycle-")]
        elif kind == "sync":
            cmd += ["sync", row["src"] or "", row["target"]]
            shas = json.loads(row["shas"]) if row["shas"] else None
            if shas:
                cmd.extend(["--sha", *shas])
        elif kind == "rerun":
            cmd += ["rerun", row["target"]]
            if row["fresh"]:
                cmd.append("--fresh")
            elif row["cycle_id"]:
                # 续跑 interrupted 任务：rerun retained 保留现场，指向来源周期
                cmd += ["--cycle", row["cycle_id"]]
        return cmd

    def _run_env(self, row: dict) -> dict[str, str]:
        """子进程环境：LOG_DIR + BSA_TASK_ID + 各 kind 的 cycle_id 覆盖。

        注入 ``BSA_TASK_ID`` 让 V1 引擎（task_reporter）复用该任务行写终态，
        executor 不再自己解析 cycle_id/写终态（只留超时/异常兜底）。

        续跑 rerun（cycle_id 指向来源周期、retained 保留现场）不注入
        BSA_RERUN_THREAD_ID——让 CLI 生成新的 rerun-* 线程，避免覆盖来源投影。
        """
        env = dict(os.environ)
        env["LOG_DIR"] = self.log_dir
        env["BSA_TASK_ID"] = str(row["id"])
        cycle_id = row.get("cycle_id")
        if cycle_id:
            if row["kind"] == "cycle":
                env["BSA_CYCLE_ID"] = cycle_id
            elif row["kind"] == "sync":
                env["BSA_MANUAL_CYCLE_ID"] = cycle_id
            # rerun fresh 走 manual_cycle_id（读取 BSA_MANUAL_CYCLE_ID）；
            # retained 续跑走 --cycle，不注入 thread id。
            elif row["kind"] == "rerun" and row.get("fresh"):
                env["BSA_MANUAL_CYCLE_ID"] = cycle_id
        return env

    def _mark(
        self,
        task_id,
        state,
        *,
        cycle_id=None,
        error=None,
        started_at=None,
        finished_at=None,
    ) -> None:
        fields = ["state=?"]
        params = [state]
        if cycle_id is not None:
            fields.append("cycle_id=?")
            params.append(cycle_id)
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

    def _run_cli(self, cmd: list[str], env: dict[str, str]) -> tuple[int, str, str]:
        """Popen 调 CLI；超时 kill。env 已含 LOG_DIR + cycle 覆盖。记录 child PID。

        有 task_id 时把子进程 stdout/stderr 流式追加到 logs/tasks/task-<id>.log
        （执行过程实时可见，任务页 tail 展示）；无 task_id（直调测试）退回捕获。
        """
        log_path = (
            task_log_path(self.log_dir, self._last_task_id)
            if self._last_task_id is not None
            else None
        )
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        sink = open(log_path, "ab") if log_path is not None else None
        try:
            if sink is not None:
                proc = subprocess.Popen(
                    cmd, stdout=sink, stderr=subprocess.STDOUT, env=env
                )
            else:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
        except OSError:
            if sink is not None:
                sink.close()
            raise
        # P0-1：记录 child PID 到任务行，重启对账据此探活（区分孤儿仍在跑 vs 真死）
        task_id = getattr(self, "_last_task_id", None)
        if task_id is not None:
            self.db.execute("UPDATE tasks SET pid=? WHERE id=?", (proc.pid, task_id))
            self.db.commit()
        try:
            if sink is not None:
                proc.wait(timeout=self._timeout_sec)
                return proc.returncode, "", ""
            stdout, stderr = proc.communicate(timeout=self._timeout_sec)
            return proc.returncode, stdout, stderr
        except subprocess.TimeoutExpired:
            proc.kill()
            if sink is not None:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            else:
                proc.communicate()
            raise
        finally:
            if sink is not None:
                sink.close()

    # ---- 内置调度器（cron） ----

    def _schedule_loop(self) -> None:
        last_key: str | None = None
        while not self._stop.is_set():
            now = self._now()
            key = f"{now.year}-{now.month}-{now.day}"
            if self._schedule_tick(now, last_key):
                last_key = key
            time.sleep(_SCHEDULE_POLL_SEC)

    def _schedule_tick(self, now, last_key: str | None) -> bool:
        """单次调度 tick：0:00 且当天未触发过则插入每日周期任务，返回是否触发。

        与 ``_schedule_loop`` 分离便于单测（循环无限，tick 可精确断言）。
        """
        key = f"{now.year}-{now.month}-{now.day}"
        if key == last_key:
            return False
        if now.hour != _CRON_HOUR or now.minute != _CRON_MINUTE:
            return False
        self._enqueue_daily_cycle()
        return True

    def _enqueue_daily_cycle(self) -> None:
        """插入当日 cron 周期任务（kind='cycle'）；已有活动周期则跳过。

        cycle_id 预生成 ``cycle-<date>`` 并写进任务行（可实时查进度、重启可重挂）。
        """
        day = self._now().date().isoformat()
        cycle_id = f"cycle-{day}"
        task_id = enqueue_task(
            self.db, "cycle", "system", None, source="cron"
        )
        if task_id is None:
            _logger.info("每日周期已存在活动任务，跳过 %s", cycle_id)
            return
        self.db.execute(
            "UPDATE tasks SET cycle_id=? WHERE id=?", (cycle_id, task_id)
        )
        self.db.commit()
        _logger.info("已入队每日周期任务 cycle_id=%s task_id=%s", cycle_id, task_id)


def main() -> int:
    """executor 入口：加载配置、初始化 DB、启动并常驻。"""
    import signal

    from bsa_web.db import init_db
    from bsa_web.settings import load_web_settings

    settings = load_web_settings()
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _logger.info("executor 启动 log_dir=%s", log_dir)

    db = init_db(log_dir / "platform.sqlite3")
    executor = TaskExecutor(db, str(log_dir))
    executor.start()

    stop = threading.Event()

    def _shutdown(signum, frame):
        _logger.info("收到信号 %s，停止 executor", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while not stop.is_set():
        time.sleep(1)
    executor.stop()
    _logger.info("executor 退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
