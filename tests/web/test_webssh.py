"""WebSSH（ttyd）受限终端测试：open/close 生命周期、授权、审计、WS 反代。

本机无 ttyd → spawn 全部 monkeypatch；真实冒烟在部署机验证。
"""

from __future__ import annotations

import re
import shutil
import threading
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

    def test_port_parse_survives_high_fd(self, tmp_path, monkeypatch):
        """端口解析不因 fd >= 1024 崩溃（select.select 的 FD_SETSIZE 硬限制）。

        回归：全量测试/长跑进程 fd 撑爆后，ttyd 子进程 stdout fd 超过 1024，
        select.select 抛 "filedescriptor out of range in select()"，WebSSH open 失败。
        selectors 走 epoll/poll 无此上限。用真实高 fd 管道验证，而非伪造 fileno。
        """
        import os as real_os

        from bsa_web.ssh import spawn_ttyd

        worktree = tmp_path / "wt"
        worktree.mkdir()

        # 造一个真实的高 fd 管道（读端 dup 到 2048，> FD_SETSIZE 1024）
        r, w = real_os.pipe()
        high_fd = 2048
        real_os.dup2(r, high_fd)
        real_os.close(r)
        try:
            real_os.write(w, b"Listening on port: 45200\n")
            real_os.close(w)

            class _HighFdProc:
                def __init__(self):
                    self.stdout = self
                    self.pid = 999999

                def fileno(self):
                    return high_fd

                def poll(self):
                    return None

            proc = _HighFdProc()
            monkeypatch.setattr("bsa_web.ssh.subprocess.Popen", lambda *a, **k: proc)
            monkeypatch.setattr("bsa_web.ssh.SSH_SPAWN_PORT_TIMEOUT_SEC", 2.0)
            # 高 fd 是真实存在的（dup2 产出），os.read 直接读即可，无需 monkeypatch

            port, _ = spawn_ttyd(str(worktree))
            assert port == 45200
        finally:
            real_os.close(high_fd)


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

    def test_ssh_page_implements_ttyd_client_protocol(self, tmp_path, monkeypatch):
        """终端页必须实现 ttyd 客户端协议，否则「连上但无任何反应」。

        历史 bug：onopen 不发握手 JSON → ttyd 永不 spawn shell。
        协议由 TestRealTtydProtocol 对真实 ttyd 实测确认，此处守住前端实现。
        仓库无 JS 测试框架，故以模板片段作为契约断言。
        """
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
        html = client.get(f"/ssh/{token}").text

        # 1) 二进制帧必须以 ArrayBuffer 收取，否则拿到 Blob 写不进终端
        assert "arraybuffer" in html.lower(), "缺少 binaryType=arraybuffer"
        # 2) 握手 JSON —— 本 bug 的根因，缺它 shell 永不派生
        assert "AuthToken" in html, "onopen 未发送 ttyd 握手 JSON"
        assert "columns" in html and "rows" in html, "握手缺 columns/rows"
        # 3) 输入需带 '0' INPUT 前缀（实测不带则完全不执行）
        assert "TextEncoder" in html, "输入未按二进制帧编码"
        # 4) 输出需按首字节分发并剥离（实测首帧为 '1' 标题帧，必须忽略）
        assert "TextDecoder" in html, "输出未解码"
        assert "subarray(1)" in html or "slice(1)" in html, "输出未剥离命令字节"

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
        assert state["uris"] == [f"ws://127.0.0.1:{_PORT}/ws"]
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

    def test_ws_disconnect_reaps_ttyd_process(self, tmp_path, monkeypatch):
        """WS 断开必须回收 ttyd 进程：客户端直接关浏览器/断网也 kill，不留孤儿。

        回归：ssh_ws 在 relay 结束后只 close 客户端 WS，从不回收 ttyd 进程——
        只有点「关闭终端」按钮走 POST /api/ssh/close 才回收，直接断开就泄漏。
        """
        import bsa_web.ssh as ssh_mod

        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _mount_cycle(
            monkeypatch,
            _payload({"feat/bad": _branch("feat/bad", "FAILED", str(worktree))}),
        )
        killed: list = []
        monkeypatch.setattr("bsa_web.views.ssh.spawn_ttyd", lambda wt: (_PORT, _fake_proc()))
        monkeypatch.setattr(ssh_mod, "_kill_process", lambda proc: killed.append(proc))
        r = _post(client, "/api/ssh/open", {"cycle_id": _CYCLE, "target": "feat/bad"})
        token = r.json()["token"]
        assert token in ssh_mod._PROCESSES

        async def silent_relay(websocket, uri, session):
            return

        monkeypatch.setattr("bsa_web.views.ssh._ws_relay", silent_relay)
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/ssh/ws/{token}") as ws:
                ws.receive_text()

        assert len(killed) == 1, "WS 断开后必须 kill ttyd 进程"
        assert token not in ssh_mod._PROCESSES
        rows = app.state.db.execute(
            "SELECT * FROM ssh_sessions WHERE token=?", (token,)
        ).fetchall()
        assert len(rows) == 0


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


class TestWsRelayContract:
    """_ws_relay 真实契约：ttyd 路径 /ws + tty 子协议 + 字节双向转发。

    真机冒烟发现 mock 掩盖的三类错误：路径连 `/`、无子协议、按文本帧转发。
    """

    def test_relay_connects_to_ws_path_tty_subprotocol_and_forwards_bytes(
        self, monkeypatch
    ):
        import asyncio

        from bsa_web.views import ssh as ssh_views

        captured: dict = {}

        class FakeConn:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def send(self, msg):
                captured["sent_to_ttyd"] = msg

            async def recv(self):
                if not captured.get("recv_done"):
                    captured["recv_done"] = True
                    return b"ttyd-output"
                raise ConnectionError("ttyd closed")  # 模拟断开，让 relay 结束

        class FakeWs:
            def __init__(self):
                self._msgs = iter([{"bytes": b"client-input"}])

            async def receive(self):
                try:
                    return next(self._msgs)
                except StopIteration:
                    raise ConnectionError("client closed") from None

            async def send_bytes(self, b):
                captured["sent_to_client"] = b

        def fake_connect(uri, subprotocols=None):
            captured["uri"] = uri
            captured["subprotocols"] = subprotocols
            return FakeConn()

        monkeypatch.setattr("bsa_web.views.ssh.websockets.connect", fake_connect)
        asyncio.run(ssh_views._ws_relay(FakeWs(), "ws://x:1234/ws", {"port": 1234}))
        # URI 由 ssh_ws 构造（test_ws_proxies_bidirectionally 断言 /ws 路径）
        assert captured["subprotocols"] == ["tty"]
        assert captured["sent_to_ttyd"] == b"client-input"
        assert captured["sent_to_client"] == b"ttyd-output"


@pytest.mark.skipif(shutil.which("ttyd") is None, reason="本机无 ttyd")
class TestRealTtydProtocol:
    """真机 ttyd 协议契约：证明「握手 JSON → shell 派生 → 输出回传」整条链路。

    ttyd 1.7 只在收到客户端首条 JSON 消息（命令字节 = JSON 的 `{`）时才
    spawn_process；不发握手就会「连上但永远无输出」——这正是 ssh.html 的
    历史 bug。本用例锁定 ssh.html 必须实现的那套字节协议：
    握手 `{"AuthToken":"","columns":N,"rows":M}`、输入 `'0'` 前缀、
    输出首字节 `'0'` 为 OUTPUT。
    """

    def _drain_until(self, ws, needle: bytes, timeout: float = 15.0) -> list[bytes]:
        """后台线程逐帧收取直到某帧含 needle；超时返回已收帧（断言报错可读）。

        返回帧列表而非拼接串：ttyd 的帧类型在**每帧首字节**，拼接后无法区分
        （实测首帧为 '1' SET_WINDOW_TITLE，OUTPUT 帧在其后）。
        """
        frames: list[bytes] = []

        def pump() -> None:
            try:
                while not any(needle in f for f in frames):
                    frames.append(ws.receive_bytes())
            except Exception:  # noqa: BLE001 — 连接关闭即结束，断言负责报错
                pass

        t = threading.Thread(target=pump, daemon=True)
        t.start()
        t.join(timeout)
        return list(frames)

    def test_handshake_spawns_shell_and_echoes_input(self, tmp_path, monkeypatch):
        import bsa_web.ssh as ssh_mod

        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "sentinel_file.txt").write_text("x", encoding="utf-8")
        _mount_cycle(
            monkeypatch,
            _payload({"feat/bad": _branch("feat/bad", "FAILED", str(worktree))}),
        )
        # 不 patch spawn_ttyd —— 起真实 ttyd 进程
        r = _post(client, "/api/ssh/open", {"cycle_id": _CYCLE, "target": "feat/bad"})
        assert r.status_code == 200, r.text
        token = r.json()["token"]
        try:
            with client.websocket_connect(f"/ssh/ws/{token}") as ws:
                # 1) 握手：没有这条，ttyd 永不 spawn shell（本 bug 根因）
                ws.send_text('{"AuthToken":"","columns":80,"rows":24}')
                # 2) 输入必须带 '0' (INPUT) 前缀
                ws.send_bytes(b"0echo __BSA_MARKER__\n")
                frames = self._drain_until(ws, b"__BSA_MARKER__")
            hit = [f for f in frames if b"__BSA_MARKER__" in f]
            assert hit, f"未收到 shell 回显，实收帧: {[f[:80] for f in frames]!r}"
            # 3) 承载回显的帧首字节必须是 '0' = OUTPUT，ssh.html 需剥离后再 write
            assert hit[0][0:1] == b"0", f"OUTPUT 帧首字节应为 '0'，实为 {hit[0][0:1]!r}"
            # 4) 记录实测：ttyd 先发 '1' SET_WINDOW_TITLE，客户端必须忽略非 '0' 帧，
            #    否则标题帧会被当作终端内容写进屏幕
            assert frames[0][0:1] in (b"0", b"1"), f"未知帧类型 {frames[0][0:1]!r}"
        finally:
            proc = ssh_mod._PROCESSES.pop(token, None)
            if proc is not None:
                ssh_mod._kill_process(proc)

    def test_input_without_command_prefix_is_not_executed(self, tmp_path, monkeypatch):
        """反证：不加 '0' 前缀的裸输入不会被当作 INPUT 执行（旧 ssh.html 的写法）。"""
        import bsa_web.ssh as ssh_mod

        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _mount_cycle(
            monkeypatch,
            _payload({"feat/bad": _branch("feat/bad", "FAILED", str(worktree))}),
        )
        r = _post(client, "/api/ssh/open", {"cycle_id": _CYCLE, "target": "feat/bad"})
        token = r.json()["token"]
        try:
            with client.websocket_connect(f"/ssh/ws/{token}") as ws:
                ws.send_text('{"AuthToken":"","columns":80,"rows":24}')
                ws.send_bytes(b"echo __NOPREFIX__\n")  # 缺 '0'
                frames = self._drain_until(ws, b"__NOPREFIX__", timeout=5.0)
            assert not any(b"__NOPREFIX__" in f for f in frames)
        finally:
            proc = ssh_mod._PROCESSES.pop(token, None)
            if proc is not None:
                ssh_mod._kill_process(proc)
