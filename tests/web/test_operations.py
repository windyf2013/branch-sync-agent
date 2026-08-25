"""操作 API 与表单接线测试：触发同步/重跑、任务状态轮询、并发拒绝、权限。"""

from __future__ import annotations

import re
import threading
import time

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


def _install_runner(app, run_func):
    runner = TaskRunner(app.state.db, str(app.state.settings.log_dir), run_func=run_func)
    runner.start()
    app.state.runner = runner
    return runner


def _wait_state(client, task_id: int, wanted: str, timeout: float = 5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/tasks/{task_id}")
        assert r.status_code == 200
        if r.json()["state"] == wanted:
            return True
        time.sleep(0.02)
    return False


class TestSyncApi:
    def test_operator_triggers_sync_201(self, tmp_path):
        app = _make_app(tmp_path)
        runner = _install_runner(app, run_func=lambda cmd: (0, "", ""))
        client = _client(app)
        _login(client)
        r = client.post(
            "/api/sync",
            json={"src": "main", "target": "feat/x", "_csrf": _csrf(client)},
        )
        assert r.status_code == 201
        body = r.json()
        assert "task_id" in body
        assert body["state"] == "queued"
        runner.join(timeout=5)
        assert runner.get(body["task_id"])["state"] == "succeeded"

    def test_sync_with_sha_list(self, tmp_path):
        app = _make_app(tmp_path)
        calls = []
        runner = _install_runner(
            app, run_func=lambda cmd: (calls.append(cmd) or (0, "", ""))
        )
        client = _client(app)
        _login(client)
        r = client.post(
            "/api/sync",
            json={"src": "main", "target": "feat/x", "sha": ["abc123"], "_csrf": _csrf(client)},
        )
        assert r.status_code == 201
        runner.join(timeout=5)
        assert "--sha" in calls[0]
        assert "abc123" in calls[0]

    def test_viewer_forbidden(self, tmp_path):
        users = {
            "alice": f"{hash_password('op')}:{OPERATOR}",
            "bob": f"{hash_password('view')}:{VIEWER}",
        }
        app = _make_app(tmp_path, users=users)
        _install_runner(app, run_func=lambda cmd: (0, "", ""))
        client = _client(app)
        _login(client, "bob", "view")
        r = client.post(
            "/api/sync",
            json={"src": "main", "target": "feat/x", "_csrf": _csrf(client)},
        )
        assert r.status_code == 403

    def test_operator_without_csrf_rejected(self, tmp_path):
        app = _make_app(tmp_path)
        _install_runner(app, run_func=lambda cmd: (0, "", ""))
        client = _client(app)
        _login(client)
        r = client.post("/api/sync", json={"src": "main", "target": "feat/x"})
        assert r.status_code == 403

    def test_missing_fields_422(self, tmp_path):
        app = _make_app(tmp_path)
        _install_runner(app, run_func=lambda cmd: (0, "", ""))
        client = _client(app)
        _login(client)
        r = client.post("/api/sync", json={"_csrf": _csrf(client)})
        assert r.status_code == 422


class TestRerunApi:
    def test_rerun_returns_task_id(self, tmp_path):
        app = _make_app(tmp_path)
        calls = []
        runner = _install_runner(
            app, run_func=lambda cmd: (calls.append(cmd) or (0, "", ""))
        )
        client = _client(app)
        _login(client)
        r = client.post(
            "/api/rerun",
            json={"target": "feat/x", "fresh": True, "_csrf": _csrf(client)},
        )
        assert r.status_code == 201
        assert "task_id" in r.json()
        runner.join(timeout=5)
        assert calls[0][-1] == "--fresh"


class TestTaskStatus:
    def test_status_polling_transitions(self, tmp_path):
        app = _make_app(tmp_path)
        release = threading.Event()

        def fake_run(cmd):
            release.wait(5)
            return 0, "", ""

        _install_runner(app, run_func=fake_run)
        client = _client(app)
        _login(client)
        r = client.post(
            "/api/sync",
            json={"src": "main", "target": "feat/x", "_csrf": _csrf(client)},
        )
        task_id = r.json()["task_id"]
        assert _wait_state(client, task_id, "running")
        release.set()
        assert _wait_state(client, task_id, "succeeded")

    def test_status_requires_login(self, tmp_path):
        app = _make_app(tmp_path)
        client = _client(app)
        r = client.get("/api/tasks/1")
        assert r.status_code == 302
        assert r.headers["location"].endswith("/login")

    def test_status_viewer_allowed(self, tmp_path):
        users = {"bob": f"{hash_password('view')}:{VIEWER}"}
        app = _make_app(tmp_path, users=users)
        runner = _install_runner(app, run_func=lambda cmd: (0, "", ""))
        client = _client(app)
        _login(client, "bob", "view")
        task_id = runner.submit("sync", "alice", "feat/x", src="main")
        runner.join(timeout=5)
        r = client.get(f"/api/tasks/{task_id}")
        assert r.status_code == 200
        assert r.json()["state"] == "succeeded"

    def test_status_missing_404(self, tmp_path):
        app = _make_app(tmp_path)
        _install_runner(app, run_func=lambda cmd: (0, "", ""))
        client = _client(app)
        _login(client)
        r = client.get("/api/tasks/99999")
        assert r.status_code == 404


class TestConcurrency:
    def test_same_target_api_409(self, tmp_path):
        app = _make_app(tmp_path)
        release = threading.Event()

        def fake_run(cmd):
            release.wait(5)
            return 0, "", ""

        runner = _install_runner(app, run_func=fake_run)
        client = _client(app)
        _login(client)
        csrf = _csrf(client)
        first = client.post(
            "/api/sync", json={"src": "main", "target": "feat/x", "_csrf": csrf}
        )
        assert first.status_code == 201
        second = client.post(
            "/api/sync", json={"src": "main", "target": "feat/x", "_csrf": csrf}
        )
        assert second.status_code == 409
        release.set()
        runner.join(timeout=5)


class TestFormWiring:
    def test_form_sync_redirects_to_task_page(self, tmp_path):
        app = _make_app(tmp_path)
        calls = []
        runner = _install_runner(
            app, run_func=lambda cmd: (calls.append(cmd) or (0, "", ""))
        )
        client = _client(app)
        _login(client)
        r = client.post(
            "/sync", data={"src": "main", "target": "feat/x", "_csrf": _csrf(client)}
        )
        assert r.status_code == 303
        assert re.match(r"/tasks/\d+", r.headers["location"])
        runner.join(timeout=5)
        assert calls[0][3] == "sync"

    def test_form_rerun_with_fresh_checkbox(self, tmp_path):
        app = _make_app(tmp_path)
        calls = []
        runner = _install_runner(
            app, run_func=lambda cmd: (calls.append(cmd) or (0, "", ""))
        )
        client = _client(app)
        _login(client)
        r = client.post(
            "/rerun",
            data={"target": "feat/x", "fresh": "on", "_csrf": _csrf(client)},
        )
        assert r.status_code == 303
        assert re.match(r"/tasks/\d+", r.headers["location"])
        runner.join(timeout=5)
        assert calls[0][-1] == "--fresh"

    def test_form_sync_busy_redirects_with_error(self, tmp_path):
        app = _make_app(tmp_path)
        release = threading.Event()

        def fake_run(cmd):
            release.wait(5)
            return 0, "", ""

        runner = _install_runner(app, run_func=fake_run)
        client = _client(app)
        _login(client)
        csrf = _csrf(client)
        first = client.post(
            "/sync", data={"src": "main", "target": "feat/x", "_csrf": csrf}
        )
        assert first.status_code == 303
        second = client.post(
            "/sync", data={"src": "main", "target": "feat/x", "_csrf": csrf}
        )
        assert second.status_code == 303
        assert second.headers["location"] == "/?error=busy"
        release.set()
        runner.join(timeout=5)

    def test_form_viewer_forbidden(self, tmp_path):
        users = {
            "alice": f"{hash_password('op')}:{OPERATOR}",
            "bob": f"{hash_password('view')}:{VIEWER}",
        }
        app = _make_app(tmp_path, users=users)
        _install_runner(app, run_func=lambda cmd: (0, "", ""))
        client = _client(app)
        _login(client, "bob", "view")
        r = client.post(
            "/sync", data={"src": "main", "target": "feat/x", "_csrf": _csrf(client)}
        )
        assert r.status_code == 403

    def test_form_without_csrf_rejected(self, tmp_path):
        app = _make_app(tmp_path)
        _install_runner(app, run_func=lambda cmd: (0, "", ""))
        client = _client(app)
        _login(client)
        r = client.post("/sync", data={"src": "main", "target": "feat/x"})
        assert r.status_code == 403

    def test_task_status_page_renders(self, tmp_path):
        app = _make_app(tmp_path)
        runner = _install_runner(app, run_func=lambda cmd: (0, "", ""))
        client = _client(app)
        _login(client)
        task_id = runner.submit("sync", "alice", "feat/x", src="main")
        runner.join(timeout=5)
        r = client.get(f"/tasks/{task_id}")
        assert r.status_code == 200
        assert "feat/x" in r.text
        assert "成功" in r.text

    def test_task_status_page_404(self, tmp_path):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        r = client.get("/tasks/99999")
        assert r.status_code == 404

    def test_workbench_shows_busy_error(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        _install_runner(app, run_func=lambda cmd: (0, "", ""))
        client = _client(app)
        _login(client)
        r = client.get("/?error=busy")
        assert r.status_code == 200
        assert "已有任务" in r.text
