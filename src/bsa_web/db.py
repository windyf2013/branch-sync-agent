import fcntl
import os
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
  cycle_id TEXT, target TEXT, src TEXT, shas TEXT, fresh INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL,
  started_at TEXT, finished_at TEXT, source TEXT NOT NULL DEFAULT 'web');
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_active_target
  ON tasks(target) WHERE state IN ('queued','running');
"""

_ABANDONS_SQL = """
CREATE TABLE IF NOT EXISTS abandons(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id TEXT NOT NULL,
  target TEXT NOT NULL,
  sha TEXT,
  user TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(cycle_id, target, sha))
"""

# SQLite 的 UNIQUE 视 NULL 互不相等，分支级（sha=NULL）幂等靠该表达式唯一索引兜底
_ABANDONS_IDX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_abandons_cycle_target_sha "
    "ON abandons(cycle_id, target, COALESCE(sha,''))"
)

_SSH_SESSIONS_SQL = """
CREATE TABLE IF NOT EXISTS ssh_sessions(
  token TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, target TEXT NOT NULL,
  worktree TEXT NOT NULL, port INTEGER NOT NULL, user TEXT NOT NULL,
  created_at TEXT NOT NULL)
"""


def init_db(path: str | Path) -> sqlite3.Connection:
    """初始化平台库，返回供多线程共享的连接。

    ``autocommit=True``：每条语句自成一个事务即时提交，避免 runner worker 与
    请求线程共享同一连接时，跨语句隐式事务被另一线程抢占导致
    ``OperationalError: not an error`` 等竞态（已实测压测消除）。现有写路径
    均为单语句 execute+commit，语义等价。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, autocommit=True)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate_tasks(conn)
    _migrate_abandons(conn)
    _migrate_ssh_sessions(conn)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_active_target "
        "ON tasks(target) WHERE state IN ('queued','running')"
    )
    conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version','1')")
    conn.commit()
    return conn


class InstanceLock:
    """平台单实例守卫：对平台库文件加独占 flock，第二个实例启动即失败。

    修复：第二个 app 实例可共享同一 platform.sqlite3，其 ``runner.start()``
    的 ``_recover_stale_tasks`` 会把仍在运行的 CLI 子进程任务误标失败
    "平台重启中断"，同时释放唯一索引导致同 target 可再排队。持锁后任何
    并发的第二个实例在此即抛错，杜绝共享库 + 误恢复。

    进程退出/GC 自动释放 flock（fd 关闭即解锁），无需显式清理。
    """

    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(path, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._fd)
            raise RuntimeError(
                f"平台单实例冲突：另一个 BSA Web 实例正占用 {path}。"
                "请勿同时启动两个 web 进程共享同一 LOG_DIR。"
            ) from None

    def close(self) -> None:
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)

    def __enter__(self) -> "InstanceLock":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _migrate_tasks(conn: sqlite3.Connection) -> None:
    """旧库 tasks 表补齐 runner 所需列（src/fresh/source），新库 schema 已含。"""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    if "src" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN src TEXT")
    if "fresh" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN fresh INTEGER NOT NULL DEFAULT 0")
    if "source" not in cols:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN source TEXT NOT NULL DEFAULT 'web'"
        )


def _migrate_abandons(conn: sqlite3.Connection) -> None:
    """abandons 表：PRAGMA 探测，旧库缺失则建表；唯一索引幂等补齐。"""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(abandons)")}
    if not cols:
        conn.execute(_ABANDONS_SQL)
    conn.execute(_ABANDONS_IDX_SQL)


def _migrate_ssh_sessions(conn: sqlite3.Connection) -> None:
    """ssh_sessions 表：旧库缺失则建表（幂等）。"""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(ssh_sessions)")}
    if not cols:
        conn.execute(_SSH_SESSIONS_SQL)


def is_abandoned(
    conn: sqlite3.Connection,
    cycle_id: str,
    target: str,
    sha: str | None = None,
) -> bool:
    """是否已放弃：commit 级先看分支级（sha=NULL 覆盖该分支所有 commit），再看精确 sha。"""
    if sha is not None:
        row = conn.execute(
            "SELECT 1 FROM abandons WHERE cycle_id=? AND target=? AND sha IS NULL",
            (cycle_id, target),
        ).fetchone()
        if row is not None:
            return True
    row = conn.execute(
        "SELECT 1 FROM abandons WHERE cycle_id=? AND target=? AND sha IS ?",
        (cycle_id, target, sha),
    ).fetchone()
    return row is not None


def abandoned_keys(
    conn: sqlite3.Connection, cycle_id: str
) -> set[tuple[str, str | None]]:
    """返回该周期已放弃的 (target, sha) 集合，sha=None 表示整分支放弃。"""
    rows = conn.execute(
        "SELECT target, sha FROM abandons WHERE cycle_id=?", (cycle_id,)
    ).fetchall()
    return {(row["target"], row["sha"]) for row in rows}
