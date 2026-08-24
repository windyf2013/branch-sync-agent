import json
import logging
import logging.handlers
import time
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from bsa_web.api.manual_review import router as manual_review_api_router
from bsa_web.api.operations import router as operations_api_router
from bsa_web.api.push import router as push_api_router
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
from bsa_web.runner import TaskRunner
from bsa_web.settings import WebSettings
from bsa_web.views.audit_log import router as audit_log_router
from bsa_web.views.detail import router as detail_router
from bsa_web.views.history import router as history_router
from bsa_web.views.operations import router as operations_view_router
from bsa_web.views.workbench import router as workbench_router

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

_access_logger = logging.getLogger("bsa_web.access")


def _configure_access_logger(log_dir: str | Path) -> logging.Logger:
    """为 ``bsa_web.access`` 挂 JSON 行文件 handler（供 logrotate 轮转）。

    用 ``WatchedFileHandler``：logrotate rename 后下次写入自动重开新文件。
    同路径 handler 幂等去重，避免多次 create_app 重复挂载。
    """
    logger = logging.getLogger("bsa_web.access")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    path = Path(log_dir) / "access.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not any(
        isinstance(h, logging.handlers.WatchedFileHandler) and h.baseFilename == str(path)
        for h in logger.handlers
    ):
        handler = logging.handlers.WatchedFileHandler(str(path), encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    return logger


def _request_username(request: Request) -> str:
    """只读会话用户名（不做续期），供访问日志记录操作者。"""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return ""
    row = request.app.state.db.execute(
        "SELECT user FROM sessions WHERE token=?", (token,)
    ).fetchone()
    return row["user"] if row else ""


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

    runner = TaskRunner(app.state.db, settings.log_dir)
    runner.start()
    app.state.runner = runner

    _configure_access_logger(settings.log_dir)

    @app.middleware("http")
    async def access_log_middleware(request: Request, call_next):
        """结构化请求日志：JSON 行 method/path/status/duration_ms/user。

        不记录 body/敏感信息；中间件在异常经 ServerErrorMiddleware 兜底前
        先记录 500，再向上重抛交由框架生成响应。
        """
        start = time.perf_counter()
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:
            status = 500
            raise
        finally:
            record = {
                "method": request.method,
                "path": request.url.path,
                "status": status,
                "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                "user": _request_username(request),
            }
            _access_logger.info(json.dumps(record, ensure_ascii=False))
        return response

    @app.get("/healthz")
    def healthz():
        """平台健康检查：DB 可打开/可写（SELECT 1）失败返回 503 供探活摘除。"""
        try:
            app.state.db.execute("SELECT 1").fetchone()
        except Exception:
            return JSONResponse(status_code=503, content={"status": "error"})
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
    app.include_router(audit_log_router)
    app.include_router(history_router)
    app.include_router(detail_router)
    app.include_router(operations_view_router)
    app.include_router(operations_api_router)
    app.include_router(manual_review_api_router)
    app.include_router(push_api_router)
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
