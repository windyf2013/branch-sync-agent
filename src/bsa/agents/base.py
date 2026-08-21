from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from bsa.config.settings import Settings
from bsa.domain.models import CommitInfo, ConflictResolution, SyncDecision
from bsa.executor.exceptions import InfrastructureError

logger = logging.getLogger("bsa.agents")


class LLMUnavailable(InfrastructureError):
    """LLM call failed after retries; callers degrade to safe defaults."""


class ConflictContext(BaseModel):
    commit: CommitInfo
    conflict_files: list[str]
    conflict_markers: dict[str, str]
    source_branch: str
    target_branch: str
    worktree: Path


class BuildErrorContext(BaseModel):
    commit: CommitInfo
    model: str
    errors: list[str]
    log_path: Path
    worktree: Path


class BuildAttribution(BaseModel):
    category: Literal["introduced_by_commit", "pre_existing", "environment", "unresolvable"]
    reason: str
    files_to_fix: list[str]
    fix_diff: str | None = None


class BuildFix(BaseModel):
    files: list[str]
    diff: str
    agent_reason: str


class FailedCommit(BaseModel):
    sha: str
    failure_summary: str
    changed_files: list[str]


class _BugFixJudgment(BaseModel):
    is_bug_fix: bool
    risk: Literal["low", "medium", "high"] | None = None
    reason: str | None = None


class _SeverityJudgment(BaseModel):
    risk: Literal["low", "medium", "high"]
    reason: str | None = None


class _ConflictOutput(BaseModel):
    files: list[str]
    diff: str
    agent_reason: str


class _BuildAttributionOutput(BaseModel):
    category: Literal["introduced_by_commit", "pre_existing", "environment", "unresolvable"]
    reason: str
    files_to_fix: list[str]


class _BuildFixOutput(BaseModel):
    files: list[str]
    diff: str
    agent_reason: str


class _FailfastJudgment(BaseModel):
    related: bool
    reason: str | None = None


def _truncate(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text[:limit] + ("..." if len(text) > limit else "")


class _ApiBackend:
    def __init__(self, settings: Settings, llm: Any | None = None) -> None:
        self._settings = settings
        if llm is None:
            from langchain_openai import ChatOpenAI

            llm = ChatOpenAI(
                model=settings.llm_model,
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                timeout=settings.llm_timeout_sec,
                max_retries=settings.llm_max_retries,
            )
        self._llm = llm

    def complete(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        structured = self._llm.with_structured_output(schema)
        result = structured.invoke(prompt)
        if isinstance(result, dict):
            return schema(**result)
        return result


def _extract_json(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is not None:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return None


class _ClaudeCliBackend:
    def __init__(self, settings: Settings, runner: Any | None = None) -> None:
        self._settings = settings
        self._runner = runner

    def _invoke(self, prompt: str) -> subprocess.CompletedProcess[str]:
        args = [self._settings.claude_cli_path, "-p", prompt]
        if self._runner is not None:
            return self._runner(args, timeout=self._settings.llm_timeout_sec)
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=self._settings.llm_timeout_sec,
        )

    def complete(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        last_err: Exception | None = None
        for _ in range(max(1, self._settings.llm_max_retries)):
            try:
                proc = self._invoke(prompt)
            except subprocess.TimeoutExpired as exc:
                last_err = exc
                continue
            if proc.returncode != 0:
                last_err = LLMUnavailable(
                    f"claude -p exited {proc.returncode}: {_truncate(proc.stderr)}"
                )
                continue
            data = _extract_json(proc.stdout)
            if data is None:
                last_err = LLMUnavailable("claude -p output was not valid JSON")
                continue
            try:
                return schema(**data)
            except ValidationError as exc:
                last_err = exc
                continue
        raise LLMUnavailable(f"claude_cli backend failed after retries: {last_err}")


class LLMClient:
    """单一后端，按 settings.llm_backend 选择（二选一，互斥，同一时间只启用一个）。

    - api（默认）：langchain-openai ChatOpenAI（DeepSeek 走 base_url），schema 强制 + token 观测。
    - claude_cli：封装 ``claude -p "<prompt>"`` 子进程，结构化输出靠 prompt 强约束 + 健壮解析器。
    接口 4 方法不变，后端实现隔离在内部；未来加新后端不破坏接口。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        if settings.llm_backend == "api":
            self._backend: _ApiBackend | _ClaudeCliBackend = _ApiBackend(settings)
        elif settings.llm_backend == "claude_cli":
            self._backend = _ClaudeCliBackend(settings)
        else:
            raise ValueError(f"unknown llm_backend: {settings.llm_backend}")

    def judge_bug_fix(self, commit: CommitInfo) -> SyncDecision:
        prompt = (
            "你是代码评审专家。判断下面这个 commit 是否属于 bug 修复，"
            "并评估其严重性（风险）。\n"
            f"commit: {commit.sha} {commit.message}\n"
            f"changed files: {', '.join(commit.changed_files)}\n"
            f"patch:\n{commit.patch_text}\n"
            "严重性参考：崩溃/宕机/死机/数据丢失/内存泄漏/缓冲区溢出/安全漏洞等"
            "高风险缺陷 → high；一般功能 bug → low 或 medium。\n"
            '只输出 JSON，不要任何其他文字或 markdown，'
            '格式：{"is_bug_fix": true/false, "risk": "low"|"medium"|"high", '
            '"reason": "简短理由"}'
        )
        try:
            j = self._complete("judge_bug_fix", prompt, _BugFixJudgment)
        except LLMUnavailable as exc:
            if self._settings.llm_degrade_to_manual:
                return SyncDecision(
                    sha=commit.sha,
                    is_bug_fix=False,
                    reason="LLM 不可用，降级人工审核",
                    recognition_source="pending:claude-agent",
                    needs_agent=True,
                )
            raise exc
        return SyncDecision(
            sha=commit.sha,
            is_bug_fix=j.is_bug_fix,
            reason=j.reason,
            recognition_source="agent:bug-fix" if j.is_bug_fix else "agent:not-bug-fix",
            needs_agent=False,
            risk=j.risk,
        )

    def judge_severity(self, commit: CommitInfo) -> Literal["low", "medium", "high"] | None:
        prompt = (
            "你是代码评审专家。评估下面这个 commit 修复缺陷的严重性（决策 41 发布线门控）。\n"
            f"commit: {commit.sha} {commit.message}\n"
            f"changed files: {', '.join(commit.changed_files)}\n"
            f"patch:\n{commit.patch_text}\n"
            "严重性参考：崩溃/宕机/死机/数据丢失/内存泄漏/缓冲区溢出/安全漏洞等"
            "高风险缺陷 → high；一般功能 bug → low 或 medium。\n"
            '只输出 JSON，不要任何其他文字或 markdown，'
            '格式：{"risk": "low"|"medium"|"high", "reason": "简短理由"}'
        )
        try:
            j = self._complete("judge_severity", prompt, _SeverityJudgment)
        except LLMUnavailable as exc:
            if self._settings.llm_degrade_to_manual:
                return None
            raise exc
        return j.risk

    def solve_conflict(self, ctx: ConflictContext) -> ConflictResolution:
        markers = "\n".join(f"{f}:\n{m}" for f, m in ctx.conflict_markers.items())
        prompt = (
            "你是资深工程师。解决 git cherry-pick 冲突，必须理解功能实现与合入目的，"
            "禁止机械删除冲突标记、禁止无理由丢弃任一边改动。\n"
            f"commit: {ctx.commit.sha} {ctx.commit.message}\n"
            f"source: {ctx.source_branch} -> target: {ctx.target_branch}\n"
            f"conflict files: {', '.join(ctx.conflict_files)}\n"
            f"conflict markers:\n{markers}\n"
            '只输出 JSON，格式：{"files": ["..."], "diff": "解决冲突后的 diff", '
            '"agent_reason": "为何如此解决"}'
        )
        result = self._complete("solve_conflict", prompt, _ConflictOutput)
        return ConflictResolution(
            files=result.files, diff=result.diff, agent_reason=result.agent_reason
        )

    def classify_build_error(self, ctx: BuildErrorContext) -> BuildAttribution:
        prompt = (
            "你是嵌入式编译错误归因专家。判断编译失败的原因类别。\n"
            f"commit: {ctx.commit.sha} {ctx.commit.message}\n"
            f"model: {ctx.model}\n"
            f"errors:\n{chr(10).join(ctx.errors)}\n"
            '只输出 JSON，格式：{"category": '
            '"introduced_by_commit"|"pre_existing"|"environment"|"unresolvable", '
            '"reason": "...", "files_to_fix": ["..."]}'
        )
        try:
            out = self._complete("classify_build_error", prompt, _BuildAttributionOutput)
        except LLMUnavailable as exc:
            if self._settings.llm_degrade_to_manual:
                return BuildAttribution(
                    category="unresolvable",
                    reason="LLM 不可用，无法归因",
                    files_to_fix=[],
                )
            raise exc
        return BuildAttribution(
            category=out.category, reason=out.reason, files_to_fix=out.files_to_fix
        )

    def fix_build_error(self, ctx: BuildErrorContext) -> BuildFix:
        prompt = (
            "你是嵌入式编译修复专家。针对下列编译错误，给出最小改动 diff 修复。\n"
            f"commit: {ctx.commit.sha} {ctx.commit.message}\n"
            f"model: {ctx.model}\n"
            f"errors:\n{chr(10).join(ctx.errors)}\n"
            '只输出 JSON，格式：{"files": ["..."], "diff": '
            '"统一格式 diff（--- a/.. b/.. + hunk）", '
            '"agent_reason": "为何如此修复"}'
        )
        result = self._complete("fix_build_error", prompt, _BuildFixOutput)
        return BuildFix(
            files=result.files, diff=result.diff, agent_reason=result.agent_reason
        )

    def judge_failfast_related(self, failed: FailedCommit, subsequent: list[CommitInfo]) -> bool:
        subs = "\n".join(
            f"- {c.sha} {c.message} files={','.join(c.changed_files)}" for c in subsequent
        )
        prompt = (
            "你是代码评审专家。判断后续 commit 是否与失败的 commit 相关。\n"
            f"failed: {failed.sha} {failed.failure_summary} "
            f"files={','.join(failed.changed_files)}\n"
            f"subsequent:\n{subs}\n"
            '只输出 JSON，格式：{"related": true/false, "reason": "简短理由"}'
        )
        try:
            j = self._complete("judge_failfast_related", prompt, _FailfastJudgment)
        except LLMUnavailable as exc:
            if self._settings.llm_degrade_to_manual:
                return True
            raise exc
        return j.related

    def _complete(self, method: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
        start = time.perf_counter()
        try:
            result = self._backend.complete(prompt, schema)
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "llm method=%s backend=%s dur_ms=%.0f in=%s err=%s",
                method,
                self._settings.llm_backend,
                duration_ms,
                _truncate(prompt, 120),
                str(exc),
            )
            if isinstance(exc, LLMUnavailable):
                raise exc
            raise LLMUnavailable(f"{method}: {exc}") from exc
        duration_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "llm method=%s backend=%s dur_ms=%.0f in=%s out=%s",
            method,
            self._settings.llm_backend,
            duration_ms,
            _truncate(prompt, 120),
            _truncate(str(result), 200),
        )
        return result
