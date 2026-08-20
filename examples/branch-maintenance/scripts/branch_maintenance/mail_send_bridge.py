"""Bridge BMA report handoff to release-integration mail_send_subflow."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_TEST_RECIPIENTS = ["yangfu@raisecom.com"]

DEFAULT_SMTP = {
    "smtp_host": "smtp.exmail.qq.com",
    "smtp_port": 465,
    "smtp_security": "SSL",
    "smtp_user": "",
    "smtp_pass": "${SMTP_PASS}",
    "secrets_file": ".cursor/platform/mail_send_secrets.local.json",
    "from_email": "",
    "from_name": "RCIOS分支维护报告",
    "timeout_sec": 20,
}

SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def load_smtp_from_bug_stale_alert() -> dict[str, Any]:
    """Reuse SMTP settings from bug超期预警 (bug_stale_alert.CONFIG_TEMPLATE)."""
    candidates = [
        SCRIPTS_DIR,
        # After copy: .cursor|.claude/scripts next to sibling common is uncommon;
        # aiskill package layout: ../../scripts under common/
        SCRIPTS_DIR.parents[2] / "scripts" if len(SCRIPTS_DIR.parents) >= 3 else None,
        Path(__file__).resolve().parents[4] / "scripts"
        if len(Path(__file__).resolve().parents) >= 5
        else None,
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        text = str(candidate)
        if candidate.is_dir() and text not in sys.path:
            sys.path.insert(0, text)
    try:
        from bug_stale_alert import CONFIG_TEMPLATE  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Cannot load SMTP from bug_stale_alert.py; keep that module available."
        ) from exc

    smtp = CONFIG_TEMPLATE.get("smtp") or {}
    if not isinstance(smtp, dict):
        raise RuntimeError("bug_stale_alert.CONFIG_TEMPLATE.smtp is missing or invalid.")

    user = str(smtp.get("user") or "").strip()
    return {
        "smtp_host": str(smtp.get("host") or DEFAULT_SMTP["smtp_host"]),
        "smtp_port": int(smtp.get("port") or DEFAULT_SMTP["smtp_port"]),
        "smtp_security": str(smtp.get("security") or DEFAULT_SMTP["smtp_security"]),
        "smtp_user": user,
        "smtp_pass": str(smtp.get("smtp_pass") or ""),
        "from_email": user,
        "from_name": "RCIOS分支维护报告",
        "timeout_sec": int(DEFAULT_SMTP["timeout_sec"]),
    }


def resolve_smtp_settings(mail_cfg: dict[str, Any]) -> dict[str, Any]:
    """Prefer bug-stale SMTP; allow mail_smtp overrides for non-secret fields."""
    source = str(
        mail_cfg.get("mail_smtp_source") or mail_cfg.get("smtp_source") or "bug_stale_alert"
    ).strip().lower()

    if source in {"bug_stale_alert", "bug-stale-alert", "bug_stale", "stale"}:
        smtp = load_smtp_from_bug_stale_alert()
    else:
        smtp = dict(DEFAULT_SMTP)

    raw_smtp = mail_cfg.get("mail_smtp") or mail_cfg.get("smtp") or {}
    if isinstance(raw_smtp, dict):
        # Map bug-stale style keys if a caller passes them through.
        mapped = dict(raw_smtp)
        if "host" in mapped and "smtp_host" not in mapped:
            mapped["smtp_host"] = mapped["host"]
        if "port" in mapped and "smtp_port" not in mapped:
            mapped["smtp_port"] = mapped["port"]
        if "security" in mapped and "smtp_security" not in mapped:
            mapped["smtp_security"] = mapped["security"]
        if "user" in mapped and "smtp_user" not in mapped:
            mapped["smtp_user"] = mapped["user"]
        for key, value in mapped.items():
            if value is None or value == "":
                continue
            if key in {"host", "port", "security", "user"}:
                continue
            smtp[key] = value
        if mapped.get("smtp_user") and not smtp.get("from_email"):
            smtp["from_email"] = mapped["smtp_user"]
    return smtp


def resolve_mail_send_script(explicit: str | None = None) -> Path:
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return path.resolve()
        raise FileNotFoundError(f"mail_send_script not found: {explicit}")

    env = os.environ.get("RELEASE_INTEGRATION_PLUGIN_ROOT", "").strip()
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env) / "scripts" / "mail_send_subflow.py")
    home = Path.home()
    candidates.extend(
        [
            home
            / ".cursor"
            / "plugins"
            / "local"
            / "release-integration"
            / "scripts"
            / "mail_send_subflow.py",
            home
            / ".claude"
            / "plugins"
            / "local"
            / "release-integration"
            / "scripts"
            / "mail_send_subflow.py",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        "mail_send_subflow.py not found. Install release-integration plugin "
        "or set RELEASE_INTEGRATION_PLUGIN_ROOT / mail_send_script."
    )


def _as_email_list(value: Any, *, default: list[str] | None = None) -> list[str]:
    if value is None:
        return list(default or [])
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ValueError("mail recipients must be a list or comma-separated string")


def recipients_for_phase(mail_cfg: dict[str, Any]) -> list[str]:
    phase = str(mail_cfg.get("mail_phase") or mail_cfg.get("phase") or "test").strip().lower()
    if phase in {"test", "testing", "dev"}:
        configured = _as_email_list(mail_cfg.get("mail_to") or mail_cfg.get("to_emails"))
        # Test phase hard-cap: only yangfu unless explicitly overridden to a subset
        # that still stays within the test allow-list.
        allow = set(DEFAULT_TEST_RECIPIENTS)
        if not configured:
            return list(DEFAULT_TEST_RECIPIENTS)
        filtered = [addr for addr in configured if addr.lower() in {a.lower() for a in allow}]
        return filtered or list(DEFAULT_TEST_RECIPIENTS)
    return _as_email_list(
        mail_cfg.get("mail_to") or mail_cfg.get("to_emails"),
        default=list(DEFAULT_TEST_RECIPIENTS),
    )


def build_mail_payload(
    *,
    report_paths: list[str],
    window_desc: str,
    repo_summaries: list[dict[str, Any]],
    mail_cfg: dict[str, Any],
    workspace_root: Path,
) -> dict[str, Any]:
    if not report_paths:
        raise ValueError("report_paths must not be empty")

    smtp = resolve_smtp_settings(mail_cfg)

    secrets_file = str(smtp.get("secrets_file") or "").strip()
    secrets_path = None
    if secrets_file:
        secrets_path = Path(secrets_file)
        if not secrets_path.is_absolute():
            secrets_path = (workspace_root / secrets_path).resolve()

    to_emails = recipients_for_phase(mail_cfg)
    cc_emails = _as_email_list(mail_cfg.get("mail_cc") or mail_cfg.get("cc_emails"))
    phase = str(mail_cfg.get("mail_phase") or "test").strip().lower()

    lines = [
        "各位好，",
        "",
        "分支维护同步建议报告（BMA）已生成，请查收附件 HTML。",
        "",
        f"【评估时间范围】{window_desc or '（见报告页眉）'}",
        f"【发信阶段】{phase}（测试阶段仅通知指定收件人）",
        "",
        "【报告列表】",
    ]
    for path in report_paths:
        lines.append(f"- {path}")
    if repo_summaries:
        lines.extend(["", "【摘要】"])
        for item in repo_summaries:
            repo_id = item.get("repo_id") or "?"
            need = item.get("need_sync_count")
            pending = item.get("pending_agent_count")
            scanned = item.get("commits_scanned")
            lines.append(
                f"- {repo_id}: 扫描 {scanned} · NeedSync {need} · 待 Agent {pending}"
            )
    lines.extend(["", "此邮件由 Branch Maintenance Agent 自动发送。", ""])
    body = "\n".join(lines)

    subject_prefix = str(mail_cfg.get("mail_subject_prefix") or "[BMA]").strip()
    subject = f"{subject_prefix} 分支维护同步建议报告（{len(report_paths)} 份）"

    from_email = str(smtp.get("from_email") or smtp.get("smtp_user") or "").strip()
    smtp_user = str(smtp.get("smtp_user") or from_email).strip()

    attachments: list[str] = []
    for raw in report_paths:
        path = Path(raw)
        if not path.is_absolute():
            path = (workspace_root / path).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"report attachment not found: {path}")
        attachments.append(str(path))

    payload: dict[str, Any] = {
        "smtp_host": str(smtp.get("smtp_host") or DEFAULT_SMTP["smtp_host"]),
        "smtp_port": int(smtp.get("smtp_port") or DEFAULT_SMTP["smtp_port"]),
        "smtp_security": str(smtp.get("smtp_security") or DEFAULT_SMTP["smtp_security"]),
        "smtp_user": smtp_user,
        "smtp_pass": str(smtp.get("smtp_pass") or ""),
        "from_email": from_email or smtp_user,
        "from_name": str(smtp.get("from_name") or DEFAULT_SMTP["from_name"]),
        "to_emails": to_emails,
        "cc_emails": cc_emails,
        "subject": subject,
        "body": body,
        "attachments": attachments,
        "timeout_sec": int(smtp.get("timeout_sec") or DEFAULT_SMTP["timeout_sec"]),
        "notice_type": "BMA_REPORT",
        "mail_phase": phase,
    }
    if secrets_path is not None:
        payload["secrets_file"] = str(secrets_path)
    return payload


def invoke_mail_send_subflow(
    payload: dict[str, Any],
    *,
    workspace_root: Path,
    payload_path: Path,
    result_path: Path,
    script_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    script = script_path or resolve_mail_send_script(
        str(payload.get("mail_send_script") or "") or None
    )
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    cmd = [
        sys.executable,
        str(script),
        "--payload",
        str(payload_path),
        "--output",
        str(result_path),
    ]
    if dry_run:
        cmd.append("--dry-run")

    completed = subprocess.run(
        cmd,
        cwd=str(workspace_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if result_path.is_file():
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            result = {
                "mail_result": "FAILURE",
                "error_type": "RESULT_INVALID",
                "error_reason_cn": "邮件结果文件不是合法 JSON",
                "error_log_excerpt": result_path.read_text(encoding="utf-8")[:500],
            }
    else:
        result = {
            "mail_result": "FAILURE",
            "error_type": "NO_RESULT",
            "error_reason_cn": "邮件子流程未写出结果文件",
            "error_log_excerpt": (completed.stderr or completed.stdout or "")[:500],
        }

    result["exit_code"] = completed.returncode
    result["payload_path"] = str(payload_path.resolve())
    result["result_path"] = str(result_path.resolve())
    if completed.returncode != 0 and result.get("mail_result") == "SUCCESS":
        result["mail_result"] = "FAILURE"
    return result


def send_bma_reports_via_mail_send(
    *,
    report_paths: list[str],
    window_desc: str,
    repo_summaries: list[dict[str, Any]],
    mail_cfg: dict[str, Any],
    workspace_root: Path,
    output_dir: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    payload = build_mail_payload(
        report_paths=report_paths,
        window_desc=window_desc,
        repo_summaries=repo_summaries,
        mail_cfg=mail_cfg,
        workspace_root=workspace_root,
    )
    script_override = mail_cfg.get("mail_send_script")
    script_path = None
    if script_override:
        script_path = resolve_mail_send_script(str(script_override))

    payload_path = output_dir / "bma_mail_send_payload.runtime.json"
    result_path = output_dir / "bma_mail_send_result.json"
    return invoke_mail_send_subflow(
        payload,
        workspace_root=workspace_root,
        payload_path=payload_path,
        result_path=result_path,
        script_path=script_path,
        dry_run=dry_run,
    )
