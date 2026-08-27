from __future__ import annotations

from pathlib import Path

from bsa.ledger import is_synced, record_status, record_synced


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


def test_record_status_failed_does_not_mark_synced(tmp_path: Path):
    record_status(tmp_path, "pid-f", "br_main", "sha9", "failed", reason="build broke")

    assert is_synced(tmp_path, "pid-f", "br_main") is False
    assert not (tmp_path / "ledger.json").read_text(encoding="utf-8") == ""


def test_record_status_blocked_with_reason(tmp_path: Path):
    record_status(tmp_path, "pid-b", "br_main", "sha9", "blocked", reason="baseline failed")

    lines = (tmp_path / "ledger.json").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    import json

    entry = json.loads(lines[0])
    assert entry["status"] == "blocked"
    assert entry["reason"] == "baseline failed"
    assert is_synced(tmp_path, "pid-b", "br_main") is False


def test_synced_after_failed_marks_synced(tmp_path: Path):
    # 同一 commit 先 failed 后成功重跑 → 有 synced 记录即视为已同步
    record_status(tmp_path, "pid-a", "br_main", "sha9", "failed")
    record_synced(tmp_path, "pid-a", "br_main", "sha9")

    assert is_synced(tmp_path, "pid-a", "br_main") is True
