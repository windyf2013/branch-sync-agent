"""BSA 原生 SMTP 发信。

本模块是发信的唯一实现，**不依赖任何外部 bridge / 插件**（历史上曾借道
``branch_maintenance``（BMA）的 mail_send_bridge 再转 release-integration 的
mail_send_subflow，导致主题/落款被改成 BMA 品牌、依赖产品仓内部脚本与用户级插件，
排障困难——此处一次性内聚掉）。

职责边界：
- 收件人阶段过滤（test/dev 硬帽到白名单）——安全阀，与 recipient 解析分离。
- 组装 MIME（正文 + 可选 HTML alternative + 附件）。
- smtplib 实发（SSL / STARTTLS / PLAIN）。
- 发送结果落盘审计（``bsa_mail_send_result.json``），失败不抛给调用方之外。

主题与正文由调用方（``scheduler/cycle.py``）给出，本模块不改写品牌——所以邮件呈现
的是 BSA（Branch Sync Agent）自己的文案。
"""

from __future__ import annotations

import json
import mimetypes
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any

from bsa.config.settings import Settings
from bsa.mail.service import MailSender

# test/dev 阶段：收件人硬帽到白名单，绝不误发到真实干系人（安全默认）。
_TEST_PHASES = {"test", "testing", "dev"}

_VALID_SECURITY = {"SSL", "STARTTLS", "PLAIN"}


def _as_email_list(value: Any) -> list[str]:
    """归一化收件人：None→[]，str→按逗号拆，list→去空保序。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ValueError("收件人必须是 list 或逗号分隔字符串")


def resolve_recipients(
    settings: Settings, mail_to: Any, mail_cc: Any
) -> tuple[list[str], list[str]]:
    """解析 (to, cc)，并按发信阶段施加硬帽。

    prod 阶段：原样返回配置收件人。
    test/dev 阶段：to = 配置 ∩ 白名单（为空则退回白名单本身），cc 同样过滤——
    测试期绝不把邮件发给白名单外的人。
    """
    to = _as_email_list(mail_to)
    cc = _as_email_list(mail_cc)
    phase = str(settings.mail_phase or "test").strip().lower()
    if phase in _TEST_PHASES:
        allow = {addr.lower() for addr in settings.mail_test_allowlist}
        filtered = [addr for addr in to if addr.lower() in allow]
        to = filtered or list(settings.mail_test_allowlist)
        cc = [addr for addr in cc if addr.lower() in allow]
    if not to:
        raise ValueError("收件人为空：请配置 MAIL_RECIPIENTS/MAIL_PM_RECIPIENTS")
    return to, cc


def _smtp_password(settings: Settings) -> str:
    """SMTP 口令：MAIL_SMTP_PASS 优先，空则回退通用 SMTP_PASS（老部署兼容）。"""
    return str(settings.mail_smtp_pass or "").strip() or os.environ.get(
        "SMTP_PASS", ""
    ).strip()


def _smtp_user(settings: Settings) -> str:
    return str(settings.mail_smtp_user or settings.mail_sender or "").strip()


def build_message(
    *,
    settings: Settings,
    subject: str,
    body: str,
    to: list[str],
    cc: list[str],
    attachments: list[Path],
    html_path: Path | None = None,
    body_html: str | None = None,
) -> EmailMessage:
    """组装 MIME：纯文本正文 + 可选 HTML alternative + 附件。

    ``text/html`` alternative 的取值优先级：

    1. ``body_html`` —— 邮件正文摘要（收件人不点开附件就能读核心信息）；
    2. ``html_path`` —— 回退：把该文件全文当正文（改造前的行为，保持向后兼容）。

    ``html_path`` 指向的报告即便降为纯附件，仍由 sender 侧插入附件列表归档
    （见 ``make_smtp_sender``），交付物不丢。
    """
    from_addr = str(settings.mail_sender or _smtp_user(settings)).strip()
    from_name = str(settings.mail_from_name or "").strip()

    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_addr)) if from_name else from_addr
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg.set_content(body or "", subtype="plain", charset="utf-8")

    if body_html is not None:
        msg.add_alternative(body_html, subtype="html", charset="utf-8")
    elif html_path is not None and html_path.is_file():
        try:
            msg.add_alternative(
                html_path.read_text(encoding="utf-8", errors="replace"),
                subtype="html",
                charset="utf-8",
            )
        except OSError:
            # HTML 读失败不阻断发信（正文 + 附件仍在），降级为纯文本邮件。
            pass

    for path in attachments:
        mime_type, _ = mimetypes.guess_type(str(path))
        if mime_type:
            maintype, subtype = mime_type.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(
            path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name
        )
    return msg


def _send(msg: EmailMessage, settings: Settings, recipients: list[str]) -> None:
    """按 security 选择 SSL / STARTTLS / PLAIN 实发。"""
    host = str(settings.mail_smtp_host).strip()
    port = int(settings.mail_smtp_port)
    security = str(settings.mail_smtp_security or "").strip().upper()
    if security not in _VALID_SECURITY:
        raise ValueError(
            f"MAIL_SMTP_SECURITY 非法: {security!r}（应为 {'/'.join(sorted(_VALID_SECURITY))}）"
        )
    password = _smtp_password(settings)
    if not password:
        raise ValueError("SMTP 口令为空：请配置 MAIL_SMTP_PASS（或 SMTP_PASS）")
    user = _smtp_user(settings)
    from_addr = str(settings.mail_sender or user).strip()
    timeout = int(settings.mail_timeout_sec)

    if security == "SSL":
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=context, timeout=timeout) as server:
            server.login(user, password)
            server.send_message(msg, from_addr=from_addr, to_addrs=recipients)
    elif security == "STARTTLS":
        context = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=timeout) as server:
            server.starttls(context=context)
            server.login(user, password)
            server.send_message(msg, from_addr=from_addr, to_addrs=recipients)
    else:  # PLAIN
        with smtplib.SMTP(host, port, timeout=timeout) as server:
            server.login(user, password)
            server.send_message(msg, from_addr=from_addr, to_addrs=recipients)


def make_smtp_sender(
    *,
    settings: Settings,
    mail_to: list[str] | None = None,
    mail_cc: list[str] | None = None,
    output_dir: Path | None = None,
    dry_run: bool = False,
) -> MailSender:
    """构造 BSA 原生 MailSender（可直接注入 MailService）。

    ``mail_to`` 必填（来自 settings 收件人解析），不设默认收件人。``output_dir`` 给出
    时落盘 ``bsa_mail_send_result.json`` 审计（与周期产物同目录）。``dry_run`` 只组装
    不实发，返回 ``skipped`` 语义。
    """
    if not mail_to:
        raise ValueError("mail_to 必填（来自 settings 收件人解析），不设默认收件人")
    out_dir = Path(output_dir).resolve() if output_dir is not None else None

    def sender(payload: dict[str, Any]) -> dict[str, Any]:
        subject = str(payload.get("subject") or "")
        body = str(payload.get("body") or "")
        html_path = payload.get("html_path") or payload.get("report_path")
        report_path = str(html_path) if html_path else None
        raw_attachments = list(payload.get("attachments") or [])
        if report_path and report_path not in raw_attachments:
            raw_attachments.insert(0, report_path)

        # 附件一律绝对化后再校验存在性：相对 log_dir 部署下防双拼/错位。
        # 缺失/收件人非法都降级为 failed 结果，绝不把异常抛进 scheduler（发信失败
        # 只应记 mail_status=failed，不能让周期崩）。
        attachments: list[Path] = []
        try:
            for item in raw_attachments:
                path = Path(str(item))
                if not path.is_absolute():
                    path = path.resolve()
                if not path.is_file():
                    raise FileNotFoundError(f"邮件附件不存在: {path}")
                attachments.append(path)
            html_abs = Path(str(html_path)).resolve() if html_path else None
            to, cc = resolve_recipients(settings, mail_to, mail_cc)
        except Exception as exc:  # noqa: BLE001
            result = {
                "mail_result": "FAILURE",
                "error_reason_cn": str(exc),
                "subject": subject,
                "to_emails": list(mail_to),
            }
            _write_result(out_dir, result)
            return {"status": "failed", "error": str(exc), "report_path": report_path}

        result: dict[str, Any] = {
            "to_emails": to,
            "cc_emails": cc,
            "subject": subject,
            "attachment_count": len(attachments),
            "smtp_host": settings.mail_smtp_host,
            "smtp_port": settings.mail_smtp_port,
            "smtp_security": settings.mail_smtp_security,
            "from_email": settings.mail_sender,
            "mail_phase": settings.mail_phase,
        }

        if dry_run:
            result["mail_result"] = "DRY_RUN"
            _write_result(out_dir, result)
            return {"status": "skipped", "error": "dry-run: not sent", "report_path": report_path}

        try:
            msg = build_message(
                settings=settings,
                subject=subject,
                body=body,
                to=to,
                cc=cc,
                attachments=attachments,
                html_path=html_abs,
                body_html=payload.get("body_html"),
            )
            _send(msg, settings, to + cc)
        except Exception as exc:  # noqa: BLE001 — 发信失败记结果并降级，绝不炸周期
            result["mail_result"] = "FAILURE"
            result["error_reason_cn"] = str(exc)
            _write_result(out_dir, result)
            return {"status": "failed", "error": str(exc), "report_path": report_path}

        result["mail_result"] = "SUCCESS"
        result["message_id"] = str(msg.get("Message-Id") or "")
        _write_result(out_dir, result)
        return {"status": "ok", "error": None, "report_path": report_path}

    return sender


def _write_result(out_dir: Path | None, result: dict[str, Any]) -> None:
    """结果落盘审计；写失败只忽略（不影响发信结论）。"""
    if out_dir is None:
        return
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "bsa_mail_send_result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        pass
