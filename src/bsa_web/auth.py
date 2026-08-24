import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Protocol

import bcrypt
from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from bsa_web.rbac import OPERATOR

SESSION_COOKIE = "bsa_session"
CSRF_SALT = "bsa-csrf"


class Authenticator(Protocol):
    """认证后端抽象，LDAP/SSO 就绪后替换实现。"""

    def authenticate(self, username: str, password: str) -> str | None: ...

    def roles_for(self, username: str) -> str: ...


class EnvAuthenticator:
    """解析 BSA_USERS（user:bcrypt_hash:role,user2:...）的静态账号认证。"""

    def __init__(self, raw: str):
        self._users: dict[str, tuple[str, str]] = {}
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(":")
            if len(parts) != 3:
                continue
            username, pwhash, role = parts
            self._users[username] = (pwhash, role)

    def roles_for(self, username: str) -> str:
        entry = self._users.get(username)
        return entry[1] if entry else ""

    def authenticate(self, username: str, password: str) -> str | None:
        entry = self._users.get(username)
        if entry is None:
            return None
        pwhash, role = entry
        if bcrypt.checkpw(password.encode(), pwhash.encode()):
            return role
        return None


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


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


def require_login(request: Request) -> dict:
    user = _session_user(request)
    if user is None:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return {"username": user[0], "role": user[1]}


def require_operator(
    request: Request, user: Annotated[dict, Depends(require_login)]
) -> dict:
    if user["role"] != OPERATOR:
        raise HTTPException(status_code=403, detail="需要操作者权限")
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
    secret_key = request.app.state.settings.secret_key
    signed_user = _loads_user(secret_key, token)
    if signed_user is None:
        raise HTTPException(status_code=403, detail="CSRF 校验失败")
    session = _session_user(request)
    if session is not None and signed_user != session[0]:
        raise HTTPException(status_code=403, detail="CSRF 校验失败")
