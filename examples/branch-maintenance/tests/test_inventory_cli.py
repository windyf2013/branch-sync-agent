from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = PACKAGE_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from branch_maintenance_report import (  # noqa: E402
    _bind_inventory_jobs_to_git_repo,
    _detect_workspace_root,
    _filter_jobs_for_single_repo,
    _repos_from_inventory,
    _resolve_inventory_path,
)


def test_resolve_inventory_nested_monorepo(tmp_path: Path):
    workspace = tmp_path / "rcios"
    nested = workspace / "component" / "wlan"
    nested.mkdir(parents=True)
    resolved = _resolve_inventory_path("rcios/component/wlan", workspace)
    assert resolved == nested.resolve()


def test_repos_from_inventory_skips_empty_and_missing(tmp_path: Path):
    workspace = tmp_path / "rcios"
    workspace.mkdir()
    (workspace / "component" / "wlan").mkdir(parents=True)
    sibling = tmp_path / "ftto"
    sibling.mkdir()

    inventory = tmp_path / "branch.md"
    inventory.write_text(
        "\n".join(
            [
                "# 所有待审核分支",
                "## 1 RCIOS",
                "- 路径：rcios",
                "### 1.1 组网",
                "- br_v4_LineA_develop_a_20260101",
                "## 2 WLAN",
                "- 路径：rcios/component/wlan",
                "### 2.1 分支",
                "- br_wlan_develop_20260101",
                "## 3 AC empty",
                "- 路径：rcios/component/ac",
                "### 3.1 分支",
                "## 4 FTTO",
                "- 路径：ftto",
                "### 4.1 分支",
                "- br_ftto_develop_20260101",
                "## 5 missing",
                "- 路径：no_such_repo",
                "### 5.1 分支",
                "- br_x_develop_20260101",
                "",
            ]
        ),
        encoding="utf-8",
    )

    jobs = _repos_from_inventory(inventory, workspace_root=workspace)
    ids = [entry["id"] for entry, _ in jobs]
    assert "rcios" in ids
    assert "rcios_component_wlan" in ids
    assert "ftto" in ids
    assert "rcios_component_ac" not in ids
    assert "no_such_repo" not in ids

    md_by_id = {entry["id"]: md for entry, md in jobs}
    assert "br_v4_LineA_develop_a_20260101" in md_by_id["rcios"]
    assert "br_wlan_develop_20260101" in md_by_id["rcios_component_wlan"]
    assert "br_ftto_develop_20260101" in md_by_id["ftto"]
    assert "br_ftto_develop_20260101" not in md_by_id["rcios"]


def test_repos_from_inventory_allowlist_exact_rcios_only(tmp_path: Path):
    workspace = tmp_path / "rcios"
    workspace.mkdir()
    (workspace / "component" / "wlan").mkdir(parents=True)
    sibling = tmp_path / "ftto"
    sibling.mkdir()

    inventory = tmp_path / "branch.md"
    inventory.write_text(
        "\n".join(
            [
                "# 所有待审核分支",
                "## 1 RCIOS",
                "- 路径：rcios",
                "### 1.1 组网",
                "- br_v4_LineA_develop_a_20260101",
                "## 2 WLAN",
                "- 路径：rcios/component/wlan",
                "### 2.1 分支",
                "- br_wlan_develop_20260101",
                "## 3 FTTO",
                "- 路径：ftto",
                "### 3.1 分支",
                "- br_ftto_develop_20260101",
                "",
            ]
        ),
        encoding="utf-8",
    )

    jobs = _repos_from_inventory(
        inventory,
        workspace_root=workspace,
        allowlist=["rcios"],
    )
    ids = [entry["id"] for entry, _ in jobs]
    assert ids == ["rcios"]


def test_repos_from_inventory_empty_allowlist_keeps_all(tmp_path: Path):
    workspace = tmp_path / "rcios"
    workspace.mkdir()
    (workspace / "component" / "wlan").mkdir(parents=True)

    inventory = tmp_path / "branch.md"
    inventory.write_text(
        "\n".join(
            [
                "# 所有待审核分支",
                "## 1 RCIOS",
                "- 路径：rcios",
                "### 1.1 组网",
                "- br_a_develop_20260101",
                "## 2 WLAN",
                "- 路径：rcios/component/wlan",
                "### 2.1 分支",
                "- br_wlan_develop_20260101",
                "",
            ]
        ),
        encoding="utf-8",
    )

    jobs = _repos_from_inventory(
        inventory,
        workspace_root=workspace,
        allowlist=[],
    )
    ids = [entry["id"] for entry, _ in jobs]
    assert ids == ["rcios", "rcios_component_wlan"]


def test_filter_jobs_for_single_repo_keeps_monorepo_only(tmp_path: Path):
    workspace = tmp_path / "rcios"
    workspace.mkdir()
    wlan = workspace / "component" / "wlan"
    wlan.mkdir(parents=True)
    ftto = tmp_path / "ftto"
    ftto.mkdir()

    jobs = [
        ({"id": "rcios", "path": str(workspace)}, "# rcios\n"),
        ({"id": "wlan", "path": str(wlan)}, "# wlan\n"),
        ({"id": "ftto", "path": str(ftto)}, "# ftto\n"),
    ]
    matched = _filter_jobs_for_single_repo(
        jobs,
        single_repo=str(workspace),
        workspace_root=workspace,
    )
    ids = [entry["id"] for entry, _ in matched]
    assert ids == ["rcios", "wlan"]


def test_detect_workspace_root_package_ssot(tmp_path: Path):
    root = tmp_path / "aiskill"
    scripts = root / "common" / "packages" / "branch-maintenance" / "scripts"
    scripts.mkdir(parents=True)
    assert _detect_workspace_root(scripts) == root.resolve()


def test_detect_workspace_root_cursor_deploy(tmp_path: Path):
    repo = tmp_path / "rcios"
    scripts = repo / ".cursor" / "scripts"
    scripts.mkdir(parents=True)
    assert _detect_workspace_root(scripts) == repo.resolve()


def test_bind_inventory_jobs_to_git_repo_by_id(tmp_path: Path):
    """aiskill hosts inventory; --repo points at the real business git root."""
    aiskill_workspace = tmp_path / "aiskill_workspace"
    aiskill_workspace.mkdir()
    business = tmp_path / "cpe" / "rcios"
    business.mkdir(parents=True)

    jobs = [
        ({"id": "rcios", "path": str(aiskill_workspace)}, "# rcios branches\n"),
        ({"id": "ftto", "path": str(tmp_path / "ftto")}, "# ftto\n"),
    ]
    bound = _bind_inventory_jobs_to_git_repo(
        jobs,
        git_repo=str(business),
        workspace_root=aiskill_workspace,
    )
    assert len(bound) == 1
    assert bound[0][0]["id"] == "rcios"
    assert bound[0][0]["path"] == str(business.resolve())
    assert "rcios branches" in bound[0][1]
