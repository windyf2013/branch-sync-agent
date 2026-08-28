"""WebSSH（ttyd）受限终端路由：open/页面/任务入口/close/WS 反代。

- ``POST /api/ssh/open``（operator + CSRF）→ spawn ttyd、存会话、审计，返回 token；
- ``GET /ssh/task/{cycle}/{target}``（operator）→ 任务详情页的 WebSSH 入口，直接
  打开并 302 到终端页（免二次认证 = 复用平台会话）；
- ``GET /ssh/{token}``（登录）→ xterm.js 终端页，前端连 ``/ssh/ws/{token}``；
- ``POST /api/ssh/close``（operator + CSRF）→ kill 进程、清行、审计；
- WS ``/ssh/ws/{token}``（登录 + token 未过期）→ 反代到 ``ws://127.0.0.1:<port>/``。

token 15 分钟过期：页面/WS 均先过平台登录态，token 仅寻址 + 绑定会话，过期即失效。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

import websockets
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.websockets import WebSocket
from pydantic import BaseModel

from bsa_web import auth, projection, ssh
from bsa_web.auth import make_csrf, require_csrf, require_login, require_operator

api_router = APIRouter(tags=["webssh_api"])
router = APIRouter(prefix="/ssh", tags=["webssh"])

logger = logging.getLogger("bsa_web.ssh")

# 测试可经本模块名 patch spawn（spawn_ttyd 为模块级函数，便于 monkeypatch 假实现）
spawn_ttyd = ssh.spawn_ttyd

_OPENABLE_STATUSES = ("FAILED", "PARTIAL", "MANUAL")


def _render(request: Request, **extra) -> object:
    return request.app.state.templates.TemplateResponse(request, "ssh.html", extra)


def _app_for(websocket: WebSocket):
    return websocket.scope["app"]


def _ws_user(websocket: WebSocket) -> dict:
    """WebSocket 会话态校验：复用平台登录 cookie（免二次认证）。"""
    headers = {k.lower(): v for k, v in websocket.scope.get("headers", [])}
    raw_cookie = headers.get(b"cookie", b"")
    cookie = raw_cookie.decode("latin-1") if isinstance(raw_cookie, bytes) else raw_cookie
    token = next(
        (
            part.split("=", 1)[1]
            for part in cookie.split("; ")
            if part.startswith(f"{auth.SESSION_COOKIE}=")
        ),
        None,
    )
    if not token:
        raise HTTPException(status_code=403, detail="未登录")
    app = _app_for(websocket)
    session = auth.get_session_user(
        app.state.db,
        token,
        app.state.settings.session_ttl_sec,
    )
    if session is None:
        raise HTTPException(status_code=403, detail="未登录")
    return {"username": session[0], "role": session[1]}


def _branch_for(request: Request, cycle_id: str, target: str) -> dict:
    """读投影取分支；周期/分支缺失抛 404。"""
    payload = projection.load_cycle(request.app.state.settings.log_dir, cycle_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="周期不存在或数据不可用")
    branch = (payload.get("branch_results") or {}).get(target)
    if branch is None:
        raise HTTPException(status_code=404, detail="目标分支不存在")
    return branch


def _open_session_for(request: Request, user: str, cycle_id: str, target: str) -> str:
    """校验任务可处理（FAILED/PARTIAL/MANUAL）且 worktree 存在 → open，返回 token。"""
    branch = _branch_for(request, cycle_id, target)
    if branch.get("status") not in _OPENABLE_STATUSES:
        raise HTTPException(status_code=400, detail="仅失败/部分成功/人工处理任务可打开终端")
    worktree = branch.get("worktree_path") or ""
    if not worktree:
        raise HTTPException(status_code=400, detail="该任务无工作树，无法打开终端")
    try:
        return ssh.open_session(
            request.app.state.db,
            user,
            cycle_id,
            target,
            worktree,
            spawn=spawn_ttyd,
        )
    except ssh.SshSpawnError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from None
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"打开终端失败: {exc}") from None


class OpenBody(BaseModel):
    cycle_id: str | None = None
    target: str | None = None


class CloseBody(BaseModel):
    token: str | None = None


@api_router.post("/api/ssh/open", dependencies=[Depends(require_operator), Depends(require_csrf)])
def api_open(
    request: Request,
    body: OpenBody,
    user: Annotated[dict, Depends(require_operator)],
):
    if not body.cycle_id or not body.target:
        raise HTTPException(status_code=400, detail="缺少 cycle_id 或 target")
    token = _open_session_for(
        request, user["username"], body.cycle_id, body.target
    )
    return {"token": token}


@api_router.post("/api/ssh/close", dependencies=[Depends(require_operator), Depends(require_csrf)])
def api_close(
    request: Request,
    body: CloseBody,
    user: Annotated[dict, Depends(require_operator)],
):
    if not body.token:
        raise HTTPException(status_code=400, detail="缺少 token")
    if not ssh.close_session(request.app.state.db, body.token):
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    return {"ok": True}


@router.get("/task/{cycle_id}/{target:path}")
def task_entry(
    request: Request,
    cycle_id: str,
    target: str,
    user: Annotated[dict, Depends(require_operator)],
):
    token = _open_session_for(request, user["username"], cycle_id, target)
    return RedirectResponse(url=f"/ssh/{token}", status_code=302)


@router.get("/{token}")
def ssh_page(
    request: Request,
    token: str,
    user: Annotated[dict, Depends(require_login)],
):
    session = ssh.session_info(request.app.state.db, token)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    return _render(
        request,
        user=user,
        csrf=make_csrf(request.app.state.settings.secret_key, user["username"]),
        token=token,
        ws_url=f"/ssh/ws/{token}",
        worktree=session["worktree"],
    )


async def _ws_relay(ws, uri: str, session: dict) -> None:
    """双向转发：客户端 <-> ttyd WebSocket；任一端断开即关闭对端。

    ttyd 1.7 用二进制帧（首字节类型标记：0=JSON 控制、1=输出/输入数据），
    必须按字节转发，不能走 receive_text/send_text（mock 假 ttyd 发文本
    会掩盖此问题——真 ttyd 全二进制帧）。
    """
    async with websockets.connect(uri, subprotocols=["tty"]) as conn:

        async def client_to_server() -> None:
            while True:
                message = await ws.receive()
                # 浏览器端：JSON 初始化/resize 走文本帧，终端输入走二进制帧
                # （ttyd 1.7 混合帧型），必须按原帧型转发。
                if "text" in message:
                    await conn.send(message["text"])
                elif "bytes" in message:
                    await conn.send(message["bytes"])
                else:
                    break

        async def server_to_client() -> None:
            while True:
                message = await conn.recv()
                await ws.send_bytes(message)

        done, pending = await asyncio.wait(
            {asyncio.create_task(client_to_server()), asyncio.create_task(server_to_client())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            task.cancel()
            try:
                task.result()
            except Exception:
                pass


@router.websocket("/ws/{token}")
async def ssh_ws(websocket: WebSocket, token: str):
    _ws_user(websocket)
    session = ssh.session_info(_app_for(websocket).state.db, token)
    if session is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    uri = f"ws://127.0.0.1:{session['port']}/ws"
    try:
        await _ws_relay(websocket, uri, session)
        # relay 正常返回 = ttyd 侧已断开（空闲超时/close 杀进程）。必须显式关闭
        # 客户端 WS，否则浏览器 onclose 不触发、页面停留在“已连接”。
        await websocket.close()
    except websockets.exceptions.WebSocketException as exc:
        logger.warning("ws 反代 ttyd 失败（%s）: %s", uri, exc)
        await websocket.close(code=1011)
    except Exception as exc:
        logger.warning("ws 反代异常（%s）: %s", uri, exc)
        await websocket.close(code=1011)
    finally:
        # WS 一旦断开（含客户端直接关浏览器/断网）必须回收 ttyd 进程，否则泄漏
        # 成孤儿（start_new_session 独立进程组，不被 web 进程重启收割）。close_session
        # 幂等：行已删返回 False，不重复审计；进程句柄缺失则仅清行。
        try:
            ssh.close_session(_app_for(websocket).state.db, token)
        except Exception as exc:  # noqa: BLE001 — 回收失败不能把 WS 错误放大
            logger.warning("回收 ttyd 会话失败（%s）: %s", token, exc)
