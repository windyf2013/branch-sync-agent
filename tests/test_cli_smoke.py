from __future__ import annotations

import json
from pathlib import Path

from bsa.cli import main
from bsa.domain.models import (
    BranchResult,
    BuildOutcome,
    CommitResult,
    Conclusion4,
    Report,
)
from bsa.report.renderer import build_email_body, render_html_report
from bsa.rules import Classification
from bsa.scheduler.cycle import run_cycle
from tests.test_config import valid_env
from tests.test_graph_nodes import (
    DEVELOP,
    TARGET,
    base_state,
    make_ctx,
    write_branch_md,
)
from tests.test_workflow import FakeWorktreeGit, inject_worktree_git


def _populate(ctx, shas: list[str]) -> None:
    git = ctx.git
    git.window_shas = {f"origin/{DEVELOP}": list(shas)}
    for sha in shas:
        git.changed[sha] = ["plat/demo.c"]
        git.patches[sha] = "+x"
        git.patch_ids[sha] = f"pid-{sha}"
        ctx.classify.results[sha] = Classification(
            is_bug_fix=True,
            recognition_source="machine:[BUG]",
            needs_agent=False,
        )
        ctx.conclude.results[(sha, TARGET)] = Conclusion4(
            kind="NeedSync", evidence=[], confidence="high"
        )
    inject_worktree_git(ctx, FakeWorktreeGit(), TARGET, cycle_id="cycle-20260820")


def test_validate_config_exits_zero_with_env(monkeypatch, tmp_path):
    env = valid_env()
    env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr("bsa.config.settings.os.environ", env)

    assert main(["validate-config"]) == 0


def test_validate_config_missing_env_exits_two(monkeypatch):
    monkeypatch.setattr("bsa.config.settings.os.environ", {})

    assert main(["validate-config"]) == 2


def test_run_cycle_dry_run_full_cycle_produces_artifacts(tmp_path):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    _populate(ctx, ["a1"])

    code = run_cycle("20260820", dry_run=True, context=ctx)

    assert code == 0
    cycle_dir = Path(ctx.settings.log_dir) / "cycle-20260820"
    assert (cycle_dir / "report.html").exists()
    assert (cycle_dir / "decisions.json").exists()
    assert (cycle_dir / "run.log").exists()
    record = json.loads((cycle_dir / "cycle.json").read_text(encoding="utf-8"))
    assert record["status"] == "REPORTED"
    assert record["mail_status"] == "skipped"
    html = (cycle_dir / "report.html").read_text(encoding="utf-8")
    assert "检测信息" in html
    assert "Action Required" in html
    assert "已成功同步" in html


def test_run_cycle_window_override_honored(tmp_path):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    _populate(ctx, ["a1"])

    code = run_cycle(
        "20260820",
        since="2026-08-18T22:00:00+08:00",
        until="2026-08-19T22:00:00+08:00",
        context=ctx,
    )

    assert code == 0
    recorded = [args for name, args in ctx.git.calls if name == "commits_in_window"]
    assert recorded[0][0] == "2026-08-18T22:00:00+08:00"
    assert recorded[0][1] == "2026-08-19T22:00:00+08:00"


def test_manual_scan_passes_window_override_to_run_cycle(monkeypatch):
    seen: dict = {}

    def fake_run_cycle(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return 0

    monkeypatch.setattr("bsa.cli.run_cycle", fake_run_cycle)

    code = main(
        [
            "manual-scan",
            "--since",
            "2026-08-18T22:00:00+08:00",
            "--until",
            "2026-08-19T22:00:00+08:00",
        ]
    )

    assert code == 0
    assert seen["kwargs"]["since"] == "2026-08-18T22:00:00+08:00"
    assert seen["kwargs"]["until"] == "2026-08-19T22:00:00+08:00"


def test_run_cycle_cli_passes_dry_run(monkeypatch):
    seen: dict = {}

    def fake_run_cycle(*args, **kwargs):
        seen["kwargs"] = kwargs
        return 0

    monkeypatch.setattr("bsa.cli.run_cycle", fake_run_cycle)

    assert main(["run-cycle", "--date", "20260101", "--dry-run"]) == 0
    assert seen["kwargs"]["dry_run"] is True


def test_run_cycle_missing_env_exits_one(monkeypatch):
    monkeypatch.setattr("bsa.config.settings.os.environ", {})

    assert main(["run-cycle"]) == 1


def test_run_cycle_checkpoint_resume_reuses_state(tmp_path):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    _populate(ctx, ["a1"])

    assert run_cycle("20260820", context=ctx) == 0
    runner_calls = len(ctx.runner.build_calls)
    git_calls = len(ctx.git.calls)

    assert run_cycle("20260820", context=ctx) == 0

    assert len(ctx.runner.build_calls) == runner_calls
    assert len(ctx.git.calls) == git_calls


def test_status_shows_latest_cycle(monkeypatch, tmp_path, capsys):
    write_branch_md(tmp_path)
    ctx = make_ctx(tmp_path)
    _populate(ctx, ["a1"])
    assert run_cycle("20260820", context=ctx) == 0

    env = valid_env()
    env["LOG_DIR"] = str(ctx.settings.log_dir)
    monkeypatch.setattr("bsa.config.settings.os.environ", env)

    assert main(["status"]) == 0
    captured = capsys.readouterr().out
    assert "cycle-20260820" in captured
    assert "REPORTED" in captured


def test_render_html_report_three_sections(tmp_path):
    state = base_state()
    state["decisions"] = {
        "abc123": {TARGET: Conclusion4(kind="NeedSync", evidence=["ev"], confidence="high")}
    }
    state["branch_results"] = {
        TARGET: BranchResult(
            target_branch=TARGET,
            worktree_path=str(tmp_path),
            status="SUCCESS",
            commits=[
                CommitResult(
                    sha="abc123",
                    cherry_pick="OK",
                    conflict_resolution=None,
                    build={
                        "RTL9617C": BuildOutcome(
                            model="RTL9617C",
                            status="OK",
                            log_path="/l",
                            errors=[],
                            agent_attempts=0,
                            fix_diff=None,
                        )
                    },
                )
            ],
            patch_path=str(tmp_path / "p.patch"),
            stop_reason=None,
        )
    }
    report = Report(
        cycle_id="cycle-20260820",
        html_path=tmp_path / "report.html",
        summary={"status": "REPORTED", "commits_detected": 1},
        action_required=[],
        decisions_json_path=tmp_path / "decisions.json",
    )

    path = render_html_report(state, report)

    assert path == tmp_path / "report.html"
    html = path.read_text(encoding="utf-8")
    assert "检测信息" in html
    assert "Action Required" in html
    assert "已成功同步" in html
    assert "abc123" in html
    assert TARGET in html

    body = build_email_body(report, state)
    assert "cycle-20260820" in body
    assert "NeedSync 1" in body
    assert "检测 commit" in body
