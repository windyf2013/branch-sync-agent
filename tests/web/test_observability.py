"""可观测性测试：健康检查（含平台 DB 可写校验）与结构化请求日志中间件。"""

from __future__ import annotations

import json
import re
import sqlite3

from fastapi.testclient import TestClient

from bsa_web.app import create_app
from bsa_web.auth import SESSION_COOKIE, hash_password
from bsa_web.rbac import OPERATOR


def _make_app(tmp_path, users=None):
    return create_app(
        settings_override={
            "log_dir": str(tmp_path),
            "secret_key": "test-secret",
            "users": users or {},
        }
    )


def _log_lines(tmp_path):
    return [
        line
        for line in (tmp_path / "access.log").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestHealthz:
    def test_healthz_ok(self, tmp_path):
        client = TestClient(_make_app(tmp_path))
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_healthz_db_failure_503(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)

        class _BoomDB:
            def execute(self, sql, *args):
                raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(app.state, "db", _BoomDB())
        r = TestClient(app).get("/healthz")
        assert r.status_code == 503

    def test_healthz_db_down_with_session_keeps_503_and_logs(self, tmp_path, monkeypatch):
        app = _make_app(
            tmp_path, users={"alice": f"{hash_password('op')}:{OPERATOR}"}
        )
        client = TestClient(app, follow_redirects=False)
        page = client.get("/login")
        m = re.search(r'name="_csrf"\s+value="([^"]+)"', page.text)
        assert m, "登录页未注入 _csrf"
        client.post(
            "/login",
            data={"username": "alice", "password": "op", "_csrf": m.group(1)},
        )
        assert client.cookies.get(SESSION_COOKIE), "登录后应带会话 cookie"

        class _BoomDB:
            def execute(self, sql, *args):
                raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(app.state, "db", _BoomDB())
        r = client.get("/healthz")
        assert r.status_code == 503, "DB 故障时 /healthz 应返回 503 而非 500"
        records = [json.loads(line) for line in _log_lines(tmp_path)]
        assert records[-1]["status"] == 503
        assert records[-1]["user"] == ""


class TestAccessLog:
    def test_access_log_json_line(self, tmp_path):
        client = TestClient(_make_app(tmp_path))
        r = client.get("/healthz")
        assert r.status_code == 200
        lines = _log_lines(tmp_path)
        assert lines, "access.log 应有请求日志行"
        record = json.loads(lines[-1])
        assert record["method"] == "GET"
        assert record["path"] == "/healthz"
        assert record["status"] == 200
        assert isinstance(record["duration_ms"], (int, float))
        assert "user" in record

    def test_access_log_records_username_after_login(self, tmp_path):
        app = _make_app(
            tmp_path, users={"alice": f"{hash_password('op')}:{OPERATOR}"}
        )
        client = TestClient(app, follow_redirects=False)
        page = client.get("/login")
        m = re.search(r'name="_csrf"\s+value="([^"]+)"', page.text)
        assert m, "登录页未注入 _csrf"
        client.post(
            "/login",
            data={"username": "alice", "password": "op", "_csrf": m.group(1)},
        )
        r = client.get("/")
        assert r.status_code == 200
        records = [json.loads(line) for line in _log_lines(tmp_path)]
        assert records[-1]["user"] == "alice"
