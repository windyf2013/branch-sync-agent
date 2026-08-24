"""每日滚动备份：打包平台库与 V1 关键状态为 tgz。

打包内容 = platform.sqlite3（平台库）+ state.sqlite3 + judgments.json +
所有 cycle-*/cycle.json（若存在），tgz 内保留 ``<log_dir 名>/...`` 相对结构；
backup_dir 内滚动保留最近 ``keep`` 份 ``backup-*.tgz``（删最旧）。

sqlite 库用 ``sqlite3.Connection.backup()`` 取一致快照（含 WAL 未 checkpoint
事务）再入包，避免周期进行中只 tar 主库丢失 ``-wal`` 里已提交的进度；
非 sqlite 文件（judgments.json / cycle.json）仍按普通文件打包。
"""

from __future__ import annotations

import os
import sqlite3
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

_FILES = ("platform.sqlite3", "state.sqlite3", "judgments.json")


def _snapshot_sqlite(path: Path) -> Path | None:
    """用 sqlite backup API 把 ``path`` 拷成一致快照临时文件并返回。

    只读打开源库，``backup()`` 会把主库 + WAL 里所有已提交事务合并到快照，
    无需手工拷贝 ``-wal``/``-shm`` 边车；调度器持有连接时也能安全读取。
    若文件不是合法 sqlite 库（占位/损坏），返回 None，调用方按普通文件回退。
    """
    fd, tmp_name = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    try:
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(tmp_name)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except (sqlite3.DatabaseError, OSError):
        Path(tmp_name).unlink(missing_ok=True)
        return None
    return Path(tmp_name)


def _collect(log_dir: Path) -> list[Path]:
    """收集待打包文件：根级固定文件 + 所有 cycle-*/cycle.json。"""
    paths = [p for name in _FILES if (p := log_dir / name).is_file()]
    paths.extend(sorted(log_dir.glob("cycle-*/cycle.json")))
    return paths


def _roll(backup_dir: Path, keep: int) -> None:
    """滚动保留最近 ``keep`` 份，删更旧的（文件名按时间戳字典序即时间序）。"""
    for old in sorted(backup_dir.glob("backup-*.tgz"))[:-keep]:
        old.unlink(missing_ok=True)


def backup_now(log_dir: str | Path, backup_dir: str | Path, keep: int = 7) -> Path:
    """执行一次备份，返回生成的 tgz 路径。

    ``log_dir`` 不存在或为空时也能成功：产物仅包含实际存在的文件。
    """
    log_dir = Path(log_dir)
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S%f")
    archive = backup_dir / f"backup-{stamp}.tgz"
    prefix = log_dir.name
    with tarfile.open(archive, "w:gz") as tf:
        for path in _collect(log_dir):
            arcname = f"{prefix}/{path.relative_to(log_dir)}"
            if path.suffix == ".sqlite3":
                snap = _snapshot_sqlite(path)
                if snap is not None:
                    try:
                        tf.add(snap, arcname=arcname)
                    finally:
                        snap.unlink(missing_ok=True)
                    continue
            tf.add(path, arcname=arcname)
    _roll(backup_dir, keep)
    return archive
