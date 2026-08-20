from __future__ import annotations

from pathlib import Path

import pytest

from branch_maintenance.mail_send_bridge import (
    DEFAULT_TEST_RECIPIENTS,
    build_mail_payload,
    recipients_for_phase,
    resolve_smtp_settings,
)


def test_smtp_matches_bug_stale_alert():
    smtp = resolve_smtp_settings({"mail_smtp_source": "bug_stale_alert"})
    assert smtp["smtp_host"] == "smtp.exmail.qq.com"
    assert smtp["smtp_port"] == 465
    assert smtp["smtp_security"] == "SSL"
    assert smtp["smtp_user"] == "soft2@raisecom.com"
    assert smtp["from_email"] == "soft2@raisecom.com"
    assert smtp["smtp_pass"]


def test_test_phase_recipients_default_yangfu():
    assert recipients_for_phase({"mail_phase": "test"}) == DEFAULT_TEST_RECIPIENTS


def test_test_phase_strips_non_allowlisted():
    addrs = recipients_for_phase(
        {
            "mail_phase": "test",
            "mail_to": ["yangfu@raisecom.com", "other@example.com"],
        }
    )
    assert addrs == ["yangfu@raisecom.com"]


def test_build_payload_attaches_reports(tmp_path: Path):
    report = tmp_path / "r.html"
    report.write_text("<html></html>", encoding="utf-8")
    payload = build_mail_payload(
        report_paths=[str(report)],
        window_desc="2026-08-01T21:00:00+08:00 ~ 2026-08-02T21:00:00+08:00",
        repo_summaries=[
            {
                "repo_id": "rcios",
                "commits_scanned": 3,
                "need_sync_count": 1,
                "pending_agent_count": 0,
            }
        ],
        mail_cfg={
            "mail_phase": "test",
            "mail_to": ["yangfu@raisecom.com"],
            "mail_smtp": {
                "smtp_user": "sender@raisecom.com",
                "from_email": "sender@raisecom.com",
            },
        },
        workspace_root=tmp_path,
    )
    assert payload["to_emails"] == ["yangfu@raisecom.com"]
    assert payload["attachments"] == [str(report.resolve())]
    assert "NeedSync 1" in payload["body"]
    assert payload["notice_type"] == "BMA_REPORT"


def test_build_payload_missing_report_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        build_mail_payload(
            report_paths=[str(tmp_path / "missing.html")],
            window_desc="w",
            repo_summaries=[],
            mail_cfg={"mail_phase": "test"},
            workspace_root=tmp_path,
        )
