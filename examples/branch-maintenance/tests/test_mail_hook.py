from branch_maintenance.mail_hook import maybe_send_mail


def test_mail_disabled_skips():
    result = maybe_send_mail({"report_path": "x.html"}, enabled=False)
    assert result["status"] == "skipped"


def test_mail_enabled_uses_default_sender():
    result = maybe_send_mail({"report_path": "/tmp/report.html"}, enabled=True)
    assert result["status"] == "ok"
    assert result["report_path"] == "/tmp/report.html"


def test_mail_sender_failure_returns_failed():
    def boom(_payload):
        raise RuntimeError("smtp down")

    result = maybe_send_mail({"report_path": "x.html"}, enabled=True, sender=boom)
    assert result["status"] == "failed"
    assert "smtp down" in result["error"]
