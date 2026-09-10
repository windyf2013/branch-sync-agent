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
