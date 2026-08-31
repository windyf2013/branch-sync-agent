"""周期/目标/commit 详情路由。

数据全部来自 `bsa report <cycle> --json` 的投影 payload；patch/编译日志按需
在详情页读取（不塞进列表页），超长内容截断展示并附原始下载链接。log_path /
patch_path 是 V1 写死的绝对或相对路径，解析前先做 log_dir 路径包含校验
（纵深防御：V1 数据被篡改时禁止越权读取 log_dir 之外的服务器文件）。
"""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from bsa_web import projection
from bsa_web.auth import make_csrf, require_login

router = APIRouter(prefix="/cycle", tags=["detail"])

_LOG_PREVIEW_LINES = 100
_PATCH_PREVIEW_LINES = 500


def _render(request: Request, template: str, **extra) -> object:
    return request.app.state.templates.TemplateResponse(request, template, extra)


def _load_payload(log_dir: str, cycle_id: str) -> dict:
    """读投影 payload；周期不存在 / 子进程失败统一抛 404 友好提示。"""
    payload = projection.load_cycle(log_dir, cycle_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="周期不存在或数据不可用")
    return payload


def _resolve_within_log_dir(log_dir: str, candidate: str | None) -> Path | None:
    """把投影里的候选路径解析成绝对路径，且必须落在 log_dir 内。

    纵深防御：投影数据若被篡改（如 state.sqlite3），禁止借详情页越权读取
    log_dir 之外的服务器文件。越界 / 空值一律返回 None。
    """
    if not candidate:
        return None
    root = Path(log_dir).resolve()
    path = Path(candidate).resolve()
    return path if path.is_relative_to(root) else None


def _read_build_log(
    log_dir: str,
    log_path: str | None,
    max_lines: int = _LOG_PREVIEW_LINES,
) -> tuple[str, bool] | None:
    """读 build 日志尾部 max_lines 行（失败原因在日志末尾）；文件缺失 / 越界 / 不可读返回 None。"""
    path = _resolve_within_log_dir(log_dir, log_path)
    if path is None or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    truncated = len(lines) > max_lines
    return "\n".join(lines[-max_lines:]), truncated


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
    detected = payload.get("detected_commits") or []
    sources = payload.get("sources") or sorted(
        {c.get("source_branch") for c in detected if c.get("source_branch")}
    )
    targets = payload.get("targets") or list(branches.keys())
    detected_by_source: dict[str, list[dict]] = {}
    for c in detected:
        detected_by_source.setdefault(c.get("source_branch") or "未知来源", []).append(c)
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
        detected_commits=detected,
        sources=sources,
        targets=targets,
        detected_by_source=detected_by_source,
        cycle_summary=projection.cycle_summary(payload),
    )


@router.get("/{cycle_id}/target/{target}")
def target_detail(
    request: Request,
    cycle_id: str,
    target: str,
    user: Annotated[dict, Depends(require_login)],
):
    # 目标分支的唯一权威页是 /task/{cycle}/{target}（含完整只读内容 + 全部操作）。
    # 本只读路由收敛为 302，旧书签/旧链接自动落到权威页；先确认周期存在（否则 404）。
    settings = request.app.state.settings
    _load_payload(settings.log_dir, cycle_id)
    return RedirectResponse(url=f"/task/{cycle_id}/{target}", status_code=302)


@router.get("/{cycle_id}/commit/{sha}")
def commit_detail(
    request: Request,
    cycle_id: str,
    sha: str,
    user: Annotated[dict, Depends(require_login)],
    target: str | None = None,
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
    diff_text = commit.get("patch_text") or ""
    diff_lines = diff_text.splitlines()
    diff_truncated = len(diff_lines) > _PATCH_PREVIEW_LINES
    conclusions = sorted(
        ((t, c) for t, c in ((payload.get("decisions") or {}).get(sha) or {}).items()),
        key=lambda kv: kv[0],
    )
    # 该 commit 在各目标分支上的处理结果（冲突解决 / 冲突失败原因 / 编译结果），
    # 供 commit 详情页透出，避免只看结论看不出处理过程。
    commit_results = []
    for target, b in (payload.get("branch_results") or {}).items():
        cr = next((c for c in (b.get("commits") or []) if c.get("sha") == sha), None)
        if cr is not None:
            commit_results.append((target, cr))
    return _render(
        request,
        "detail.html",
        view="commit",
        user=user,
        csrf=csrf,
        payload=payload,
        target=target,
        commit=commit,
        conclusions=conclusions,
        commit_results=commit_results,
        diff_preview="\n".join(diff_lines[:_PATCH_PREVIEW_LINES]),
        diff_truncated=diff_truncated,
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
    path = _resolve_within_log_dir(request.app.state.settings.log_dir, patch_path)
    if path is None or not path.is_file():
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
    path = _resolve_within_log_dir(request.app.state.settings.log_dir, log_path)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="build 日志不存在")
    return FileResponse(path, media_type="text/plain", filename=path.name)


@router.get("/{cycle_id}/target/{target}/baseline/{model}/log")
def baseline_log_download(
    request: Request,
    cycle_id: str,
    target: str,
    model: str,
    user: Annotated[dict, Depends(require_login)],
):
    payload = _load_payload(request.app.state.settings.log_dir, cycle_id)
    branch = (payload.get("branch_results") or {}).get(target)
    if branch is None:
        raise HTTPException(status_code=404, detail="目标分支不存在")
    outcome = ((branch or {}).get("baseline") or {}).get(model)
    log_path = (outcome or {}).get("log_path")
    if not log_path:
        raise HTTPException(status_code=404, detail="build 日志不存在")
    path = _resolve_within_log_dir(request.app.state.settings.log_dir, log_path)
    if path is None or not path.is_file():
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
