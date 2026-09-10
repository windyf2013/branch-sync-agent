import json
import logging
import logging.handlers
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from bsa_web import failure
from bsa_web.api.abandon import router as abandon_api_router
from bsa_web.api.manual_review import router as manual_review_api_router
from bsa_web.api.operations import router as operations_api_router
from bsa_web.api.push import router as push_api_router
from bsa_web.api.users import router as users_api_router
from bsa_web.auth import (
    SESSION_COOKIE,
    DbAuthenticator,
    create_session,
    current_user,
    make_csrf,
    provision_user,
    require_admin,
    require_csrf,
    require_login,
)
from bsa_web.db import InstanceLock, bootstrap_users, init_db
from bsa_web.rbac import role_label_zh
from bsa_web.runner import enqueue_task, get_task
from bsa_web.settings import WebSettings
from bsa_web.views.audit_log import router as audit_log_router
from bsa_web.views.detail import router as detail_router
from bsa_web.views.history import router as history_router
from bsa_web.views.operations import router as operations_view_router
from bsa_web.views.ssh import api_router as ssh_api_router
from bsa_web.views.ssh import router as ssh_router
from bsa_web.views.task_detail import router as task_detail_router
from bsa_web.views.workbench import router as workbench_router

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

# 静态资源版本号：取 style.css 的 mtime。StaticFiles 只发 ETag、不发
# Cache-Control，浏览器会按启发式缓存（≈ 距 last-modified 时长的 10%）而
# 不回源校验——改版后用户长时间看到旧样式（旧 CSS + 新 HTML 会错位，
# 例如侧栏 logo 白字压白底而"消失"）。把 mtime 拼进 URL，样式一改即换 URL。
_STYLE_CSS = Path(__file__).resolve().parent / "static" / "style.css"


def _asset_version() -> str:
    try:
        return str(int(_STYLE_CSS.stat().st_mtime))
    except OSError:
        return "0"


# 注册的是函数本身，模板里须写成 {{ asset_v() }}；每次渲染重取 mtime，改样式即自动换 URL。
templates.env.globals["asset_v"] = _asset_version


def _localtime(value) -> str:
    """ISO 时间字符串转宿主本地时区显示（统一 YYYY-MM-DD HH:MM:SS）。

    平台时间（tasks/audit）存 UTC（``+00:00``）、V1 周期存本地 naive、git
    commit 带偏移（如 ``+08:00``）；直接原样显示会让 UTC 时间在 CST 宿主上
    落后 8 小时。这里统一解析并转到本地时区：
    - 带 tzinfo 的（UTC / +08:00）→ astimezone() 转本地
    - naive（V1 周期本地时间）→ 视为本地原样格式化
    解析失败返回原值。
    """
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return str(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


templates.env.filters["localtime"] = _localtime


# 面向人类的状态值/四态 → 中文映射（统一收敛在过滤器，模板只写 `| status_zh` /
# `| kind_zh`，避免散落各页的硬编码英→中 if/else）。值不在表内则原样返回（兜底）。
_STATUS_ZH = {
    "SUCCESS": "成功",
    "FAILED": "失败",
    "PARTIAL": "停批",
    "MANUAL": "待处理",
    "UNKNOWN": "未知",
    "RUNNING": "进行中",
    "REPORTED": "已报告",
    "OK": "通过",
    "EMPTY": "已应用",
    "CONFLICT": "冲突",
    "SKIPPED": "已跳过",
}

# 进度步骤徽章（progress.jsonl 的节点终态 → 中文）。与 _STATUS_ZH 分离：这些值
# 只出现在步骤清单（_steps.html），不参与分支/commit/build 的既有 status_zh 展示。
_STEP_ZH = {
    "CHERRY_PICK_OK": "已应用",
    "CHERRY_PICK_EMPTY": "已在目标",
    "CHERRY_PICK_CONFLICT": "冲突",
    "CHERRY_PICK_FAILED": "cherry-pick 失败",
    "BUILD_OK": "编译通过",
    "BUILD_FAILED": "编译失败",
    "BASELINE_OK": "基线通过",
    "BASELINE_FAILED": "基线失败",
    "RESOLVED": "冲突已解决",
    "RESOLUTION_FAILED": "解决失败",
    "FAILFAST_STOP": "停批",
    "PATCHED": "已出补丁",
    "PREPARED": "已就绪",
    "DETECTED": "已检出",
    "DECIDED": "已判定",
    "UNRESOLVABLE": "无法修复",
    "SUCCESS": "成功",
    "FAILED": "失败",
    "PARTIAL": "停批",
    "MANUAL": "需人工处理",
}

_FOUR_STATE_ZH = {
    "NeedSync": "待同步",
    "AlreadyIncluded": "已包含",
    "OutOfScope": "不适用",
    "ManualReview": "待人工",
}


templates.env.filters["status_zh"] = lambda v: _STATUS_ZH.get(str(v), v)
templates.env.filters["step_zh"] = lambda v: _STEP_ZH.get(str(v), v)
templates.env.filters["kind_zh"] = lambda v: _FOUR_STATE_ZH.get(str(v), v)
templates.env.filters["role_zh"] = role_label_zh
# 引擎写死的英文 stop_reason / 节点名 → 自然中文（统一收敛在过滤器，模板只写
# `| stop_reason_zh` / `| node_zh`，避免英文代码化文案散落各页）。
templates.env.filters["stop_reason_zh"] = failure.humanize_stop_reason
templates.env.filters["node_zh"] = failure.node_label

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
    """只读会话用户名（不做续期），供访问日志记录操作者。

    best-effort：DB 故障时静默降级为 ""，避免在中间件 finally 中抛错
    毁掉响应状态（如 /healthz 的 503）与请求日志行。
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return ""
    try:
        row = request.app.state.db.execute(
            "SELECT user FROM sessions WHERE token=?", (token,)
        ).fetchone()
    except Exception:
        return ""
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


def create_app(*, settings_override: dict | None = None, env_file: str | None = ".env") -> FastAPI:
    settings = _build_settings(settings_override, env_file=env_file)
    app = FastAPI(title="BSA Web 工作台")
    app.state.settings = settings
    app.state.templates = templates
    db_path = Path(settings.log_dir) / "platform.sqlite3"
    # 单实例守卫：必须先于 init_db/runner 持锁，第二个 web 实例在此即抛错，
    # 杜绝两个实例共享同一平台库时 _recover_stale_tasks 误标运行中任务。
    instance_lock = InstanceLock(db_path)
    app.state.db = init_db(db_path)
    app.state.instance_lock = instance_lock
    bootstrap_users(app.state.db, settings.users)
    app.state.authenticator = DbAuthenticator(app.state.db)

    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
        name="static",
    )

    # 任务执行由独立 bsa_web.executor 守护进程负责（web 不持有执行线程、
    # 不 spawn CLI）。web 暴露 DB 直读直写工具供提交/查询：
    app.state.enqueue_task = enqueue_task
    app.state.get_task = get_task

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
        # 令牌绑定当前会话用户（未登录则为空字符串）：已登录用户再访登录页
        # 重新登录时，提交的 _csrf 才能与会话匹配，避免 require_csrf 误判 403。
        user = current_user(request)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "csrf": make_csrf(settings.secret_key, user["username"] if user else ""),
                "error": None,
            },
        )

    @app.post("/login", dependencies=[Depends(require_csrf)])
    def login_submit(
        request: Request, username: str = Form(...), password: str = Form(...)
    ):
        role = app.state.authenticator.authenticate(username, password)
        if role is None:
            # 常规账号认证失败后，尝试 JIT 自助登记：密码命中共享口令且用户名
            # 未占用则建号（白名单内建 admin，否则 operator）。
            role = provision_user(
                app.state.db,
                username,
                password,
                settings.signup_password,
                settings.signup_admin_users,
            )
        if role is None:
            user = current_user(request)
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "csrf": make_csrf(
                        settings.secret_key, user["username"] if user else ""
                    ),
                    "error": "用户名或密码错误",
                },
                status_code=400,
            )
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
    app.include_router(abandon_api_router)
    app.include_router(push_api_router)
    app.include_router(users_api_router)
    app.include_router(task_detail_router)
    app.include_router(ssh_api_router)
    app.include_router(ssh_router)
    @app.get("/settings")
    def settings_page(
        request: Request, user: Annotated[dict, Depends(require_admin)]
    ):
        # 只读展示非敏感有效配置：不暴露 secret_key 与 bcrypt hash。
        # 用户清单从 users 表读取（admin 经 UI 增删改后的权威真相源）。
        rows = app.state.db.execute(
            "SELECT username, role FROM users ORDER BY username"
        ).fetchall()
        users = [
            {"username": r["username"], "role": r["role"], "role_zh": role_label_zh(r["role"])}
            for r in rows
        ]
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "user": user,
                "csrf": make_csrf(settings.secret_key, user["username"]),
                "config": {
                    "日志目录": settings.log_dir,
                    "分支清单文件": settings.branch_file or "（未配置）",
                    "Web 端口": settings.bsa_web_port,
                    "会话有效期（秒）": settings.session_ttl_sec,
                    "Cookie 仅 HTTPS": "是" if settings.cookie_secure else "否",
                },
                "users": users,
                "roles": [
                    {"value": "admin", "label": "管理员"},
                    {"value": "operator", "label": "操作者"},
                    {"value": "viewer", "label": "查看者"},
                ],
            },
        )

    return app
