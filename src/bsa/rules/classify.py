from __future__ import annotations

import hashlib
import importlib.resources
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Classification(BaseModel):
    is_bug_fix: bool
    recognition_source: str
    issue_ids: list[str] = Field(default_factory=list)
    cherry_pick_from: str | None = None
    reason: str | None = None
    needs_agent: bool = False


class MarkerRule(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pattern: str | None = None
    source: str
    mode: Literal["regex", "version-file", "docs-only"] = "regex"
    reason: str | None = None


class _PathRules(BaseModel):
    model_config = ConfigDict(extra="ignore")

    public_dirs: list[str] = Field(default_factory=list)


class _ClassifyRules(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bugfix_markers: list[MarkerRule]
    issue_id_pattern: str
    issue_prefixes: list[str]
    cherry_pick_pattern: str
    not_bugfix_markers: list[MarkerRule]
    version_file_suffix: str
    docs_only_paths: list[str]
    pending_source: str = "pending:claude-agent"
    not_included_source: str = "not-included"
    agent_bug_fix_source: str = "agent:bug-fix"
    agent_not_bug_fix_source: str = "agent:not-bug-fix"
    path_rules: _PathRules = Field(default_factory=_PathRules)


@dataclass(frozen=True)
class _CompiledMarker:
    pattern: re.Pattern | None
    source: str
    mode: str
    reason: str | None


@dataclass(frozen=True)
class _CompiledRules:
    bugfix_markers: tuple[_CompiledMarker, ...]
    issue_id_re: re.Pattern
    issue_prefixes: tuple[str, ...]
    cherry_pick_re: re.Pattern
    not_bugfix_markers: tuple[_CompiledMarker, ...]
    version_file_suffix: str
    docs_only_paths: tuple[str, ...]
    pending_source: str
    not_included_source: str
    agent_bug_fix_source: str
    agent_not_bug_fix_source: str
    public_dirs: tuple[str, ...]


@dataclass(frozen=True)
class _CompiledSeverity:
    high_keywords: tuple[re.Pattern, ...]
    severity_paths: tuple[str, ...]


def _compile_markers(markers: list[MarkerRule]) -> tuple[_CompiledMarker, ...]:
    compiled: list[_CompiledMarker] = []
    for marker in markers:
        pattern = None
        if marker.mode == "regex" and marker.pattern:
            pattern = re.compile(marker.pattern, re.IGNORECASE)
        compiled.append(
            _CompiledMarker(
                pattern=pattern,
                source=marker.source,
                mode=marker.mode,
                reason=marker.reason,
            )
        )
    return tuple(compiled)


def _load_rules_yaml() -> dict:
    try:
        text = (
            importlib.resources.files("bsa.rules")
            .joinpath("decision_rules.yaml")
            .read_text(encoding="utf-8")
        )
    except (ModuleNotFoundError, FileNotFoundError):
        text = (Path(__file__).with_name("decision_rules.yaml")).read_text(
            encoding="utf-8"
        )
    return yaml.safe_load(text) or {}


def _compile_classify_rules(data: dict) -> _CompiledRules:
    model = _ClassifyRules(**data.get("classify", {}))
    return _CompiledRules(
        bugfix_markers=_compile_markers(model.bugfix_markers),
        issue_id_re=re.compile(model.issue_id_pattern, re.IGNORECASE),
        issue_prefixes=tuple(model.issue_prefixes),
        cherry_pick_re=re.compile(model.cherry_pick_pattern, re.IGNORECASE),
        not_bugfix_markers=_compile_markers(model.not_bugfix_markers),
        version_file_suffix=model.version_file_suffix,
        docs_only_paths=tuple(model.docs_only_paths),
        pending_source=model.pending_source,
        not_included_source=model.not_included_source,
        agent_bug_fix_source=model.agent_bug_fix_source,
        agent_not_bug_fix_source=model.agent_not_bug_fix_source,
        public_dirs=tuple(model.path_rules.public_dirs),
    )


def _compile_severity_rules(data: dict) -> _CompiledSeverity:
    high_keywords = list((data.get("severity") or {}).get("high_keywords", []))
    severity_paths = list((data.get("severity") or {}).get("severity_paths", []))
    return _CompiledSeverity(
        high_keywords=tuple(
            re.compile(kw, re.IGNORECASE) for kw in high_keywords if kw
        ),
        severity_paths=tuple(severity_paths),
    )


_RULES_DATA = _load_rules_yaml()
_RULES = _compile_classify_rules(_RULES_DATA)
_SEVERITY_RULES = _compile_severity_rules(_RULES_DATA)


def compute_fingerprint(message: str, patch_id: str | None) -> str:
    """语义身份 fingerprint（决策 5.1）：sha256(subject + body + patch_id)。

    rebase/cherry-pick 不改内容 → patch-id 稳定、subject/body 不变 → fingerprint
    稳定；内容改动 → fingerprint 变化（人工覆盖自动失效，不静默错用）。
    """
    subject, _, body = message.partition("\n")
    raw = f"{subject}\n{body}\n{patch_id or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def lookup_agent_judgment(
    sha: str,
    judgments: Mapping[str, Any] | None,
    fingerprint: str | None = None,
) -> dict[str, Any] | None:
    """Match full SHA or any judgment key that is a prefix of sha (min 7 chars).

    ``fingerprint`` 提供语义身份匹配：judgments 中 ``fp:<fingerprint>`` 键在 sha
    未命中时匹配（内容不变 rebase 后 sha 漂移仍命中）。
    """
    if not judgments:
        return None
    if sha:
        sha_l = sha.lower()
        if sha_l in judgments and isinstance(judgments[sha_l], dict):
            return judgments[sha_l]
        if sha in judgments and isinstance(judgments[sha], dict):
            return judgments[sha]
        best_key = ""
        best_val: dict[str, Any] | None = None
        for key, value in judgments.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            key_l = key.lower().strip()
            if len(key_l) < 7:
                continue
            if sha_l.startswith(key_l) and len(key_l) > len(best_key):
                best_key = key_l
                best_val = value
        if best_val is not None:
            return best_val
    if fingerprint:
        fp_key = f"fp:{fingerprint}"
        if fp_key in judgments and isinstance(judgments[fp_key], dict):
            return judgments[fp_key]
    return None


def _extract_issue_ids(
    message: str,
    issue_id_re: re.Pattern,
    prefixes: tuple[str, ...],
) -> list[str]:
    seen: set[str] = set()
    issue_ids: list[str] = []
    for match in issue_id_re.finditer(message):
        head = match.group(0).split("-")[0].split("#")[0].strip().upper()
        prefix = next((p for p in prefixes if head.startswith(p)), None)
        if prefix is not None:
            token = f"{prefix}{match.group(1)}"
        else:
            token = match.group(0).upper().replace(" ", "").replace("#", "")
        if token not in seen:
            seen.add(token)
            issue_ids.append(token)
    return issue_ids


def _is_docs_only(path: str, docs_only_paths: tuple[str, ...]) -> bool:
    return any(
        (p.endswith("/") and path.startswith(p)) or (not p.endswith("/") and path == p)
        for p in docs_only_paths
    )


def classify_commit(
    message: str,
    changed_files: list[str],
    symbols: list[str],
    patch_text: str,
    *,
    sha: str | None = None,
    patch_id: str | None = None,
    agent_judgments: Mapping[str, Any] | None = None,
) -> Classification:
    rules = _RULES
    _ = (symbols, patch_text)
    cherry_match = rules.cherry_pick_re.search(message)
    cherry_pick_from = cherry_match.group(1) if cherry_match else None
    issue_ids = _extract_issue_ids(message, rules.issue_id_re, rules.issue_prefixes)
    fp = compute_fingerprint(message, patch_id) if patch_id else None
    judgment = lookup_agent_judgment(sha or "", agent_judgments, fingerprint=fp)
    if judgment is not None:
        # 决策 6: 人工判定最高优先级，先于所有机器规则。
        is_bug = bool(judgment.get("is_bug_fix"))
        reason = str(judgment.get("reason") or "").strip() or "Claude 主 Agent 判定结果。"
        return Classification(
            is_bug_fix=is_bug,
            recognition_source=(
                rules.agent_bug_fix_source if is_bug else rules.agent_not_bug_fix_source
            ),
            issue_ids=issue_ids,
            cherry_pick_from=cherry_pick_from,
            reason=reason,
            needs_agent=False,
        )

    for marker in rules.bugfix_markers:
        if marker.pattern and marker.pattern.search(message):
            return Classification(
                is_bug_fix=True,
                recognition_source=marker.source,
                issue_ids=issue_ids,
                cherry_pick_from=cherry_pick_from,
            )

    if issue_ids:
        return Classification(
            is_bug_fix=True,
            recognition_source="machine:issue",
            issue_ids=issue_ids,
            cherry_pick_from=cherry_pick_from,
        )

    if cherry_pick_from is not None:
        return Classification(
            is_bug_fix=True,
            recognition_source="machine:cherry-pick",
            issue_ids=issue_ids,
            cherry_pick_from=cherry_pick_from,
        )

    if not changed_files:
        return Classification(
            is_bug_fix=False,
            recognition_source=rules.not_included_source,
            reason="无关联文件可提取（多为 merge 等），不交 Agent 判定。",
        )

    for marker in rules.not_bugfix_markers:
        if marker.mode == "version-file":
            hit = len(changed_files) == 1 and changed_files[0].endswith(
                rules.version_file_suffix
            )
        elif marker.mode == "docs-only":
            hit = bool(changed_files) and all(
                _is_docs_only(f, rules.docs_only_paths) for f in changed_files
            )
        else:
            hit = marker.pattern is not None and marker.pattern.search(message) is not None
        if hit:
            return Classification(
                is_bug_fix=False,
                recognition_source=marker.source,
                reason=marker.reason,
            )

    return Classification(
        is_bug_fix=False,
        recognition_source=rules.pending_source,
        issue_ids=issue_ids,
        cherry_pick_from=cherry_pick_from,
        reason="无机器可读修复标记；交由 Claude 主 Agent 阅读 diff 后判定。",
        needs_agent=True,
    )


def _is_severity_path(path: str, severity_paths: tuple[str, ...]) -> bool:
    return any(
        (p.endswith("/") and path.startswith(p)) or (not p.endswith("/") and path == p)
        for p in severity_paths
    )


def classify_severity(
    message: str, changed_files: list[str]
) -> Literal["high", "medium", "low", "unknown"]:
    """规则层严重性判定（决策 41）：命中 high 标记/核心路径 → high；否则 unknown 交 LLM。"""
    for keyword in _SEVERITY_RULES.high_keywords:
        if keyword.search(message):
            return "high"
    if any(
        _is_severity_path(f, _SEVERITY_RULES.severity_paths) for f in changed_files
    ):
        return "high"
    return "unknown"
