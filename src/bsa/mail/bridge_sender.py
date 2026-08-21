"""Adapter: bsa MailSender -> reference mail_send_bridge (real SMTP).

Reuses the proven reference bridge (send_bma_reports_via_mail_send -> the
release-integration mail_send_subflow subprocess). No smtplib is written here;
SMTP config comes from bug_stale_alert via the bridge. Recipients are hard-capped
to the test phase allow-list (yangfu@raisecom.com) unless overridden.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from bsa.mail.service import MailSender

_DEFAULT_BRIDGE_PATH = (
    "/home/sam/rc_projects/aiskill/aiskill/common/packages/branch-maintenance/scripts"
)
_BRIDGE_PATH_ENV = "BMA_BRIDGE_PATH"


def _load_bridge() -> Any:
    path = Path(os.environ.get(_BRIDGE_PATH_ENV, _DEFAULT_BRIDGE_PATH))
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
    from branch_maintenance import mail_send_bridge  # type: ignore[import-not-found]

    return mail_send_bridge


def make_bridge_sender(
    *,
    workspace_root: Path,
    output_dir: Path,
    mail_phase: str = "test",
    mail_to: list[str] | None = None,
    dry_run: bool = False,
) -> MailSender:
    """Return a MailSender that sends via the reference mail_send_bridge.

    bsa payload (subject/body/html_path/attachments) is mapped to the bridge's
    payload shape; the bridge builds the full payload and invokes the
    mail_send_subflow subprocess. Returns a dict normalized to bsa's MailResult
    expectation (status/error/report_path).
    """

    def sender(payload: dict[str, Any]) -> dict[str, Any]:
        bridge = _load_bridge()
        report_path = str(payload.get("html_path") or payload.get("report_path") or "")
        attachments = list(payload.get("attachments") or [])
        if report_path and report_path not in attachments:
            attachments.insert(0, report_path)
        window = payload.get("window_desc") or payload.get("subject") or ""
        result = bridge.send_bma_reports_via_mail_send(
            report_paths=[report_path],
            window_desc=window,
            repo_summaries=payload.get("repo_summaries") or [],
            mail_cfg={
                "mail_phase": mail_phase,
                "mail_to": mail_to or ["yangfu@raisecom.com"],
            },
            workspace_root=workspace_root,
            output_dir=output_dir,
            dry_run=dry_run,
        )
        status = (
            "ok"
            if result.get("mail_result") in {"SUCCESS", "DRY_RUN"}
            else "failed"
        )
        return {
            "status": status,
            "error": (
                str(result.get("error_reason_cn") or result.get("mail_result"))
                if status == "failed"
                else None
            ),
            "report_path": report_path or None,
            "bridge_result": result,
        }

    return sender


def send_via_bridge(
    *,
    report_path: Path,
    subject: str,
    workspace_root: Path,
    output_dir: Path,
    mail_phase: str = "test",
    mail_to: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Direct convenience wrapper for callers that already have the final report."""
    return make_bridge_sender(
        workspace_root=workspace_root,
        output_dir=output_dir,
        mail_phase=mail_phase,
        mail_to=mail_to,
        dry_run=dry_run,
    )(
        {
            "html_path": str(report_path),
            "subject": subject,
            "attachments": [str(report_path)],
        }
    )
