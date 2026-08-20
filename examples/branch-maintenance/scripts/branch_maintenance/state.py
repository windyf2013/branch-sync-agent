from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    data = json.loads(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"State root must be a mapping, got {type(data).__name__}.")
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def update_repo_baselines(
    state: dict[str, Any],
    repo_id: str,
    baselines: dict[str, str],
) -> dict[str, Any]:
    new_state = copy.deepcopy(state)
    repos = new_state.setdefault("repos", {})
    if not isinstance(repos, dict):
        repos = {}
        new_state["repos"] = repos
    repo_entry = repos.setdefault(repo_id, {})
    if not isinstance(repo_entry, dict):
        repo_entry = {}
        repos[repo_id] = repo_entry
    last_scan_ref = repo_entry.setdefault("last_scan_ref", {})
    if not isinstance(last_scan_ref, dict):
        last_scan_ref = {}
        repo_entry["last_scan_ref"] = last_scan_ref
    last_scan_ref.update(baselines)
    return new_state
