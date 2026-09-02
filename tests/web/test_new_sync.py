"""B 区引导式新建同步：候选 commit 端点、shas 直同步、三步表单渲染与权限。"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from bsa_web.api import operations
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
        env_file=None,
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


_COMMITS = [
    {
        "sha": "abc123",
        "message": "fix: bug",
        "committed_at": "2026-08-25T09:00:00+08:00",
    },
    {
        "sha": "def456",
        "message": "feat: x",
        "committed_at": "2026-08-25T08:00:00+08:00",
    },
]


class TestCommitsEndpoint:
    def test_commits_returns_list(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        monkeypatch.setattr(
            operations, "_load_commits", lambda log_dir, src, limit: _COMMITS
        )
        r = client.get("/api/commits", params={"src": "main"})
        assert r.status_code == 200
        assert r.json() == _COMMITS

    def test_load_commits_failure_returns_empty(self, monkeypatch):
        class FakeProc:
            returncode = 1
            stdout = ""

        monkeypatch.setattr(operations.subprocess, "run", lambda *a, **k: FakeProc())
        assert operations._load_commits("/tmp/logs", "main", 50) == []

    def test_load_commits_passes_refresh_flag(self, monkeypatch):
        class FakeProc:
            returncode = 0
            stdout = "[]"

        seen = {}

        def fake_run(*args, **kwargs):
            seen["args"] = args[0]
            return FakeProc()

        monkeypatch.setattr(operations.subprocess, "run", fake_run)
        operations._load_commits("/tmp/logs", "main", 50)
        assert "--refresh" in seen["args"]
        assert "commits" in seen["args"]

    def test_commits_viewer_forbidden(self, tmp_path):
        users = {
            "alice": f"{hash_password('op')}:{OPERATOR}",
            "bob": f"{hash_password('view')}:{VIEWER}",
        }
        app = _make_app(tmp_path, users=users)
        client = _client(app)
        _login(client, "bob", "view")
        r = client.get("/api/commits", params={"src": "main"})
        assert r.status_code == 403

    def test_commits_unauthenticated_forbidden(self, tmp_path):
        app = _make_app(tmp_path)
        client = _client(app)
        r = client.get("/api/commits", params={"src": "main"})
        assert r.status_code == 403


class TestSyncShas:
    def test_sync_with_shas_passes_to_submit(self, tmp_path):
        app = _make_app(tmp_path)
        _install_runner(app, run_func=lambda cmd: (0, "", ""))
        client = _client(app)
        _login(client)
        calls = []
        app.state.enqueue_task = lambda *a, **k: (calls.append((a, k)) or 42)
        r = client.post(
            "/api/sync",
            json={
                "src": "main",
                "target": "feat/x",
                "shas": ["abc123", "def456"],
                "_csrf": _csrf(client),
            },
        )
        assert r.status_code == 201
        assert r.json()["task_id"] == 42
        (args, kwargs) = calls[0]
        assert args == (app.state.db, "sync", "alice", "feat/x")
        assert kwargs["shas"] == ["abc123", "def456"]
        assert kwargs["src"] == "main"


class TestGuidedForm:
    def test_workbench_renders_guided_sync_form(self, tmp_path, monkeypatch):
        branch_file = tmp_path / "branch.md"
        branch_file.write_text(
            "## 1 RCIOS代码库\n- 路径：rcios\n\n"
            "### 1.1 组网产品分支\n- main\n- feat/x\n",
            encoding="utf-8",
        )
        app = create_app(
            settings_override={
                "log_dir": str(tmp_path),
                "secret_key": "test-secret",
                "users": {"alice": f"{hash_password('op')}:{OPERATOR}"},
                "branch_file": str(branch_file),
            },
            env_file=None,
        )
        client = _client(app)
        _login(client)
        monkeypatch.setattr("bsa_web.projection.list_cycles", lambda log_dir: [])
        monkeypatch.setattr(
            "bsa_web.projection.latest_completed_cycle", lambda log_dir: None
        )
        r = client.get("/")
        assert r.status_code == 200
        assert '<select name="sync-src"' in r.text
        assert '<select name="sync-target"' in r.text
        assert 'id="sync-commits"' in r.text
        assert 'id="sync-submit"' in r.text
        assert "/api/commits" in r.text
        assert "shas" in r.text
