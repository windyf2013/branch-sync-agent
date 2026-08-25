"""A 区任务中心 + 任务详情页聚合操作测试。

覆盖：首页自动/手动两区块聚合、窗口过滤（超窗口手动任务收敛历史页）、
已放弃分支（徽章 + 恢复入口、不再可推送）、任务详情页操作按状态/权限显示
（SUCCESS→推送、FAILED+worktree→WebSSH+重跑、已放弃→恢复）、
详情页复用证据渲染（patch/编译日志）。
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from bsa_web.app import create_app
from bsa_web.auth import hash_password
from bsa_web.rbac import OPERATOR, VIEWER

_CYCLE = "cycle-2026-08-21"
_WINDOW = ["2026-08-20T22:00:00+08:00", "2026-08-21T22:00:00+08:00"]


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


def _branch(target, status="SUCCESS", commits=None, patch_path=None):
    return {
        "target_branch": target,
        "worktree_path": f"/wt/{target}",
        "status": status,
        "commits": commits or [],
        "patch_path": patch_path,
        "stop_reason": None,
    }


def _commit_result(sha, build=None):
    return {
        "sha": sha,
        "cherry_pick": "OK",
        "conflict_resolution": None,
        "build": build or {},
    }


def _build_outcome(status="FAILED", log_path=None, errors=None):
    return {
        "status": status,
        "log_path": log_path,
        "errors": list(errors or []),
        "agent_attempts": 1,
    }


def _payload(*, status="REPORTED", branch_results=None, action_required=None, scan_window=None):
    return {
        "cycle_id": _CYCLE,
        "status": status,
        "scan_window": scan_window if scan_window is not None else _WINDOW,
        "detected_commits": [],
        "decisions": {},
        "branch_results": branch_results or {},
        "action_required": action_required or [],
    }


def _mount_cycle(monkeypatch, payload, cycle_id=_CYCLE, latest=None):
    if latest is None:
        latest = cycle_id
    monkeypatch.setattr(
        "bsa_web.projection.list_cycles",
        lambda log_dir: [{"cycle_id": cycle_id, "status": "REPORTED"}],
    )
    monkeypatch.setattr(
        "bsa_web.projection.latest_completed_cycle", lambda log_dir: latest
    )
    monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)


def _add_task(app, *, target, state, created_at, kind="sync"):
    app.state.db.execute(
        "INSERT INTO tasks(kind, user, target, state, created_at) VALUES (?,?,?,?,?)",
        (kind, "alice", target, state, created_at),
    )
    app.state.db.commit()


class TestTaskCenter:
    def test_center_splits_auto_and_manual_blocks(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        payload = _payload(
            branch_results={
                "feat/auto": _branch("feat/auto", "SUCCESS"),
                "feat/fail": _branch("feat/fail", "FAILED"),
            }
        )
        _mount_cycle(monkeypatch, payload)
        _add_task(
            app, target="feat/manual", state="succeeded",
            created_at="2026-08-21T09:00:00+00:00",
        )
        _add_task(
            app, target="feat/manual-run", state="running",
            created_at="2026-08-21T09:10:00+00:00",
        )
        r = client.get("/")
        assert r.status_code == 200
        assert "自动任务" in r.text
        assert "手动任务" in r.text
        for target in ("feat/auto", "feat/fail", "feat/manual", "feat/manual-run"):
            assert target in r.text
        assert "成功" in r.text
        assert "失败" in r.text
        assert "进行中" in r.text
        # 自动任务面板链接到任务详情页
        assert f"/task/{_CYCLE}/feat/auto" in r.text

    def test_center_filters_out_of_window_manual_tasks(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        payload = _payload(branch_results={"feat/auto": _branch("feat/auto", "SUCCESS")})
        _mount_cycle(monkeypatch, payload)
        # 窗口起点 2026-08-20T22:00+08:00（= 14:00 UTC），8/19 已超窗口
        _add_task(
            app, target="feat/old", state="succeeded",
            created_at="2026-08-19T10:00:00+00:00",
        )
        _add_task(
            app, target="feat/recent", state="succeeded",
            created_at="2026-08-21T09:00:00+00:00",
        )
        r = client.get("/")
        assert r.status_code == 200
        assert "feat/recent" in r.text
        assert "feat/old" not in r.text

    def test_center_keeps_active_manual_tasks_outside_window(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        payload = _payload(branch_results={})
        _mount_cycle(monkeypatch, payload)
        for target, state in (
            ("feat/queued", "queued"),
            ("feat/running", "running"),
        ):
            _add_task(app, target=target, state=state, created_at="2026-08-19T10:00:00+00:00")
        r = client.get("/")
        assert r.status_code == 200
        assert "feat/queued" in r.text
        assert "feat/running" in r.text

    def test_center_manual_task_with_cycle_id_links_to_detail_page(self, tmp_path, monkeypatch):
        # I1：runner 回写 cycle_id 后，手动面板链接任务详情页 /task/{cycle}/{target}
        # （非 /tasks/{id}）；未回写（排队/执行中）仍回退 /tasks/{id}。
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        payload = _payload(branch_results={})
        _mount_cycle(monkeypatch, payload)
        app.state.db.execute(
            "INSERT INTO tasks(kind, user, target, cycle_id, state, created_at) "
            "VALUES ('sync','alice','feat/done','manual-20260821-093000-4242','succeeded',?)",
            ("2026-08-21T09:00:00+00:00",),
        )
        app.state.db.execute(
            "INSERT INTO tasks(kind, user, target, state, created_at) "
            "VALUES ('sync','alice','feat/pending','queued',?)",
            ("2026-08-21T09:10:00+00:00",),
        )
        app.state.db.commit()
        r = client.get("/")
        assert r.status_code == 200
        assert "/task/manual-20260821-093000-4242/feat/done" in r.text
        assert "/tasks/2" in r.text  # 未回写 cycle_id 的手动任务仍指向任务状态页

    def test_center_abandoned_branch_badge_and_restore(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        payload = _payload(branch_results={"feat/x": _branch("feat/x", "SUCCESS")})
        _mount_cycle(monkeypatch, payload)

        r = client.get("/")
        assert 'data-push-target="feat/x"' in r.text
        assert f'data-push-cycle="{_CYCLE}"' in r.text
        assert "已放弃" not in r.text

        _post(client, "/api/abandon", {"cycle_id": _CYCLE, "target": "feat/x"})
        r = client.get("/")
        assert "已放弃" in r.text
        assert "feat/x" in r.text
        assert 'data-push-target="feat/x"' not in r.text
        assert 'data-restore-target="feat/x"' in r.text

        _post(client, "/api/restore", {"cycle_id": _CYCLE, "target": "feat/x"})
        r = client.get("/")
        assert "已放弃" not in r.text
        assert 'data-push-target="feat/x"' in r.text


class TestTaskDetail:
    def test_detail_success_shows_push_key(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        payload = _payload(branch_results={"feat/ok": _branch("feat/ok", "SUCCESS")})
        _mount_cycle(monkeypatch, payload)
        r = client.get(f"/task/{_CYCLE}/feat/ok")
        assert r.status_code == 200
        assert "推送" in r.text
        assert 'data-push-target="feat/ok"' in r.text
        assert f'data-push-cycle="{_CYCLE}"' in r.text

    def test_detail_manual_success_shows_push_key(self, tmp_path, monkeypatch):
        # 手动（manual cycle）SUCCESS 分支任务详情页有推送键：手动周期不写 cycle
        # record，latest_completed_cycle 仍指向自动周期（非 manual cycle），推送键
        # 须照常开放（四道闸在 API 侧兜底 worktree/状态校验）。
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        manual_cycle = "manual-20260821-093000-4242"
        payload = _payload(branch_results={"feat/manual": _branch("feat/manual", "SUCCESS")})
        _mount_cycle(monkeypatch, payload, cycle_id=manual_cycle, latest=_CYCLE)
        r = client.get(f"/task/{manual_cycle}/feat/manual")
        assert r.status_code == 200
        assert "推送" in r.text
        assert 'data-push-target="feat/manual"' in r.text
        assert f'data-push-cycle="{manual_cycle}"' in r.text

    def test_detail_rerun_success_shows_push_key(self, tmp_path, monkeypatch):
        # rerun（retained）独立线程投影 SUCCESS 同样开放推送键。
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        rerun_cycle = "rerun-release-2.4-20260821-093000-4242"
        payload = _payload(branch_results={"release-2.4": _branch("release-2.4", "SUCCESS")})
        _mount_cycle(monkeypatch, payload, cycle_id=rerun_cycle, latest=_CYCLE)
        r = client.get(f"/task/{rerun_cycle}/release-2.4")
        assert r.status_code == 200
        assert "推送" in r.text
        assert 'data-push-target="release-2.4"' in r.text
        assert f'data-push-cycle="{rerun_cycle}"' in r.text

    def test_detail_manual_non_success_no_push_key(self, tmp_path, monkeypatch):
        # 非 SUCCESS 手动任务无推送键（FAILED 显示 WebSSH/重跑而非推送）。
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        manual_cycle = "manual-20260821-093000-4242"
        payload = _payload(branch_results={"feat/manual": _branch("feat/manual", "FAILED")})
        _mount_cycle(monkeypatch, payload, cycle_id=manual_cycle, latest=_CYCLE)
        r = client.get(f"/task/{manual_cycle}/feat/manual")
        assert r.status_code == 200
        assert 'data-push-target="feat/manual"' not in r.text
        assert "WebSSH" in r.text

    def test_detail_failed_with_worktree_shows_webssh_and_rerun(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        payload = _payload(branch_results={"feat/bad": _branch("feat/bad", "FAILED")})
        _mount_cycle(monkeypatch, payload)
        r = client.get(f"/task/{_CYCLE}/feat/bad")
        assert r.status_code == 200
        assert "WebSSH" in r.text
        assert f'/ssh/task/{_CYCLE}/feat/bad' in r.text
        assert "重跑" in r.text
        assert 'action="/rerun"' in r.text

    def test_detail_abandoned_shows_restore_only(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        payload = _payload(branch_results={"feat/x": _branch("feat/x", "SUCCESS")})
        _mount_cycle(monkeypatch, payload)
        _post(client, "/api/abandon", {"cycle_id": _CYCLE, "target": "feat/x"})
        r = client.get(f"/task/{_CYCLE}/feat/x")
        assert r.status_code == 200
        assert "已放弃" in r.text
        assert 'data-restore-target="feat/x"' in r.text
        assert 'data-push-target="feat/x"' not in r.text
        assert "WebSSH" not in r.text

    def test_detail_non_abandoned_shows_abandon_button(self, tmp_path, monkeypatch):
        # I3：未放弃分支详情页提供放弃入口（分支级，operator），POST /api/abandon
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        payload = _payload(branch_results={"feat/ok": _branch("feat/ok", "SUCCESS")})
        _mount_cycle(monkeypatch, payload)
        r = client.get(f"/task/{_CYCLE}/feat/ok")
        assert r.status_code == 200
        assert 'data-abandon-target="feat/ok"' in r.text
        assert f'data-abandon-cycle="{_CYCLE}"' in r.text
        assert 'data-abandon-sha=""' in r.text
        assert re.search(
            r'<button[^>]*data-abandon-target="feat/ok"[^>]*>.*?</button>\s*'
            r'<span class="abandon-result">',
            r.text,
            re.S,
        ), "放弃按钮与 .abandon-result 必须相邻（同父级，JS parentElement 才能命中）"

    def test_detail_abandoned_hides_abandon_button(self, tmp_path, monkeypatch):
        # 已放弃分支不再显示放弃按钮（只有恢复入口）
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        payload = _payload(branch_results={"feat/x": _branch("feat/x", "SUCCESS")})
        _mount_cycle(monkeypatch, payload)
        _post(client, "/api/abandon", {"cycle_id": _CYCLE, "target": "feat/x"})
        r = client.get(f"/task/{_CYCLE}/feat/x")
        assert r.status_code == 200
        assert 'data-abandon-target="feat/x"' not in r.text

    def test_detail_abandon_button_wires_abandon_api(self, tmp_path, monkeypatch):
        # 放弃按钮调 /api/abandon（带 _csrf）真实落库：abandon 后详情页切换为恢复
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        payload = _payload(branch_results={"feat/x": _branch("feat/x", "FAILED")})
        _mount_cycle(monkeypatch, payload)
        r = client.get(f"/task/{_CYCLE}/feat/x")
        assert 'data-abandon-target="feat/x"' in r.text
        _post(client, "/api/abandon", {"cycle_id": _CYCLE, "target": "feat/x"})
        r = client.get(f"/task/{_CYCLE}/feat/x")
        assert 'data-abandon-target="feat/x"' not in r.text
        assert 'data-restore-target="feat/x"' in r.text

    def test_detail_restore_button_result_are_siblings(self, tmp_path, monkeypatch):
        # C1：恢复 JS 用 btn.parentElement.querySelector(".restore-result") 定位结果，
        # 详情页按钮在 .ops-row div 内（无 <li> 祖先），按钮与结果必须是兄弟节点。
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        payload = _payload(branch_results={"feat/x": _branch("feat/x", "SUCCESS")})
        _mount_cycle(monkeypatch, payload)
        _post(client, "/api/abandon", {"cycle_id": _CYCLE, "target": "feat/x"})
        r = client.get(f"/task/{_CYCLE}/feat/x")
        assert r.status_code == 200
        assert re.search(
            r'<button[^>]*data-restore-target="feat/x"[^>]*>.*?</button>\s*'
            r'<span class="restore-result">',
            r.text,
            re.S,
        ), "详情页恢复按钮与 .restore-result 必须相邻（同父级，JS parentElement 才能命中）"
        assert "parentElement" in r.text

    def test_detail_shows_manual_review_ops(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        payload = _payload(
            branch_results={"feat/m": _branch("feat/m", "MANUAL")},
            action_required=[
                {"sha": "abc123", "branch": "feat/m", "kind": "ManualReview",
                 "reason": "存疑", "evidence": ["ev"]}
            ],
        )
        _mount_cycle(monkeypatch, payload)
        r = client.get(f"/task/{_CYCLE}/feat/m")
        assert r.status_code == 200
        assert "人工项" in r.text
        assert "abc123" in r.text
        assert 'data-confirm-target="feat/m"' in r.text
        assert 'data-override-sha="abc123"' in r.text

    def test_detail_renders_patch_and_build_log_evidence(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        log_file = tmp_path / "build.log"
        log_file.write_text("compile error here", encoding="utf-8")
        patch_file = tmp_path / "b.patch"
        patch_file.write_text("diff content", encoding="utf-8")
        branch = _branch(
            "feat/bad",
            "FAILED",
            commits=[
                _commit_result(
                    "a1",
                    build={"RTL9617C": _build_outcome(log_path=str(log_file), errors=["boom"])},
                )
            ],
            patch_path=str(patch_file),
        )
        payload = _payload(branch_results={"feat/bad": branch})
        _mount_cycle(monkeypatch, payload)
        monkeypatch.setattr(
            "bsa_web.views.detail._read_build_log",
            lambda log_dir, log_path: ("compile error here", False),
        )
        r = client.get(f"/task/{_CYCLE}/feat/bad")
        assert r.status_code == 200
        assert "下载完整 patch" in r.text
        assert "compile error here" in r.text
        assert "下载完整日志" in r.text

    def test_detail_viewer_sees_no_ops(self, tmp_path, monkeypatch):
        users = {
            "alice": f"{hash_password('op')}:{OPERATOR}",
            "bob": f"{hash_password('view')}:{VIEWER}",
        }
        app = _make_app(tmp_path, users)
        client = _client(app)
        _login(client, "bob", "view")
        payload = _payload(branch_results={"feat/ok": _branch("feat/ok", "SUCCESS")})
        _mount_cycle(monkeypatch, payload)
        r = client.get(f"/task/{_CYCLE}/feat/ok")
        assert r.status_code == 200
        assert 'data-push-target="feat/ok"' not in r.text
        assert "WebSSH" not in r.text
        assert 'action="/rerun"' not in r.text
        assert 'data-abandon-target="feat/ok"' not in r.text

    def test_detail_missing_branch_404(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        payload = _payload(branch_results={})
        _mount_cycle(monkeypatch, payload)
        r = client.get(f"/task/{_CYCLE}/nope")
        assert r.status_code == 404

    def test_detail_unauthenticated_redirects_to_login(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _mount_cycle(monkeypatch, _payload(branch_results={"x": _branch("x", "SUCCESS")}))
        r = client.get(f"/task/{_CYCLE}/x")
        assert r.status_code == 302
        assert r.headers["location"].endswith("/login")
