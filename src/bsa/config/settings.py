import os  # noqa: F401 — os.environ is the env source for pydantic-settings; tests monkeypatch bsa.config.settings.os.environ
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    pydantic-settings maps each field to an env var by upper-casing the field
    name by default: ``repo_path`` -> ``REPO_PATH``, ``llm_model`` ->
    ``LLM_MODEL``, ``docker_prefix`` -> ``DOCKER_PREFIX``, etc.

    Real paths/keys are never hardcoded here — everything comes from the
    environment or an optional ``.env`` file next to the repo root.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- repo / git ---
    repo_path: str
    branch_file: str
    worktree_root: str

    # --- LLM ---
    llm_backend: Literal["api", "claude_cli"] = "api"
    llm_model: str
    llm_api_key: str
    llm_base_url: str
    claude_cli_path: str = "claude"
    llm_timeout_sec: int = 60
    llm_max_retries: int = 3
    llm_degrade_to_manual: bool = True

    # --- docker ---
    docker_prefix: str = ""
    docker_image: str
    docker_container_prefix: str = "rcios-sync"
    docker_mount_workspace: str

    # --- build ---
    build_script_dir: str
    max_conflict_attempts: int = 3
    max_build_attempts: int = 3

    # --- mail ---
    mail_dry_run: bool = True
    mail_sender: str
    mail_recipients: list[str]

    # --- logging ---
    log_dir: str

    # --- scan window ---
    # None = detect_commits node computes the default 22:00~22:00 window
    scan_since: str | None = None
    scan_until: str | None = None

    # --- fetch retry (exponential backoff) ---
    fetch_retry_count: int = 3
    fetch_retry_base_sec: int = 30

    # --- fail-fast ---
    failfast_region_gap: int = 200

    # NOTE: required_models (型号) deliberately NOT in Settings — single
    # authoritative source is safety_rules.yaml, loaded by the rules module.


def load_settings() -> Settings:
    """Load settings from the environment, validating required fields.

    A missing required field raises pydantic ValidationError. Optional fields
    fall back to their defaults (or None for the scan window).
    """
    return Settings()
