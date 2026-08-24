"""操作日志页：只读展示审计记录（时间倒序，limit 500），无任何写入口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from bsa_web import audit
from bsa_web.auth import make_csrf, require_login

router = APIRouter(tags=["audit"])

PAGE_LIMIT = 500


@router.get("/audit")
def audit_log(
    request: Request,
    user: Annotated[dict, Depends(require_login)],
):
    records = audit.list_records(request.app.state.db, limit=PAGE_LIMIT)
    csrf = make_csrf(request.app.state.settings.secret_key, user["username"])
    return request.app.state.templates.TemplateResponse(
        request,
        "audit_log.html",
        {"user": user, "csrf": csrf, "records": records},
    )
