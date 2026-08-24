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
"""


def init_db(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate_tasks(conn)
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
