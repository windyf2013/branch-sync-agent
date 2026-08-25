"""推送执行链测试：四道闸、受限 push 执行器、confirm/push API、审计。"""

from __future__ import annotations

import contextlib
import re

import pytest
from fastapi.testclient import TestClient

from bsa.executor.base import CompletedProcess
from bsa.executor.exceptions import SafetyViolation
from bsa_web import push
from bsa_web.app import create_app
from bsa_web.auth import hash_password
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


def _audit_rows(app):
    return [
        dict(r)
        for r in app.state.db.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    ]


def _install_projection(monkeypatch, payload, cycle_id="cycle-2026-08-24"):
    monkeypatch.setattr(
        "bsa_web.projection.latest_completed_cycle", lambda log_dir: cycle_id
    )
    monkeypatch.setattr(
        "bsa_web.projection.load_cycle", lambda log_dir, cid: payload
    )


def _branch(target, status="SUCCESS", worktree=None, shas=None, patch="/patches/x.patch"):
    return {
        "target_branch": target,
        "worktree_path": str(worktree) if worktree else f"/wt/{target}",
        "status": status,
        "commits": [
            {"sha": s, "cherry_pick": "OK", "conflict_resolution": None, "build": {}}
            for s in (shas or [])
        ],
        "patch_path": patch,
        "stop_reason": None,
    }


def _payload(branch_results=None, status="REPORTED"):
    return {
        "cycle_id": "cycle-2026-08-24",
        "status": status,
        "scan_window": ["", ""],
        "detected_commits": [],
        "decisions": {},
        "branch_results": branch_results or {},
        "action_required": [],
    }


def _fake_worktree(base, target, cycle_id="cycle-2026-08-24"):
    """构造符合 V1 命名 <target>-<cycle_id> 且 gitdir 有效的假 worktree。"""
    path = base / f"{target}-{cycle_id}"
    path.mkdir(parents=True, exist_ok=True)
    gitdir = base / (".git-" + f"{target}-{cycle_id}".replace("/", "_"))
    gitdir.mkdir(parents=True, exist_ok=True)
    (path / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    return path


class _Rec:
    """记录 argv 并模拟成功返回的假 executor。"""

    def __init__(self):
        self.calls = []

    def run(self, args, **kwargs):
        self.calls.append(args)
        return CompletedProcess(returncode=0, stdout="", stderr="")


class TestCheckPushGates:
    def test_status_not_success_rejected(self, tmp_path):
        payload = _payload(
            {"feat/x": _branch("feat/x", status="FAILED", worktree=_fake_worktree(tmp_path, "feat/x"))}
        )
        reasons = push.check_push_gates(payload, "feat/x", forbidden=[], status_clean=True)
        assert any("SUCCESS" in r for r in reasons)

    def test_worktree_missing_rejected(self):
        payload = _payload({"feat/x": _branch("feat/x", worktree="/nonexistent")})
        reasons = push.check_push_gates(payload, "feat/x", forbidden=[], status_clean=True)
        assert any("worktree" in r for r in reasons)

    def test_forbidden_branch_rejected(self, tmp_path):
        payload = _payload({"main": _branch("main", worktree=_fake_worktree(tmp_path, "main"))})
        reasons = push.check_push_gates(payload, "main", forbidden=["main"], status_clean=True)
        assert any("禁止" in r for r in reasons)

    def test_dirty_worktree_rejected(self, tmp_path):
        payload = _payload(
            {"feat/x": _branch("feat/x", worktree=_fake_worktree(tmp_path, "feat/x"))}
        )
        reasons = push.check_push_gates(payload, "feat/x", forbidden=[], status_clean=False)
        assert any("未提交" in r for r in reasons)

    def test_all_gates_pass_empty(self, tmp_path):
        payload = _payload(
            {"feat/x": _branch("feat/x", worktree=_fake_worktree(tmp_path, "feat/x"))}
        )
        reasons = push.check_push_gates(
            payload, "feat/x", forbidden=["main"], status_clean=True
        )
        assert reasons == []

    def test_missing_branch_rejected(self, tmp_path):
        reasons = push.check_push_gates(
            _payload(), "feat/x", forbidden=[], status_clean=True
        )
        assert any("不存在" in r for r in reasons)

    def test_stale_worktree_wrong_name_rejected(self, tmp_path):
        # 投影指向的 worktree 命名不是 <target>-<cycle_id> → 不视为本 target 的 worktree
        other = _fake_worktree(tmp_path, "stale/target")
        payload = _payload({"feat/x": _branch("feat/x", worktree=other)})
        reasons = push.check_push_gates(
            payload, "feat/x", forbidden=[], status_clean=True,
            cycle_id="cycle-2026-08-24",
        )
        assert any("不匹配" in r for r in reasons)

    def test_worktree_on_wrong_branch_rejected(self, tmp_path, monkeypatch):
        payload = _payload(
            {"feat/x": _branch("feat/x", worktree=_fake_worktree(tmp_path, "feat/x"))}
        )
        monkeypatch.setattr("bsa_web.push._checked_out_branch", lambda wt: "release/2.0")
        reasons = push.check_push_gates(
            payload, "feat/x", forbidden=[], status_clean=True,
            cycle_id="cycle-2026-08-24",
        )
        assert any("不一致" in r for r in reasons)

    def test_worktree_binding_ok(self, tmp_path):
        payload = _payload(
            {"feat/x": _branch("feat/x", worktree=_fake_worktree(tmp_path, "feat/x"))}
        )
        reasons = push.check_push_gates(
            payload, "feat/x", forbidden=["main"], status_clean=True,
            cycle_id="cycle-2026-08-24",
        )
        assert reasons == []


class TestExecutePush:
    def test_argv_exact_and_no_force(self):
        rec = _Rec()
        rc, msg = push.execute_push(rec, "/wt/feat/x", "feat/x")
        assert rec.calls == [["git", "push", "origin", "HEAD:feat/x"]]
        assert "-f" not in rec.calls[0]
        assert "--force" not in rec.calls[0]
        assert rc == 0

    def test_non_fast_forward_message(self):
        class _Rejected:
            def run(self, args, **kwargs):
                return CompletedProcess(
                    returncode=1,
                    stdout="",
                    stderr="! [rejected]  HEAD -> feat/x (non-fast-forward)",
                )

        rc, msg = push.execute_push(_Rejected(), "/wt/feat/x", "feat/x")
        assert rc != 0
        assert "远端已前进" in msg


class TestPushExecutor:
    def test_rejects_non_push(self):
        ex = push.PushExecutor(inner=_Rec())
        with pytest.raises(SafetyViolation):
            ex.run(["git", "fetch", "origin"], cwd="/wt")

    def test_rejects_force(self):
        ex = push.PushExecutor(inner=_Rec())
        with pytest.raises(SafetyViolation):
            ex.run(["git", "push", "origin", "HEAD:feat/x", "-f"], cwd="/wt")
        with pytest.raises(SafetyViolation):
            ex.run(["git", "push", "--force", "origin", "HEAD:feat/x"], cwd="/wt")

    def test_rejects_illegal_target(self):
        ex = push.PushExecutor(inner=_Rec())
        for bad in ["HEAD:feat x", "HEAD:../../etc", "HEAD:"]:
            with pytest.raises(SafetyViolation):
                ex.run(["git", "push", "origin", bad], cwd="/wt")

    def test_allows_valid_push(self):
        rec = _Rec()
        ex = push.PushExecutor(inner=rec)
        proc = ex.run(["git", "push", "origin", "HEAD:feat/x"], cwd="/wt")
        assert proc.returncode == 0
        assert rec.calls == [["git", "push", "origin", "HEAD:feat/x"]]


class TestConfirmApi:
    def test_confirm_returns_commit_range_and_patch(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        worktree = tmp_path / "wt"
        worktree.mkdir(parents=True)
        payload = _payload(
            {"feat/x": _branch("feat/x", worktree=worktree, shas=["abc123", "def456"])}
        )
        _install_projection(monkeypatch, payload)
        r = client.post(
            "/api/push/confirm",
            json={"target": "feat/x", "_csrf": _csrf(client)},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["target"] == "feat/x"
        assert body["commits"] == ["abc123", "def456"]
        assert body["patch_summary"]
        assert body["cycle_id"] == "cycle-2026-08-24"

    def test_confirm_missing_target_400(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        _install_projection(monkeypatch, _payload())
        r = client.post("/api/push/confirm", json={"_csrf": _csrf(client)})
        assert r.status_code == 400

    def test_confirm_nonexistent_branch_400(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        _install_projection(monkeypatch, _payload())
        r = client.post(
            "/api/push/confirm",
            json={"target": "feat/x", "_csrf": _csrf(client)},
        )
        assert r.status_code == 400

    def test_confirm_non_success_400(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        worktree = tmp_path / "wt"
        worktree.mkdir(parents=True)
        payload = _payload({"feat/x": _branch("feat/x", status="FAILED", worktree=worktree)})
        _install_projection(monkeypatch, payload)
        r = client.post(
            "/api/push/confirm",
            json={"target": "feat/x", "_csrf": _csrf(client)},
        )
        assert r.status_code == 400

    def test_confirm_viewer_forbidden(self, tmp_path, monkeypatch):
        users = {
            "alice": f"{hash_password('op')}:{OPERATOR}",
            "bob": f"{hash_password('view')}:{VIEWER}",
        }
        app = _make_app(tmp_path, users=users)
        client = _client(app)
        _login(client, "bob", "view")
        _install_projection(monkeypatch, _payload())
        r = client.post(
            "/api/push/confirm",
            json={"target": "feat/x", "_csrf": _csrf(client)},
        )
        assert r.status_code == 403

    def test_confirm_unauth_redirects(self, tmp_path):
        app = _make_app(tmp_path)
        client = _client(app)
        r = client.post("/api/push/confirm", json={"target": "feat/x"})
        assert r.status_code == 302
        assert r.headers["location"].endswith("/login")


class TestPushApi:
    def test_push_success_200_and_audit(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        worktree = _fake_worktree(tmp_path, "feat/x")
        payload = _payload(
            {"feat/x": _branch("feat/x", worktree=worktree, shas=["abc123"])}
        )
        _install_projection(monkeypatch, payload)
        monkeypatch.setattr("bsa_web.push.worktree_is_clean", lambda wt: True)
        calls = []
        monkeypatch.setattr(
            "bsa_web.push.execute_push",
            lambda executor, wt, target: (calls.append((executor, wt, target)) or (0, "推送成功")),
        )
        r = client.post(
            "/api/push",
            json={"target": "feat/x", "shas": ["abc123"], "_csrf": _csrf(client)},
        )
        assert r.status_code == 200
        assert "推送成功" in r.json()["message"]
        assert calls and calls[0][1] == str(worktree) and calls[0][2] == "feat/x"

        audit = [row for row in _audit_rows(app) if row["action"] == "push"]
        assert len(audit) == 1
        assert audit[0]["user"] == "alice"
        assert audit[0]["target"] == "feat/x"
        assert audit[0]["result"] == "ok"
        assert "abc123" in (audit[0]["sha"] or "")

    def test_push_safety_violation_400_and_failed_audit(self, tmp_path, monkeypatch):
        # target 名过了四道闸但被受限 executor 拒绝（如 feature@2.0）→ 400 且留痕 failed
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        worktree = _fake_worktree(tmp_path, "feat@2.0")
        payload = _payload(
            {"feat@2.0": _branch("feat@2.0", worktree=worktree, shas=["abc123"])}
        )
        _install_projection(monkeypatch, payload)
        monkeypatch.setattr("bsa_web.push.worktree_is_clean", lambda wt: True)
        r = client.post(
            "/api/push",
            json={"target": "feat@2.0", "shas": ["abc123"], "_csrf": _csrf(client)},
        )
        assert r.status_code == 400
        assert "安全策略" in r.json()["detail"]
        audit = [row for row in _audit_rows(app) if row["action"] == "push"]
        assert len(audit) == 1
        assert audit[0]["user"] == "alice"
        assert audit[0]["target"] == "feat@2.0"
        assert audit[0]["result"] == "failed"

    def test_push_gate_fail_400_with_reasons(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        worktree = _fake_worktree(tmp_path, "feat/x")
        payload = _payload(
            {"feat/x": _branch("feat/x", status="FAILED", worktree=worktree)}
        )
        _install_projection(monkeypatch, payload)
        monkeypatch.setattr("bsa_web.push.worktree_is_clean", lambda wt: True)
        r = client.post(
            "/api/push",
            json={"target": "feat/x", "shas": [], "_csrf": _csrf(client)},
        )
        assert r.status_code == 400
        assert any("SUCCESS" in reason for reason in r.json()["detail"]["reasons"])

    def test_push_forbidden_gate_400(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        worktree = _fake_worktree(tmp_path, "main")
        payload = _payload({"main": _branch("main", worktree=worktree)})
        _install_projection(monkeypatch, payload)
        monkeypatch.setattr("bsa_web.push.worktree_is_clean", lambda wt: True)
        monkeypatch.setattr("bsa_web.push.load_forbidden_branches", lambda: ["main"])
        r = client.post(
            "/api/push",
            json={"target": "main", "shas": [], "_csrf": _csrf(client)},
        )
        assert r.status_code == 400
        assert any("禁止" in reason for reason in r.json()["detail"]["reasons"])

    def test_push_stale_worktree_binding_400(self, tmp_path, monkeypatch):
        # 投影的 worktree 命名不是 <target>-<cycle_id> → 闸②拒绝，绝不从错仓库推送
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        worktree = _fake_worktree(tmp_path, "other/target")
        payload = _payload(
            {"feat/x": _branch("feat/x", worktree=worktree, shas=["abc123"])}
        )
        _install_projection(monkeypatch, payload)
        monkeypatch.setattr("bsa_web.push.worktree_is_clean", lambda wt: True)
        r = client.post(
            "/api/push",
            json={"target": "feat/x", "shas": ["abc123"], "_csrf": _csrf(client)},
        )
        assert r.status_code == 400
        assert any("不匹配" in reason for reason in r.json()["detail"]["reasons"])

    def test_push_non_fast_forward_echo(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        worktree = _fake_worktree(tmp_path, "feat/x")
        payload = _payload(
            {"feat/x": _branch("feat/x", worktree=worktree, shas=["abc123"])}
        )
        _install_projection(monkeypatch, payload)
        monkeypatch.setattr("bsa_web.push.worktree_is_clean", lambda wt: True)
        monkeypatch.setattr(
            "bsa_web.push.execute_push",
            lambda executor, wt, target: (1, "远端已前进，请重新同步"),
        )
        r = client.post(
            "/api/push",
            json={"target": "feat/x", "shas": ["abc123"], "_csrf": _csrf(client)},
        )
        assert r.status_code == 200
        assert "远端已前进" in r.json()["message"]
        audit = [row for row in _audit_rows(app) if row["action"] == "push"]
        assert audit and audit[0]["result"] == "failed"

    def test_push_viewer_forbidden(self, tmp_path, monkeypatch):
        users = {
            "alice": f"{hash_password('op')}:{OPERATOR}",
            "bob": f"{hash_password('view')}:{VIEWER}",
        }
        app = _make_app(tmp_path, users=users)
        client = _client(app)
        _login(client, "bob", "view")
        _install_projection(monkeypatch, _payload())
        r = client.post(
            "/api/push",
            json={"target": "feat/x", "shas": [], "_csrf": _csrf(client)},
        )
        assert r.status_code == 403

    def test_push_unauth_redirects(self, tmp_path):
        app = _make_app(tmp_path)
        client = _client(app)
        r = client.post("/api/push", json={"target": "feat/x", "shas": []})
        assert r.status_code == 302
        assert r.headers["location"].endswith("/login")

    def test_push_without_csrf_403(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        _install_projection(monkeypatch, _payload())
        r = client.post("/api/push", json={"target": "feat/x", "shas": []})
        assert r.status_code == 403

    def test_push_holds_global_flock_through_execute(self, tmp_path, monkeypatch):
        # C1：闸预检与 git push 必须持全局 bsa.lock（与 V1 周期/清理互斥），
        # 防并发 cron 在闸过与 push 之间删除/重建目标 worktree（TOCTOU）。
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        worktree = _fake_worktree(tmp_path, "feat/x")
        payload = _payload(
            {"feat/x": _branch("feat/x", worktree=worktree, shas=["abc123"])}
        )
        _install_projection(monkeypatch, payload)
        monkeypatch.setattr("bsa_web.push.worktree_is_clean", lambda wt: True)
        seen = {}

        @contextlib.contextmanager
        def spy_flock_acquire(path, timeout=1800.0):
            seen["lock_path"] = str(path)
            seen["in_lock"] = True
            try:
                yield
            finally:
                seen["in_lock"] = False

        monkeypatch.setattr("bsa_web.api.push.flock_acquire", spy_flock_acquire)

        def fake_execute(executor, wt, target):
            seen["in_lock_at_execute"] = seen.get("in_lock")
            return 0, "推送成功"

        monkeypatch.setattr("bsa_web.push.execute_push", fake_execute)
        r = client.post(
            "/api/push",
            json={"target": "feat/x", "shas": ["abc123"], "_csrf": _csrf(client)},
        )
        assert r.status_code == 200
        assert seen["lock_path"] == str(tmp_path / "bsa.lock")
        assert seen["in_lock_at_execute"] is True
