import os  # noqa: F401 — os.environ is the env source for pydantic-settings; tests monkeypatch bsa.config.settings.os.environ
from typing import Literal

from pydantic import Field
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
    # 完整分支清单：服务手动同步的型号解析与平台工作台下拉。
    branch_file: str
    # cron 周期专用分支文件（带「主分支/业务分支」标注，决定同步拓扑）。
    # 空 = 回退 branch_file，老部署零改动兼容。
    cron_branch_file: str = ""
    worktree_root: str

    # --- LLM ---
    llm_backend: Literal["api", "claude_cli"] = "api"
    llm_model: str
    llm_api_key: str
    llm_base_url: str
    claude_cli_path: str = "claude"
    # claude -p 子进程认证上下文。systemd 守护进程不 source 用户 shell（.bashrc 里的
    # ANTHROPIC_* / DEEPSEEK_* 对 executor 不可见），claude 子进程拿不到认证 → LLMUnavailable。
    # 故运维须把认证环境显式写进 .env（与 web/executor 共用同一份），由 pydantic-settings
    # 解析进 Settings，后端再显式注入 claude 子进程 env（见 base._ClaudeCliBackend）。
    # 字段名 claude_cli_env 天然映射 env 变量 CLAUDE_CLI_ENV（JSON dict，同 mail_recipients）。
    claude_cli_env: dict[str, str] = Field(default_factory=dict)
    llm_timeout_sec: int = 60
    llm_max_retries: int = 3
    llm_degrade_to_manual: bool = True

    # --- docker ---
    docker_prefix: str = ""
    docker_image: str
    docker_container_prefix: str = "rcios-sync"
    docker_mount_workspace: str
    # docker 容器运行用户。空 = 自动用宿主机 uid:gid（避免容器 root 写宿主
    # worktree 导致文件变 root 无法清理，真机测试 77805 个 root 文件）。
    # 可设 "root" 强制 root，或 "1000:1000" 显式指定。
    docker_user: str = ""

    # --- build ---
    build_script_dir: str
    max_conflict_attempts: int = 5
    max_build_attempts: int = 3
    # 编译错误输出文本长度之和的上限（字符）。超限即转人工，不截断——截断会丢上下文、
    # 诱导 LLM 猜，违反「绝不静默猜测」。与 max_conflict_context_chars 同源同阈值。
    max_build_context_chars: int = 50_000
    # 冲突文件解码后 Unicode 文本长度之和的上限（字符）。超限即转人工，不截断——
    # 截断会丢上下文、诱导 LLM 猜，违反「绝不静默猜测」。阈值只影响转人工的快慢与
    # 归因清晰度，不改变最终结果（超限本就该人工），故保守无害。
    max_conflict_context_chars: int = 50_000

    # --- mail ---
    mail_dry_run: bool = True
    mail_sender: str
    mail_recipients: list[str]
    # 项目经理名单（周期报告正常收件人）。空则回退 mail_recipients（老部署零改动兼容）；
    # 非空则正式周期只发此名单。有分支失败/报错时引擎会在此名单外追加失败 commit 的
    # 合入人邮箱（见 scheduler/recipients.py），二者去重。
    mail_pm_recipients: list[str] = []
    # bridge 收件阶段过滤：test/dev 会把 mail_to 硬帽到测试白名单，正式对 PM 发信必须
    # 配 prod。默认 test 保兼容，升级后若要发非白名单收件人需显式设 MAIL_PHASE=prod。
    mail_phase: str = "test"
    mail_bridge_path: str = ""

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

    @property
    def cron_branch_file_resolved(self) -> str:
        """cron 读取的分支文件：显式配置则用之，否则回退 branch_file。

        用 property 而非字段承载派生值，``Settings.model_fields`` 保持只含真实
        env 映射项（避免 CRON_BRANCH_FILE_RESOLVED 这种不存在的环境变量）。
        """
        return self.cron_branch_file or self.branch_file


def load_settings(*, env_file: str | None = ".env") -> Settings:
    """Load settings from the environment, validating required fields.

    A missing required field raises pydantic ValidationError. Optional fields
    fall back to their defaults (or None for the scan window).
    ``env_file=None`` disables the .env file so callers (e.g. tests) can
    isolate from a working-directory .env (真机测试: .env 污染测试环境).
    """
    return Settings(_env_file=env_file)
