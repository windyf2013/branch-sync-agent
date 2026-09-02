"""操作 API 与表单接线测试：触发同步/重跑、任务状态轮询、并发拒绝、权限。"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from bsa_web.app import create_app
from bsa_web.auth import hash_password
from bsa_web.rbac import OPERATOR, VIEWER
from bsa_web.runner import TaskRunner


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


def _install_runner(app, run_func):
    """注入内存 TaskRunner 作为执行者：web 提交经 app.state.enqueue_task 委托给它。

    wrapped 模拟 V1 引擎行为：先经 task_reporter 写 succeeded 终态（读 env 注入的
    BSA_TASK_ID/LOG_DIR），再透传调用原 run_func（兼容单参数）。
    """
    from bsa.commands.task_reporter import register_finish

    def wrapped(cmd, env):
        try:
            result = run_func(cmd)
        except TypeError:
            result = run_func(cmd, env)
        try:
            register_finish(
                env["LOG_DIR"], int(env["BSA_TASK_ID"]),
                state="succeeded", cycle_id="cycle-test",
            )
        except Exception:
            pass
        return result

    from pathlib import Path

    from bsa_web.db import init_db

    # 用独立连接（与生产 executor 独立进程一致），避免与 web 请求线程共享同一
    # 连接导致的并发 execute 竞态（sqlite3.InterfaceError 偶发）。
    runner_db = init_db(Path(app.state.settings.log_dir) / "platform.sqlite3")
    runner = TaskRunner(runner_db, str(app.state.settings.log_dir), run_func=wrapped)
    runner.start()
    app.state.enqueue_task = lambda db, kind, user, target, **kw: runner.submit(
        kind, user, target, **kw
    )
    app.state.get_task = lambda db, task_id: runner.get(task_id)
    return runner


def _wait_state(client, task_id: int, wanted: str, timeout: float = 8) -> bool:
    """轮询任务状态直到命中 wanted。

    超时必须严格小于 worker 侧 ``release.wait(60)`` 的自释放时限：观察侧要能在
    worker 自释放前就观察到 running 并主动 release.set()。两者接近或相等时，
    满载下 worker 被调度饿死先自释放、running 窗口消失，观察侧等不到而 flaky。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/tasks/{task_id}")
        assert r.status_code == 200
        if r.json()["state"] == wanted:
            return True
        time.sleep(0.02)
    return False


class TestSyncApi:
    def test_operator_triggers_sync_201(self, tmp_path):
        app = _make_app(tmp_path)
        runner = _install_runner(app, run_func=lambda cmd: (0, "", ""))
        client = _client(app)
        _login(client)
        r = client.post(
            "/api/sync",
            json={"src": "main", "target": "feat/x", "_csrf": _csrf(client)},
        )
        assert r.status_code == 201
        body = r.json()
        assert "task_id" in body
        assert body["state"] == "queued"
        runner.join(timeout=5)
        assert runner.get(body["task_id"])["state"] == "succeeded"

    def test_sync_with_sha_list(self, tmp_path):
        app = _make_app(tmp_path)
        calls = []
        runner = _install_runner(
            app, run_func=lambda cmd: (calls.append(cmd) or (0, "", ""))
        )
        client = _client(app)
        _login(client)
        r = client.post(
            "/api/sync",
            json={"src": "main", "target": "feat/x", "sha": ["abc123"], "_csrf": _csrf(client)},
        )
        assert r.status_code == 201
        runner.join(timeout=5)
        assert "--sha" in calls[0]
        assert "abc123" in calls[0]

    def test_viewer_forbidden(self, tmp_path):
        users = {
            "alice": f"{hash_password('op')}:{OPERATOR}",
            "bob": f"{hash_password('view')}:{VIEWER}",
        }
        app = _make_app(tmp_path, users=users)
        _install_runner(app, run_func=lambda cmd: (0, "", ""))
        client = _client(app)
        _login(client, "bob", "view")
        r = client.post(
            "/api/sync",
            json={"src": "main", "target": "feat/x", "_csrf": _csrf(client)},
        )
        assert r.status_code == 403

    def test_operator_without_csrf_rejected(self, tmp_path):
        app = _make_app(tmp_path)
        _install_runner(app, run_func=lambda cmd: (0, "", ""))
        client = _client(app)
        _login(client)
        r = client.post("/api/sync", json={"src": "main", "target": "feat/x"})
        assert r.status_code == 403

    def test_missing_fields_422(self, tmp_path):
        app = _make_app(tmp_path)
        _install_runner(app, run_func=lambda cmd: (0, "", ""))
        client = _client(app)
        _login(client)
        r = client.post("/api/sync", json={"_csrf": _csrf(client)})
        assert r.status_code == 422


class TestRerunApi:
    def test_rerun_returns_task_id(self, tmp_path):
        app = _make_app(tmp_path)
        calls = []
        runner = _install_runner(
            app, run_func=lambda cmd: (calls.append(cmd) or (0, "", ""))
        )
        client = _client(app)
        _login(client)
        r = client.post(
            "/api/rerun",
            json={"target": "feat/x", "fresh": True, "_csrf": _csrf(client)},
        )
        assert r.status_code == 201
        assert "task_id" in r.json()
        runner.join(timeout=5)
        assert calls[0][-1] == "--fresh"


class TestTaskStatus:
    def test_status_polling_transitions(self, tmp_path):
        app = _make_app(tmp_path)
        release = threading.Event()

        def fake_run(cmd):
            # 自释放是保险丝（主线程在 release.set() 前崩溃时 worker 不能永久阻塞），
            # 但值必须远大于观察侧 _wait_state 的 8s 超时——否则满载下 worker 先
            # 自释放、running 窗口消失，观察侧等不到 running 而 flaky。
            release.wait(60)
            return 0, "", ""

        _install_runner(app, run_func=fake_run)
        client = _client(app)
        _login(client)
        r = client.post(
            "/api/sync",
            json={"src": "main", "target": "feat/x", "_csrf": _csrf(client)},
        )
        task_id = r.json()["task_id"]
        assert _wait_state(client, task_id, "running")
        release.set()
        assert _wait_state(client, task_id, "succeeded")

    def test_status_requires_login(self, tmp_path):
        app = _make_app(tmp_path)
        client = _client(app)
        r = client.get("/api/tasks/1")
        assert r.status_code == 302
        assert r.headers["location"].endswith("/login")

    def test_status_viewer_allowed(self, tmp_path):
        users = {"bob": f"{hash_password('view')}:{VIEWER}"}
        app = _make_app(tmp_path, users=users)
        runner = _install_runner(app, run_func=lambda cmd: (0, "", ""))
        client = _client(app)
        _login(client, "bob", "view")
        task_id = runner.submit("sync", "alice", "feat/x", src="main")
        runner.join(timeout=5)
        r = client.get(f"/api/tasks/{task_id}")
        assert r.status_code == 200
        assert r.json()["state"] == "succeeded"

    def test_status_missing_404(self, tmp_path):
        app = _make_app(tmp_path)
        _install_runner(app, run_func=lambda cmd: (0, "", ""))
        client = _client(app)
        _login(client)
        r = client.get("/api/tasks/99999")
        assert r.status_code == 404


class TestConcurrency:
    def test_same_target_api_409(self, tmp_path):
        app = _make_app(tmp_path)
        release = threading.Event()

        def fake_run(cmd):
            release.wait(5)
            return 0, "", ""

        runner = _install_runner(app, run_func=fake_run)
        client = _client(app)
        _login(client)
        csrf = _csrf(client)
        first = client.post(
            "/api/sync", json={"src": "main", "target": "feat/x", "_csrf": csrf}
        )
        assert first.status_code == 201
        second = client.post(
            "/api/sync", json={"src": "main", "target": "feat/x", "_csrf": csrf}
        )
        assert second.status_code == 409
        release.set()
        runner.join(timeout=5)


class TestFormWiring:
    def test_form_sync_redirects_to_task_page(self, tmp_path):
        app = _make_app(tmp_path)
        calls = []
        runner = _install_runner(
            app, run_func=lambda cmd: (calls.append(cmd) or (0, "", ""))
        )
        client = _client(app)
        _login(client)
        r = client.post(
            "/sync", data={"src": "main", "target": "feat/x", "_csrf": _csrf(client)}
        )
        assert r.status_code == 303
        assert re.match(r"/tasks/\d+", r.headers["location"])
        runner.join(timeout=5)
        assert calls[0][3] == "sync"

    def test_form_rerun_with_fresh_checkbox(self, tmp_path):
        app = _make_app(tmp_path)
        calls = []
        runner = _install_runner(
            app, run_func=lambda cmd: (calls.append(cmd) or (0, "", ""))
        )
        client = _client(app)
        _login(client)
        r = client.post(
            "/rerun",
            data={"target": "feat/x", "fresh": "on", "_csrf": _csrf(client)},
        )
        assert r.status_code == 303
        assert re.match(r"/tasks/\d+", r.headers["location"])
        runner.join(timeout=5)
        assert calls[0][-1] == "--fresh"

    def test_form_sync_busy_redirects_with_error(self, tmp_path):
        app = _make_app(tmp_path)
        release = threading.Event()

        def fake_run(cmd):
            release.wait(5)
            return 0, "", ""

        runner = _install_runner(app, run_func=fake_run)
        client = _client(app)
        _login(client)
        csrf = _csrf(client)
        first = client.post(
            "/sync", data={"src": "main", "target": "feat/x", "_csrf": csrf}
        )
        assert first.status_code == 303
        second = client.post(
            "/sync", data={"src": "main", "target": "feat/x", "_csrf": csrf}
        )
        assert second.status_code == 303
        assert second.headers["location"] == "/?error=busy"
        release.set()
        runner.join(timeout=5)

    def test_form_viewer_forbidden(self, tmp_path):
        users = {
            "alice": f"{hash_password('op')}:{OPERATOR}",
            "bob": f"{hash_password('view')}:{VIEWER}",
        }
        app = _make_app(tmp_path, users=users)
        _install_runner(app, run_func=lambda cmd: (0, "", ""))
        client = _client(app)
        _login(client, "bob", "view")
        r = client.post(
            "/sync", data={"src": "main", "target": "feat/x", "_csrf": _csrf(client)}
        )
        assert r.status_code == 403

    def test_form_without_csrf_rejected(self, tmp_path):
        app = _make_app(tmp_path)
        _install_runner(app, run_func=lambda cmd: (0, "", ""))
        client = _client(app)
        _login(client)
        r = client.post("/sync", data={"src": "main", "target": "feat/x"})
        assert r.status_code == 403

    def test_task_status_page_renders(self, tmp_path):
        app = _make_app(tmp_path)
        runner = _install_runner(app, run_func=lambda cmd: (0, "", ""))
        client = _client(app)
        _login(client)
        task_id = runner.submit("sync", "alice", "feat/x", src="main")
        runner.join(timeout=5)
        r = client.get(f"/tasks/{task_id}")
        assert r.status_code == 200
        assert "feat/x" in r.text
        assert "成功" in r.text

    def test_task_status_page_404(self, tmp_path):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        r = client.get("/tasks/99999")
        assert r.status_code == 404

    def test_task_status_page_shows_cycle_id_and_live_log(self, tmp_path):
        from datetime import UTC, datetime
        from pathlib import Path

        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        db = app.state.db
        db.execute(
            "INSERT INTO tasks(kind,user,target,cycle_id,state,shas,created_at,source) "
            "VALUES ('sync','alice','feat/x','manual-test-1','running','[\"a1\",\"a2\"]',?,'web')",
            (datetime.now(UTC).isoformat(),),
        )
        db.commit()
        task_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        log_dir = app.state.settings.log_dir
        log = Path(log_dir) / "tasks" / f"task-{task_id}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("line1\nline2\n", encoding="utf-8")

        r = client.get(f"/tasks/{task_id}")

        assert r.status_code == 200
        assert "manual-test-1" in r.text
        assert "line2" in r.text
        assert "commit 数：2" in r.text
        assert "实时输出" in r.text

    def _insert_task(self, app, state: str):
        from datetime import UTC, datetime

        db = app.state.db
        db.execute(
            "INSERT INTO tasks(kind,user,target,cycle_id,state,created_at,source) "
            "VALUES ('sync','alice','feat/x','manual-test-2',?,?,'web')",
            (state, datetime.now(UTC).isoformat()),
        )
        db.commit()
        return db.execute("SELECT last_insert_rowid()").fetchone()[0]

    def test_running_task_page_has_return_to_workbench_button(self, tmp_path):
        """执行中也必须有可见的返回工作台入口（此前只有已完成分支才有裸链接）。"""
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        task_id = self._insert_task(app, "running")

        html = client.get(f"/tasks/{task_id}").text

        assert "返回工作台" in html
        assert re.search(r'<a[^>]+href="/"[^>]*class="[^"]*btn', html) or re.search(
            r'<a[^>]+class="[^"]*btn[^"]*"[^>]+href="/"', html
        ), "返回工作台应是 btn 样式按钮，不是裸链接"

    def test_finished_task_page_has_return_to_workbench_button(self, tmp_path):
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        task_id = self._insert_task(app, "failed")

        html = client.get(f"/tasks/{task_id}").text

        assert "返回工作台" in html

    def test_task_page_shows_step_progress(self, tmp_path):
        """有 cycle_id 的任务页渲染步骤清单（含耗时与中文步骤名）。"""
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        task_id = self._insert_task(app, "running")
        # 手动写入进度文件（引擎侧由 node_wrapper 生成，此处直接造数据）
        cycle_dir = Path(app.state.settings.log_dir) / "manual-test-2"
        cycle_dir.mkdir(parents=True, exist_ok=True)
        (cycle_dir / "progress.jsonl").write_text(
            "\n".join(
                [
                    json.dumps({"cycle_id": "manual-test-2", "node": "prepare_worktree",
                                "step": "建立 worktree", "phase": "start", "ts": 1000.0}),
                    json.dumps({"cycle_id": "manual-test-2", "node": "prepare_worktree",
                                "step": "建立 worktree", "phase": "end", "status": "PREPARED",
                                "ts": 1003.5}),
                    json.dumps({"cycle_id": "manual-test-2", "node": "build", "step": "编译",
                                "model": "2600m", "phase": "start", "ts": 1004.0}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        html = client.get(f"/tasks/{task_id}").text

        assert "步骤进度" in html
        assert "建立 worktree" in html
        assert "3.5s" in html  # 1003.5 - 1000.0
        assert "2600m" in html
        assert "进行中" in html

    def test_running_step_shows_live_build_log(self, tmp_path):
        """运行中步骤的编译日志尾部直接展示（非折叠），供每 10s 刷新实时滚动。"""
        app = _make_app(tmp_path)
        client = _client(app)
        _login(client)
        task_id = self._insert_task(app, "running")
        cycle_dir = Path(app.state.settings.log_dir) / "manual-test-2"
        build_dir = cycle_dir / "build" / "feat/x" / "abc"
        build_dir.mkdir(parents=True, exist_ok=True)
        log_path = str(build_dir / "build.log")
        (build_dir / "build.log").write_text(
            "\n".join(f"make[{i}]: compiling file{i}.c" for i in range(120)),
            encoding="utf-8",
        )
        (cycle_dir / "progress.jsonl").write_text(
            json.dumps({"cycle_id": "manual-test-2", "node": "build", "step": "编译",
                        "model": "5200", "phase": "start", "log_path": log_path})
            + "\n",
            encoding="utf-8",
        )

        html = client.get(f"/tasks/{task_id}").text

        # 运行中日志直接展示（非 <details> 折叠）
        assert "step-live-log" in html
        assert "compiling file119.c" in html  # 日志尾部最后一行
        assert "已截断，随刷新更新" in html

    def test_workbench_shows_busy_error(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        _install_runner(app, run_func=lambda cmd: (0, "", ""))
        client = _client(app)
        _login(client)
        r = client.get("/?error=busy")
        assert r.status_code == 200
        assert "已有任务" in r.text
