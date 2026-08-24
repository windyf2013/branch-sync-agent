import json
import re

from fastapi.testclient import TestClient

from bsa_web.app import create_app
from bsa_web.auth import hash_password
from bsa_web.projection import latest_completed_cycle, list_cycles, load_cycle
from bsa_web.rbac import OPERATOR

# bsa report 子进程需要完整 V1 设置（Settings 必填字段），
# 测试用与 tests/test_config.valid_env 等价的完整 env 注入。
BSA_ENV = {
    "REPO_PATH": "/srv/rcios",
    "BRANCH_FILE": "/srv/rcios/branch.md",
    "WORKTREE_ROOT": "/srv/wt",
    "LLM_MODEL": "deepseek-chat",
    "LLM_API_KEY": "sk-test",
    "LLM_BASE_URL": "https://api.deepseek.com/v1",
    "DOCKER_IMAGE": "rcios-build:latest",
    "DOCKER_MOUNT_WORKSPACE": "/workspace/rcios",
    "BUILD_SCRIPT_DIR": "build/platform/RTL9617C",
    "MAIL_SENDER": "bsa@raisecom.com",
    "MAIL_RECIPIENTS": '["ops@raisecom.com"]',
}


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


def _branch(target, status="SUCCESS"):
    return {
        "target_branch": target,
        "worktree_path": f"/wt/{target}",
        "status": status,
        "commits": [],
        "patch_path": None,
        "stop_reason": None,
    }


def _payload(*, status="REPORTED", branch_results=None, action_required=None):
    return {
        "cycle_id": "cycle-2026-08-20",
        "status": status,
        "scan_window": ["", ""],
        "detected_commits": [],
        "decisions": {},
        "branch_results": branch_results or {},
        "action_required": action_required or [],
    }


def _write_cycle_record(log_dir, cycle_id, status, started_at):
    d = log_dir / cycle_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "cycle.json").write_text(
        json.dumps(
            {
                "cycle_id": cycle_id,
                "status": status,
                "report_path": None,
                "mail_status": None,
                "started_at": started_at,
                "finished_at": started_at,
            }
        ),
        encoding="utf-8",
    )


def _set_bsa_env(monkeypatch):
    for key, val in BSA_ENV.items():
        monkeypatch.setenv(key, val)


class TestWorkbenchView:
    def test_workbench_shows_success_branches_with_push(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        payload = _payload(branch_results={"feat/x": _branch("feat/x", "SUCCESS")})
        monkeypatch.setattr(
            "bsa_web.projection.list_cycles",
            lambda log_dir: [{"cycle_id": "cycle-2026-08-20", "status": "REPORTED"}],
        )
        monkeypatch.setattr(
            "bsa_web.projection.latest_completed_cycle", lambda log_dir: "cycle-2026-08-20"
        )
        monkeypatch.setattr(
            "bsa_web.projection.load_cycle", lambda log_dir, cycle_id: payload
        )
        r = client.get("/")
        assert r.status_code == 200
        assert "feat/x" in r.text
        assert "推送" in r.text

    def test_workbench_shows_manual_review_pending(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        payload = _payload(
            action_required=[{"sha": "abc123", "kind": "ManualReview", "reason": "存疑"}]
        )
        monkeypatch.setattr(
            "bsa_web.projection.list_cycles",
            lambda log_dir: [{"cycle_id": "cycle-2026-08-20", "status": "REPORTED"}],
        )
        monkeypatch.setattr(
            "bsa_web.projection.latest_completed_cycle", lambda log_dir: "cycle-2026-08-20"
        )
        monkeypatch.setattr(
            "bsa_web.projection.load_cycle", lambda log_dir, cycle_id: payload
        )
        r = client.get("/")
        assert r.status_code == 200
        assert "待确认" in r.text
        assert "abc123" in r.text

    def test_workbench_running_cycle_shows_progress_without_details(
        self, tmp_path, monkeypatch
    ):
        client = _client(_make_app(tmp_path))
        _login(client)
        monkeypatch.setattr(
            "bsa_web.projection.list_cycles",
            lambda log_dir: [{"cycle_id": "cycle-2026-08-24", "status": "running"}],
        )
        monkeypatch.setattr(
            "bsa_web.projection.latest_completed_cycle", lambda log_dir: None
        )
        monkeypatch.setattr(
            "bsa_web.projection.load_cycle",
            lambda log_dir, cycle_id: {"status": "running"},
        )
        r = client.get("/")
        assert r.status_code == 200
        assert "进行中" in r.text
        assert "feat/x" not in r.text
        assert "推送" not in r.text

    def test_workbench_unauthenticated_redirects_to_login(self, tmp_path):
        client = _client(_make_app(tmp_path))
        r = client.get("/")
        assert r.status_code == 302
        assert r.headers["location"].endswith("/login")

    def test_workbench_tolerates_load_cycle_none(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        monkeypatch.setattr(
            "bsa_web.projection.list_cycles",
            lambda log_dir: [{"cycle_id": "cycle-2026-08-20", "status": "REPORTED"}],
        )
        monkeypatch.setattr(
            "bsa_web.projection.latest_completed_cycle", lambda log_dir: "cycle-2026-08-20"
        )
        monkeypatch.setattr(
            "bsa_web.projection.load_cycle", lambda log_dir, cycle_id: None
        )
        r = client.get("/")
        assert r.status_code == 200
        assert "不可用" in r.text

    def test_workbench_empty_state_when_no_cycle(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        monkeypatch.setattr("bsa_web.projection.list_cycles", lambda log_dir: [])
        monkeypatch.setattr(
            "bsa_web.projection.latest_completed_cycle", lambda log_dir: None
        )
        r = client.get("/")
        assert r.status_code == 200
        assert "暂无" in r.text

    def test_workbench_base_nav_has_history_and_ops_links(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        monkeypatch.setattr("bsa_web.projection.list_cycles", lambda log_dir: [])
        monkeypatch.setattr(
            "bsa_web.projection.latest_completed_cycle", lambda log_dir: None
        )
        r = client.get("/")
        assert r.status_code == 200
        assert "历史" in r.text
        assert "操作日志" in r.text
        assert "alice" in r.text


class TestProjection:
    def test_latest_completed_cycle_picks_recent_non_running(self, tmp_path):
        log_dir = tmp_path / "logs"
        _write_cycle_record(log_dir, "cycle-2026-08-20", "REPORTED", "2026-08-20T22:00:00")
        _write_cycle_record(log_dir, "cycle-2026-08-21", "FAILED", "2026-08-21T22:00:00")
        _write_cycle_record(log_dir, "cycle-2026-08-24", "running", "2026-08-24T22:00:00")
        assert latest_completed_cycle(str(log_dir)) == "cycle-2026-08-21"

    def test_latest_completed_cycle_none_when_only_running(self, tmp_path):
        log_dir = tmp_path / "logs"
        _write_cycle_record(log_dir, "cycle-2026-08-24", "running", "2026-08-24T22:00:00")
        assert latest_completed_cycle(str(log_dir)) is None

    def test_latest_completed_cycle_none_when_empty(self, tmp_path):
        assert latest_completed_cycle(str(tmp_path)) is None

    def test_list_cycles_desc_by_started(self, tmp_path):
        log_dir = tmp_path / "logs"
        _write_cycle_record(log_dir, "cycle-2026-08-20", "REPORTED", "2026-08-20T22:00:00")
        _write_cycle_record(log_dir, "cycle-2026-08-21", "FAILED", "2026-08-21T22:00:00")
        records = list_cycles(str(log_dir))
        assert [r["cycle_id"] for r in records] == [
            "cycle-2026-08-21",
            "cycle-2026-08-20",
        ]

    def test_load_cycle_subprocess_failure_returns_none(self, tmp_path, monkeypatch):
        _set_bsa_env(monkeypatch)
        log_dir = tmp_path / "logs"
        _write_cycle_record(log_dir, "cycle-2099-01-01", "REPORTED", "2026-01-01T00:00:00")
        assert load_cycle(str(log_dir), "cycle-2099-01-01") is None

    def test_load_cycle_parses_subprocess_json(self, tmp_path, monkeypatch):
        _set_bsa_env(monkeypatch)
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        assert load_cycle(str(log_dir), "cycle-2099-01-01") == {"status": "running"}
