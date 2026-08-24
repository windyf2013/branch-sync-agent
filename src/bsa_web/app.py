import secrets

from fastapi import FastAPI
from pydantic import ValidationError

from bsa_web.settings import WebSettings


def _build_settings(
    settings_override: dict | None, *, env_file: str | None = ".env"
) -> WebSettings:
    """合并 settings_override 构造 WebSettings。

    测试/开发环境未配置 SECRET_KEY 时生成随机密钥（幂等生产必须显式配置）。
    """
    kwargs = dict(settings_override or {})
    try:
        return WebSettings(_env_file=env_file, **kwargs)
    except ValidationError:
        kwargs.setdefault("secret_key", secrets.token_hex(32))
        return WebSettings(_env_file=env_file, **kwargs)


def create_app(*, settings_override: dict | None = None) -> FastAPI:
    app = FastAPI(title="BSA Web 工作台")
    app.state.settings = _build_settings(settings_override)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
