from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class WebSettings(BaseSettings):
    """Web 工作台配置，由环境变量 / .env 加载。

    必填字段 secret_key 必须在生产环境显式配置（SECRET_KEY 或 .env 或
    settings_override 注入）；缺失时 create_app 抛错，不再自动生成随机密钥。
    users 契约 env 为 BSA_USERS（JSON：username -> "bcrypt_hash:role"）。
    """

    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", populate_by_name=True
    )
    bsa_web_port: int = 8888
    log_dir: str = "logs"
    secret_key: str
    session_ttl_sec: int = 8 * 3600
    cookie_secure: bool = False
    users: dict[str, str] = Field(default_factory=dict, validation_alias="BSA_USERS")


def load_web_settings(*, env_file: str | None = ".env") -> WebSettings:
    """加载 Web 配置；env_file=None 时禁用 .env，便于测试隔离。"""
    return WebSettings(_env_file=env_file)
