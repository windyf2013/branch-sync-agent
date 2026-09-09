from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from bsa.mail.bridge_sender import _load_bridge, make_bridge_sender, send_via_bridge

STUB_BRIDGE_SRC = '''\
def send_bma_reports_via_mail_send(*, report_paths, window_desc, repo_summaries,
                                   mail_cfg, workspace_root, output_dir, dry_run):
    return {
        "mail_result": "SUCCESS",
        "got_mail_cfg": mail_cfg,
        "got_report_paths": report_paths,
    }
'''


@pytest.fixture
def stub_bridge(tmp_path: Path) -> Path:
    pkg = tmp_path / "branch_maintenance"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "mail_send_bridge.py").write_text(STUB_BRIDGE_SRC)
    yield tmp_path
    for name in list(sys.modules):
        if name == "branch_maintenance" or name.startswith("branch_maintenance."):
            del sys.modules[name]


def test_load_bridge_raises_without_path(monkeypatch) -> None:
    monkeypatch.setattr("bsa.mail.bridge_sender.os.environ", {})
    with pytest.raises(RuntimeError, match="bridge scripts path is not configured"):
        _load_bridge()


def test_make_bridge_sender_requires_mail_to(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mail_to is required"):
        make_bridge_sender(workspace_root=tmp_path, output_dir=tmp_path)


def test_bridge_sender_maps_payload_and_recipients(stub_bridge: Path, tmp_path: Path) -> None:
    report = tmp_path / "report.html"
    report.write_text("<html></html>")
    sender = make_bridge_sender(
        workspace_root=tmp_path,
        output_dir=tmp_path,
        mail_phase="test",
        mail_to=["recv@raisecom.com"],
        bridge_path=stub_bridge,
    )
    result = sender(
        {
            "html_path": str(report),
            "subject": "test",
            "attachments": [str(report)],
        }
    )
    assert result["status"] == "ok"
    assert result["report_path"] == str(report)
    assert result["bridge_result"]["got_mail_cfg"]["mail_to"] == ["recv@raisecom.com"]
    assert result["bridge_result"]["got_report_paths"] == [str(report)]


def test_bridge_resolves_relative_report_path(stub_bridge, tmp_path, monkeypatch):
    """相对附件须绝对化,不能丢给 bridge 用 workspace_root 再拼(否则 logs/logs 双叠)。

    回归: report_path 可能是相对 cwd(如 ``logs/cycle-x/report.html``), bridge 拿
    workspace_root(=settings.log_dir) 拼接会找不到。bsa 侧必须 resolve 成绝对。
    """
    monkeypatch.chdir(tmp_path)  # 使相对路径相对 tmp_path
    sub = tmp_path / "logs" / "cycle-x"
    sub.mkdir(parents=True)
    report = sub / "report.html"
    report.write_text("<html></html>")

    # 相对路径 payload(模拟引擎传相对 report_path)
    sender = make_bridge_sender(
        workspace_root=tmp_path / "logs",  # 模拟相对 log_dir
        output_dir=tmp_path / "logs" / "cycle-x",
        mail_phase="prod",
        mail_to=["pm@raisecom.com"],
        bridge_path=stub_bridge,
    )
    result = sender(
        {
            "html_path": "logs/cycle-x/report.html",  # 相对 cwd(=tmp_path)
            "subject": "t",
            "attachments": [],
        }
    )
    assert result["status"] == "ok"
    got = result["bridge_result"]["got_report_paths"]
    # 传给 bridge 的必须是绝对路径(相对 cwd resolve),而非 "logs/cycle-x/report.html"
    assert got == [str(report.resolve())]
    assert all(p.startswith("/") for p in got)


def test_bridge_sender_passes_mail_cc_when_set(stub_bridge: Path, tmp_path: Path) -> None:
    """mail_cc 透传给 bridge 的 mail_cfg.mail_cc。"""
    report = tmp_path / "report.html"
    report.write_text("<html></html>")
    sender = make_bridge_sender(
        workspace_root=tmp_path,
        output_dir=tmp_path,
        mail_phase="test",
        mail_to=["recv@raisecom.com"],
        mail_cc=["cc1@raisecom.com", "cc2@raisecom.com"],
        bridge_path=stub_bridge,
    )
    result = sender(
        {
            "html_path": str(report),
            "subject": "test",
            "attachments": [str(report)],
        }
    )
    assert result["status"] == "ok"
    assert result["bridge_result"]["got_mail_cfg"]["mail_to"] == ["recv@raisecom.com"]
    assert result["bridge_result"]["got_mail_cfg"]["mail_cc"] == [
        "cc1@raisecom.com",
        "cc2@raisecom.com",
    ]


def test_send_via_bridge_dry_run_returns_ok(stub_bridge: Path, tmp_path: Path) -> None:
    report = tmp_path / "r.html"
    report.write_text("<html></html>")
    result = send_via_bridge(
        report_path=report,
        subject="dry",
        workspace_root=tmp_path,
        output_dir=tmp_path,
        mail_to=["recv@raisecom.com"],
        mail_cc=["cc1@raisecom.com"],
        bridge_path=stub_bridge,
        dry_run=True,
    )
    assert result["status"] == "ok"
    assert result["bridge_result"]["got_mail_cfg"]["mail_to"] == ["recv@raisecom.com"]
    assert result["bridge_result"]["got_mail_cfg"]["mail_cc"] == ["cc1@raisecom.com"]


def _real_bridge():
    raw = os.environ.get("BMA_BRIDGE_PATH", "")
    if not raw:
        pytest.skip("BMA_BRIDGE_PATH not set; reference bridge unavailable")
    return _load_bridge(Path(raw))


def test_recipients_for_phase_test_caps_to_yangfu() -> None:
    rcpt = _real_bridge().recipients_for_phase({"mail_phase": "test"})
    assert rcpt == ["yangfu@raisecom.com"]


def test_recipients_for_phase_test_with_to_other_is_filtered() -> None:
    rcpt = _real_bridge().recipients_for_phase(
        {"mail_phase": "test", "mail_to": ["someone@raisecom.com"]}
    )
    assert rcpt == ["yangfu@raisecom.com"]


def test_smtp_loads_from_bug_stale_alert() -> None:
    smtp = _real_bridge().load_smtp_from_bug_stale_alert()
    assert smtp["smtp_host"]
    assert smtp["smtp_user"]
    assert smtp["smtp_security"] == "SSL"


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
