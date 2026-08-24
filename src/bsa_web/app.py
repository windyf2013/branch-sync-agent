from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from bsa_web.auth import (
    SESSION_COOKIE,
    EnvAuthenticator,
    create_session,
    make_csrf,
    require_csrf,
    require_login,
    require_operator,
)
from bsa_web.db import init_db
from bsa_web.settings import WebSettings
from bsa_web.views.workbench import router as workbench_router

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


def _build_settings(
    settings_override: dict | None, *, env_file: str | None = ".env"
) -> WebSettings:
    """合并 settings_override 构造 WebSettings。

    生产必须显式配置非空 SECRET_KEY（env/.env 或 override 注入），
    缺失时抛错，不做随机回退（随机回退会使多 worker 会话各自为政）。
    """
    kwargs = dict(settings_override or {})
    explicit_secret = bool(kwargs.get("secret_key"))
    try:
        settings = WebSettings(_env_file=env_file, **kwargs)
    except ValidationError:
        if not explicit_secret:
            raise RuntimeError(
                "SECRET_KEY 未配置：生产环境必须显式设置（SECRET_KEY 或 settings_override）"
            ) from None
        raise
    if not settings.secret_key:
        raise RuntimeError(
            "SECRET_KEY 未配置：生产环境必须显式设置（SECRET_KEY 或 settings_override）"
        )
    return settings


def create_app(*, settings_override: dict | None = None) -> FastAPI:
    settings = _build_settings(settings_override)
    app = FastAPI(title="BSA Web 工作台")
    app.state.settings = settings
    app.state.templates = templates
    app.state.db = init_db(Path(settings.log_dir) / "platform.sqlite3")
    raw_users = ",".join(f"{name}:{creds}" for name, creds in settings.users.items())
    app.state.authenticator = EnvAuthenticator(raw_users)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/login")
    def login_page(request: Request):
        return templates.TemplateResponse(
            request, "login.html", {"csrf": make_csrf(settings.secret_key, "")}
        )

    @app.post("/login", dependencies=[Depends(require_csrf)])
    def login_submit(
        request: Request, username: str = Form(...), password: str = Form(...)
    ):
        role = app.state.authenticator.authenticate(username, password)
        if role is None:
            return RedirectResponse("/login", status_code=302)
        token = create_session(
            app.state.db, username, role, settings.session_ttl_sec
        )
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=settings.session_ttl_sec,
            httponly=True,
            samesite="lax",
            secure=settings.cookie_secure,
        )
        return resp

    @app.post("/logout", dependencies=[Depends(require_csrf)])
    def logout(
        request: Request, user: Annotated[dict, Depends(require_login)]
    ):
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            app.state.db.execute("DELETE FROM sessions WHERE token=?", (token,))
            app.state.db.commit()
        resp = RedirectResponse("/login", status_code=302)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    app.include_router(workbench_router)
    @app.get("/settings")
    def settings_page(
        request: Request, user: Annotated[dict, Depends(require_operator)]
    ):
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "user": user,
                "csrf": make_csrf(settings.secret_key, user["username"]),
            },
        )

    return app
