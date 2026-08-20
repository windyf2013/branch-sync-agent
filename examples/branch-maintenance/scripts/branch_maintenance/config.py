from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

DEFAULT_STATE_PATH = "ai_reports/branch_maintenance_state.json"
DEFAULT_OUTPUT_DIR = "ai_reports"
DEFAULT_BRANCH_FILE = ".cursor/scripts/branch.md"
DEFAULT_AGENT_JUDGMENTS_PATH = "ai_reports/bma_agent_judgments.json"


@dataclass
class BmaConfig:
    repos: list[dict[str, Any]]
    state_path: str = DEFAULT_STATE_PATH
    output_dir: str = DEFAULT_OUTPUT_DIR
    branch_file: str = DEFAULT_BRANCH_FILE
    mail_enabled: bool = False
    mail_phase: str = "test"
    mail_to: list[str] = field(default_factory=lambda: ["yangfu@raisecom.com"])
    mail_cc: list[str] = field(default_factory=list)
    mail_smtp: dict[str, Any] = field(default_factory=dict)
    mail_smtp_source: str = "bug_stale_alert"
    mail_send_script: str | None = None
    mail_dry_run: bool = False
    develop_backfill_enabled: bool = True
    similarity_high: float = 0.90
    similarity_low: float = 0.50
    cross_product_links: list[dict[str, Any]] = field(default_factory=list)
    lifecycle_overrides: dict[str, str] = field(default_factory=dict)
    skip_fetch: bool = False
    single_repo: str | None = None
    since: str | None = None
    until: str | None = None
    update_state: bool = True
    # Claude main-agent judgments for commits without machine-readable markers.
    agent_judgments_path: str = DEFAULT_AGENT_JUDGMENTS_PATH
    # When non-empty, inventory branch.md only keeps entries whose 路径 matches
    # exactly (normalized). Empty = all inventory repos. Ignored when --repo set.
    inventory_repo_paths: list[str] = field(default_factory=list)


def _load_mapping(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(text)
    except ImportError:
        data = json.loads(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Config root must be a mapping, got {type(data).__name__}.")
    return data


def load_agent_judgments(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("agent judgments file must be a JSON object keyed by commit SHA.")
    return data


def load_config(path: Path) -> BmaConfig:
    raw = _load_mapping(path)
    repos = raw.get("repos", [])
    if repos is None:
        repos = []
    if not isinstance(repos, list):
        raise ValueError("Config field repos must be a list.")

    cross_product_links = raw.get("cross_product_links", [])
    if cross_product_links is None:
        cross_product_links = []
    if not isinstance(cross_product_links, list):
        raise ValueError("Config field cross_product_links must be a list.")

    lifecycle_overrides = raw.get("lifecycle_overrides", {})
    if lifecycle_overrides is None:
        lifecycle_overrides = {}
    if not isinstance(lifecycle_overrides, dict):
        raise ValueError("Config field lifecycle_overrides must be a mapping.")

    mail_to = raw.get("mail_to") or raw.get("to_emails") or ["yangfu@raisecom.com"]
    if isinstance(mail_to, str):
        mail_to = [part.strip() for part in mail_to.split(",") if part.strip()]
    if not isinstance(mail_to, list):
        raise ValueError("Config field mail_to must be a list or comma-separated string.")

    mail_cc = raw.get("mail_cc") or raw.get("cc_emails") or []
    if isinstance(mail_cc, str):
        mail_cc = [part.strip() for part in mail_cc.split(",") if part.strip()]
    if not isinstance(mail_cc, list):
        raise ValueError("Config field mail_cc must be a list or comma-separated string.")

    mail_smtp = raw.get("mail_smtp") or {}
    if mail_smtp is None:
        mail_smtp = {}
    if not isinstance(mail_smtp, dict):
        raise ValueError("Config field mail_smtp must be a mapping.")

    inventory_repo_paths = raw.get("inventory_repo_paths") or []
    if isinstance(inventory_repo_paths, str):
        inventory_repo_paths = [
            part.strip() for part in inventory_repo_paths.split(",") if part.strip()
        ]
    if not isinstance(inventory_repo_paths, list):
        raise ValueError(
            "Config field inventory_repo_paths must be a list or comma-separated string."
        )

    return BmaConfig(
        repos=repos,
        state_path=str(raw.get("state_path", DEFAULT_STATE_PATH)),
        output_dir=str(raw.get("output_dir", DEFAULT_OUTPUT_DIR)),
        branch_file=str(raw.get("branch_file", DEFAULT_BRANCH_FILE)),
        mail_enabled=bool(raw.get("mail_enabled", False)),
        mail_phase=str(raw.get("mail_phase", "test")),
        mail_to=[str(item).strip() for item in mail_to if str(item).strip()],
        mail_cc=[str(item).strip() for item in mail_cc if str(item).strip()],
        mail_smtp=mail_smtp,
        mail_smtp_source=str(raw.get("mail_smtp_source", "bug_stale_alert")),
        mail_send_script=(
            str(raw["mail_send_script"]) if raw.get("mail_send_script") else None
        ),
        mail_dry_run=bool(raw.get("mail_dry_run", False)),
        develop_backfill_enabled=bool(raw.get("develop_backfill_enabled", True)),
        similarity_high=float(raw.get("similarity_high", 0.90)),
        similarity_low=float(raw.get("similarity_low", 0.50)),
        cross_product_links=cross_product_links,
        lifecycle_overrides=lifecycle_overrides,
        skip_fetch=bool(raw.get("skip_fetch", False)),
        single_repo=raw.get("single_repo"),
        agent_judgments_path=str(
            raw.get("agent_judgments_path", DEFAULT_AGENT_JUDGMENTS_PATH)
        ),
        inventory_repo_paths=[
            str(item).strip().replace("\\", "/").rstrip("/")
            for item in inventory_repo_paths
            if str(item).strip()
        ],
    )


def merge_cli_overrides(
    cfg: BmaConfig,
    *,
    repo: str | None,
    output: str | None,
    branch_file: str | None,
    mail_enabled: bool | None,
    skip_fetch: bool | None,
    since: str | None = None,
    until: str | None = None,
    update_state: bool | None = None,
    agent_judgments: str | None = None,
    mail_dry_run: bool | None = None,
) -> BmaConfig:
    overrides: dict[str, Any] = {}
    if repo is not None:
        overrides["single_repo"] = repo
    if output is not None:
        overrides["output_dir"] = output
    if branch_file is not None:
        overrides["branch_file"] = branch_file
    if mail_enabled is not None:
        overrides["mail_enabled"] = mail_enabled
    if skip_fetch is not None:
        overrides["skip_fetch"] = skip_fetch
    if since is not None:
        overrides["since"] = since
    if until is not None:
        overrides["until"] = until
    if update_state is not None:
        overrides["update_state"] = update_state
    if agent_judgments is not None:
        overrides["agent_judgments_path"] = agent_judgments
    if mail_dry_run is not None:
        overrides["mail_dry_run"] = mail_dry_run
    if not overrides:
        return cfg
    return replace(cfg, **overrides)


def mail_cfg_dict(cfg: BmaConfig) -> dict[str, Any]:
    return {
        "mail_phase": cfg.mail_phase,
        "mail_to": list(cfg.mail_to),
        "mail_cc": list(cfg.mail_cc),
        "mail_smtp": dict(cfg.mail_smtp),
        "mail_smtp_source": cfg.mail_smtp_source,
        "mail_send_script": cfg.mail_send_script,
    }
