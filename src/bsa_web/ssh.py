"""WebSSH（ttyd）受限终端生命周期：spawn/端口解析/close/审计。

安全模型（G1/G5/G6）：
- 入口固定 ``timeout 1800 bash -c "cd <worktree> && exec bash"``，仅允许进入
  该任务自己的工作树；禁 sudo 依赖部署约束（平台账号无 sudo 密码），命令中不再
  额外清 PATH（详见 deploy 说明）。
- ttyd ``-i 127.0.0.1`` 仅本机监听，对外只经平台 WS 反代；``-o`` 单客户端断开
  即退出，杜绝空闲会话残留。
- token 为短期（15 分钟）且与平台登录态绑定：页面/WS 均先过平台会话，
  token 仅作一次性的会话寻址（免二次认证），过期即失效。
- open/close 均写会话级审计（user/cycle/target/worktree/时长）。
"""

from __future__ import annotations

import os
import re
import secrets
import select
import shlex
import signal
import subprocess
import time
from datetime import UTC, datetime, timedelta

from bsa_web import audit

SSH_TOKEN_TTL_SEC = 15 * 60  # token 短期：15 分钟
SSH_IDLE_TIMEOUT_SEC = 1800  # 空闲超时：ttyd 包装 timeout 1800
SSH_SPAWN_PORT_TIMEOUT_SEC = 5.0  # 等 "Listening on port N" 首行的上限
SSH_MAX_SESSIONS_PER_USER = 8  # 单用户并发上限，防资源滥用

# 进程注册表：token -> subprocess.Popen（进程句柄不入库，重启后仅靠 DB 行 + 状态校验）
_PROCESSES: dict[str, subprocess.Popen] = {}


def _build_cmd(worktree: str) -> list[str]:
    """构造 ttyd 受限命令：固定 worktree + timeout 空闲回收。

    worktree 已由调用方校验为绝对目录；引号化防止路径含空格/特殊字符破坏命令。
    """
    inner = f'cd {shlex.quote(worktree)} && exec bash'
    return [
        "ttyd",
        "-o",  # 单客户端，断开即退出
        "-p", "0",  # 随机端口
        "-W",  # 可写
        "-i", "127.0.0.1",  # 仅本机监听
        "--",
        "timeout",
        str(SSH_IDLE_TIMEOUT_SEC),
        "bash",
        "-c",
        inner,
    ]


def _parse_port(line: str) -> int | None:
    # ttyd 1.7 真实输出为 "Listening on port: 44251"（带冒号）；兼容 "port 44251"。
    m = re.search(r"port:?\s+(\d+)", line)
    return int(m.group(1)) if m else None


def _kill_process(proc: subprocess.Popen) -> None:
    """整进程组终止（ttyd + timeout + bash 一并回收，不残留 pty/子进程）。"""
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        try:
            proc.wait(timeout=2)
            break
        except subprocess.TimeoutExpired:
            continue
        except OSError:
            break


def spawn_ttyd(worktree: str) -> tuple[int, subprocess.Popen]:
    """spawn 受限 ttyd 并解析随机端口。

    返回 ``(port, proc)``；启动失败（进程起不来 / 5s 内读不到端口行）抛
    ``SshSpawnError``，已起的进程被回收。
    """
    if not os.path.isdir(worktree):
        raise SshSpawnError("worktree 不存在")
    cmd = _build_cmd(worktree)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # 独立进程组，close 时整组回收
            text=True,
            errors="replace",
        )
    except OSError as exc:
        raise SshSpawnError(f"ttyd 启动失败: {exc}") from None

    deadline = time.monotonic() + SSH_SPAWN_PORT_TIMEOUT_SEC
    port = None
    try:
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        # 用 os.read 原始字节 + 累积缓冲解析：select 只看 OS 管道缓冲，
        # 而 TextIOWrapper.readline 会把整批读进 Python 缓冲导致 select 假超时
        # （端口行已在 Python 缓冲里、fd 却显示不可读）——真机 ttyd 间歇复现。
        buf = b""
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], deadline - time.monotonic())
            if not ready:
                break
            chunk = os.read(fd, 8192)
            if not chunk:
                break
            buf += chunk
            # ttyd 日志为 ASCII，解码后复用 _parse_port（单点解析）
            port = _parse_port(buf.decode("ascii", errors="ignore"))
            if port is not None:
                break
    finally:
        if port is None:
            _kill_process(proc)
    if port is None:
        raise SshSpawnError("无法从 ttyd 输出解析端口")
    return port, proc


class SshSpawnError(Exception):
    """ttyd spawn/端口解析失败。"""


def open_session(
    db,
    user: str,
    cycle_id: str,
    target: str,
    worktree: str,
    *,
    spawn=None,
) -> str:
    """spawn ttyd、写 ssh_sessions 行、审计 action=ssh_open，返回 token。

    ``spawn`` 可注入（默认 spawn_ttyd），测试用假 spawn 规避真实 ttyd。
    失败（spawn 抛错）不留行、不审计，由调用方转为 500。
    """
    if spawn is None:
        spawn = spawn_ttyd
    active = db.execute(
        "SELECT COUNT(*) AS n FROM ssh_sessions WHERE user=? AND created_at>=?",
        (
            user,
            (datetime.now(UTC) - timedelta(seconds=SSH_TOKEN_TTL_SEC)).isoformat(),
        ),
    ).fetchone()
    if active["n"] >= SSH_MAX_SESSIONS_PER_USER:
        raise SshSpawnError(f"会话数已达上限（{SSH_MAX_SESSIONS_PER_USER}），请先关闭旧会话")

    port, proc = spawn(worktree)
    token = secrets.token_urlsafe(24)
    now = datetime.now(UTC)
    db.execute(
        "INSERT INTO ssh_sessions(token, cycle_id, target, worktree, port, user, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (token, cycle_id, target, worktree, port, user, now.isoformat()),
    )
    db.commit()
    _PROCESSES[token] = proc
    audit.record(
        db,
        user,
        "ssh_open",
        cycle_id=cycle_id,
        target=target,
        detail={
            "worktree": worktree,
            "port": port,
            "idle_timeout_sec": SSH_IDLE_TIMEOUT_SEC,
        },
        result="ok",
    )
    return token


def close_session(db, token: str) -> bool:
    """kill 进程、清 ssh_sessions 行、审计 action=ssh_close（时长）。

    返回是否确实关闭（行不存在返回 False，不重复审计）。
    """
    row = db.execute(
        "SELECT * FROM ssh_sessions WHERE token=?", (token,)
    ).fetchone()
    if row is None:
        return False
    proc = _PROCESSES.pop(token, None)
    if proc is not None:
        _kill_process(proc)
    db.execute("DELETE FROM ssh_sessions WHERE token=?", (token,))
    db.commit()
    try:
        created = datetime.fromisoformat(row["created_at"])
        duration = max(0.0, (datetime.now(UTC) - created).total_seconds())
    except (ValueError, TypeError):
        duration = None
    detail: dict = {"worktree": row["worktree"], "port": row["port"]}
    if duration is not None:
        detail["duration_sec"] = round(duration, 1)
    audit.record(
        db,
        row["user"],
        "ssh_close",
        cycle_id=row["cycle_id"],
        target=row["target"],
        detail=detail,
        result="ok",
    )
    return True


def session_info(db, token: str) -> dict | None:
    """返回未过期会话信息（含 proc 句柄）；无效/过期返回 None 并清理过期行。"""
    row = db.execute(
        "SELECT * FROM ssh_sessions WHERE token=?", (token,)
    ).fetchone()
    if row is None:
        return None
    try:
        created = datetime.fromisoformat(row["created_at"])
    except (ValueError, TypeError):
        db.execute("DELETE FROM ssh_sessions WHERE token=?", (token,))
        db.commit()
        return None
    # 防御 naive 时间戳（畸形/手工插入行）：按 UTC 解释，避免减法抛 TypeError
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    if datetime.now(UTC) - created > timedelta(seconds=SSH_TOKEN_TTL_SEC):
        db.execute("DELETE FROM ssh_sessions WHERE token=?", (token,))
        db.commit()
        _PROCESSES.pop(token, None)
        return None
    info = dict(row)
    info["proc"] = _PROCESSES.get(token)
    return info
