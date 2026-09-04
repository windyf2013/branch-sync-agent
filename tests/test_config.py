import json

import pytest
from pydantic import ValidationError
from pydantic_settings.exceptions import SettingsError

from bsa.config.settings import Settings, load_settings

REQUIRED = [
    "repo_path",
    "branch_file",
    "worktree_root",
    "llm_model",
    "llm_api_key",
    "llm_base_url",
    "docker_image",
    "docker_mount_workspace",
    "build_script_dir",
    "mail_sender",
    "mail_recipients",
    "log_dir",
]

DEFAULTS = {
    "llm_backend": "api",
    "llm_timeout_sec": 60,
    "llm_max_retries": 3,
    "llm_degrade_to_manual": True,
    "claude_cli_path": "claude",
    "claude_cli_env": {},
    "docker_prefix": "",
    "docker_container_prefix": "rcios-sync",
    "max_conflict_attempts": 5,
    "max_build_attempts": 3,
    "max_build_context_chars": 50_000,
    "max_conflict_context_chars": 50_000,
    "mail_dry_run": True,
    "mail_bridge_path": "",
    "scan_since": None,
    "scan_until": None,
    "fetch_retry_count": 3,
    "fetch_retry_base_sec": 30,
    "failfast_region_gap": 200,
}


def valid_env() -> dict[str, str]:
    return {
        "REPO_PATH": "/srv/rcios",
        "BRANCH_FILE": "/srv/rcios/branch.md",
        "WORKTREE_ROOT": "/srv/wt",
        "LLM_MODEL": "deepseek-chat",
        "LLM_API_KEY": "sk-test",
        "LLM_BASE_URL": "https://api.deepseek.com/v1",
        "DOCKER_IMAGE": "rcios-build:latest",
        "DOCKER_MOUNT_WORKSPACE": "/workspace/rcios",
        "BUILD_SCRIPT_DIR": "build/platform/RTL9617C",
        "MAIL_SENDER": "bsa@raisecom.com",
        "MAIL_RECIPIENTS": '["ops@raisecom.com","dev@raisecom.com"]',
        "LOG_DIR": "/srv/bsa/logs",
    }


def make(monkeypatch, env: dict[str, str] | None = None) -> Settings:
    monkeypatch.setattr("bsa.config.settings.os.environ", {**env} if env else {})
    return load_settings(env_file=None)


class TestRequiredFields:
    def test_missing_required_field_raises(self, monkeypatch):
        for field in REQUIRED:
            env = valid_env()
            env.pop(field.upper(), None)
            monkeypatch.setattr("bsa.config.settings.os.environ", env)
            with pytest.raises(ValidationError):
                load_settings(env_file=None)

    def test_all_required_present_ok(self, monkeypatch):
        s = make(monkeypatch, valid_env())
        assert s.repo_path == "/srv/rcios"


class TestDefaults:
    def test_default_values(self, monkeypatch):
        s = make(monkeypatch, valid_env())
        for field, expected in DEFAULTS.items():
            assert getattr(s, field) == expected

    def test_defaults_need_no_env_vars(self, monkeypatch):
        monkeypatch.setattr("bsa.config.settings.os.environ", {})
        s = Settings(
            _env_file=None,
            repo_path="r",
            branch_file="b",
            worktree_root="w",
            llm_model="m",
            llm_api_key="k",
            llm_base_url="u",
            docker_image="i",
            docker_mount_workspace="/workspace/rcios",
            build_script_dir="build/platform/RTL9617C",
            mail_sender="s",
            mail_recipients=["ops@x.com"],
            log_dir="l",
        )
        for field, expected in DEFAULTS.items():
            assert getattr(s, field) == expected


class TestEnvInjection:
    def test_load_settings_reads_env(self, monkeypatch):
        env = valid_env()
        env["LLM_TIMEOUT_SEC"] = "120"
        env["LLM_MAX_RETRIES"] = "5"
        env["MAIL_DRY_RUN"] = "false"
        env["DOCKER_PREFIX"] = "sudo "
        s = make(monkeypatch, env)
        assert s.llm_timeout_sec == 120
        assert s.llm_max_retries == 5
        assert s.llm_degrade_to_manual is True
        assert s.mail_dry_run is False
        assert s.docker_prefix == "sudo "

    def test_env_overrides_default(self, monkeypatch):
        env = valid_env()
        env["DOCKER_CONTAINER_PREFIX"] = "my-sync"
        s = make(monkeypatch, env)
        assert s.docker_container_prefix == "my-sync"

    def test_scan_window_env(self, monkeypatch):
        env = valid_env()
        env["SCAN_SINCE"] = "2026-08-18T22:00:00+08:00"
        env["SCAN_UNTIL"] = "2026-08-19T22:00:00+08:00"
        s = make(monkeypatch, env)
        assert s.scan_since == "2026-08-18T22:00:00+08:00"
        assert s.scan_until == "2026-08-19T22:00:00+08:00"

    def test_mail_bridge_path_env(self, monkeypatch):
        env = valid_env()
        env["MAIL_BRIDGE_PATH"] = "/srv/bsa/bridge-scripts"
        s = make(monkeypatch, env)
        assert s.mail_bridge_path == "/srv/bsa/bridge-scripts"

    def test_claude_cli_env_parsed_from_json_dict(self, monkeypatch):
        env = valid_env()
        env["CLAUDE_CLI_ENV"] = '{"ANTHROPIC_API_KEY": "sk-abc", "ANTHROPIC_BASE_URL": "https://x"}'
        s = make(monkeypatch, env)
        assert s.claude_cli_env == {
            "ANTHROPIC_API_KEY": "sk-abc",
            "ANTHROPIC_BASE_URL": "https://x",
        }

    def test_claude_cli_env_invalid_json_raises(self, monkeypatch):
        env = valid_env()
        env["CLAUDE_CLI_ENV"] = "not-a-dict"
        monkeypatch.setattr("bsa.config.settings.os.environ", env)
        with pytest.raises(SettingsError):
            load_settings(env_file=None)


class TestListField:
    def test_mail_recipients_parsed_from_json_list(self, monkeypatch):
        s = make(monkeypatch, valid_env())
        assert s.mail_recipients == ["ops@raisecom.com", "dev@raisecom.com"]

    def test_mail_recipients_single_item(self, monkeypatch):
        env = valid_env()
        env["MAIL_RECIPIENTS"] = '["ops@raisecom.com"]'
        s = make(monkeypatch, env)
        assert s.mail_recipients == ["ops@raisecom.com"]

    def test_mail_recipients_invalid_raises(self, monkeypatch):
        env = valid_env()
        env["MAIL_RECIPIENTS"] = "not-a-list"
        monkeypatch.setattr("bsa.config.settings.os.environ", env)
        with pytest.raises(SettingsError):
            load_settings(env_file=None)


class TestEnvMapping:
    def test_field_name_to_env_uppercase(self, monkeypatch):
        env = valid_env()
        monkeypatch.setattr("bsa.config.settings.os.environ", env)
        s = load_settings(env_file=None)
        for field in REQUIRED:
            if field == "mail_recipients":
                assert s.mail_recipients == json.loads(env["MAIL_RECIPIENTS"])
            else:
                assert getattr(s, field) == env[field.upper()]

    def test_unknown_env_ignored(self, monkeypatch):
        env = valid_env()
        env["UNRELATED_VAR"] = "noise"
        s = make(monkeypatch, env)
        assert not hasattr(s, "unrelated_var")


class TestNoModelsField:
    def test_no_models_field(self):
        assert "models" not in Settings.model_fields
        assert "required_models" not in Settings.model_fields


class TestCronBranchFile:
    """cron 专用分支文件：未配置时回退 branch_file（老部署零改动兼容）。"""

    def test_defaults_to_empty(self, monkeypatch):
        s = make(monkeypatch, valid_env())
        assert s.cron_branch_file == ""

    def test_resolved_falls_back_to_branch_file(self, monkeypatch):
        s = make(monkeypatch, valid_env())
        assert s.cron_branch_file_resolved == "/srv/rcios/branch.md"

    def test_resolved_prefers_explicit_cron_file(self, monkeypatch):
        env = valid_env()
        env["CRON_BRANCH_FILE"] = "/srv/rcios/branch-cron.md"
        s = make(monkeypatch, env)
        assert s.cron_branch_file_resolved == "/srv/rcios/branch-cron.md"
        assert s.branch_file == "/srv/rcios/branch.md"

    def test_resolved_is_not_a_settings_field(self):
        assert "cron_branch_file_resolved" not in Settings.model_fields


class TestLoadSettings:
    def test_returns_settings_instance(self, monkeypatch):
        s = make(monkeypatch, valid_env())
        assert isinstance(s, Settings)
