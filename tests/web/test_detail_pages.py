import re

from fastapi.testclient import TestClient

from bsa_web.app import create_app
from bsa_web.auth import hash_password
from bsa_web.rbac import OPERATOR


def _make_app(tmp_path):
    users = {"alice": f"{hash_password('op')}:{OPERATOR}"}
    return create_app(
        settings_override={
            "log_dir": str(tmp_path),
            "secret_key": "test-secret",
            "users": users,
        },
        env_file=None
    )


def _client(app):
    return TestClient(app, follow_redirects=False)


def _extract_csrf(html: str) -> str:
    m = re.search(r'name="_csrf"\s+value="([^"]+)"', html)
    assert m, "页面未注入 _csrf 隐藏字段"
    return m.group(1)


def _login(client, username="alice", password="op"):
    r = client.get("/login")
    assert r.status_code == 200
    csrf = _extract_csrf(r.text)
    return client.post(
        "/login",
        data={"username": username, "password": password, "_csrf": csrf},
    )


def _record(cycle_id, status, started_at):
    return {
        "cycle_id": cycle_id,
        "status": status,
        "report_path": None,
        "mail_status": None,
        "started_at": started_at,
        "finished_at": started_at,
    }


def _commit_info(sha, patch_text="+x"):
    return {
        "sha": sha,
        "message": f"msg-{sha}",
        "author": "dev",
        "committed_at": "2026-08-20T10:00:00+08:00",
        "changed_files": ["plat/demo.c"],
        "patch_text": patch_text,
        "symbols": [],
        "patch_id": f"pid-{sha}",
        "issue_ids": [],
        "source_branch": "develop",
        "homologous_section": "组网",
    }


def _conclusion(kind="NeedSync"):
    return {"kind": kind, "evidence": [f"ev-{kind}"], "confidence": "high"}


def _build_outcome(model="RTL9617C", status="FAILED", log_path=None, errors=None):
    return {
        "model": model,
        "status": status,
        "log_path": log_path,
        "errors": list(errors or []),
        "agent_attempts": 1,
        "fix_diff": None,
    }


def _commit_result(sha, cherry_pick="OK", build=None, conflict_resolution=None):
    return {
        "sha": sha,
        "cherry_pick": cherry_pick,
        "conflict_resolution": conflict_resolution,
        "build": build or {},
    }


def _branch(target, status="SUCCESS", commits=None, patch_path=None):
    return {
        "target_branch": target,
        "worktree_path": f"/wt/{target}",
        "status": status,
        "commits": commits or [],
        "patch_path": patch_path,
        "stop_reason": None,
    }


def _payload(**kw):
    base = {
        "cycle_id": "cycle-2026-08-20",
        "status": "REPORTED",
        "scan_window": ["2026-08-19T22:00:00+08:00", "2026-08-20T22:00:00+08:00"],
        "detected_commits": [],
        "decisions": {},
        "branch_results": {},
        "action_required": [],
    }
    base.update(kw)
    return base


class TestHistory:
    def test_history_lists_cycles_desc(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        records = [
            _record("cycle-2026-08-21", "REPORTED", "2026-08-21T22:00:00"),
            _record("cycle-2026-08-20", "FAILED", "2026-08-20T22:00:00"),
        ]
        monkeypatch.setattr("bsa_web.projection.list_cycles", lambda log_dir: records)
        r = client.get("/history")
        assert r.status_code == 200
        assert "cycle-2026-08-21" in r.text
        assert "cycle-2026-08-20" in r.text
        assert r.text.index("cycle-2026-08-21") < r.text.index("cycle-2026-08-20")

    def test_history_filters_by_conclusion_kind(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)

        def fake_load(log_dir, cycle_id):
            if cycle_id == "cycle-2026-08-21":
                return _payload(
                    cycle_id=cycle_id,
                    decisions={"a1": {"t": _conclusion("NeedSync")}},
                )
            return _payload(
                cycle_id=cycle_id,
                decisions={"b1": {"t": _conclusion("AlreadyIncluded")}},
            )

        monkeypatch.setattr(
            "bsa_web.projection.list_cycles",
            lambda log_dir: [
                _record("cycle-2026-08-21", "REPORTED", "2026-08-21T22:00:00"),
                _record("cycle-2026-08-20", "REPORTED", "2026-08-20T22:00:00"),
            ],
        )
        monkeypatch.setattr("bsa_web.projection.load_cycle", fake_load)
        r = client.get("/history", params={"kind": "NeedSync"})
        assert r.status_code == 200
        assert "cycle-2026-08-21" in r.text
        assert "cycle-2026-08-20" not in r.text

    def test_history_filters_failed_branches(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)

        def fake_load(log_dir, cycle_id):
            if cycle_id == "cycle-2026-08-21":
                return _payload(
                    cycle_id=cycle_id,
                    branch_results={"ok": _branch("ok", "SUCCESS")},
                )
            return _payload(
                cycle_id=cycle_id,
                branch_results={"bad": _branch("bad", "FAILED")},
            )

        monkeypatch.setattr(
            "bsa_web.projection.list_cycles",
            lambda log_dir: [
                _record("cycle-2026-08-21", "REPORTED", "2026-08-21T22:00:00"),
                _record("cycle-2026-08-20", "REPORTED", "2026-08-20T22:00:00"),
            ],
        )
        monkeypatch.setattr("bsa_web.projection.load_cycle", fake_load)
        r = client.get("/history", params={"kind": "failed"})
        assert r.status_code == 200
        assert "cycle-2026-08-20" in r.text
        assert "cycle-2026-08-21" not in r.text

    def test_history_unauthenticated_redirects_to_login(self, tmp_path):
        client = _client(_make_app(tmp_path))
        r = client.get("/history")
        assert r.status_code == 302
        assert r.headers["location"].endswith("/login")


class TestCycleDetail:
    def test_cycle_overview_three_blocks(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        payload = _payload(
            detected_commits=[_commit_info("a1")],
            decisions={"a1": {"t": _conclusion("ManualReview")}},
            branch_results={"t": _branch("t", "SUCCESS", commits=[_commit_result("a1")])},
            action_required=[
                {"sha": "a1", "branch": "t", "kind": "ManualReview", "evidence": ["ev"]}
            ],
        )
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        r = client.get("/cycle/cycle-2026-08-20")
        assert r.status_code == 200
        assert "Action Required" in r.text
        assert "已成功同步" in r.text
        assert "检测信息" in r.text
        assert "a1" in r.text
        assert "t" in r.text

    def test_cycle_detail_running_shows_friendly_notice(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        monkeypatch.setattr(
            "bsa_web.projection.load_cycle", lambda log_dir, cid: {"status": "running"}
        )
        r = client.get("/cycle/cycle-2026-08-24")
        assert r.status_code == 200
        assert "进行中" in r.text

    def test_cycle_detail_missing_cycle_404(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: None)
        r = client.get("/cycle/cycle-2099-01-01")
        assert r.status_code == 404

    def test_cycle_detail_unauthenticated_redirects_to_login(self, tmp_path):
        client = _client(_make_app(tmp_path))
        r = client.get("/cycle/cycle-2026-08-20")
        assert r.status_code == 302
        assert r.headers["location"].endswith("/login")


class TestTargetDetail:
    def test_target_detail_renders_build_log_preview_and_download_links(
        self, tmp_path, monkeypatch
    ):
        client = _client(_make_app(tmp_path))
        _login(client)
        branch = _branch(
            "t",
            "PARTIAL",
            commits=[
                _commit_result(
                    "a1",
                    cherry_pick="CONFLICT",
                    build={"RTL9617C": _build_outcome(log_path="/logs/x.log")},
                )
            ],
            patch_path="/tmp/b.patch",
        )
        payload = _payload(branch_results={"t": branch})
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        monkeypatch.setattr(
            "bsa_web.views.detail._read_build_log",
            lambda log_dir, log_path: ("[LOG] compile error", True),
        )
        r = client.get("/cycle/cycle-2026-08-20/target/t")
        assert r.status_code == 200
        assert "[LOG] compile error" in r.text
        assert "下载" in r.text
        assert "/cycle/cycle-2026-08-20/target/t/patch" in r.text

    def test_target_detail_missing_target_404(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        payload = _payload(branch_results={})
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        r = client.get("/cycle/cycle-2026-08-20/target/nope")
        assert r.status_code == 404

    def test_target_patch_download_serves_file(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        patch_file = tmp_path / "b.patch"
        patch_file.write_text("patch-content", encoding="utf-8")
        payload = _payload(
            branch_results={"t": _branch("t", "SUCCESS", patch_path=str(patch_file))}
        )
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        r = client.get("/cycle/cycle-2026-08-20/target/t/patch")
        assert r.status_code == 200
        assert r.text == "patch-content"

    def test_target_patch_download_rejects_path_outside_log_dir(
        self, tmp_path, monkeypatch
    ):
        client = _client(_make_app(tmp_path))
        _login(client)
        outside = tmp_path.parent / "outside.patch"
        outside.write_text("secret", encoding="utf-8")
        payload = _payload(
            branch_results={"t": _branch("t", "SUCCESS", patch_path=str(outside))}
        )
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        r = client.get("/cycle/cycle-2026-08-20/target/t/patch")
        assert r.status_code == 404
        assert "secret" not in r.text

    def test_build_log_download_serves_file(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        log_file = tmp_path / "build.log"
        log_file.write_text("log-content", encoding="utf-8")
        branch = _branch(
            "t",
            "SUCCESS",
            commits=[
                _commit_result(
                    "a1", build={"RTL9617C": _build_outcome(log_path=str(log_file))}
                )
            ],
        )
        payload = _payload(branch_results={"t": branch})
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        r = client.get(
            "/cycle/cycle-2026-08-20/target/t/commit/a1/build/RTL9617C/log"
        )
        assert r.status_code == 200
        assert r.text == "log-content"

    def test_build_log_download_rejects_path_outside_log_dir(
        self, tmp_path, monkeypatch
    ):
        client = _client(_make_app(tmp_path))
        _login(client)
        outside = tmp_path.parent / "build.log"
        outside.write_text("secret", encoding="utf-8")
        branch = _branch(
            "t",
            "SUCCESS",
            commits=[
                _commit_result(
                    "a1", build={"RTL9617C": _build_outcome(log_path=str(outside))}
                )
            ],
        )
        payload = _payload(branch_results={"t": branch})
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        r = client.get(
            "/cycle/cycle-2026-08-20/target/t/commit/a1/build/RTL9617C/log"
        )
        assert r.status_code == 404
        assert "secret" not in r.text


class TestCommitDetail:
    def test_commit_detail_renders_patch_and_conclusion(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        patch = "diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new\n"
        payload = _payload(
            detected_commits=[_commit_info("a1", patch_text=patch)],
            decisions={"a1": {"t": _conclusion("NeedSync")}},
        )
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        r = client.get("/cycle/cycle-2026-08-20/commit/a1")
        assert r.status_code == 200
        assert "+new" in r.text
        assert "NeedSync" in r.text
        assert "下载" in r.text

    def test_commit_detail_missing_commit_404(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        payload = _payload(detected_commits=[])
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        r = client.get("/cycle/cycle-2026-08-20/commit/unknown")
        assert r.status_code == 404

    def test_commit_patch_download_serves_patch(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        payload = _payload(detected_commits=[_commit_info("a1", "full-patch-content")])
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        r = client.get("/cycle/cycle-2026-08-20/commit/a1/patch")
        assert r.status_code == 200
        assert r.text == "full-patch-content"


class TestBuildLogHelper:
    def test_read_build_log_truncates_long_file(self, tmp_path):
        from bsa_web.views.detail import _read_build_log

        log = tmp_path / "build.log"
        log.write_text("\n".join(f"line {i}" for i in range(600)), encoding="utf-8")
        text, truncated = _read_build_log(str(tmp_path), str(log), max_lines=500)
        assert truncated is True
        assert text.splitlines()[0] == "line 0"
        assert text.splitlines()[-1] == "line 499"

    def test_read_build_log_none_when_missing(self, tmp_path):
        from bsa_web.views.detail import _read_build_log

        assert _read_build_log(str(tmp_path), str(tmp_path / "missing.log")) is None
        assert _read_build_log(str(tmp_path), None) is None

    def test_read_build_log_rejects_path_outside_log_dir(self, tmp_path):
        from bsa_web.views.detail import _read_build_log

        outside = tmp_path.parent / "secret.log"
        outside.write_text("secret", encoding="utf-8")
        assert _read_build_log(str(tmp_path), str(outside)) is None
