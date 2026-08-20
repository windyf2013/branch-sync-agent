import builtins
import json
import sys
from pathlib import Path

from branch_maintenance.config import BmaConfig, load_config, merge_cli_overrides
from branch_maintenance.state import load_state, save_state, update_repo_baselines


def _purge_yaml_modules() -> None:
    for key in list(sys.modules):
        if key == "yaml" or key.startswith("yaml."):
            del sys.modules[key]


def test_defaults(tmp_path: Path):
    p = tmp_path / "cfg.yaml"
    p.write_text("repos: []\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.develop_backfill_enabled is True
    assert cfg.mail_enabled is False
    assert cfg.similarity_high == 0.90
    assert cfg.similarity_low == 0.50
    assert cfg.inventory_repo_paths == []


def test_load_config_inventory_repo_paths(tmp_path: Path):
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "\n".join(
            [
                "repos: []",
                "inventory_repo_paths:",
                "  - rcios",
                "  - ftto/",
            ]
        ),
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.inventory_repo_paths == ["rcios", "ftto"]


def test_load_config_full_fields(tmp_path: Path):
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "\n".join(
            [
                "repos:",
                "  - id: demo",
                "    path: /tmp/demo",
                "state_path: custom/state.json",
                "output_dir: out",
                "branch_file: custom/branch.md",
                "mail_enabled: true",
                "develop_backfill_enabled: false",
                "similarity_high: 0.95",
                "similarity_low: 0.40",
                "cross_product_links:",
                "  - from: a",
                "    to: b",
                "lifecycle_overrides:",
                "  br_frozen: frozen",
            ]
        ),
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert len(cfg.repos) == 1
    assert cfg.repos[0]["id"] == "demo"
    assert cfg.state_path == "custom/state.json"
    assert cfg.output_dir == "out"
    assert cfg.branch_file == "custom/branch.md"
    assert cfg.mail_enabled is True
    assert cfg.develop_backfill_enabled is False
    assert cfg.similarity_high == 0.95
    assert cfg.similarity_low == 0.40
    assert cfg.cross_product_links == [{"from": "a", "to": "b"}]
    assert cfg.lifecycle_overrides == {"br_frozen": "frozen"}


def test_load_config_json_fallback_when_yaml_unavailable(tmp_path: Path, monkeypatch):
    p = tmp_path / "cfg.json"
    json_text = '{"repos": [{"id": "j1", "path": "/tmp/j1"}], "mail_enabled": true}'
    p.write_text(json_text, encoding="utf-8")
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "yaml" or (fromlist and "yaml" in fromlist):
            raise ImportError("forced for test")
        return real_import(name, globals, locals, fromlist, level)

    _purge_yaml_modules()
    monkeypatch.delitem(sys.modules, "yaml", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    loads_calls: list[str] = []
    real_loads = json.loads

    def spy_loads(text, *args, **kwargs):
        loads_calls.append(text)
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr("branch_maintenance.config.json.loads", spy_loads)

    cfg = load_config(p)
    assert len(cfg.repos) == 1
    assert cfg.repos[0]["id"] == "j1"
    assert cfg.mail_enabled is True
    assert cfg.develop_backfill_enabled is True
    assert len(loads_calls) == 1
    assert loads_calls[0] == json_text


def test_load_state_whitespace_only_returns_empty(tmp_path: Path):
    p = tmp_path / "state.json"
    p.write_text("   \n\t  \n", encoding="utf-8")
    assert load_state(p) == {}


def test_merge_cli_overrides(tmp_path: Path):
    p = tmp_path / "cfg.yaml"
    p.write_text("repos: []\n", encoding="utf-8")
    cfg = load_config(p)
    merged = merge_cli_overrides(
        cfg,
        repo="/tmp/single",
        output="reports",
        branch_file="branches.md",
        mail_enabled=True,
        skip_fetch=True,
    )
    assert merged.single_repo == "/tmp/single"
    assert merged.output_dir == "reports"
    assert merged.branch_file == "branches.md"
    assert merged.mail_enabled is True
    assert merged.skip_fetch is True
    assert cfg.mail_enabled is False
    assert cfg.skip_fetch is False


def test_merge_cli_overrides_none_leaves_original():
    cfg = BmaConfig(repos=[])
    merged = merge_cli_overrides(
        cfg,
        repo=None,
        output=None,
        branch_file=None,
        mail_enabled=None,
        skip_fetch=None,
    )
    assert merged == cfg


def test_state_round_trip(tmp_path: Path):
    p = tmp_path / "state.json"
    state = {"repos": {"r1": {"last_scan_ref": {"b1": "sha1"}}}}
    save_state(p, state)
    loaded = load_state(p)
    assert loaded == state


def test_load_state_missing_returns_empty(tmp_path: Path):
    p = tmp_path / "missing.json"
    assert load_state(p) == {}


def test_update_repo_baselines_returns_new_state():
    state = {"repos": {"r1": {"last_scan_ref": {"b1": "old"}}}}
    new_state = update_repo_baselines(state, "r1", {"b1": "new", "b2": "sha2"})
    assert state["repos"]["r1"]["last_scan_ref"]["b1"] == "old"
    assert new_state["repos"]["r1"]["last_scan_ref"]["b1"] == "new"
    assert new_state["repos"]["r1"]["last_scan_ref"]["b2"] == "sha2"


def test_update_repo_baselines_other_repos_unchanged():
    state = {
        "repos": {
            "r1": {"last_scan_ref": {}},
            "r2": {"last_scan_ref": {"b": "x"}},
        }
    }
    new_state = update_repo_baselines(state, "r1", {"b1": "sha"})
    assert new_state["repos"]["r2"] == state["repos"]["r2"]
    assert state["repos"]["r1"]["last_scan_ref"] == {}


def test_update_repo_baselines_creates_repo_entry():
    state: dict = {}
    new_state = update_repo_baselines(state, "new_repo", {"main": "abc"})
    assert new_state["repos"]["new_repo"]["last_scan_ref"]["main"] == "abc"
    assert state == {}
