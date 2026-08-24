import re
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from bsa_web.app import create_app
from bsa_web.auth import SESSION_COOKIE, create_session, hash_password
from bsa_web.rbac import OPERATOR, VIEWER


def _make_app(tmp_path, users=None, **kw):
    if users is None:
        users = {"alice": f"{hash_password('op')}:{OPERATOR}"}
    overrides = {"log_dir": str(tmp_path), "secret_key": "test-secret", "users": users}
    overrides.update(kw)
    return create_app(settings_override=overrides)


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


class TestLoginLogout:
    def test_unauthenticated_root_redirects_to_login(self, tmp_path):
        client = _client(_make_app(tmp_path))
        r = client.get("/")
        assert r.status_code == 302
        assert r.headers["location"].endswith("/login")

    def test_login_success_sets_secure_cookie(self, tmp_path):
        client = _client(_make_app(tmp_path))
        r = _login(client)
        assert r.status_code == 302
        assert r.headers["location"].endswith("/")
        set_cookie = r.headers["set-cookie"]
        assert SESSION_COOKIE in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=lax" in set_cookie
        assert "Secure" not in set_cookie  # 默认 http 测试

    def test_login_success_cookie_secure_enabled(self, tmp_path):
        client = _client(_make_app(tmp_path, cookie_secure=True))
        r = _login(client)
        assert "Secure" in r.headers["set-cookie"]

    def test_login_wrong_password_redirects_back(self, tmp_path):
        client = _client(_make_app(tmp_path))
        r = _login(client, password="wrong")
        assert r.status_code in (400, 302, 303)
        if r.status_code in (302, 303):
            assert r.headers["location"].endswith("/login")
        assert SESSION_COOKIE not in r.headers.get("set-cookie", "")

    def test_login_then_access_then_logout(self, tmp_path):
        client = _client(_make_app(tmp_path))
        _login(client)
        r = client.get("/")
        assert r.status_code == 200
        csrf = _extract_csrf(r.text)
        r = client.post("/logout", data={"_csrf": csrf})
        assert r.status_code in (302, 303)
        r = client.get("/")
        assert r.status_code == 302  # 登出后会话失效

    def test_expired_session_treated_as_unauthenticated(self, tmp_path):
        app = _make_app(tmp_path)
        token = create_session(app.state.db, "alice", OPERATOR, ttl_sec=8 * 3600)
        app.state.db.execute(
            "UPDATE sessions SET expires_at=? WHERE token=?",
            ("2000-01-01T00:00:00+00:00", token),
        )
        app.state.db.commit()
        client = _client(app)
        client.cookies.set(SESSION_COOKIE, token)
        r = client.get("/")
        assert r.status_code == 302
        assert r.headers["location"].endswith("/login")

    def test_unused_session_expires_after_ttl(self, tmp_path):
        app = _make_app(tmp_path)
        token = create_session(app.state.db, "alice", OPERATOR, ttl_sec=1)
        time.sleep(1.1)
        client = _client(app)
        client.cookies.set(SESSION_COOKIE, token)
        r = client.get("/")
        assert r.status_code == 302
        assert r.headers["location"].endswith("/login")

    def test_active_session_slides_expiry(self, tmp_path):
        app = _make_app(tmp_path)
        token = create_session(app.state.db, "alice", OPERATOR, ttl_sec=1)
        soon = (datetime.now(UTC) + timedelta(seconds=2)).isoformat()
        app.state.db.execute(
            "UPDATE sessions SET expires_at=? WHERE token=?", (soon, token)
        )
        app.state.db.commit()
        client = _client(app)
        client.cookies.set(SESSION_COOKIE, token)
        r = client.get("/")
        assert r.status_code == 200
        new_expires = app.state.db.execute(
            "SELECT expires_at FROM sessions WHERE token=?", (token,)
        ).fetchone()["expires_at"]
        assert datetime.fromisoformat(new_expires) > datetime.fromisoformat(soon)


class TestCsrf:
    def test_post_without_token_rejected(self, tmp_path):
        client = _client(_make_app(tmp_path))
        _login(client)
        r = client.post("/logout", data={})
        assert r.status_code == 403

    def test_login_post_without_token_rejected(self, tmp_path):
        client = _client(_make_app(tmp_path))
        r = client.post("/login", data={"username": "alice", "password": "op"})
        assert r.status_code == 403

    def test_post_with_token_passes(self, tmp_path):
        client = _client(_make_app(tmp_path))
        _login(client)
        r = client.get("/")
        csrf = _extract_csrf(r.text)
        r = client.post("/logout", data={"_csrf": csrf})
        assert r.status_code in (302, 303)


class TestRoles:
    def test_viewer_forbidden_operator_allowed(self, tmp_path):
        users = {
            "alice": f"{hash_password('op')}:{OPERATOR}",
            "bob": f"{hash_password('view')}:{VIEWER}",
        }
        client = _client(_make_app(tmp_path, users=users))
        _login(client, "bob", "view")
        r = client.get("/settings")
        assert r.status_code == 403
        csrf = _extract_csrf(client.get("/").text)
        client.post("/logout", data={"_csrf": csrf})
        _login(client, "alice", "op")
        r = client.get("/settings")
        assert r.status_code == 200


class TestSecretKeyEnforcement:
    def test_production_missing_secret_key_raises(self, tmp_path):
        with pytest.raises(RuntimeError):
            create_app(settings_override={"log_dir": str(tmp_path)})

    def test_override_injects_secret_key(self, tmp_path):
        app = create_app(
            settings_override={
                "log_dir": str(tmp_path),
                "secret_key": "test-secret",
            }
        )
        assert app.state.settings.secret_key == "test-secret"
        assert _client(app).get("/healthz").status_code == 200
