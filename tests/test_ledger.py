from __future__ import annotations

from pathlib import Path

from bsa.ledger import is_synced, record_synced


def test_ledger_empty_dir_no_synced(tmp_path: Path):
    assert is_synced(tmp_path, "pid-a", "br_main") is False


def test_record_synced_then_is_synced(tmp_path: Path):
    record_synced(tmp_path, "pid-a", "br_main", "abc123")

    assert is_synced(tmp_path, "pid-a", "br_main") is True
    assert is_synced(tmp_path, "pid-a", "br_other") is False
    assert is_synced(tmp_path, "pid-b", "br_main") is False


def test_record_synced_idempotent(tmp_path: Path):
    record_synced(tmp_path, "pid-a", "br_main", "abc123")
    record_synced(tmp_path, "pid-a", "br_main", "abc123")

    assert is_synced(tmp_path, "pid-a", "br_main") is True


def test_ledger_corrupt_file_is_treated_empty(tmp_path: Path):
    (tmp_path / "ledger.json").write_text("{broken", encoding="utf-8")

    assert is_synced(tmp_path, "pid-a", "br_main") is False


def test_ledger_persists_across_loads(tmp_path: Path):
    record_synced(tmp_path, "pid-x", "br_main", "sha9")

    # 重新加载（新进程语义）：直接读文件
    assert is_synced(tmp_path, "pid-x", "br_main") is True
