"""WebSSH（ttyd）受限终端测试：open/close 生命周期、授权、审计、WS 反代。

本机无 ttyd → spawn 全部 monkeypatch；真实冒烟在部署机验证。
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from bsa_web.app import create_app
from bsa_web.auth import hash_password
from bsa_web.rbac import OPERATOR, VIEWER

_CYCLE = "cycle-2026-08-21"


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


def _post(client, path, body):
    return client.post(path, json={**body, "_csrf": _csrf(client)})


def _audit_rows(app):
    return [
        dict(r)
        for r in app.state.db.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    ]


def _branch(target, status="FAILED", worktree=None):
    return {
        "target_branch": target,
        "worktree_path": worktree,
        "status": status,
        "commits": [],
        "patch_path": None,
        "stop_reason": None,
    }


def _payload(branch_results=None):
    return {
        "cycle_id": _CYCLE,
        "status": "REPORTED",
        "scan_window": [],
        "detected_commits": [],
        "decisions": {},
        "branch_results": branch_results or {},
        "action_required": [],
    }


def _mount_cycle(monkeypatch, payload):
    monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)


def _fake_proc():
    return SimpleNamespace(
        pid=999999,
        poll=lambda: None,
        terminate=lambda: None,
        kill=lambda: None,
        wait=lambda: None,
        stdout=None,
    )


def _audit_for(app, action):
    return [row for row in _audit_rows(app) if row["action"] == action]


def _open(client, worktree):
    """登录态下开一个会话：patch spawn 为假实现，避免真实 ttyd。"""
    import bsa_web.views.ssh as ssh_views

    orig = ssh_views.spawn_ttyd
    ssh_views.spawn_ttyd = lambda wt: (_PORT, _fake_proc())
    try:
        r = _post(client, "/api/ssh/open", {"cycle_id": _CYCLE, "target": "feat/bad"})
        assert r.status_code == 200, r.text
        return r.json()["token"]
    finally:
        ssh_views.spawn_ttyd = orig


class TestSshOpen:
    def test_spawn_command_is_restricted(self, tmp_path):
        from bsa_web.ssh import _build_cmd

        cmd = _build_cmd("/srv/wt/feat-bad")
        assert cmd[0] == "ttyd"
        assert "-o" in cmd
        assert "-W" in cmd
        assert cmd[cmd.index("-i") + 1] == "127.0.0.1"
        assert cmd[cmd.index("-p") + 1] == "0"
        shell = cmd[cmd.index("bash") :]
        assert shell[1] == "-c"
        assert "cd /srv/wt/feat-bad" in shell[2]
        assert "timeout" in cmd and "1800" in cmd

    def test_open_spawns_ttyd_and_records_session(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _mount_cycle(
            monkeypatch,
            _payload({"feat/bad": _branch("feat/bad", "FAILED", str(worktree))}),
        )
        spawn_calls = []
        procs = [_fake_proc()]

        def fake_spawn(wt):
            spawn_calls.append(wt)
            return _PORT, procs[0]

        monkeypatch.setattr("bsa_web.views.ssh.spawn_ttyd", fake_spawn)
        r = _post(client, "/api/ssh/open", {"cycle_id": _CYCLE, "target": "feat/bad"})
        assert r.status_code == 200
        token = r.json()["token"]
        assert len(token) >= 24
        assert spawn_calls == [str(worktree)]
        row = app.state.db.execute(
            "SELECT * FROM ssh_sessions WHERE token=?", (token,)
        ).fetchone()
        assert row is not None
        assert row["cycle_id"] == _CYCLE
        assert row["target"] == "feat/bad"
        assert row["worktree"] == str(worktree)
        assert row["port"] == _PORT
        assert row["user"] == "alice"

    def test_open_rejects_success_task(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _mount_cycle(
            monkeypatch,
            _payload({"feat/ok": _branch("feat/ok", "SUCCESS", str(worktree))}),
        )
        r = _post(client, "/api/ssh/open", {"cycle_id": _CYCLE, "target": "feat/ok"})
        assert r.status_code == 400

    def test_open_rejects_missing_worktree(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        _mount_cycle(monkeypatch, _payload({"feat/bad": _branch("feat/bad")}))
        r = _post(client, "/api/ssh/open", {"cycle_id": _CYCLE, "target": "feat/bad"})
        assert r.status_code == 400

    def test_open_rejects_missing_branch(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        _mount_cycle(monkeypatch, _payload({}))
        r = _post(client, "/api/ssh/open", {"cycle_id": _CYCLE, "target": "nope"})
        assert r.status_code == 404

    def test_open_viewer_forbidden(self, tmp_path, monkeypatch):
        users = {
            "alice": f"{hash_password('op')}:{OPERATOR}",
            "bob": f"{hash_password('view')}:{VIEWER}",
        }
        app = _make_app(tmp_path, users=users)
        client = _client(app)
        _login(client, "bob", "view")
        _mount_cycle(monkeypatch, _payload({"x": _branch("x", "FAILED", "/tmp")}))
        r = _post(client, "/api/ssh/open", {"cycle_id": _CYCLE, "target": "x"})
        assert r.status_code == 403

    def test_open_unauthenticated_redirects(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        r = client.post(
            "/api/ssh/open",
            json={"cycle_id": _CYCLE, "target": "x", "_csrf": "x"},
        )
        assert r.status_code == 302
        assert r.headers["location"].endswith("/login")

    def test_spawn_times_out_and_reaps_unresponsive_process(
        self, tmp_path, monkeypatch
    ):
        import subprocess as real_subprocess
        import time

        from bsa_web.ssh import SshSpawnError, spawn_ttyd

        worktree = tmp_path / "wt"
        worktree.mkdir()
        proc = real_subprocess.Popen(
            ["sleep", "1000"],
            stdout=real_subprocess.PIPE,
            stderr=real_subprocess.STDOUT,
            start_new_session=True,
        )
        monkeypatch.setattr("bsa_web.ssh.SSH_SPAWN_PORT_TIMEOUT_SEC", 0.5)
        monkeypatch.setattr("bsa_web.ssh.subprocess.Popen", lambda *a, **k: proc)
        start = time.monotonic()
        with pytest.raises(SshSpawnError):
            spawn_ttyd(str(worktree))
        assert time.monotonic() - start < 5  # 未永久挂起，按约 0.5s 超时抛错
        assert proc.poll() is not None  # 超时后整进程组已回收


class TestSshSession:
    def test_ssh_page_renders_with_ws_path(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _mount_cycle(
            monkeypatch,
            _payload({"feat/bad": _branch("feat/bad", "FAILED", str(worktree))}),
        )
        token = _open(client, worktree)
        r = client.get(f"/ssh/{token}")
        assert r.status_code == 200
        assert f"/ssh/ws/{token}" in r.text

    def test_ssh_page_invalid_token_404(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        r = client.get("/ssh/nope")
        assert r.status_code == 404

    def test_ssh_page_unauthenticated_redirects(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        r = client.get("/ssh/some-token")
        assert r.status_code == 302
        assert r.headers["location"].endswith("/login")

    def test_ssh_page_expired_token_404(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _mount_cycle(
            monkeypatch,
            _payload({"feat/bad": _branch("feat/bad", "FAILED", str(worktree))}),
        )
        token = _open(client, worktree)
        app.state.db.execute(
            "UPDATE ssh_sessions SET created_at='2000-01-01T00:00:00+00:00' "
            "WHERE token=?",
            (token,),
        )
        app.state.db.commit()
        r = client.get(f"/ssh/{token}")
        assert r.status_code == 404


class TestSshClose:
    def test_close_audits_and_clears(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _mount_cycle(
            monkeypatch,
            _payload({"feat/bad": _branch("feat/bad", "FAILED", str(worktree))}),
        )
        token = _open(client, worktree)
        r = _post(client, "/api/ssh/close", {"token": token})
        assert r.status_code == 200
        row = app.state.db.execute(
            "SELECT 1 FROM ssh_sessions WHERE token=?", (token,)
        ).fetchone()
        assert row is None
        audit = _audit_for(app, "ssh_close")
        assert len(audit) == 1
        assert audit[0]["user"] == "alice"
        assert audit[0]["cycle_id"] == _CYCLE
        assert audit[0]["target"] == "feat/bad"
        assert "duration" in audit[0]["detail_json"]

    def test_close_viewer_forbidden(self, tmp_path, monkeypatch):
        users = {
            "alice": f"{hash_password('op')}:{OPERATOR}",
            "bob": f"{hash_password('view')}:{VIEWER}",
        }
        app = _make_app(tmp_path, users=users)
        client = _client(app)
        _login(client, "bob", "view")
        r = _post(client, "/api/ssh/close", {"token": "x"})
        assert r.status_code == 403


class TestSshAuditAndLifecycle:
    def test_open_writes_audit(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _mount_cycle(
            monkeypatch,
            _payload({"feat/bad": _branch("feat/bad", "FAILED", str(worktree))}),
        )
        _open(client, worktree)
        audit = _audit_for(app, "ssh_open")
        assert len(audit) == 1
        assert audit[0]["user"] == "alice"
        assert audit[0]["cycle_id"] == _CYCLE
        assert audit[0]["target"] == "feat/bad"
        assert audit[0]["detail_json"].find(str(worktree)) != -1

    def test_spawn_failure_500_no_row(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _mount_cycle(
            monkeypatch,
            _payload({"feat/bad": _branch("feat/bad", "FAILED", str(worktree))}),
        )

        def boom(wt):
            from bsa_web.ssh import SshSpawnError

            raise SshSpawnError("ttyd 启动失败")

        monkeypatch.setattr("bsa_web.views.ssh.spawn_ttyd", boom, raising=False)
        r = _post(client, "/api/ssh/open", {"cycle_id": _CYCLE, "target": "feat/bad"})
        assert r.status_code == 500
        rows = app.state.db.execute("SELECT * FROM ssh_sessions").fetchall()
        assert len(rows) == 0


class TestSshEntryLink:
    def test_task_entry_opens_and_redirects(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _mount_cycle(
            monkeypatch,
            _payload({"feat/bad": _branch("feat/bad", "FAILED", str(worktree))}),
        )
        monkeypatch.setattr(
            "bsa_web.views.ssh.spawn_ttyd",
            lambda wt: (_PORT, _fake_proc()),
        )
        r = client.get(f"/ssh/task/{_CYCLE}/feat/bad")
        assert r.status_code == 302
        assert r.headers["location"].startswith("/ssh/")
        token = r.headers["location"].split("/")[-1]
        assert client.get(f"/ssh/{token}").status_code == 200

    def test_task_entry_rejects_success(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _mount_cycle(
            monkeypatch,
            _payload({"feat/ok": _branch("feat/ok", "SUCCESS", str(worktree))}),
        )
        r = client.get(f"/ssh/task/{_CYCLE}/feat/ok")
        assert r.status_code == 400

    def test_task_entry_viewer_forbidden(self, tmp_path, monkeypatch):
        users = {
            "alice": f"{hash_password('op')}:{OPERATOR}",
            "bob": f"{hash_password('view')}:{VIEWER}",
        }
        app = _make_app(tmp_path, users=users)
        client = _client(app)
        _login(client, "bob", "view")
        _mount_cycle(monkeypatch, _payload({"x": _branch("x", "FAILED", "/tmp")}))
        r = client.get(f"/ssh/task/{_CYCLE}/x")
        assert r.status_code == 403


class TestSshWs:
    def test_ws_proxies_bidirectionally(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _mount_cycle(
            monkeypatch,
            _payload({"feat/bad": _branch("feat/bad", "FAILED", str(worktree))}),
        )
        token = _open(client, worktree)
        state = {"uris": [], "client_msgs": [], "server_msgs": ["s1", "s2"]}

        async def fake_relay(websocket, uri, session):
            state["uris"].append(uri)
            assert session["port"] == _PORT
            await websocket.send_text(state["server_msgs"].pop(0))
            data = await websocket.receive_text()
            state["client_msgs"].append(data)
            await websocket.send_text(state["server_msgs"].pop(0))

        monkeypatch.setattr("bsa_web.views.ssh._ws_relay", fake_relay)
        with client.websocket_connect(f"/ssh/ws/{token}") as ws:
            assert ws.receive_text() == "s1"
            ws.send_text("client->server")
            assert ws.receive_text() == "s2"
        assert state["uris"] == [f"ws://127.0.0.1:{_PORT}/"]
        assert state["client_msgs"] == ["client->server"]

    def test_ws_invalid_token_rejected(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ssh/ws/nope"):
                pass
        assert exc.value.code == 4401

    def test_ws_closed_when_server_side_relay_ends(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _mount_cycle(
            monkeypatch,
            _payload({"feat/bad": _branch("feat/bad", "FAILED", str(worktree))}),
        )
        token = _open(client, worktree)
        from starlette.websockets import WebSocketDisconnect

        async def silent_relay(websocket, uri, session):
            return

        monkeypatch.setattr("bsa_web.views.ssh._ws_relay", silent_relay)
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"/ssh/ws/{token}") as ws:
                ws.receive_text()
        assert exc.value.code == 1000


class TestParsePortRealTtyd:
    """ttyd 1.7 真实输出回归：`Listening on port: 44251`（带冒号）。"""

    def test_real_ttyd_line(self):
        from bsa_web.ssh import _parse_port

        assert _parse_port("Listening on port: 44251") == 44251

    def test_legacy_no_colon(self):
        from bsa_web.ssh import _parse_port

        assert _parse_port("Listening on port 44251") == 44251

    def test_unrelated_line(self):
        from bsa_web.ssh import _parse_port

        assert _parse_port("ttyd 1.7.7 (libwebsockets)") is None


# ---- helpers ----

_PORT = 43210
