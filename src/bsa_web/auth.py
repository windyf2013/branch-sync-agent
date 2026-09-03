import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Protocol

import bcrypt
from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from bsa_web.rbac import ADMIN, OPERATOR, VIEWER, is_operator_role, normalize_role

SESSION_COOKIE = "bsa_session"
CSRF_SALT = "bsa-csrf"


class Authenticator(Protocol):
    """认证后端抽象，LDAP/SSO 就绪后替换实现。"""

    def authenticate(self, username: str, password: str) -> str | None: ...


class DbAuthenticator:
    """基于 ``users`` 表的账号认证：角色可经 admin UI 运行时变更。"""

    def __init__(self, db):
        self._db = db

    def authenticate(self, username: str, password: str) -> str | None:
        row = self._db.execute(
            "SELECT pwhash, role FROM users WHERE username=?", (username,)
        ).fetchone()
        if row is None:
            return None
        if bcrypt.checkpw(password.encode(), row["pwhash"].encode()):
            return normalize_role(row["role"])
        return None


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def provision_user(
    db, username: str, password: str, signup_password: str, admin_users: list[str]
) -> str | None:
    """JIT 自助登记：密码命中共享 ``signup_password`` 且用户名未占用时建号。

    返回新账号角色；不满足条件（功能关闭 / 密码不符 / 用户名已存在）返回
    None 且不创建。已存在用户即使输对共享密码也**不覆盖**其既有账号与角色，
    杜绝用共享密码顶掉他人身份的越权路径。
    """
    if not signup_password or not username or password != signup_password:
        return None
    row = db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
    if row is not None:
        return None
    role = ADMIN if username in admin_users else OPERATOR
    pwhash = hash_password(password)
    db.execute(
        "INSERT INTO users(username, pwhash, role, created_at) VALUES (?,?,?,?)",
        (username, pwhash, role, datetime.now(UTC).isoformat()),
    )
    db.commit()
    return role


def create_session(db, user: str, role: str, ttl_sec: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=ttl_sec)
    db.execute(
        "INSERT INTO sessions(token, user, role, created_at, expires_at) "
        "VALUES (?,?,?,?,?)",
        (token, user, role, now.isoformat(), expires_at.isoformat()),
    )
    db.commit()
    return token


def get_session_user(
    db, token: str, ttl_sec: int | None = None
) -> tuple[str, str] | None:
    """查询会话；有效会话在 ttl_sec 给定后滑动续期（闲置过期）。"""
    row = db.execute(
        "SELECT user, role, expires_at FROM sessions WHERE token=?", (token,)
    ).fetchone()
    if row is None:
        return None
    now = datetime.now(UTC)
    expires_at = datetime.fromisoformat(row["expires_at"])
    if expires_at <= now:
        return None
    if ttl_sec is not None:
        new_expires = now + timedelta(seconds=ttl_sec)
        db.execute(
            "UPDATE sessions SET expires_at=? WHERE token=?", (new_expires.isoformat(), token)
        )
        db.commit()
    return row["user"], row["role"]


def _session_user(request: Request) -> tuple[str, str] | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return get_session_user(
        request.app.state.db, token, request.app.state.settings.session_ttl_sec
    )


def current_user(request: Request) -> dict | None:
    """只读当前会话用户；无会话返回 None（API 层复用，JSON 场景不用 302）。

    角色从 ``users`` 表实时解析（而非会话行里的历史角色），使 admin 的角色
    变更/删除在用户下一次请求即生效；被删除的用户视同已登出。附派生布尔供
    模板与依赖使用，避免散落各处做原始字符串比较。
    """
    session = _session_user(request)
    if session is None:
        return None
    row = request.app.state.db.execute(
        "SELECT role FROM users WHERE username=?", (session[0],)
    ).fetchone()
    if row is None:  # 用户会话期内被删除 → 视同登出
        return None
    role = normalize_role(row["role"])
    return {
        "username": session[0],
        "role": role,
        "is_viewer": role == VIEWER,
        "is_operator": is_operator_role(role),
        "is_admin": role == ADMIN,
    }


def require_login(request: Request) -> dict:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return user


def require_operator(
    request: Request, user: Annotated[dict, Depends(require_login)]
) -> dict:
    if not user["is_operator"]:
        raise HTTPException(status_code=403, detail="需要操作者权限")
    return user


def require_admin(
    request: Request, user: Annotated[dict, Depends(require_login)]
) -> dict:
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def make_csrf(secret_key: str, user: str) -> str:
    return URLSafeTimedSerializer(secret_key, salt=CSRF_SALT).dumps(user)


def _loads_user(secret_key: str, token: str | None) -> str | None:
    if not token:
        return None
    try:
        return URLSafeTimedSerializer(secret_key, salt=CSRF_SALT).loads(token)
    except BadSignature:
        return None


async def require_csrf(request: Request) -> None:
    form = await request.form()
    token = form.get("_csrf")
    if token is None:
        # JSON API 场景：token 放请求体（与表单隐藏字段同一套签名）
        try:
            body = await request.json()
        except Exception:
            body = {}
        token = body.get("_csrf")
    secret_key = request.app.state.settings.secret_key
    signed_user = _loads_user(secret_key, token)
    if signed_user is None:
        raise HTTPException(status_code=403, detail="CSRF 校验失败")
    session = _session_user(request)
    if session is not None and signed_user != session[0]:
        raise HTTPException(status_code=403, detail="CSRF 校验失败")
