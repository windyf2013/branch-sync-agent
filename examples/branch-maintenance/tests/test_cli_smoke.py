from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
WORKSPACE_ROOT = SCRIPTS_DIR.parent.parent

sys.path.insert(0, str(SCRIPTS_DIR))

from branch_maintenance.state import load_state
from fixtures.mini_repo import build_multi_develop_fix_repo


def _write_branch_md(repo: Path, branch_file: str) -> None:
    content = "\n".join(
        [
            "# Test branches",
            "",
            "## LineA",
            "- br_v4_LineA_develop_a_20260101",
            "- br_v4_LineA_develop_b_20260101",
            "",
        ]
    )
    path = repo / branch_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_config(
    path: Path,
    *,
    good_repo: Path,
    bad_repo: Path,
    state_path: Path,
    output_dir: Path,
    branch_file: str,
) -> None:
    text = "\n".join(
        [
            "repos:",
            f"  - id: good",
            f"    path: {good_repo.as_posix()}",
            f"  - id: bad",
            f"    path: {bad_repo.as_posix()}",
            f"state_path: {state_path.as_posix()}",
            f"output_dir: {output_dir.as_posix()}",
            f"branch_file: {branch_file}",
            "mail_enabled: false",
            "skip_fetch: true",
        ]
    )
    path.write_text(text, encoding="utf-8")


def test_cli_smoke_good_and_bad_repo(tmp_path: Path, capsys):
    good_repo = tmp_path / "good.git"
    meta = build_multi_develop_fix_repo(good_repo)
    branch_file = ".cursor/scripts/branch.md"
    _write_branch_md(good_repo, branch_file)

    bad_repo = tmp_path / "missing-branch.git"
    build_multi_develop_fix_repo(bad_repo)

    state_path = tmp_path / "state.json"
    output_dir = tmp_path / "reports"
    config_path = tmp_path / "cfg.yaml"
    _write_config(
        config_path,
        good_repo=good_repo,
        bad_repo=bad_repo,
        state_path=state_path,
        output_dir=output_dir,
        branch_file=branch_file,
    )

    prior_state = {
        "repos": {
            "good": {"last_scan_ref": {meta["develop_a"]: meta["base_sha"]}},
            "bad": {"last_scan_ref": {"stale": "deadbeef"}},
        }
    }
    state_path.write_text(json.dumps(prior_state), encoding="utf-8")

    cli = SCRIPTS_DIR / "branch_maintenance_report.py"
    completed = subprocess.run(
        [sys.executable, str(cli), "--config", str(config_path), "--skip-fetch"],
        cwd=str(WORKSPACE_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert completed.returncode == 1

    stdout = completed.stdout
    stderr = completed.stderr
    ok_lines = [line for line in stdout.splitlines() if line.startswith("[OK] 报表已生成:")]
    assert len(ok_lines) == 1
    assert "reports" in ok_lines[0]
    assert "branch_maintenance_good_" in ok_lines[0]

    assert "bad" in stderr.lower() or "branch file not found" in stderr.lower()

    # Default CLI applies 21:00~21:00 window and does not advance last_scan_ref.
    assert "[INFO] default eval window:" in stdout

    state = load_state(state_path)
    good_refs = state["repos"]["good"]["last_scan_ref"]
    assert meta["develop_a"] in good_refs
    assert good_refs[meta["develop_a"]] == meta["base_sha"]

    bad_refs = state["repos"]["bad"]["last_scan_ref"]
    assert bad_refs == {"stale": "deadbeef"}


def test_cli_main_importable():
    from branch_maintenance_report import main

    assert callable(main)
