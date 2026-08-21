from pathlib import Path

import pytest
from pydantic import ValidationError

from bsa.config.settings import Settings
from bsa.mail import MailResult, MailService


def make_settings(tmp_path: Path, *, dry_run: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        repo_path="/srv/rcios",
        branch_file="/srv/rcios/branch.md",
        worktree_root="/srv/wt",
        llm_model="m",
        llm_api_key="k",
        llm_base_url="u",
        docker_image="rcios-build:latest",
        docker_mount_workspace="/workspace/rcios",
        build_script_dir="build/platform/RTL9617C",
        mail_dry_run=dry_run,
        mail_sender="s",
        mail_recipients=["ops@x.com"],
        log_dir=str(tmp_path / "logs"),
    )


class TestDryRun:
    def test_skips_and_never_calls_sender(self, tmp_path):
        called: list[dict] = []

        def sender(payload):
            called.append(payload)
            return {"status": "ok"}

        service = MailService(make_settings(tmp_path, dry_run=True), sender=sender)
        result = service.send_report("subj", "body", tmp_path / "r.html", [])
        assert result.status == "skipped"
        assert "dry-run" in (result.error or "")
        assert called == []

    def test_skips_even_without_sender(self, tmp_path):
        service = MailService(make_settings(tmp_path, dry_run=True))
        result = service.send_report("subj", "body", tmp_path / "r.html", [])
        assert result.status == "skipped"


class TestSender:
    def test_sender_receives_payload_and_ok_result(self, tmp_path):
        seen: dict = {}

        def sender(payload):
            seen.update(payload)
            return {"status": "ok", "report_path": str(tmp_path / "r.html")}

        html = tmp_path / "report.html"
        attachment = tmp_path / "patch.patch"
        service = MailService(make_settings(tmp_path, dry_run=False), sender=sender)
        result = service.send_report("subject line", "body text", html, [attachment])
        assert result.status == "ok"
        assert result.error is None
        assert result.report_path == tmp_path / "r.html"
        assert seen["subject"] == "subject line"
        assert seen["body"] == "body text"
        assert seen["html_path"] == str(html)
        assert seen["attachments"] == [str(attachment)]

    def test_sender_reported_failure_is_warned_not_raised(self, tmp_path):
        def sender(payload):
            return {"status": "failed", "error": "SMTP 550 rejected"}

        service = MailService(make_settings(tmp_path, dry_run=False), sender=sender)
        result = service.send_report("s", "b", tmp_path / "r.html", [])
        assert result.status == "failed"
        assert result.error == "SMTP 550 rejected"

    def test_sender_exception_is_warned_not_raised(self, tmp_path):
        def sender(payload):
            raise ConnectionError("smtp down")

        service = MailService(make_settings(tmp_path, dry_run=False), sender=sender)
        result = service.send_report("s", "b", tmp_path / "r.html", [])
        assert result.status == "failed"
        assert "smtp down" in result.error

    def test_non_dict_sender_result_fails(self, tmp_path):
        def sender(payload):
            return "ok"

        service = MailService(make_settings(tmp_path, dry_run=False), sender=sender)
        result = service.send_report("s", "b", tmp_path / "r.html", [])
        assert result.status == "failed"
        assert "non-dict" in result.error

    def test_unknown_sender_status_normalized_to_ok(self, tmp_path):
        def sender(payload):
            return {"status": "PENDING"}

        service = MailService(make_settings(tmp_path, dry_run=False), sender=sender)
        result = service.send_report("s", "b", tmp_path / "r.html", [])
        assert result.status == "ok"

    def test_empty_sender_status_normalized_to_failed(self, tmp_path):
        def sender(payload):
            return {"status": ""}

        service = MailService(make_settings(tmp_path, dry_run=False), sender=sender)
        result = service.send_report("s", "b", tmp_path / "r.html", [])
        assert result.status == "failed"


class TestNoSender:
    def test_no_sender_not_dry_run_fails(self, tmp_path):
        service = MailService(make_settings(tmp_path, dry_run=False))
        result = service.send_report("s", "b", tmp_path / "r.html", [])
        assert result.status == "failed"
        assert result.error == "no sender configured"


class TestMailResult:
    def test_status_must_be_known_literal(self):
        with pytest.raises(ValidationError):
            MailResult(status="pending")

    def test_round_trip_ok(self):
        result = MailResult(status="ok")
        assert result.error is None
        assert result.report_path is None
