"""放弃/恢复 API 与任务列表过滤联动测试。

覆盖：放弃→is_abandoned True / 恢复→False；重复放弃幂等；commit 级与
分支级独立；权限（viewer 403 / 未登录 302）；审计 action=abandon/restore；
任务列表过滤（放弃项不在待处理/可推送、显示已放弃、恢复后重新出现）。
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from bsa_web.app import create_app
from bsa_web.auth import hash_password
from bsa_web.db import abandoned_keys, is_abandoned
from bsa_web.rbac import OPERATOR, VIEWER


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


def _post(client, path, body):
    return client.post(path, json={**body, "_csrf": _csrf(client)})


def _audit_rows(app):
    return [
        dict(r)
        for r in app.state.db.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    ]


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


def _mount_cycle(monkeypatch, payload):
    monkeypatch.setattr(
        "bsa_web.projection.list_cycles",
        lambda log_dir: [{"cycle_id": "cycle-2026-08-20", "status": "REPORTED"}],
    )
    monkeypatch.setattr(
        "bsa_web.projection.latest_completed_cycle",
        lambda log_dir: "cycle-2026-08-20",
    )
    monkeypatch.setattr(
        "bsa_web.projection.load_cycle", lambda log_dir, cycle_id: payload
    )


def test_abandon_then_restore(tmp_path):
    app = _make_app(tmp_path)
    client = _client(app)
    _login(client)
    r = _post(client, "/api/abandon", {"cycle_id": "c1", "target": "release/2.4"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert is_abandoned(app.state.db, "c1", "release/2.4") is True
    assert (("release/2.4", None)) in abandoned_keys(app.state.db, "c1")
    r = _post(client, "/api/restore", {"cycle_id": "c1", "target": "release/2.4"})
    assert r.status_code == 200
    assert is_abandoned(app.state.db, "c1", "release/2.4") is False


def test_abandon_idempotent_same_key(tmp_path):
    app = _make_app(tmp_path)
    client = _client(app)
    _login(client)
    body = {"cycle_id": "c1", "target": "feat/x", "sha": "abc123"}
    assert _post(client, "/api/abandon", body).status_code == 200
    assert _post(client, "/api/abandon", body).status_code == 200
    n = app.state.db.execute(
        "SELECT COUNT(*) FROM abandons "
        "WHERE cycle_id='c1' AND target='feat/x' AND sha='abc123'"
    ).fetchone()[0]
    assert n == 1


def test_branch_and_commit_level_independent(tmp_path):
    # 分支级（sha=None）与 commit 级（sha=abc）是独立行：恢复其一不影响另一
    app = _make_app(tmp_path)
    client = _client(app)
    _login(client)
    _post(client, "/api/abandon", {"cycle_id": "c1", "target": "feat/x"})
    _post(client, "/api/abandon", {"cycle_id": "c1", "target": "feat/x", "sha": "abc"})
    keys = abandoned_keys(app.state.db, "c1")
    assert ("feat/x", None) in keys
    assert ("feat/x", "abc") in keys
    _post(client, "/api/restore", {"cycle_id": "c1", "target": "feat/x", "sha": "abc"})
    keys = abandoned_keys(app.state.db, "c1")
    assert ("feat/x", "abc") not in keys
    assert ("feat/x", None) in keys  # 分支级行仍在


def test_commit_level_abandon_matches_specific_sha(tmp_path):
    app = _make_app(tmp_path)
    client = _client(app)
    _login(client)
    _post(client, "/api/abandon", {"cycle_id": "c1", "target": "feat/x", "sha": "abc"})
    assert is_abandoned(app.state.db, "c1", "feat/x", "abc") is True
    assert is_abandoned(app.state.db, "c1", "feat/x", "def") is False  # 其他 commit 不受影响
    assert is_abandoned(app.state.db, "c1", "feat/x") is False  # commit 级不影响分支级
    _post(client, "/api/abandon", {"cycle_id": "c1", "target": "feat/x"})
    assert is_abandoned(app.state.db, "c1", "feat/x", "def") is True  # 分支级覆盖全部 commit


def test_branch_abandon_cancels_running_task(tmp_path, monkeypatch):
    # 分支级放弃运行中任务 → 触发 cancel_task，tasks 行变 cancelled。
    app = _make_app(tmp_path)
    client = _client(app)
    _login(client)
    db = app.state.db
    # 造一个 running 任务（executor 认领后状态）
    db.execute(
        "INSERT INTO tasks(kind, user, target, src, fresh, state, created_at, "
        "cycle_id, pid) VALUES ('sync','alice','feat/x','main',0,'running',?,?,4242)",
        ("2026-08-31T00:00:00+00:00", "manual-20260831-120000-99"),
    )
    db.commit()
    # 拦截 cancel 内部子进程/清理动作，避免真杀进程与 docker 调用
    monkeypatch.setattr(
        "bsa_web.api.abandon.cancel_task",
        lambda db, log_dir, task_id: {"cancelled": True, "task_id": task_id},
    )

    r = _post(client, "/api/abandon", {"cycle_id": "manual-20260831-120000-99", "target": "feat/x"})

    assert r.status_code == 200
    assert r.json()["cancelled"] is True
    assert r.json()["task_id"] is not None

    # commit 级放弃不触发取消
    r2 = _post(
        client, "/api/abandon",
        {"cycle_id": "manual-20260831-120000-99", "target": "feat/x", "sha": "abc"},
    )
    assert r2.json().get("cancelled") is False


def test_branch_abandon_without_active_task_no_cancel(tmp_path):
    app = _make_app(tmp_path)
    client = _client(app)
    _login(client)
    r = _post(client, "/api/abandon", {"cycle_id": "c1", "target": "feat/x"})
    assert r.status_code == 200
    assert r.json().get("cancelled") is False


def test_abandon_viewer_forbidden_and_restore_too(tmp_path):
    users = {
        "alice": f"{hash_password('op')}:{OPERATOR}",
        "bob": f"{hash_password('view')}:{VIEWER}",
    }
    app = _make_app(tmp_path, users=users)
    client = _client(app)
    _login(client, "bob", "view")
    assert (
        _post(client, "/api/abandon", {"cycle_id": "c1", "target": "feat/x"}).status_code
        == 403
    )
    assert (
        _post(client, "/api/restore", {"cycle_id": "c1", "target": "feat/x"}).status_code
        == 403
    )


def test_abandon_requires_login(tmp_path):
    # 未登录 POST：CSRF 依赖先于 require_login 校验（与既有 /api/* 端点一致）→ 403
    app = _make_app(tmp_path)
    client = _client(app)
    r = client.post("/api/abandon", json={"cycle_id": "c1", "target": "feat/x"})
    assert r.status_code == 403


def test_audit_actions_written(tmp_path):
    app = _make_app(tmp_path)
    client = _client(app)
    _login(client)
    _post(client, "/api/abandon", {"cycle_id": "c1", "target": "feat/x", "sha": "abc"})
    _post(client, "/api/restore", {"cycle_id": "c1", "target": "feat/x", "sha": "abc"})
    actions = [(r["action"], r["cycle_id"], r["target"], r["sha"]) for r in _audit_rows(app)]
    assert ("abandon", "c1", "feat/x", "abc") in actions
    assert ("restore", "c1", "feat/x", "abc") in actions


def test_workbench_filters_abandoned_and_restores(tmp_path, monkeypatch):
    app = _make_app(tmp_path)
    client = _client(app)
    _login(client)
    payload = _payload(
        branch_results={"feat/x": _branch("feat/x", "SUCCESS")},
        action_required=[
            {"sha": "abc123", "branch": "feat/x", "kind": "ManualReview", "reason": "存疑"}
        ],
    )
    _mount_cycle(monkeypatch, payload)

    r = client.get("/")
    assert 'data-push-target="feat/x"' in r.text
    assert "待确认" in r.text
    # 待确认 sha 不再占工作台任务表行（收敛到 KPI 计数与分支详情人工项）
    assert "abc123" not in r.text

    _post(client, "/api/abandon", {"cycle_id": "cycle-2026-08-20", "target": "feat/x"})
    r = client.get("/")
    assert 'data-push-target="feat/x"' not in r.text
    # KPI 恒显"待确认"卡
    assert "待确认" in r.text
    assert "abc123" not in r.text
    assert "已放弃" in r.text
    assert "feat/x" in r.text

    _post(client, "/api/restore", {"cycle_id": "cycle-2026-08-20", "target": "feat/x"})
    r = client.get("/")
    assert "已放弃" not in r.text
    assert 'data-push-target="feat/x"' in r.text
    assert "待确认" in r.text
    assert "abc123" not in r.text
