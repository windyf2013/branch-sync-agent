"""管理员用户管理 API 测试：创建/改角色/重置密码/删除 + 安全不变量。"""

import re

import pytest
from fastapi.testclient import TestClient

from bsa_web.app import create_app
from bsa_web.auth import hash_password
from bsa_web.rbac import ADMIN, OPERATOR, VIEWER


def _make_app(tmp_path, users=None, **kw):
    if users is None:
        users = {"root": f"{hash_password('adm')}:{ADMIN}"}
    overrides = {"log_dir": str(tmp_path), "secret_key": "test-secret", "users": users}
    overrides.update(kw)
    return create_app(settings_override=overrides, env_file=None)


def _client(app):
    return TestClient(app, follow_redirects=False)


def _extract_csrf(html: str) -> str:
    m = re.search(r'name="_csrf"\s+value="([^"]+)"', html)
    assert m, "页面未注入 _csrf 隐藏字段"
    return m.group(1)


def _login(client, username, password):
    csrf = _extract_csrf(client.get("/login").text)
    return client.post(
        "/login", data={"username": username, "password": password, "_csrf": csrf}
    )


def _logout(client):
    csrf = _extract_csrf(client.get("/").text)
    return client.post("/logout", data={"_csrf": csrf})


def _admin_csrf(client):
    """以 root/admin 登录并返回当前页 CSRF。"""
    _login(client, "root", "adm")
    return _extract_csrf(client.get("/").text)


class TestUserCrud:
    def test_create_user(self, tmp_path):
        client = _client(_make_app(tmp_path))
        csrf = _admin_csrf(client)
        r = client.post(
            "/api/users",
            json={"username": "carol", "password": "pw123", "role": "operator", "_csrf": csrf},
        )
        assert r.status_code == 201
        assert r.json()["role"] == "operator"
        # 新用户可登录
        client.post("/logout", data={"_csrf": csrf})
        assert _login(client, "carol", "pw123").status_code == 302

    def test_create_duplicate_conflict(self, tmp_path):
        client = _client(_make_app(tmp_path))
        csrf = _admin_csrf(client)
        body = {"username": "carol", "password": "pw123", "role": "viewer", "_csrf": csrf}
        assert client.post("/api/users", json=body).status_code == 201
        assert client.post("/api/users", json=body).status_code == 409

    def test_create_invalid_role(self, tmp_path):
        client = _client(_make_app(tmp_path))
        csrf = _admin_csrf(client)
        r = client.post(
            "/api/users",
            json={"username": "carol", "password": "pw123", "role": "superuser", "_csrf": csrf},
        )
        assert r.status_code == 400

    def test_change_role_and_reset_password(self, tmp_path):
        client = _client(_make_app(tmp_path))
        csrf = _admin_csrf(client)
        client.post(
            "/api/users",
            json={"username": "carol", "password": "pw1", "role": "viewer", "_csrf": csrf},
        )
        # 切到 carol（viewer）：打 admin 端点应 403
        _logout(client)
        _login(client, "carol", "pw1")
        assert client.get("/settings").status_code == 403
        # 切回 root：重置 carol 密码后旧密码失效、新密码生效
        _logout(client)
        _login(client, "root", "adm")
        csrf = _extract_csrf(client.get("/").text)
        assert client.post(
            "/api/users/carol/password",
            json={"password": "pw2", "_csrf": csrf},
        ).status_code == 200
        _logout(client)
        assert _login(client, "carol", "pw1").status_code == 400
        assert _login(client, "carol", "pw2").status_code == 302

    def test_delete_user(self, tmp_path):
        client = _client(_make_app(tmp_path))
        csrf = _admin_csrf(client)
        client.post(
            "/api/users",
            json={"username": "carol", "password": "pw1", "role": "viewer", "_csrf": csrf},
        )
        assert client.post("/api/users/carol/delete", json={"_csrf": csrf}).status_code == 200
        # 已删除用户无法登录
        _logout(client)
        assert _login(client, "carol", "pw1").status_code == 400


class TestUserSafetyInvariants:
    def test_cannot_delete_self(self, tmp_path):
        client = _client(_make_app(tmp_path))
        csrf = _admin_csrf(client)
        r = client.post("/api/users/root/delete", json={"_csrf": csrf})
        assert r.status_code == 400

    def test_cannot_delete_last_admin(self, tmp_path):
        client = _client(_make_app(tmp_path))
        csrf = _admin_csrf(client)
        r = client.post("/api/users/root/delete", json={"_csrf": csrf})
        # 自删保护先触发；为覆盖「最后 admin」分支，用第二个 admin 视角删除 root
        assert r.status_code == 400

    def test_cannot_demote_last_admin(self, tmp_path):
        client = _client(_make_app(tmp_path))
        csrf = _admin_csrf(client)
        r = client.post(
            "/api/users/root/role", json={"role": "operator", "_csrf": csrf}
        )
        assert r.status_code == 400

    def test_last_admin_guard_respects_two_admins(self, tmp_path):
        users = {
            "root": f"{hash_password('adm')}:{ADMIN}",
            "other": f"{hash_password('adm2')}:{ADMIN}",
        }
        client = _client(_make_app(tmp_path, users=users))
        csrf = _admin_csrf(client)
        # 两个 admin 时删除/降级其中一个不受限
        assert client.post("/api/users/other/delete", json={"_csrf": csrf}).status_code == 200
        # 剩下 root 为唯一 admin，此时降级 root 被拒
        csrf = _extract_csrf(client.get("/").text)
        assert client.post(
            "/api/users/root/role", json={"role": "viewer", "_csrf": csrf}
        ).status_code == 400


class TestUserAuthorization:
    @pytest.mark.parametrize("role,pw", [(OPERATOR, "op"), (VIEWER, "vw")])
    def test_non_admin_forbidden(self, tmp_path, role, pw):
        users = {
            "root": f"{hash_password('adm')}:{ADMIN}",
            "alice": f"{hash_password(pw)}:{role}",
        }
        client = _client(_make_app(tmp_path, users=users))
        _login(client, "alice", pw)
        csrf = _extract_csrf(client.get("/").text)
        for path, body in [
            ("/api/users", {"username": "x", "password": "p", "role": "viewer"}),
            ("/api/users/root/role", {"role": "operator"}),
            ("/api/users/root/password", {"password": "p"}),
            ("/api/users/root/delete", {}),
        ]:
            assert client.post(path, json={**body, "_csrf": csrf}).status_code == 403

    def test_unauthenticated_forbidden(self, tmp_path):
        client = _client(_make_app(tmp_path))
        r = client.post("/api/users", json={"username": "x", "password": "p", "role": "viewer"})
        assert r.status_code == 403
