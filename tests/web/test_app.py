import pytest
from fastapi.testclient import TestClient

from bsa_web.app import _localtime, create_app
from bsa_web.db import InstanceLock, init_db
from bsa_web.settings import WebSettings, load_web_settings


def test_healthcheck(tmp_path):
    client = TestClient(
        create_app(
            settings_override={
                "log_dir": str(tmp_path),
                "secret_key": "test-secret",
            },
            env_file=None,
        )
    )
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_settings_override_merged(tmp_path):
    app = create_app(
        settings_override={"log_dir": str(tmp_path), "secret_key": "test-secret"},
        env_file=None
    )
    assert app.state.settings.log_dir == str(tmp_path)
    assert app.state.settings.bsa_web_port == 8888
    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


class TestWebSettings:
    def test_defaults(self):
        s = WebSettings(_env_file=None, secret_key="test-secret")
        assert s.bsa_web_port == 8888
        assert s.log_dir == "logs"
        assert s.session_ttl_sec == 8 * 3600
        assert s.users == {}

    def test_secret_key_required_without_env(self):
        try:
            WebSettings(_env_file=None)
        except Exception:
            return
        raise AssertionError("secret_key 未配置时应报错")

    def test_env_parsing(self, monkeypatch):
        monkeypatch.setenv("BSA_WEB_PORT", "9000")
        monkeypatch.setenv("SECRET_KEY", "env-secret")
        monkeypatch.setenv("BSA_USERS", '{"alice": "op"}')
        s = load_web_settings(env_file=None)
        assert s.bsa_web_port == 9000
        assert s.secret_key == "env-secret"
        assert s.users == {"alice": "op"}


class TestInitDb:
    def test_schema_and_schema_version(self, tmp_path):
        db = tmp_path / "bsa.db"
        conn = init_db(db)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"meta", "sessions", "audit_log", "tasks"} <= tables
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        assert row["value"] == "1"
        conn.close()

    def test_idempotent(self, tmp_path):
        db = tmp_path / "bsa.db"
        init_db(db)
        conn = init_db(db)
        n = conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
        assert n == 1
        conn.close()


class TestInstanceLock:
    def test_second_instance_acquire_fails(self, tmp_path):
        db = tmp_path / "platform.sqlite3"
        first = InstanceLock(db)
        try:
            with pytest.raises(RuntimeError, match="单实例冲突"):
                InstanceLock(db)
        finally:
            first.close()

    def test_reacquire_after_close_succeeds(self, tmp_path):
        db = tmp_path / "platform.sqlite3"
        InstanceLock(db).close()
        InstanceLock(db).close()

    def test_context_manager_releases(self, tmp_path):
        db = tmp_path / "platform.sqlite3"
        with InstanceLock(db):
            pass
        InstanceLock(db).close()

    def test_second_app_same_log_dir_raises(self, tmp_path):
        create_app(
            settings_override={"log_dir": str(tmp_path), "secret_key": "test-secret"},
            env_file=None,
        )
        with pytest.raises(RuntimeError, match="单实例冲突"):
            create_app(
                settings_override={
                    "log_dir": str(tmp_path),
                    "secret_key": "test-secret",
                },
                env_file=None,
            )


class TestLocaltimeFilter:
    def test_utc_to_local(self):
        # UTC 时间转宿主本地时区（非 +8 宿主也能过，偏移按当前时区推导）
        from datetime import UTC, datetime

        src = datetime(2026, 8, 25, 10, 25, 7, tzinfo=UTC)
        expect = src.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        assert _localtime("2026-08-25T10:25:07.311877+00:00") == expect

    def test_naive_treated_as_local(self):
        # V1 周期 naive 本地时间原样格式化
        assert _localtime("2026-08-25T17:13:40") == "2026-08-25 17:13:40"

    def test_offset_converted(self):
        # git commit +08:00 -> 宿主本地时区（当前 +8 时同值，其他时区按偏移转）
        from datetime import datetime, timedelta, timezone

        src = datetime(2026, 8, 25, 10, 25, 7, tzinfo=timezone(timedelta(hours=8)))
        expect = src.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        assert _localtime("2026-08-25T10:25:07+08:00") == expect

    def test_empty_and_invalid(self):
        assert _localtime("") == ""
        assert _localtime(None) == ""
        assert _localtime("not-a-date") == "not-a-date"
