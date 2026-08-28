"""进度埋点（graph/progress.py + node_wrapper）+ 平台 read_progress 测试。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from bsa.graph.nodes import node_wrapper


def _ctx(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(settings=SimpleNamespace(log_dir=str(tmp_path)))


def _lines(tmp_path: Path, cycle_id: str = "c1") -> list[dict]:
    path = Path(tmp_path) / cycle_id / "progress.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_journal_creates_cycle_dir_on_first_write(tmp_path):
    """手动同步只 mkdir(log_dir)，cycle 子目录由 journal 自建。"""
    from bsa.graph.progress import ProgressJournal

    ProgressJournal(str(tmp_path)).write(
        {"cycle_id": "manual-x", "phase": "start", "step": "建立 worktree"}
    )
    assert (tmp_path / "manual-x" / "progress.jsonl").exists()


def test_journal_write_failure_never_raises(tmp_path):
    from bsa.graph.progress import ProgressJournal

    # 目录位置是一个文件 → mkdir 必失败；write 必须吞掉而非抛
    blocker = tmp_path / "cycle-blocked"
    blocker.write_text("x", encoding="utf-8")
    ProgressJournal(str(tmp_path)).write(
        {"cycle_id": "cycle-blocked", "phase": "start", "step": "建立 worktree"}
    )


def test_node_wrapper_emits_start_and_end_with_duration(tmp_path):
    def fake_node(state, ctx):
        return {"status": "PREPARED"}

    fake_node.__name__ = "prepare_worktree"  # 用真实节点名，验证 NODE_LABELS 映射
    wrapped = node_wrapper(fake_node, ctx=_ctx(tmp_path))
    state = {"cycle_id": "c1", "current_target": "t", "current_commit": "abc"}
    result = wrapped(state)

    assert result == {"status": "PREPARED"}
    recs = _lines(tmp_path)
    assert [r["phase"] for r in recs] == ["start", "end"]
    start, end = recs
    assert start["step"] == "建立 worktree"
    assert start["target"] == "t" and start["sha"] == "abc"
    assert end["status"] == "PREPARED"
    assert end["duration_ms"] >= 0
    assert end["step"] == "建立 worktree"


def test_node_wrapper_emits_failed_status_on_exception(tmp_path):
    def fake_node(state, ctx):
        raise RuntimeError("boom")

    wrapped = node_wrapper(fake_node, ctx=_ctx(tmp_path))
    result = wrapped({"cycle_id": "c1", "current_target": "t", "current_commit": None})

    assert result["status"] == "FAILED"
    recs = _lines(tmp_path)
    assert [r["phase"] for r in recs] == ["start", "end"]
    assert recs[1]["status"] == "FAILED"


def test_progress_failure_never_alters_node_result(tmp_path, monkeypatch):
    """journal 抛异常也不许改变节点返回值（埋点对节点透明）。"""
    from bsa.graph import nodes

    def boom_write(self, record):
        raise OSError("disk full")

    monkeypatch.setattr(nodes.ProgressJournal, "write", boom_write)

    def fake_node(state, ctx):
        return {"status": "PREPARED"}

    wrapped = node_wrapper(fake_node, ctx=_ctx(tmp_path))
    assert wrapped({"cycle_id": "c1"}) == {"status": "PREPARED"}


def test_build_node_records_model(tmp_path):
    """build 节点记录当前编译型号（编译 a / 编译 b 的粒度来源）。"""

    def fake_build(state, ctx):
        return {"status": "BUILD_OK"}

    fake_build.__name__ = "build"  # 真实节点名，验证 build 走 _next_model 取型号
    wrapped = node_wrapper(fake_build, ctx=_ctx(tmp_path))
    state = {
        "cycle_id": "c1",
        "current_target": "t",
        "current_commit": None,
        "build_models": {"t": ["5200", "5200B"]},
    }
    wrapped(state)

    recs = _lines(tmp_path)
    assert recs[0]["model"] == "5200"  # 第一个未编译的型号
    assert recs[0]["step"] == "编译"


def test_selector_nodes_not_instrumented(tmp_path):
    """next_branch/next_commit 以 ctx=None 包装，选择器不算步骤、不写记录。"""

    def fake_selector(state):
        return {"current_target": "t"}

    wrapped = node_wrapper(fake_selector, ctx=None)
    wrapped({"cycle_id": "c1"})
    assert _lines(tmp_path) == []


def test_read_progress_pairs_and_tolerates_partial_line(tmp_path):
    from bsa_web.progress import read_progress

    cycle = tmp_path / "c1"
    cycle.mkdir()
    (cycle / "progress.jsonl").write_text(
        json.dumps({"cycle_id": "c1", "node": "prepare_worktree", "step": "建立 worktree",
                    "phase": "start"})
        + "\n"
        + json.dumps({"cycle_id": "c1", "node": "prepare_worktree", "step": "建立 worktree",
                      "phase": "end", "status": "PREPARED", "duration_ms": 42})
        + "\n"
        + json.dumps({"cycle_id": "c1", "node": "build", "step": "编译", "model": "5200",
                      "phase": "start"})
        + "\n"
        + '{"cycle_id": "c1", "node": "build", "step": "编译", "ph'  # 截断的半行
        + "\n",
        encoding="utf-8",
    )

    steps = read_progress(str(tmp_path), "c1")

    assert len(steps) == 2
    done, running = steps
    assert done["step"] == "建立 worktree"
    assert done["duration_ms"] == 42
    assert done["running"] is False
    assert running["step"] == "编译"
    assert running["model"] == "5200"
    assert running["running"] is True
    assert running["duration_ms"] is None


def test_read_progress_running_step_carries_log_path(tmp_path):
    """进行中步骤也要带出 log_path，任务页才能在刷新时实时 tail 编译日志尾部。"""
    from bsa_web.progress import read_progress

    cycle = tmp_path / "c1"
    cycle.mkdir()
    (cycle / "progress.jsonl").write_text(
        json.dumps({"cycle_id": "c1", "node": "build", "step": "编译", "model": "5200",
                    "phase": "start", "log_path": "logs/c1/build/t/abc/build.log"})
        + "\n",
        encoding="utf-8",
    )

    steps = read_progress(str(tmp_path), "c1")

    assert len(steps) == 1
    assert steps[0]["running"] is True
    assert steps[0]["log_path"] == "logs/c1/build/t/abc/build.log"


def test_read_progress_missing_file_returns_empty(tmp_path):
    from bsa_web.progress import read_progress

    assert read_progress(str(tmp_path), "no-such-cycle") == []
