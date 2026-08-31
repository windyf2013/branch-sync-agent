import re

from fastapi.testclient import TestClient

from bsa_web.app import create_app
from bsa_web.auth import hash_password
from bsa_web.rbac import OPERATOR


def _make_app(tmp_path):
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


def _record(cycle_id, status, started_at):
    return {
        "cycle_id": cycle_id,
        "status": status,
        "report_path": None,
        "mail_status": None,
        "started_at": started_at,
        "finished_at": started_at,
    }


def _commit_info(sha, patch_text="+x"):
    return {
        "sha": sha,
        "message": f"msg-{sha}",
        "author": "dev",
        "committed_at": "2026-08-20T10:00:00+08:00",
        "changed_files": ["plat/demo.c"],
        "patch_text": patch_text,
        "symbols": [],
        "patch_id": f"pid-{sha}",
        "issue_ids": [],
        "source_branch": "develop",
        "homologous_section": "组网",
    }


def _conclusion(kind="NeedSync"):
    return {"kind": kind, "evidence": [f"ev-{kind}"], "confidence": "high"}


def _build_outcome(model="RTL9617C", status="FAILED", log_path=None, errors=None):
    return {
        "model": model,
        "status": status,
        "log_path": log_path,
        "errors": list(errors or []),
        "agent_attempts": 1,
        "fix_diff": None,
    }


def _commit_result(sha, cherry_pick="OK", build=None, conflict_resolution=None):
    return {
        "sha": sha,
        "cherry_pick": cherry_pick,
        "conflict_resolution": conflict_resolution,
        "build": build or {},
    }


def _branch(target, status="SUCCESS", commits=None, patch_path=None):
    return {
        "target_branch": target,
        "worktree_path": f"/wt/{target}",
        "status": status,
        "commits": commits or [],
        "patch_path": patch_path,
        "stop_reason": None,
    }


def _payload(**kw):
    base = {
        "cycle_id": "cycle-2026-08-20",
        "status": "REPORTED",
        "scan_window": ["2026-08-19T22:00:00+08:00", "2026-08-20T22:00:00+08:00"],
        "detected_commits": [],
        "decisions": {},
        "branch_results": {},
        "action_required": [],
    }
    base.update(kw)
    return base


class TestHistory:
    def _seed_tasks(self, app):
        """造 tasks 行：超期终态/活动/窗口内终态 各若干，含 shas。"""
        db = app.state.db
        rows = [
            # (kind, target, src, state, cycle_id, created_at, finished_at, shas)
            # 独立 sync 主任务（succeeded）
            ("sync", "br_develop", "br_fttr", "succeeded", "manual-old", "2026-08-19T10:00:00+00:00", "2026-08-19T11:00:00+00:00", '["a1","a2"]'),
            # sync 失败 + rerun 成功 → 归并后主任务取 rerun 最终结果（succeeded）
            ("sync", "br_retry", "br_fttr", "failed", "manual-retry", "2026-08-18T10:00:00+00:00", "2026-08-18T11:00:00+00:00", '["b1"]'),
            ("rerun", "br_retry", None, "succeeded", "rerun-retry", "2026-08-18T11:30:00+00:00", "2026-08-18T12:00:00+00:00", None),
            # 同一 cycle_id 多次失败尝试 → 折叠成一行（取最后一次）
            ("cycle", None, None, "failed", "cycle-old", "2026-08-17T09:00:00+00:00", "2026-08-17T09:10:00+00:00", None),
            ("cycle", None, None, "failed", "cycle-old", "2026-08-17T10:00:00+00:00", "2026-08-17T10:10:00+00:00", None),
            # 活动任务不应归档
            ("sync", "br_active", "br_fttr", "running", "manual-active", "2026-08-25T10:00:00+00:00", None, '["c1"]'),
            # 窗口内终态（未超期）不应归档
            ("sync", "br_recent", "br_fttr", "succeeded", "manual-recent", "2026-08-21T10:00:00+00:00", "2026-08-21T11:00:00+00:00", '["d1"]'),
        ]
        for kind, target, src, state, cycle_id, created, finished, shas in rows:
            db.execute(
                "INSERT INTO tasks(kind, user, target, src, fresh, state, error, "
                "cycle_id, created_at, finished_at, source, shas) "
                "VALUES (?,?,?,?,0,?,NULL,?,?,?,'web',?)",
                (kind, "alice", target, src, state, cycle_id, created, finished, shas),
            )
        db.commit()

    def test_history_lists_archived_tasks(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        self._seed_tasks(app)
        monkeypatch.setattr(
            "bsa_web.projection.window_start", lambda log_dir: "2026-08-20T22:00:00+08:00"
        )
        r = client.get("/history")
        assert r.status_code == 200
        # 超期终态任务出现在历史
        assert "br_develop" in r.text
        assert "cycle-old" in r.text
        # 活动任务不进历史
        assert "br_active" not in r.text
        # 窗口内终态任务不进历史
        assert "br_recent" not in r.text

    def test_history_dedups_cycle_and_merges_rerun(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        self._seed_tasks(app)
        monkeypatch.setattr(
            "bsa_web.projection.window_start", lambda log_dir: "2026-08-20T22:00:00+08:00"
        )
        r = client.get("/history")
        assert r.status_code == 200
        # rerun 不单列（无 rerun 类型的独立行），而是归并到 sync 主任务
        assert "rerun-retry" in r.text  # 归并后主任务跳转到 rerun 的 cycle_id 详情
        # 归并后主任务取 rerun 最终结果：br_retry 显示成功
        assert "br_retry" in r.text
        assert "/task/rerun-retry/br_retry" in r.text
        # 同一 cycle_id 只渲染一行（href + 展示文本各出现一次 = 2 次；两行会变 4 次）
        assert r.text.count("cycle-old") == 2

    def test_history_has_no_conclusion_filters(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        self._seed_tasks(app)
        monkeypatch.setattr(
            "bsa_web.projection.window_start", lambda log_dir: "2026-08-20T22:00:00+08:00"
        )
        r = client.get("/history")
        assert r.status_code == 200
        # 不再出现「待同步/已包含」等操作型结论标签
        assert "待同步" not in r.text
        assert "已包含" not in r.text
        assert "按结论筛选" not in r.text

    def test_history_task_links(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        self._seed_tasks(app)
        monkeypatch.setattr(
            "bsa_web.projection.window_start", lambda log_dir: "2026-08-20T22:00:00+08:00"
        )
        r = client.get("/history")
        assert r.status_code == 200
        # cycle → 周期概览
        assert "/cycle/cycle-old" in r.text
        # sync → 任务详情
        assert "/task/manual-old/br_develop" in r.text

    def test_history_empty_when_no_window(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        self._seed_tasks(app)
        monkeypatch.setattr("bsa_web.projection.window_start", lambda log_dir: None)
        r = client.get("/history")
        assert r.status_code == 200
        assert "暂无周期任务" in r.text
        assert "暂无同步任务" in r.text

    def test_history_unauthenticated_redirects_to_login(self, tmp_path):
        client = _client(_make_app(tmp_path))
        r = client.get("/history")
        assert r.status_code == 302
        assert r.headers["location"].endswith("/login")


class TestCycleDetail:
    def test_cycle_overview_three_blocks(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        payload = _payload(
            detected_commits=[_commit_info("a1")],
            decisions={"a1": {"t": _conclusion("ManualReview")}},
            branch_results={"t": _branch("t", "SUCCESS", commits=[_commit_result("a1")])},
            action_required=[
                {"sha": "a1", "branch": "t", "kind": "ManualReview", "evidence": ["ev"]}
            ],
        )
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        r = client.get("/cycle/cycle-2026-08-20")
        assert r.status_code == 200
        assert "需人工处理" in r.text
        assert "已成功同步" in r.text
        assert "检测信息" in r.text
        assert "a1" in r.text
        assert "t" in r.text

    def test_cycle_overview_shows_sources_targets_and_summary(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        payload = _payload(
            sources=["br_fttr", "br_msg"],
            targets=["br_develop"],
            detected_commits=[
                _commit_info("a1"),
            ],
            decisions={"a1": {"br_develop": _conclusion("OutOfScope")}},
        )
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        r = client.get("/cycle/cycle-2026-08-20")
        assert r.status_code == 200
        assert "整体任务信息" in r.text
        assert "br_fttr" in r.text
        assert "br_msg" in r.text
        assert "br_develop" in r.text
        assert "检测" in r.text
        assert "跳过" in r.text

    def test_cycle_overview_groups_detection_by_source(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        payload = _payload(
            sources=["br_fttr", "br_msg"],
            detected_commits=[
                _commit_info("a1"),
            ],
        )
        payload["detected_commits"][0]["source_branch"] = "br_fttr"
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        r = client.get("/cycle/cycle-2026-08-20")
        assert r.status_code == 200
        assert "br_msg" in r.text
        assert "0 个 commit" in r.text

    def test_cycle_detail_running_shows_friendly_notice(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        monkeypatch.setattr(
            "bsa_web.projection.load_cycle", lambda log_dir, cid: {"status": "running"}
        )
        r = client.get("/cycle/cycle-2026-08-24")
        assert r.status_code == 200
        assert "进行中" in r.text

    def test_cycle_detail_missing_cycle_404(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: None)
        r = client.get("/cycle/cycle-2099-01-01")
        assert r.status_code == 404

    def test_cycle_detail_unauthenticated_redirects_to_login(self, tmp_path):
        client = _client(_make_app(tmp_path))
        r = client.get("/cycle/cycle-2026-08-20")
        assert r.status_code == 302
        assert r.headers["location"].endswith("/login")


class TestTargetDetail:
    def test_target_detail_renders_build_log_preview_and_download_links(
        self, tmp_path, monkeypatch
    ):
        client = _client(_make_app(tmp_path))
        _login(client)
        branch = _branch(
            "t",
            "PARTIAL",
            commits=[
                _commit_result(
                    "a1",
                    cherry_pick="CONFLICT",
                    build={"RTL9617C": _build_outcome(log_path="/logs/x.log")},
                )
            ],
            patch_path="/tmp/b.patch",
        )
        payload = _payload(branch_results={"t": branch})
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        monkeypatch.setattr(
            "bsa_web.build_labels._read_build_log",
            lambda log_dir, log_path, max_lines=100: (
                "line1\nline2\nline3\nline4\nline5\nline6",
                True,
            ),
        )
        r = client.get("/task/cycle-2026-08-20/t")
        assert r.status_code == 200
        assert "line2" in r.text
        assert "下载" in r.text
        assert "/cycle/cycle-2026-08-20/target/t/patch" in r.text
        assert "build-log" in r.text

    def test_target_detail_renders_baseline_failure(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        branch = _branch("t", "FAILED")
        branch["baseline"] = {
            "2600m": _build_outcome(
                "2600m", "FAILED", log_path="/logs/baseline.log",
                errors=["fatal: unable to auto-detect email address"],
            )
        }
        payload = _payload(branch_results={"t": branch})
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        monkeypatch.setattr(
            "bsa_web.build_labels._read_build_log",
            lambda log_dir, log_path, max_lines=100: (
                "fatal: unable to auto-detect email address",
                False,
            ),
        )
        r = client.get("/task/cycle-2026-08-20/t")
        assert r.status_code == 200
        assert "基线编译" in r.text
        assert "auto-detect email" in r.text
        assert "/cycle/cycle-2026-08-20/target/t/baseline/2600m/log" in r.text

    def test_target_detail_readonly_redirects_to_task(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        payload = _payload(branch_results={"t": _branch("t", "SUCCESS")})
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        r = client.get("/cycle/cycle-2026-08-20/target/t", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/task/cycle-2026-08-20/t"

    def test_target_detail_missing_target_404(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        payload = _payload(branch_results={})
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        # 只读路由 302 到权威页，权威页再因目标不存在而 404
        r = client.get("/task/cycle-2026-08-20/nope")
        assert r.status_code == 404

    def test_baseline_log_download_serves_file(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        log_file = tmp_path / "baseline.log"
        log_file.write_text("baseline-log-content", encoding="utf-8")
        branch = _branch("t", "FAILED")
        branch["baseline"] = {
            "2600m": _build_outcome("2600m", "FAILED", log_path=str(log_file))
        }
        payload = _payload(branch_results={"t": branch})
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        r = client.get("/cycle/cycle-2026-08-20/target/t/baseline/2600m/log")
        assert r.status_code == 200
        assert r.text == "baseline-log-content"

    def test_baseline_log_download_rejects_path_outside_log_dir(
        self, tmp_path, monkeypatch
    ):
        client = _client(_make_app(tmp_path))
        _login(client)
        outside = tmp_path.parent / "baseline.log"
        outside.write_text("secret", encoding="utf-8")
        branch = _branch("t", "FAILED")
        branch["baseline"] = {
            "2600m": _build_outcome("2600m", "FAILED", log_path=str(outside))
        }
        payload = _payload(branch_results={"t": branch})
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        r = client.get("/cycle/cycle-2026-08-20/target/t/baseline/2600m/log")
        assert r.status_code == 404
        assert "secret" not in r.text

    def test_target_patch_download_serves_file(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        patch_file = tmp_path / "b.patch"
        patch_file.write_text("patch-content", encoding="utf-8")
        payload = _payload(
            branch_results={"t": _branch("t", "SUCCESS", patch_path=str(patch_file))}
        )
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        r = client.get("/cycle/cycle-2026-08-20/target/t/patch")
        assert r.status_code == 200
        assert r.text == "patch-content"

    def test_target_patch_download_rejects_path_outside_log_dir(
        self, tmp_path, monkeypatch
    ):
        client = _client(_make_app(tmp_path))
        _login(client)
        outside = tmp_path.parent / "outside.patch"
        outside.write_text("secret", encoding="utf-8")
        payload = _payload(
            branch_results={"t": _branch("t", "SUCCESS", patch_path=str(outside))}
        )
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        r = client.get("/cycle/cycle-2026-08-20/target/t/patch")
        assert r.status_code == 404
        assert "secret" not in r.text

    def test_build_log_download_serves_file(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        log_file = tmp_path / "build.log"
        log_file.write_text("log-content", encoding="utf-8")
        branch = _branch(
            "t",
            "SUCCESS",
            commits=[
                _commit_result(
                    "a1", build={"RTL9617C": _build_outcome(log_path=str(log_file))}
                )
            ],
        )
        payload = _payload(branch_results={"t": branch})
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        r = client.get(
            "/cycle/cycle-2026-08-20/target/t/commit/a1/build/RTL9617C/log"
        )
        assert r.status_code == 200
        assert r.text == "log-content"

    def test_build_log_download_rejects_path_outside_log_dir(
        self, tmp_path, monkeypatch
    ):
        client = _client(_make_app(tmp_path))
        _login(client)
        outside = tmp_path.parent / "build.log"
        outside.write_text("secret", encoding="utf-8")
        branch = _branch(
            "t",
            "SUCCESS",
            commits=[
                _commit_result(
                    "a1", build={"RTL9617C": _build_outcome(log_path=str(outside))}
                )
            ],
        )
        payload = _payload(branch_results={"t": branch})
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        r = client.get(
            "/cycle/cycle-2026-08-20/target/t/commit/a1/build/RTL9617C/log"
        )
        assert r.status_code == 404
        assert "secret" not in r.text


class TestCommitDetail:
    def test_commit_detail_renders_patch_and_conclusion(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        patch = "diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new\n"
        payload = _payload(
            detected_commits=[_commit_info("a1", patch_text=patch)],
            decisions={"a1": {"t": _conclusion("NeedSync")}},
        )
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        r = client.get("/cycle/cycle-2026-08-20/commit/a1")
        assert r.status_code == 200
        assert "+new" in r.text
        assert "NeedSync" in r.text
        assert "下载" in r.text
        # 单 commit 的改动展示不叫 patch，叫 diff
        assert "diff" in r.text

    def test_commit_detail_renders_conflict_resolution_error(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        branch = _branch(
            "t",
            "PARTIAL",
            commits=[_commit_result("a1", cherry_pick="CONFLICT")],
        )
        branch["commits"][0]["resolution_error"] = "冲突文件 x 疑似二进制"
        payload = _payload(
            detected_commits=[_commit_info("a1")],
            branch_results={"t": branch},
        )
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        r = client.get("/cycle/cycle-2026-08-20/commit/a1")
        assert r.status_code == 200
        assert "处理结果" in r.text
        assert "CONFLICT" in r.text
        assert "冲突解决失败" in r.text
        assert "疑似二进制" in r.text

    def test_commit_detail_missing_commit_404(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        payload = _payload(detected_commits=[])
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        r = client.get("/cycle/cycle-2026-08-20/commit/unknown")
        assert r.status_code == 404

    def test_commit_patch_download_serves_patch(self, tmp_path, monkeypatch):
        client = _client(_make_app(tmp_path))
        _login(client)
        payload = _payload(detected_commits=[_commit_info("a1", "full-patch-content")])
        monkeypatch.setattr("bsa_web.projection.load_cycle", lambda log_dir, cid: payload)
        r = client.get("/cycle/cycle-2026-08-20/commit/a1/patch")
        assert r.status_code == 200
        assert r.text == "full-patch-content"


class TestBuildLogHelper:
    def test_read_build_log_truncates_long_file(self, tmp_path):
        from bsa_web.views.detail import _read_build_log

        log = tmp_path / "build.log"
        log.write_text("\n".join(f"line {i}" for i in range(600)), encoding="utf-8")
        text, truncated = _read_build_log(str(tmp_path), str(log), max_lines=500)
        assert truncated is True
        assert text.splitlines()[0] == "line 100"
        assert text.splitlines()[-1] == "line 599"

    def test_read_build_log_none_when_missing(self, tmp_path):
        from bsa_web.views.detail import _read_build_log

        assert _read_build_log(str(tmp_path), str(tmp_path / "missing.log")) is None
        assert _read_build_log(str(tmp_path), None) is None

    def test_read_build_log_rejects_path_outside_log_dir(self, tmp_path):
        from bsa_web.views.detail import _read_build_log

        outside = tmp_path.parent / "secret.log"
        outside.write_text("secret", encoding="utf-8")
        assert _read_build_log(str(tmp_path), str(outside)) is None
