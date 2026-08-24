"""审计日志测试：record/list_records 倒序、/audit 页面、只读无写入口。"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from bsa_web.app import create_app
from bsa_web.audit import list_records, record
from bsa_web.auth import hash_password
from bsa_web.db import init_db
from bsa_web.rbac import OPERATOR


def _make_app(tmp_path):
    users = {"alice": f"{hash_password('op')}:{OPERATOR}"}
    return create_app(
        settings_override={
            "log_dir": str(tmp_path),
            "secret_key": "test-secret",
            "users": users,
        }
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


class TestListRecords:
    def test_record_then_list_desc(self, tmp_path):
        conn = init_db(tmp_path / "bsa.db")
        id1 = record(conn, "alice", "override", target="feat/x", result="ok")
        id2 = record(
            conn,
            "alice",
            "confirm",
            cycle_id="cycle-1",
            target="feat/x",
            sha="abc",
            detail={"k": "v"},
        )
        rows = list_records(conn)
        assert len(rows) == 2
        assert rows[0]["id"] == id2
        assert rows[1]["id"] == id1
        assert rows[0]["action"] == "confirm"
        assert rows[0]["cycle_id"] == "cycle-1"
        assert rows[0]["sha"] == "abc"
        assert rows[0]["detail_json"] == '{"k": "v"}'
        conn.close()

    def test_list_limit(self, tmp_path):
        conn = init_db(tmp_path / "bsa.db")
        for _ in range(5):
            record(conn, "alice", "override")
        rows = list_records(conn, limit=3)
        assert len(rows) == 3
        assert rows[0]["id"] == 5
        conn.close()


class TestAuditPage:
    def test_page_lists_records_after_login(self, tmp_path):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        record(
            app.state.db,
            "alice",
            "confirm",
            cycle_id="cycle-1",
            target="feat/x",
            sha="abc",
            result="ok",
        )
        r = client.get("/audit")
        assert r.status_code == 200
        assert "操作日志" in r.text
        assert "confirm" in r.text
        assert "cycle-1" in r.text
        assert "abc" in r.text

    def test_page_empty(self, tmp_path):
        client = _client(_make_app(tmp_path))
        _login(client)
        r = client.get("/audit")
        assert r.status_code == 200
        assert "暂无操作记录" in r.text

    def test_unauthenticated_redirects_to_login(self, tmp_path):
        client = _client(_make_app(tmp_path))
        r = client.get("/audit")
        assert r.status_code == 302
        assert r.headers["location"].endswith("/login")


class TestAppendOnly:
    def test_no_delete_route(self, tmp_path):
        client = _client(_make_app(tmp_path))
        r = client.delete("/audit")
        assert r.status_code in (404, 405)
        r2 = client.delete("/api/audit")
        assert r2.status_code in (404, 405)

    def test_no_put_route(self, tmp_path):
        client = _client(_make_app(tmp_path))
        r = client.put("/audit")
        assert r.status_code in (404, 405)
        r2 = client.put("/api/audit")
        assert r2.status_code in (404, 405)
