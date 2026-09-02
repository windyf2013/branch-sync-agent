from __future__ import annotations

import importlib.resources
from pathlib import Path

from bsa.agents.base import LLMClient
from bsa.agents.build_agent import BuildAgent
from bsa.agents.conflict import ConflictAgent
from bsa.agents.sync_decision import SyncDecisionAgent
from bsa.build.runner import BuildRunner
from bsa.config.settings import Settings
from bsa.executor.subprocess import SubprocessExecutor
from bsa.executor.whitelist import WhitelistExecutor
from bsa.git.service import GitService
from bsa.graph.nodes import GraphContext
from bsa.rules import (
    SafetyEnforcer,
    load_build_rules,
    load_decision_rules,
    load_safety_rules,
)


def _bundled_rules_dir() -> Path:
    try:
        resource = importlib.resources.files("bsa.rules")
        if resource.is_dir():
            return Path(str(resource))
    except (ModuleNotFoundError, FileNotFoundError):
        pass
    return Path(__file__).resolve().parent.parent / "rules"


def build_graph_context(
    settings: Settings,
    *,
    rules_dir: Path | None = None,
    cycle_id: str | None = None,
) -> GraphContext:
    """Build the real GraphContext from Settings — the production entry point.

    Services are real: git behind the WhitelistExecutor (subprocess), docker
    BuildRunner, SafetyEnforcer over safety_rules.yaml, DecisionRules, three
    agent subgraphs over one LLMClient, and the LLM itself (fail-fast judge).
    """
    rules_dir = rules_dir or _bundled_rules_dir()
    safety = SafetyEnforcer(load_safety_rules(rules_dir / "safety_rules.yaml"))
    decision_rules = load_decision_rules(rules_dir / "decision_rules.yaml")
    build_rules = load_build_rules(rules_dir / "build_rules.yaml")

    executor = WhitelistExecutor(SubprocessExecutor())
    git = GitService(executor=executor, repo_path=Path(settings.repo_path))
    git.fetch_retry_count = settings.fetch_retry_count
    git.fetch_retry_base_sec = settings.fetch_retry_base_sec

    llm = LLMClient(settings)
    # BuildRunner 用裸 SubprocessExecutor：docker 命令不经 git 白名单
    # （WhitelistExecutor 只拦截 git；真机测试暴露 docker 被 git 白名单
    # 误拦 "git command not whitelisted: 'docker'"）。docker 由 BuildRunner
    # 独立管控，不属 git 白名单范畴。
    runner = BuildRunner(
        executor=SubprocessExecutor(),
        settings=settings,
        cycle_id=cycle_id,
        build_types=build_rules.build_types,
    )
    sync_decision_agent = SyncDecisionAgent(
        llm=llm,
        judgments_path=Path(settings.log_dir) / "judgments.json",
    )
    conflict_agent = ConflictAgent(
        llm=llm,
        git=git,
        safety=safety,
        max_attempts=settings.max_conflict_attempts,
    )
    build_agent = BuildAgent(
        llm=llm,
        git=git,
        runner=runner,
        safety=safety,
        max_attempts=settings.max_build_attempts,
    )

    return GraphContext(
        settings=settings,
        executor=executor,
        git=git,
        runner=runner,
        sync_decision_agent=sync_decision_agent,
        conflict_agent=conflict_agent,
        build_agent=build_agent,
        safety=safety,
        decision_rules=decision_rules,
        build_rules=build_rules,
        llm=llm,
    )
