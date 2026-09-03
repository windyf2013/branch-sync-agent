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

from bsa_web import failure, projection
from bsa_web.auth import make_csrf, require_login
from bsa_web.build_labels import enrich_build_outcomes
from bsa_web.progress import read_progress
from bsa_web.steps import enrich_steps

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
    # 基线编译失败的完整日志/错误定位：投影原始 errors 是大块编译输出，逐分支
    # 用 build_labels 从日志尾部提炼 error_lines + log_preview（读一次缓存 outcome
    # 上），周期概览/任务详情时间线据此展开「错误定位 / build 日志尾部」。
    for branch in branches.values():
        enrich_build_outcomes(branch, settings.log_dir)
    # 处理过程时间线：读该周期 progress.jsonl，沿执行顺序把相邻同 target 步骤
    # 合成一组（探测/判定/报告等周期级步骤无 target → 「周期流程」段；分支级
    # 步骤各自成段）。分组只切分不重排，忠实反映工作流；再把各分支投影的处理
    # 产物富化挂到对应步骤展开（复用任务详情页同一套 _steps 交互）。
    # 运行中周期已在上方 early-return，此处必为非 running。
    step_groups: list[dict] = []
    raw = read_progress(settings.log_dir, cycle_id)
    for s in raw:
        key = s.get("target") or None
        if not step_groups or step_groups[-1]["target"] != key:
            step_groups.append({"target": key, "steps": []})
        step_groups[-1]["steps"].append(s)
    # 周期级 report 步骤补一句周期失败归因旁注：report 节点回传的是周期终态
    # （FAILED = 周期有分支失败/需人工，报告本身已生成、report.html 已落盘），
    # 进度行把它记为 FAILED 是语义正确的。只有节点真正抛异常（异常被 node_wrapper
    # 捕获记入 action_required node:report 且 errors 里无它）才算报告失败。
    report_node_failed = any(
        item.get("node") == "report"
        for item in (payload.get("action_required") or [])
    )
    for g in step_groups:
        for s in g["steps"]:
            if s.get("node") == "report" and not report_node_failed:
                s["detail"] = {
                    "reason": (
                        "报告已生成（report.html 已落盘）。此处「失败」是报告节点"
                        "回传的周期终态——周期内有分支失败 / 需人工处理，并非"
                        "生成报告本身失败。"
                    ),
                }
    for g in step_groups:
        enrich_steps(
            g["steps"], branches.get(g["target"]),
            cycle_id=cycle_id,
        )
    # 完整拓扑字段（含零检出源）由引擎投影提供（change1）；缺失时降级从
    # detected_commits 推导，仅能覆盖检出过 commit 的源，零检出源不可推导。
    topology_degraded = "sources" not in payload and "targets" not in payload
    sources = payload.get("sources") or sorted(
        {c.get("source_branch") for c in detected if c.get("source_branch")}
    )
    targets = payload.get("targets") or list(branches.keys())
    detected_by_source: dict[str, list[dict]] = {}
    for c in detected:
        detected_by_source.setdefault(c.get("source_branch") or "未知来源", []).append(c)
    # 零检出源 = 完整源全集 - 检出过 commit 的源，标注「本期无新 commit（正常）」。
    zero_detected_sources = [
        s for s in sources if s not in detected_by_source
    ]
    return _render(
        request,
        "detail.html",
        view="cycle",
        user=user,
        csrf=csrf,
        payload=payload,
        running=False,
        cycle_id=cycle_id,
        step_groups=step_groups,
        steps_cycle_id=cycle_id,
        action_required=payload.get("action_required") or [],
        synced_branches=[
            b for b in branches.values() if b.get("status") == "SUCCESS"
        ],
        all_branches=list(branches.values()),
        detected_commits=detected,
        sources=sources,
        targets=targets,
        detected_by_source=detected_by_source,
        topology_degraded=topology_degraded,
        zero_detected_sources=zero_detected_sources,
        cycle_summary=projection.cycle_summary(payload),
        decision_breakdown=failure.decision_breakdown(payload),
        destinations=failure.commit_destinations(payload),
        cycle_failures=failure.failure_summary(payload),
        cycle_has_review=bool(
            any(
                item.get("kind") == "ManualReview"
                for item in (payload.get("action_required") or [])
            )
        ),
        commit_verdicts={
            c.get("sha"): failure.commit_bucket(payload, c.get("sha"))
            for c in detected if c.get("sha")
        },
        commit_rationales={
            c.get("sha"): failure.commit_rationale(payload, c.get("sha"))
            for c in detected if c.get("sha")
        },
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
