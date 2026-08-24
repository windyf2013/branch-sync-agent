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


def test_send_via_bridge_dry_run_returns_ok(stub_bridge: Path, tmp_path: Path) -> None:
    report = tmp_path / "r.html"
    report.write_text("<html></html>")
    result = send_via_bridge(
        report_path=report,
        subject="dry",
        workspace_root=tmp_path,
        output_dir=tmp_path,
        mail_to=["recv@raisecom.com"],
        bridge_path=stub_bridge,
        dry_run=True,
    )
    assert result["status"] == "ok"
    assert result["bridge_result"]["got_mail_cfg"]["mail_to"] == ["recv@raisecom.com"]


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
