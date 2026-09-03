"""管理员用户管理 API：创建用户 / 改角色 / 重置密码 / 删除用户。

全部端点 require_admin + CSRF（JSON body ``_csrf``），未登录/非管理员一律 403
（JSON 场景不用 302 跳转）。每次变更写审计留痕。安全不变量：

- 永远保留至少一个 admin：删除或降级「最后一个 admin」返回 400，杜绝锁死；
- 禁止删除当前登录者自身（防止自锁）；
- 角色值写入前用 ``ALL_ROLES`` 白名单 fail-fast（400），权威存储不落脏值——
  与 env 引导的「宽松回退」不同，这是管理员的显式输入，静默降级是脚枪。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from bsa_web import audit
from bsa_web.auth import current_user, hash_password, require_csrf
from bsa_web.rbac import ADMIN, ALL_ROLES

router = APIRouter(prefix="/api/users", tags=["users"])


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _require_admin_api(request: Request) -> dict:
    """API 权限校验：未登录/非管理员一律 403（JSON 场景不用 302 跳转）。"""
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=403, detail="需要登录")
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _get_row(request: Request, username: str):
    return request.app.state.db.execute(
        "SELECT username, role FROM users WHERE username=?", (username,)
    ).fetchone()


def _admin_count(request: Request) -> int:
    row = request.app.state.db.execute(
        "SELECT COUNT(*) AS n FROM users WHERE role=?", (ADMIN,)
    ).fetchone()
    return row["n"] if row is not None else 0


def _guard_last_admin(request: Request, target_role: str, new_role: str | None = None) -> None:
    """目标为 admin 且是最后一个 admin 时，删除/降级将被拒绝。

    ``new_role`` 为 None 表示删除；否则表示改角色目标值。仅当「从 admin 变成
    非 admin」或「删除 admin」且 admin 数量为 1 时触发保护。
    """
    if target_role != ADMIN:
        return
    if new_role == ADMIN:
        return  # 仍保留 admin，不触发
    if _admin_count(request) <= 1:
        raise HTTPException(status_code=400, detail="不能删除或降级最后一个管理员")


class CreateUserBody(BaseModel):
    username: str
    password: str
    role: str


class RoleBody(BaseModel):
    role: str


class PasswordBody(BaseModel):
    password: str


@router.post("", status_code=201, dependencies=[Depends(require_csrf)])
def create_user(
    request: Request,
    body: CreateUserBody,
    user: Annotated[dict, Depends(_require_admin_api)],
):
    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="用户名不能为空")
    if not body.password:
        raise HTTPException(status_code=400, detail="密码不能为空")
    role = body.role.strip().lower()
    if role not in ALL_ROLES:
        raise HTTPException(status_code=400, detail="角色非法（admin/operator/viewer）")
    if _get_row(request, username) is not None:
        raise HTTPException(status_code=409, detail="用户名已存在")
    pwhash = hash_password(body.password)
    request.app.state.db.execute(
        "INSERT INTO users(username, pwhash, role, created_at) VALUES (?,?,?,?)",
        (username, pwhash, role, _now_iso()),
    )
    request.app.state.db.commit()
    audit.record(
        request.app.state.db,
        user["username"],
        "user_create",
        target=username,
        detail={"role": role},
        result="ok",
    )
    return {"ok": True, "username": username, "role": role}


@router.post("/{username}/role", dependencies=[Depends(require_csrf)])
def change_role(
    request: Request,
    username: str,
    body: RoleBody,
    user: Annotated[dict, Depends(_require_admin_api)],
):
    row = _get_row(request, username)
    if row is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    role = body.role.strip().lower()
    if role not in ALL_ROLES:
        raise HTTPException(status_code=400, detail="角色非法（admin/operator/viewer）")
    _guard_last_admin(request, row["role"], new_role=role)
    request.app.state.db.execute(
        "UPDATE users SET role=? WHERE username=?", (role, username)
    )
    request.app.state.db.commit()
    audit.record(
        request.app.state.db,
        user["username"],
        "user_role_change",
        target=username,
        detail={"role": role},
        result="ok",
    )
    return {"ok": True, "username": username, "role": role}


@router.post("/{username}/password", dependencies=[Depends(require_csrf)])
def reset_password(
    request: Request,
    username: str,
    body: PasswordBody,
    user: Annotated[dict, Depends(_require_admin_api)],
):
    if _get_row(request, username) is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if not body.password:
        raise HTTPException(status_code=400, detail="密码不能为空")
    pwhash = hash_password(body.password)
    request.app.state.db.execute(
        "UPDATE users SET pwhash=? WHERE username=?", (pwhash, username)
    )
    request.app.state.db.commit()
    audit.record(
        request.app.state.db,
        user["username"],
        "user_password_reset",
        target=username,
        result="ok",
    )
    return {"ok": True, "username": username}


@router.post("/{username}/delete", dependencies=[Depends(require_csrf)])
def delete_user(
    request: Request,
    username: str,
    user: Annotated[dict, Depends(_require_admin_api)],
):
    row = _get_row(request, username)
    if row is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if username == user["username"]:
        raise HTTPException(status_code=400, detail="不能删除当前登录用户")
    _guard_last_admin(request, row["role"])
    request.app.state.db.execute("DELETE FROM users WHERE username=?", (username,))
    # 同时清掉该用户的活跃会话，使其即刻失效（current_user 也已按 users 表兜底登出）
    request.app.state.db.execute("DELETE FROM sessions WHERE user=?", (username,))
    request.app.state.db.commit()
    audit.record(
        request.app.state.db,
        user["username"],
        "user_delete",
        target=username,
        result="ok",
    )
    return {"ok": True, "username": username}
