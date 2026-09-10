from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from bsa.config.settings import Settings

MailSender = Callable[[dict[str, Any]], dict[str, Any]]

_KNOWN_STATUSES = {"ok", "failed", "skipped"}


class MailResult(BaseModel):
    status: Literal["ok", "skipped", "failed"]
    error: str | None = None
    report_path: Path | None = None


class MailService:
    """Sends the end-of-cycle report email.

    Real SMTP is deferred (decision 11/28 — mail starts in dry-run). A sender
    callable may be injected (tests use a fake). Failures return a ``failed``
    MailResult and never raise, so a broken mailer never fails the run.
    """

    def __init__(self, settings: Settings, sender: MailSender | None = None) -> None:
        self.settings = settings
        self.sender = sender

    def send_report(
        self,
        subject: str,
        body: str,
        html_path: Path,
        attachments: list[Path],
        *,
        body_html: str | None = None,
    ) -> MailResult:
        """``body_html`` 是邮件正文的 HTML 摘要；缺省（None）时发信方回退到把
        ``html_path`` 全文当正文（改造前的行为，老调用方零改动）。"""
        payload = {
            "subject": subject,
            "body": body,
            "html_path": str(html_path),
            "attachments": [str(p) for p in attachments],
            "report_path": str(html_path),
            "body_html": body_html,
        }

        if self.settings.mail_dry_run:
            return MailResult(status="skipped", error="dry-run: not sent")

        if self.sender is None:
            return MailResult(status="failed", error="no sender configured")

        try:
            result = self.sender(payload)
        except Exception as exc:
            return MailResult(status="failed", error=str(exc), report_path=html_path)

        if not isinstance(result, dict):
            return MailResult(
                status="failed",
                error="sender returned non-dict result",
                report_path=html_path,
            )

        status = result.get("status", "ok")
        if status not in _KNOWN_STATUSES:
            status = "ok" if status else "failed"

        report_path = result.get("report_path")
        if report_path is None:
            report_path = payload.get("report_path")
        return MailResult(status=status, error=result.get("error"), report_path=report_path)
