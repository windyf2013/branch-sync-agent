from __future__ import annotations

from collections.abc import Callable
from typing import Any


MailSender = Callable[[dict[str, Any]], dict[str, Any]]


def _default_sender(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "ok",
        "report_path": payload.get("report_path"),
        "detail": "mail sent via default stub sender",
    }


def maybe_send_mail(
    payload: dict[str, Any],
    *,
    enabled: bool,
    sender: MailSender | None = None,
) -> dict[str, Any]:
    if not enabled:
        return {
            "status": "skipped",
            "reason": "mail_enabled is false",
        }

    send = sender or _default_sender
    try:
        result = send(payload)
    except Exception as exc:  # noqa: BLE001 — boundary captures transport failures
        return {
            "status": "failed",
            "error": str(exc),
            "report_path": payload.get("report_path"),
        }

    if not isinstance(result, dict):
        return {
            "status": "failed",
            "error": "sender returned non-dict result",
            "report_path": payload.get("report_path"),
        }

    status = result.get("status", "ok")
    if status not in {"ok", "failed", "skipped"}:
        status = "ok" if status else "failed"

    merged = dict(result)
    merged["status"] = status
    if "report_path" not in merged and payload.get("report_path") is not None:
        merged["report_path"] = payload.get("report_path")
    return merged
