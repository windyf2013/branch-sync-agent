"""BSA 原生 SMTP 发信测试（不实发：只测收件人解析 / MIME 组装 / 结果语义）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bsa.config.settings import Settings
from bsa.mail.smtp_sender import (
    build_message,
    make_smtp_sender,
    resolve_recipients,
)


def make_settings(tmp_path: Path, **overrides) -> Settings:
    base = {
        "_env_file": None,
        "repo_path": "/srv/rcios",
        "branch_file": "/srv/rcios/branch.md",
        "worktree_root": "/srv/worktrees",
        "llm_model": "m",
        "llm_api_key": "k",
        "llm_base_url": "https://x",
        "docker_image": "rcios-build:latest",
        "docker_mount_workspace": "/workspace/rcios",
        "build_script_dir": "build/platform/RTL9617C",
        "log_dir": str(tmp_path),
        "mail_sender": "soft2@raisecom.com",
        "mail_recipients": ["yangfu@raisecom.com"],
    }
    base.update(overrides)
    return Settings(**base)


class TestResolveRecipients:
    def test_prod_passes_configured_through(self, tmp_path: Path):
        s = make_settings(tmp_path, mail_phase="prod")
        to, cc = resolve_recipients(s, ["a@x.com", "b@x.com"], ["c@x.com"])
        assert to == ["a@x.com", "b@x.com"]
        assert cc == ["c@x.com"]

    def test_test_phase_hardcaps_to_allowlist(self, tmp_path: Path):
        s = make_settings(
            tmp_path, mail_phase="test", mail_test_allowlist=["safe@x.com"]
        )
        to, cc = resolve_recipients(s, ["outsider@x.com"], ["another@x.com"])
        # 配置收件人全在白名单外 → 退回白名单本身；cc 被滤空
        assert to == ["safe@x.com"]
        assert cc == []

    def test_test_phase_keeps_allowlisted_subset(self, tmp_path: Path):
        s = make_settings(
            tmp_path, mail_phase="test", mail_test_allowlist=["safe@x.com"]
        )
        to, _ = resolve_recipients(s, ["safe@x.com", "outsider@x.com"], [])
        assert to == ["safe@x.com"]

    def test_empty_recipients_raises(self, tmp_path: Path):
        s = make_settings(tmp_path, mail_phase="prod")
        with pytest.raises(ValueError, match="收件人为空"):
            resolve_recipients(s, [], [])

    def test_comma_string_is_split(self, tmp_path: Path):
        s = make_settings(tmp_path, mail_phase="prod")
        to, _ = resolve_recipients(s, "a@x.com, b@x.com", None)
        assert to == ["a@x.com", "b@x.com"]


class TestBuildMessage:
    def test_message_has_bsa_branding_and_attachments(self, tmp_path: Path):
        s = make_settings(tmp_path)
        report = tmp_path / "report.html"
        report.write_text("<html>报告</html>", encoding="utf-8")
        patch = tmp_path / "x.patch"
        patch.write_text("diff --git a b\n", encoding="utf-8")
        msg = build_message(
            settings=s,
            subject="Branch Sync Agent 周期报告 cycle-2026-09-09 [FAILED]",
            body="正文摘要",
            to=["yangfu@raisecom.com"],
            cc=[],
            attachments=[patch],
            html_path=report,
        )
        assert msg["Subject"].startswith("Branch Sync Agent")
        assert msg["To"] == "yangfu@raisecom.com"
        assert "RCIOS 分支同步报告" in msg["From"]
        # multipart/mixed → [multipart/alternative(plain + html), 附件]
        assert msg.get_content_type() == "multipart/mixed"
        parts = msg.get_payload()
        assert len(parts) == 2
        assert parts[0].get_content_type() == "multipart/alternative"
        # 正文含纯文本与 HTML 两种形态（客户端优先渲染 HTML）
        assert msg.get_body(preferencelist=("html",)) is not None
        assert msg.get_body(preferencelist=("plain",)) is not None
        assert parts[1].get_filename() == "x.patch"

    def test_missing_html_degrades_to_plain(self, tmp_path: Path):
        s = make_settings(tmp_path)
        msg = build_message(
            settings=s,
            subject="s",
            body="b",
            to=["y@x.com"],
            cc=[],
            attachments=[],
            html_path=None,
        )
        assert msg.get_body(preferencelist=("html",)) is None


class TestMakeSmtpSender:
    def test_requires_mail_to(self, tmp_path: Path):
        s = make_settings(tmp_path)
        with pytest.raises(ValueError, match="mail_to 必填"):
            make_smtp_sender(settings=s, mail_to=None)

    def test_dry_run_does_not_send_and_writes_result(self, tmp_path: Path):
        s = make_settings(tmp_path)
        sender = make_smtp_sender(
            settings=s, mail_to=["yangfu@raisecom.com"], output_dir=tmp_path, dry_run=True
        )
        out = sender({"subject": "s", "body": "b", "attachments": []})
        assert out["status"] == "skipped"
        result = json.loads((tmp_path / "bsa_mail_send_result.json").read_text("utf-8"))
        assert result["mail_result"] == "DRY_RUN"
        assert result["to_emails"] == ["yangfu@raisecom.com"]

    def test_missing_attachment_returns_failed(self, tmp_path: Path):
        s = make_settings(tmp_path)
        sender = make_smtp_sender(
            settings=s, mail_to=["yangfu@raisecom.com"], output_dir=tmp_path
        )
        out = sender(
            {"subject": "s", "body": "b", "attachments": [str(tmp_path / "nope.patch")]}
        )
        assert out["status"] == "failed"
        assert "不存在" in out["error"]

    def test_no_password_returns_failed(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("SMTP_PASS", raising=False)
        s = make_settings(tmp_path)  # mail_smtp_pass 为空
        sender = make_smtp_sender(
            settings=s, mail_to=["yangfu@raisecom.com"], output_dir=tmp_path
        )
        out = sender({"subject": "s", "body": "b", "attachments": []})
        assert out["status"] == "failed"
        result = json.loads((tmp_path / "bsa_mail_send_result.json").read_text("utf-8"))
        assert result["mail_result"] == "FAILURE"
        assert result["error_reason_cn"]


class TestBodyHtmlAlternative:
    """正文摘要（body_html）与完整报告（html_path）的分工。

    改造后：``body_html`` 作 text/html 正文，report.html 退为纯附件归档。
    ``body_html`` 缺省时回退到旧行为（把 html_path 全文当正文），老调用方零改动。
    """

    def _report_and_patch(self, tmp_path: Path) -> tuple[Path, Path]:
        report = tmp_path / "report.html"
        report.write_text("<html>完整报告</html>", encoding="utf-8")
        patch = tmp_path / "x.patch"
        patch.write_text("diff --git a b\n", encoding="utf-8")
        return report, patch

    def test_body_html_wins_over_html_path(self, tmp_path: Path):
        s = make_settings(tmp_path)
        report, patch = self._report_and_patch(tmp_path)

        msg = build_message(
            settings=s,
            subject="s",
            body="纯文本兜底",
            to=["y@x.com"],
            cc=[],
            attachments=[patch],
            html_path=report,
            body_html="<html><body>摘要正文</body></html>",
        )

        html_body = msg.get_body(preferencelist=("html",)).get_content()
        assert "摘要正文" in html_body
        assert "完整报告" not in html_body

    def test_structure_stays_two_top_level_parts(self, tmp_path: Path):
        # 摘要与纯文本都装在 alternative 容器内，顶层计数不变（附件仍是 1 个 patch）。
        s = make_settings(tmp_path)
        report, patch = self._report_and_patch(tmp_path)

        msg = build_message(
            settings=s,
            subject="s",
            body="纯文本",
            to=["y@x.com"],
            cc=[],
            attachments=[patch],
            html_path=report,
            body_html="<html>摘要</html>",
        )

        assert msg.get_content_type() == "multipart/mixed"
        parts = msg.get_payload()
        assert len(parts) == 2
        assert parts[0].get_content_type() == "multipart/alternative"
        assert parts[1].get_filename() == "x.patch"
        assert msg.get_body(preferencelist=("plain",)) is not None

    def test_none_falls_back_to_html_path(self, tmp_path: Path):
        # 向后兼容：body_html=None 时行为与改造前逐字一致。
        s = make_settings(tmp_path)
        report, patch = self._report_and_patch(tmp_path)

        msg = build_message(
            settings=s,
            subject="s",
            body="b",
            to=["y@x.com"],
            cc=[],
            attachments=[patch],
            html_path=report,
            body_html=None,
        )

        assert "完整报告" in msg.get_body(preferencelist=("html",)).get_content()

    def test_body_html_alone_produces_alternative(self, tmp_path: Path):
        # 无 html_path 但给了 body_html → 仍应有 HTML 正文。
        s = make_settings(tmp_path)

        msg = build_message(
            settings=s,
            subject="s",
            body="b",
            to=["y@x.com"],
            cc=[],
            attachments=[],
            html_path=None,
            body_html="<html>摘要</html>",
        )

        assert "摘要" in msg.get_body(preferencelist=("html",)).get_content()


class TestSenderReportIsAttachment:
    def test_report_html_is_attached_while_summary_is_inline(self, tmp_path: Path, monkeypatch):
        """report.html 退为纯附件，但必须仍在附件列表里（交付物可归档）。"""
        from bsa.mail import smtp_sender

        s = make_settings(tmp_path, mail_phase="prod")
        cycle_dir = tmp_path / "cycle-2026-08-20"
        cycle_dir.mkdir()
        report = cycle_dir / "report.html"
        report.write_text("<html>完整报告</html>", encoding="utf-8")

        captured: dict = {}

        def fake_send(msg, settings, recipients):
            captured["msg"] = msg

        monkeypatch.setattr(smtp_sender, "_send", fake_send)

        sender = make_smtp_sender(
            settings=s,
            mail_to=["yangfu@raisecom.com"],
            output_dir=cycle_dir,
        )
        out = sender(
            {
                "subject": "s",
                "body": "纯文本",
                "html_path": str(report),
                "report_path": str(report),
                "attachments": [],
                "body_html": "<html><body>摘要正文</body></html>",
            }
        )

        assert out["status"] == "ok"
        msg = captured["msg"]
        assert "摘要正文" in msg.get_body(preferencelist=("html",)).get_content()
        filenames = [p.get_filename() for p in msg.iter_attachments()]
        assert "report.html" in filenames

        result = json.loads((cycle_dir / "bsa_mail_send_result.json").read_text(encoding="utf-8"))
        assert result["attachment_count"] == 1
