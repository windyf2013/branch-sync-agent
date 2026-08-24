"""周期/目标/commit 详情路由。

数据全部来自 `bsa report <cycle> --json` 的投影 payload；patch/编译日志按需
在详情页读取（不塞进列表页），超长内容截断展示并附原始下载链接。log_path /
patch_path 是 V1 写死的绝对或相对路径，按投影里的值直接解析。
"""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response

from bsa_web import projection
from bsa_web.auth import make_csrf, require_login

router = APIRouter(prefix="/cycle", tags=["detail"])

_LOG_PREVIEW_LINES = 500
_PATCH_PREVIEW_LINES = 500


def _render(request: Request, template: str, **extra) -> object:
    return request.app.state.templates.TemplateResponse(request, template, extra)


def _load_payload(log_dir: str, cycle_id: str) -> dict:
    """读投影 payload；周期不存在 / 子进程失败统一抛 404 友好提示。"""
    payload = projection.load_cycle(log_dir, cycle_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="周期不存在或数据不可用")
    return payload


def _read_build_log(
    log_path: str | None, max_lines: int = _LOG_PREVIEW_LINES
) -> tuple[str, bool] | None:
    """读 build 日志前 N 行；文件缺失 / 不可读返回 None。"""
    if not log_path:
        return None
    path = Path(log_path)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    truncated = len(lines) > max_lines
    return "\n".join(lines[:max_lines]), truncated


@router.get("/{cycle_id}")
def cycle_detail(
    request: Request,
    cycle_id: str,
    user: Annotated[dict, Depends(require_login)],
):
    settings = request.app.state.settings
    csrf = make_csrf(settings.secret_key, user["username"])
    payload = _load_payload(settings.log_dir, cycle_id)
    if payload.get("status") == "running":
        # 运行中周期无最终投影，仅提示进行中，不渲染详情
        return _render(
            request, "detail.html", view="cycle", running=True, cycle_id=cycle_id,
            user=user, csrf=csrf,
        )
    branches = payload.get("branch_results") or {}
    return _render(
        request,
        "detail.html",
        view="cycle",
        user=user,
        csrf=csrf,
        payload=payload,
        running=False,
        action_required=payload.get("action_required") or [],
        synced_branches=[
            b for b in branches.values() if b.get("status") == "SUCCESS"
        ],
        all_branches=list(branches.values()),
        detected_commits=payload.get("detected_commits") or [],
    )


@router.get("/{cycle_id}/target/{target}")
def target_detail(
    request: Request,
    cycle_id: str,
    target: str,
    user: Annotated[dict, Depends(require_login)],
):
    settings = request.app.state.settings
    csrf = make_csrf(settings.secret_key, user["username"])
    payload = _load_payload(settings.log_dir, cycle_id)
    branch = (payload.get("branch_results") or {}).get(target)
    if branch is None:
        raise HTTPException(status_code=404, detail="目标分支不存在")
    # 按需读取各 commit build 日志前 N 行（不塞进列表/概览页）
    for cr in branch.get("commits") or []:
        for outcome in (cr.get("build") or {}).values():
            preview = _read_build_log(outcome.get("log_path"))
            outcome["log_preview"] = preview[0] if preview else None
            outcome["log_truncated"] = preview[1] if preview else False
    return _render(
        request,
        "detail.html",
        view="target",
        user=user,
        csrf=csrf,
        payload=payload,
        target=target,
        branch=branch,
    )


@router.get("/{cycle_id}/commit/{sha}")
def commit_detail(
    request: Request,
    cycle_id: str,
    sha: str,
    user: Annotated[dict, Depends(require_login)],
):
    settings = request.app.state.settings
    csrf = make_csrf(settings.secret_key, user["username"])
    payload = _load_payload(settings.log_dir, cycle_id)
    commit = next(
        (c for c in (payload.get("detected_commits") or []) if c.get("sha") == sha),
        None,
    )
    if commit is None:
        raise HTTPException(status_code=404, detail="commit 不存在")
    patch_text = commit.get("patch_text") or ""
    patch_lines = patch_text.splitlines()
    patch_truncated = len(patch_lines) > _PATCH_PREVIEW_LINES
    conclusions = sorted(
        ((t, c) for t, c in ((payload.get("decisions") or {}).get(sha) or {}).items()),
        key=lambda kv: kv[0],
    )
    return _render(
        request,
        "detail.html",
        view="commit",
        user=user,
        csrf=csrf,
        payload=payload,
        commit=commit,
        conclusions=conclusions,
        patch_preview="\n".join(patch_lines[:_PATCH_PREVIEW_LINES]),
        patch_truncated=patch_truncated,
    )


@router.get("/{cycle_id}/target/{target}/patch")
def target_patch_download(
    request: Request,
    cycle_id: str,
    target: str,
    user: Annotated[dict, Depends(require_login)],
):
    payload = _load_payload(request.app.state.settings.log_dir, cycle_id)
    branch = (payload.get("branch_results") or {}).get(target)
    patch_path = (branch or {}).get("patch_path")
    if branch is None or not patch_path:
        raise HTTPException(status_code=404, detail="patch 文件不存在")
    path = Path(patch_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="patch 文件不存在")
    return FileResponse(path, media_type="text/plain", filename=path.name)


@router.get("/{cycle_id}/target/{target}/commit/{sha}/build/{model}/log")
def build_log_download(
    request: Request,
    cycle_id: str,
    target: str,
    sha: str,
    model: str,
    user: Annotated[dict, Depends(require_login)],
):
    payload = _load_payload(request.app.state.settings.log_dir, cycle_id)
    branch = (payload.get("branch_results") or {}).get(target)
    if branch is None:
        raise HTTPException(status_code=404, detail="目标分支不存在")
    cr = next(
        (c for c in (branch.get("commits") or []) if c.get("sha") == sha), None
    )
    outcome = ((cr or {}).get("build") or {}).get(model) if cr else None
    log_path = (outcome or {}).get("log_path")
    if not log_path:
        raise HTTPException(status_code=404, detail="build 日志不存在")
    path = Path(log_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="build 日志不存在")
    return FileResponse(path, media_type="text/plain", filename=path.name)


@router.get("/{cycle_id}/commit/{sha}/patch")
def commit_patch_download(
    request: Request,
    cycle_id: str,
    sha: str,
    user: Annotated[dict, Depends(require_login)],
):
    payload = _load_payload(request.app.state.settings.log_dir, cycle_id)
    commit = next(
        (c for c in (payload.get("detected_commits") or []) if c.get("sha") == sha),
        None,
    )
    if commit is None:
        raise HTTPException(status_code=404, detail="commit 不存在")
    return Response(
        content=commit.get("patch_text") or "",
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{sha}.patch"'},
    )
