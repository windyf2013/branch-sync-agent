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
  started_at TEXT, finished_at TEXT);
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
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_active_target "
        "ON tasks(target) WHERE state IN ('queued','running')"
    )
    conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version','1')")
    conn.commit()
    return conn


def _migrate_tasks(conn: sqlite3.Connection) -> None:
    """旧库 tasks 表补齐 runner 所需列（src/fresh），新库 schema 已含。"""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    if "src" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN src TEXT")
    if "fresh" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN fresh INTEGER NOT NULL DEFAULT 0")


def _migrate_abandons(conn: sqlite3.Connection) -> None:
    """abandons 表：PRAGMA 探测，旧库缺失则建表；唯一索引幂等补齐。"""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(abandons)")}
    if not cols:
        conn.execute(_ABANDONS_SQL)
    conn.execute(_ABANDONS_IDX_SQL)


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
