"""数据保留与备份：L2 体积日志分级清理 + 每日滚动备份。

clean_logs 只删超龄的 L2 日志（build/run.log），绝不碰 L1 audit 产物；
backup_now 打包平台库与 V1 关键状态为 tgz，滚动保留最近 keep 份；
run_maintenance 是组合入口，供 cron 每日调用。
"""

from __future__ import annotations

import os
import sqlite3
import tarfile
import time
from pathlib import Path

from bsa_web.backup import backup_now
from bsa_web.retention import clean_logs, run_maintenance

_KEEP_DAYS = 30
_OLD_SECONDS = _KEEP_DAYS * 86400 + 3600


def _age(path: Path, seconds: float = _OLD_SECONDS) -> Path:
    t = time.time() - seconds
    os.utime(path, (t, t))
    return path


def _make_cycle(log_dir: Path, cycle: str, *, old: bool) -> Path:
    cycle_dir = log_dir / cycle
    build_dir = cycle_dir / "build" / "feat/x" / "abc123"
    audit_dir = cycle_dir / "audit" / "feat/x"
    for d in (cycle_dir, build_dir, audit_dir):
        d.mkdir(parents=True, exist_ok=True)
    paths = [
        cycle_dir / "cycle.json",
        cycle_dir / "run.log",
        cycle_dir / "decisions.json",
        cycle_dir / "report.html",
        build_dir / "build.log",
        audit_dir / "abc123_conflict.diff",
    ]
    for path in paths:
        path.write_text("x", encoding="utf-8")
    if old:
        for path in paths:
            _age(path)
    return cycle_dir


def _make_log_dir(tmp_path: Path) -> Path:
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "platform.sqlite3").write_bytes(b"platform")
    (log_dir / "state.sqlite3").write_bytes(b"state")
    (log_dir / "judgments.json").write_text('{"x": 1}', encoding="utf-8")
    patch_dir = log_dir / "patch"
    patch_dir.mkdir()
    (patch_dir / "cycle-2026-01-01_feat-x.patch").write_text("patch", encoding="utf-8")
    _make_cycle(log_dir, "cycle-2026-01-01", old=True)
    _make_cycle(log_dir, "cycle-2026-01-02", old=False)
    return log_dir


class TestCleanLogs:
    def test_old_build_and_run_logs_deleted(self, tmp_path):
        log_dir = _make_log_dir(tmp_path)
        old_cycle = log_dir / "cycle-2026-01-01"
        build_log = old_cycle / "build" / "feat/x" / "abc123" / "build.log"
        run_log = old_cycle / "run.log"
        assert build_log.exists() and run_log.exists()

        deleted = clean_logs(log_dir, keep_days=_KEEP_DAYS)

        assert deleted == 2
        assert not build_log.exists()
        assert not run_log.exists()

    def test_l1_artifacts_never_deleted(self, tmp_path):
        log_dir = _make_log_dir(tmp_path)
        old_cycle = log_dir / "cycle-2026-01-01"
        kept = [
            old_cycle / "cycle.json",
            old_cycle / "decisions.json",
            old_cycle / "report.html",
            old_cycle / "audit" / "feat/x" / "abc123_conflict.diff",
            log_dir / "patch" / "cycle-2026-01-01_feat-x.patch",
        ]

        clean_logs(log_dir, keep_days=_KEEP_DAYS)

        assert all(path.exists() for path in kept)

    def test_fresh_logs_kept(self, tmp_path):
        log_dir = _make_log_dir(tmp_path)
        fresh = log_dir / "cycle-2026-01-02"

        clean_logs(log_dir, keep_days=_KEEP_DAYS)

        assert (fresh / "run.log").exists()
        assert (fresh / "build" / "feat/x" / "abc123" / "build.log").exists()

    def test_state_files_never_deleted(self, tmp_path):
        log_dir = _make_log_dir(tmp_path)
        for name in ("platform.sqlite3", "state.sqlite3", "judgments.json"):
            _age(log_dir / name)

        clean_logs(log_dir, keep_days=_KEEP_DAYS)

        for name in ("platform.sqlite3", "state.sqlite3", "judgments.json"):
            assert (log_dir / name).exists()

    def test_missing_log_dir_returns_zero(self, tmp_path):
        assert clean_logs(tmp_path / "nope", keep_days=_KEEP_DAYS) == 0


class TestBackupNow:
    def _assert_content(self, archive: Path) -> None:
        with tarfile.open(archive, "r:gz") as tf:
            names = tf.getnames()
        assert "logs/platform.sqlite3" in names
        assert "logs/state.sqlite3" in names
        assert "logs/judgments.json" in names
        assert any(n.startswith("logs/cycle-") and n.endswith("/cycle.json") for n in names)
        assert not any(n.endswith("build.log") for n in names)
        assert not any(n.endswith("run.log") for n in names)

    def test_backup_packs_expected_artifacts(self, tmp_path):
        log_dir = _make_log_dir(tmp_path)

        archive = backup_now(log_dir, tmp_path / "backups", keep=7)

        assert archive.is_file()
        self._assert_content(archive)

    def test_rolling_keeps_only_keep_copies(self, tmp_path):
        log_dir = _make_log_dir(tmp_path)
        backup_dir = tmp_path / "backups"

        for _ in range(9):
            backup_now(log_dir, backup_dir, keep=7)

        copies = sorted(backup_dir.glob("backup-*.tgz"))
        assert len(copies) == 7

    def test_missing_artifacts_skipped(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "judgments.json").write_text("{}", encoding="utf-8")

        archive = backup_now(log_dir, tmp_path / "backups", keep=7)

        with tarfile.open(archive, "r:gz") as tf:
            assert tf.getnames() == ["logs/judgments.json"]

    def test_wal_snapshot_includes_uncheckpointed_data(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        db_path = log_dir / "state.sqlite3"
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t(x)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.commit()
        assert (log_dir / "state.sqlite3-wal").exists()

        archive = backup_now(log_dir, tmp_path / "backups", keep=7)

        extract_dir = tmp_path / "restored"
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(extract_dir)
        restored = sqlite3.connect(extract_dir / "logs" / "state.sqlite3")
        try:
            rows = restored.execute("SELECT x FROM t").fetchall()
        finally:
            restored.close()
        assert rows == [(42,)]


class TestRunMaintenance:
    def test_combined_entry(self, tmp_path):
        log_dir = _make_log_dir(tmp_path)
        backup_dir = tmp_path / "backups"

        result = run_maintenance(log_dir, backup_dir, keep_days=_KEEP_DAYS, keep=7)

        assert result["deleted"] == 2
        assert list(backup_dir.glob("backup-*.tgz"))
        assert (log_dir / "cycle-2026-01-01" / "cycle.json").exists()
        assert not (log_dir / "cycle-2026-01-01" / "run.log").exists()
