from __future__ import annotations

from pathlib import Path

from bsa.mail.bridge_sender import _load_bridge, make_bridge_sender, send_via_bridge

BRIDGE = _load_bridge()


def test_recipients_for_phase_test_caps_to_yangfu() -> None:
    rcpt = BRIDGE.recipients_for_phase({"mail_phase": "test"})
    assert rcpt == ["yangfu@raisecom.com"]


def test_recipients_for_phase_test_with_to_other_is_filtered() -> None:
    rcpt = BRIDGE.recipients_for_phase(
        {"mail_phase": "test", "mail_to": ["someone@raisecom.com"]}
    )
    assert rcpt == ["yangfu@raisecom.com"]


def test_smtp_loads_from_bug_stale_alert() -> None:
    smtp = BRIDGE.load_smtp_from_bug_stale_alert()
    assert smtp["smtp_host"]
    assert smtp["smtp_user"]
    assert smtp["smtp_security"] == "SSL"


def test_bridge_sender_maps_payload_to_bridge(tmp_path: Path) -> None:
    report = tmp_path / "report.html"
    report.write_text("<html></html>")
    sender = make_bridge_sender(
        workspace_root=tmp_path, output_dir=tmp_path, mail_phase="test"
    )
    result = sender(
        {
            "html_path": str(report),
            "subject": "test",
            "attachments": [str(report)],
        }
    )
    assert isinstance(result, dict)
    assert result["status"] in {"ok", "failed"}
    assert result["report_path"] == str(report)


def test_send_via_bridge_dry_run_returns_ok(tmp_path: Path) -> None:
    report = tmp_path / "r.html"
    report.write_text("<html></html>")
    result = send_via_bridge(
        report_path=report,
        subject="dry",
        workspace_root=tmp_path,
        output_dir=tmp_path,
        dry_run=True,
    )
    assert result["status"] == "ok"
    assert result["bridge_result"]["mail_result"] == "DRY_RUN"


def test_mailservice_dry_run_short_circuits_before_sender(tmp_path: Path) -> None:
    from bsa.config.settings import Settings
    from bsa.mail.service import MailService

    settings = Settings(
        _env_file=None,
        repo_path=str(tmp_path),
        branch_file="branch.md",
        worktree_root=str(tmp_path / "wt"),
        llm_backend="api",
        llm_model="m",
        llm_api_key="k",
        llm_base_url="u",
        docker_image="img",
        docker_mount_workspace="/w",
        build_script_dir="b",
        mail_sender="s@x.com",
        mail_recipients=["yangfu@raisecom.com"],
        log_dir=str(tmp_path),
        mail_dry_run=True,
    )
    called = []
    svc = MailService(settings, sender=lambda p: called.append(p) or {"status": "ok"})
    r = svc.send_report("s", "b", tmp_path / "r.html", [])
    assert r.status == "skipped"
    assert called == []
