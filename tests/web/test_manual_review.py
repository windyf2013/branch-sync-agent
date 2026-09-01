"""人工项处理 API 测试：改判定（override）/ 确认继续（confirm）。"""

from __future__ import annotations

import re
import sys
import threading
from subprocess import CompletedProcess

from fastapi.testclient import TestClient

from bsa_web.app import create_app
from bsa_web.auth import hash_password
from bsa_web.rbac import OPERATOR, VIEWER
from bsa_web.runner import TaskRunner


def _make_app(tmp_path, users=None):
    if users is None:
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


def _csrf(client) -> str:
    return _extract_csrf(client.get("/").text)


def _audit_rows(app):
    return [
        dict(r)
        for r in app.state.db.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    ]


def _install_runner(app, run_func):
    from bsa.commands.task_reporter import register_finish

    def wrapped(cmd, env):
        try:
            result = run_func(cmd)
        except TypeError:
            result = run_func(cmd, env)
        try:
            register_finish(
                env["LOG_DIR"], int(env["BSA_TASK_ID"]),
                state="succeeded", cycle_id="cycle-test",
            )
        except Exception:
            pass
        return result

    from pathlib import Path

    from bsa_web.db import init_db

    runner_db = init_db(Path(app.state.settings.log_dir) / "platform.sqlite3")
    runner = TaskRunner(runner_db, str(app.state.settings.log_dir), run_func=wrapped)
    runner.start()
    app.state.enqueue_task = lambda db, kind, user, target, **kw: runner.submit(
        kind, user, target, **kw
    )
    app.state.get_task = lambda db, task_id: runner.get(task_id)
    return runner


class TestOverrideApi:
    def test_override_runs_cli_and_audits(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        calls = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            return CompletedProcess(cmd, 0, stdout="{}", stderr="")

        monkeypatch.setattr("bsa_web.api.manual_review._run_cli", fake_run)
        r = client.post(
            "/api/override",
            json={
                "sha": "abc123",
                "is_bug_fix": False,
                "risk": "high",
                "_csrf": _csrf(client),
            },
        )
        assert r.status_code == 200
        assert "生效" in r.json()["message"]
        assert calls[0][:4] == [sys.executable, "-m", "bsa.cli", "override"]
        assert "abc123" in calls[0]
        assert "--is-bug-fix" in calls[0]
        assert "false" in calls[0]
        assert "--risk" in calls[0]
        assert "high" in calls[0]

        audit = [row for row in _audit_rows(app) if row["action"] == "override"]
        assert len(audit) == 1
        assert audit[0]["user"] == "alice"
        assert audit[0]["sha"] == "abc123"
        assert '"risk": "high"' in audit[0]["detail_json"]

    def test_override_risk_only(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        calls = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            return CompletedProcess(cmd, 0, stdout="{}", stderr="")

        monkeypatch.setattr("bsa_web.api.manual_review._run_cli", fake_run)
        r = client.post(
            "/api/override",
            json={"sha": "abc123", "risk": "low", "_csrf": _csrf(client)},
        )
        assert r.status_code == 200
        assert "--risk" in calls[0]
        assert "--is-bug-fix" not in calls[0]

    def test_override_cli_failure_500(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)

        def fake_run(cmd, *args, **kwargs):
            return CompletedProcess(cmd, 1, stdout="", stderr="覆盖失败")

        monkeypatch.setattr("bsa_web.api.manual_review._run_cli", fake_run)
        r = client.post(
            "/api/override",
            json={"sha": "abc123", "risk": "low", "_csrf": _csrf(client)},
        )
        assert r.status_code == 500

    def test_override_viewer_forbidden(self, tmp_path, monkeypatch):
        users = {
            "alice": f"{hash_password('op')}:{OPERATOR}",
            "bob": f"{hash_password('view')}:{VIEWER}",
        }
        app = _make_app(tmp_path, users=users)
        client = _client(app)
        _login(client, "bob", "view")
        r = client.post(
            "/api/override",
            json={"sha": "abc123", "risk": "low", "_csrf": _csrf(client)},
        )
        assert r.status_code == 403

    def test_override_without_csrf_rejected(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        r = client.post("/api/override", json={"sha": "abc123", "risk": "low"})
        assert r.status_code == 403

    def test_override_missing_sha_400(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        r = client.post(
            "/api/override",
            json={"risk": "low", "_csrf": _csrf(client)},
        )
        assert r.status_code == 400

    def test_override_invalid_risk_400(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        r = client.post(
            "/api/override",
            json={"sha": "abc123", "risk": "urgent", "_csrf": _csrf(client)},
        )
        assert r.status_code == 400

    def test_override_no_fields_400(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        r = client.post(
            "/api/override",
            json={"sha": "abc123", "_csrf": _csrf(client)},
        )
        assert r.status_code == 400

    def test_override_clear_runs_cli_and_audits(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        calls = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            return CompletedProcess(cmd, 0, stdout="{}", stderr="")

        monkeypatch.setattr("bsa_web.api.manual_review._run_cli", fake_run)
        r = client.post(
            "/api/override",
            json={"sha": "abc123", "clear": True, "_csrf": _csrf(client)},
        )
        assert r.status_code == 200
        assert "--clear" in calls[0]
        assert "--is-bug-fix" not in calls[0]
        assert "--risk" not in calls[0]
        audit = [row for row in _audit_rows(app) if row["action"] == "override"]
        assert len(audit) == 1
        assert audit[0]["sha"] == "abc123"


class TestConfirmApi:
    def test_confirm_triggers_direct_sync_and_audits(self, tmp_path):
        app = _make_app(tmp_path)
        calls = []
        runner = _install_runner(app, run_func=lambda cmd: (calls.append(cmd) or (0, "", "")))
        client = _client(app)
        _login(client)
        r = client.post(
            "/api/confirm",
            json={"target": "feat/x", "sha": "abc123", "_csrf": _csrf(client)},
        )
        assert r.status_code == 201
        assert "task_id" in r.json()
        assert r.json()["state"] == "queued"
        runner.join(timeout=5)
        assert calls[0] == [
            sys.executable,
            "-m",
            "bsa.cli",
            "sync",
            "",
            "feat/x",
            "--sha",
            "abc123",
        ]
        audit = [row for row in _audit_rows(app) if row["action"] == "confirm_continue"]
        assert len(audit) == 1
        assert audit[0]["user"] == "alice"
        assert audit[0]["target"] == "feat/x"
        assert audit[0]["sha"] == "abc123"

    def test_confirm_viewer_forbidden(self, tmp_path):
        users = {
            "alice": f"{hash_password('op')}:{OPERATOR}",
            "bob": f"{hash_password('view')}:{VIEWER}",
        }
        app = _make_app(tmp_path, users=users)
        _install_runner(app, run_func=lambda cmd: (0, "", ""))
        client = _client(app)
        _login(client, "bob", "view")
        r = client.post(
            "/api/confirm",
            json={"target": "feat/x", "sha": "abc123", "_csrf": _csrf(client)},
        )
        assert r.status_code == 403

    def test_confirm_missing_fields_400(self, tmp_path):
        app = _make_app(tmp_path)
        _install_runner(app, run_func=lambda cmd: (0, "", ""))
        client = _client(app)
        _login(client)
        csrf = _csrf(client)
        assert (
            client.post("/api/confirm", json={"target": "feat/x", "_csrf": csrf}).status_code
            == 400
        )
        assert (
            client.post("/api/confirm", json={"sha": "abc123", "_csrf": csrf}).status_code
            == 400
        )

    def test_confirm_same_target_busy_409(self, tmp_path):
        app = _make_app(tmp_path)
        release = threading.Event()

        def fake_run(cmd):
            release.wait(5)
            return 0, "", ""

        runner = _install_runner(app, run_func=fake_run)
        client = _client(app)
        _login(client)
        csrf = _csrf(client)
        runner.submit("sync", "alice", "feat/x", src="main")
        r = client.post(
            "/api/confirm",
            json={"target": "feat/x", "sha": "abc123", "_csrf": csrf},
        )
        assert r.status_code == 409
        release.set()
        runner.join(timeout=5)
