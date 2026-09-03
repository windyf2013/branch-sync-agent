import logging
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from bsa.agents import LLMClient, LLMUnavailable
from bsa.agents.base import (
    BuildAttribution,
    BuildErrorContext,
    ConflictContext,
    FailedCommit,
    _ApiBackend,
    _BugFixJudgment,
    _ClaudeCliBackend,
)
from bsa.config.settings import Settings
from bsa.domain.models import CommitInfo, ConflictResolution, SyncDecision


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        repo_path="/srv/rcios",
        branch_file="/srv/rcios/branch.md",
        worktree_root="/srv/wt",
        llm_model="deepseek-chat",
        llm_api_key="sk-test",
        llm_base_url="https://api.deepseek.com/v1",
        docker_image="rcios-build:latest",
        docker_mount_workspace="/workspace/rcios",
        build_script_dir="build/platform/RTL9617C",
        mail_sender="bsa@raisecom.com",
        mail_recipients=["ops@raisecom.com"],
        log_dir=str(tmp_path / "logs"),
    )
    values.update(overrides)
    return Settings(**values, _env_file=None)


def make_commit(**overrides) -> CommitInfo:
    values = dict(
        sha="abc123",
        message="fix: null deref in foo",
        author="dev",
        committed_at="2026-08-20T10:00:00+08:00",
        changed_files=["src/foo.c"],
        patch_text="@@ -1 +1 @@\n-*p;\n+if (p) *p;\n",
        symbols=["foo"],
        patch_id=None,
        issue_ids=["#42"],
        source_branch="develop",
        homologous_section="RTK",
    )
    values.update(overrides)
    return CommitInfo(**values)


def make_conflict_context() -> ConflictContext:
    return ConflictContext(
        commit=make_commit(),
        conflict_files=["src/foo.c"],
        conflict_markers={"src/foo.c": "<<<<<<< HEAD\nint a;\n=======\nint b;\n>>>>>>> abc123"},
        source_branch="develop",
        target_branch="release/1.0",
        worktree=Path("/srv/wt/rcios"),
    )


def make_build_context() -> BuildErrorContext:
    return BuildErrorContext(
        commit=make_commit(),
        model="RTL9617C",
        errors=["error: 'foo' undeclared (first use in this function)"],
        log_path=Path("/srv/logs/build.log"),
        worktree=Path("/srv/wt/rcios"),
    )


def make_failed() -> FailedCommit:
    return FailedCommit(
        sha="abc123",
        failure_summary="compile error: 'foo' undeclared",
        changed_files=["src/foo.c"],
    )


def _completed(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestBackendSelection:
    def test_api_backend_selected(self, tmp_path):
        client = LLMClient(make_settings(tmp_path, llm_backend="api"))
        assert isinstance(client._backend, _ApiBackend)
        assert not isinstance(client._backend, _ClaudeCliBackend)

    def test_claude_cli_backend_selected(self, tmp_path):
        client = LLMClient(make_settings(tmp_path, llm_backend="claude_cli"))
        assert isinstance(client._backend, _ClaudeCliBackend)
        assert not isinstance(client._backend, _ApiBackend)

    def test_only_one_backend_instantiated_per_config(self, tmp_path, monkeypatch):
        import bsa.agents.base as base

        created: list[str] = []

        class FakeApi:
            def __init__(self, *args, **kwargs):
                created.append("api")

            def complete(self, *args, **kwargs):
                raise AssertionError("not used")

        class FakeCli:
            def __init__(self, *args, **kwargs):
                created.append("claude_cli")

            def complete(self, *args, **kwargs):
                raise AssertionError("not used")

        monkeypatch.setattr(base, "_ApiBackend", FakeApi)
        monkeypatch.setattr(base, "_ClaudeCliBackend", FakeCli)

        LLMClient(make_settings(tmp_path, llm_backend="api"))
        assert created == ["api"]

        created.clear()
        LLMClient(make_settings(tmp_path, llm_backend="claude_cli"))
        assert created == ["claude_cli"]


class TestJudgeBugFix:
    def test_parses_api_response(self, tmp_path, monkeypatch):
        client = LLMClient(make_settings(tmp_path, llm_backend="api"))
        monkeypatch.setattr(
            client._backend,
            "complete",
            lambda prompt, schema: schema(is_bug_fix=True, reason="null deref"),
        )
        decision = client.judge_bug_fix(make_commit())
        assert isinstance(decision, SyncDecision)
        assert decision.sha == "abc123"
        assert decision.is_bug_fix is True
        assert decision.recognition_source == "agent:bug-fix"
        assert decision.needs_agent is False

    def test_parses_not_bug_fix(self, tmp_path, monkeypatch):
        client = LLMClient(make_settings(tmp_path, llm_backend="api"))
        monkeypatch.setattr(
            client._backend,
            "complete",
            lambda prompt, schema: schema(is_bug_fix=False, reason="docs only"),
        )
        decision = client.judge_bug_fix(make_commit())
        assert decision.is_bug_fix is False
        assert decision.recognition_source == "agent:not-bug-fix"
        assert decision.needs_agent is False

    def test_parses_risk_from_judgment(self, tmp_path, monkeypatch):
        client = LLMClient(make_settings(tmp_path, llm_backend="api"))
        monkeypatch.setattr(
            client._backend,
            "complete",
            lambda prompt, schema: schema(is_bug_fix=True, risk="high", reason="crash"),
        )
        decision = client.judge_bug_fix(make_commit())
        assert decision.risk == "high"
        assert decision.is_bug_fix is True

    def test_failure_degrades_to_manual(self, tmp_path, monkeypatch):
        client = LLMClient(make_settings(tmp_path, llm_backend="api"))

        def boom(prompt, schema):
            raise LLMUnavailable("api down")

        monkeypatch.setattr(client._backend, "complete", boom)
        decision = client.judge_bug_fix(make_commit())
        assert decision.sha == "abc123"
        assert decision.is_bug_fix is False
        assert decision.recognition_source == "pending:claude-agent"
        assert decision.needs_agent is True
        # 真实失败原因必须随 reason 透出，而不是被固定文案抹掉。
        assert decision.reason is not None and "api down" in decision.reason

    def test_failure_raises_when_no_degrade(self, tmp_path, monkeypatch):
        client = LLMClient(make_settings(tmp_path, llm_backend="api", llm_degrade_to_manual=False))

        def boom(prompt, schema):
            raise LLMUnavailable("api down")

        monkeypatch.setattr(client._backend, "complete", boom)
        with pytest.raises(LLMUnavailable):
            client.judge_bug_fix(make_commit())


class TestJudgeSeverity:
    def test_parses_risk(self, tmp_path, monkeypatch):
        client = LLMClient(make_settings(tmp_path, llm_backend="api"))
        monkeypatch.setattr(
            client._backend,
            "complete",
            lambda prompt, schema: schema(risk="high", reason="crash"),
        )
        assert client.judge_severity(make_commit()) == "high"

    def test_parses_low_risk(self, tmp_path, monkeypatch):
        client = LLMClient(make_settings(tmp_path, llm_backend="api"))
        monkeypatch.setattr(
            client._backend,
            "complete",
            lambda prompt, schema: schema(risk="low"),
        )
        assert client.judge_severity(make_commit()) == "low"

    def test_failure_degrades_to_none(self, tmp_path, monkeypatch):
        client = LLMClient(make_settings(tmp_path, llm_backend="api"))

        def boom(prompt, schema):
            raise LLMUnavailable("api down")

        monkeypatch.setattr(client._backend, "complete", boom)
        assert client.judge_severity(make_commit()) is None

    def test_failure_raises_when_no_degrade(self, tmp_path, monkeypatch):
        client = LLMClient(make_settings(tmp_path, llm_backend="api", llm_degrade_to_manual=False))

        def boom(prompt, schema):
            raise LLMUnavailable("api down")

        monkeypatch.setattr(client._backend, "complete", boom)
        with pytest.raises(LLMUnavailable):
            client.judge_severity(make_commit())


class TestSolveConflict:
    def test_success(self, tmp_path, monkeypatch):
        client = LLMClient(make_settings(tmp_path, llm_backend="api"))
        monkeypatch.setattr(
            client._backend,
            "complete",
            lambda prompt, schema: schema(
                files=["src/foo.c"],
                diff="@@ -1 +1 @@\n-*p;\n+if (p) *p;\n",
                agent_reason="保留双方改动：解引用前判空",
            ),
        )
        resolution = client.solve_conflict(make_conflict_context())
        assert isinstance(resolution, ConflictResolution)
        assert resolution.files == ["src/foo.c"]
        assert resolution.agent_reason

    def test_failure_raises_for_manual(self, tmp_path, monkeypatch):
        client = LLMClient(make_settings(tmp_path, llm_backend="api"))

        def boom(prompt, schema):
            raise LLMUnavailable("api down")

        monkeypatch.setattr(client._backend, "complete", boom)
        with pytest.raises(LLMUnavailable):
            client.solve_conflict(make_conflict_context())


class TestClassifyBuildError:
    def test_success(self, tmp_path, monkeypatch):
        client = LLMClient(make_settings(tmp_path, llm_backend="api"))
        monkeypatch.setattr(
            client._backend,
            "complete",
            lambda prompt, schema: schema(
                category="introduced_by_commit",
                reason="commit 引入对未声明符号的引用",
                files_to_fix=["src/foo.c"],
            ),
        )
        attribution = client.classify_build_error(make_build_context())
        assert isinstance(attribution, BuildAttribution)
        assert attribution.category == "introduced_by_commit"
        assert attribution.files_to_fix == ["src/foo.c"]

    def test_failure_unresolvable(self, tmp_path, monkeypatch):
        client = LLMClient(make_settings(tmp_path, llm_backend="api"))

        def boom(prompt, schema):
            raise LLMUnavailable("api down")

        monkeypatch.setattr(client._backend, "complete", boom)
        attribution = client.classify_build_error(make_build_context())
        assert attribution.category == "unresolvable"


class TestJudgeFailfastRelated:
    def test_related_false(self, tmp_path, monkeypatch):
        client = LLMClient(make_settings(tmp_path, llm_backend="api"))
        monkeypatch.setattr(
            client._backend,
            "complete",
            lambda prompt, schema: schema(related=False, reason="无关文件"),
        )
        assert client.judge_failfast_related(make_failed(), [make_commit()]) is False

    def test_related_true(self, tmp_path, monkeypatch):
        client = LLMClient(make_settings(tmp_path, llm_backend="api"))
        monkeypatch.setattr(
            client._backend,
            "complete",
            lambda prompt, schema: schema(related=True, reason="同文件改动"),
        )
        assert client.judge_failfast_related(make_failed(), [make_commit()]) is True

    def test_failure_conservative_true(self, tmp_path, monkeypatch):
        client = LLMClient(make_settings(tmp_path, llm_backend="api"))

        def boom(prompt, schema):
            raise LLMUnavailable("api down")

        monkeypatch.setattr(client._backend, "complete", boom)
        assert client.judge_failfast_related(make_failed(), [make_commit()]) is True

    def test_failure_raises_when_no_degrade(self, tmp_path, monkeypatch):
        client = LLMClient(make_settings(tmp_path, llm_backend="api", llm_degrade_to_manual=False))

        def boom(prompt, schema):
            raise LLMUnavailable("api down")

        monkeypatch.setattr(client._backend, "complete", boom)
        with pytest.raises(LLMUnavailable):
            client.judge_failfast_related(make_failed(), [make_commit()])


class TestApiBackend:
    def test_constructs_chatopenai_with_settings(self, tmp_path, monkeypatch):
        captured: dict = {}

        class FakeChat:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def with_structured_output(self, schema):
                raise AssertionError("not used")

        monkeypatch.setattr("langchain_openai.ChatOpenAI", FakeChat)
        _ApiBackend(make_settings(tmp_path, llm_backend="api"))
        assert captured["model"] == "deepseek-chat"
        assert captured["api_key"] == "sk-test"
        assert captured["base_url"] == "https://api.deepseek.com/v1"
        assert captured["timeout"] == 60
        assert captured["max_retries"] == 3

    def test_complete_parses_structured_output(self, tmp_path):
        class FakeStructured:
            def __init__(self, result):
                self._result = result

            def invoke(self, prompt):
                return self._result

        class FakeLLM:
            def __init__(self):
                self._schema = None

            def with_structured_output(self, schema):
                self._schema = schema
                return FakeStructured({"is_bug_fix": True, "reason": "r"})

        backend = _ApiBackend(make_settings(tmp_path, llm_backend="api"), llm=FakeLLM())
        result = backend.complete("prompt", _BugFixJudgment)
        assert isinstance(result, _BugFixJudgment)
        assert result.is_bug_fix is True
        assert result.reason == "r"


class TestClaudeCliBackend:
    def test_parses_json(self, tmp_path):
        calls: list[list[str]] = []

        def fake_run(args, timeout=None):
            calls.append(args)
            return _completed('{"is_bug_fix": true, "reason": "null deref"}')

        client = LLMClient(make_settings(tmp_path, llm_backend="claude_cli"))
        client._backend._runner = fake_run
        decision = client.judge_bug_fix(make_commit())
        assert decision.is_bug_fix is True
        assert calls[0][0] == "claude"
        assert calls[0][1] == "-p"

    def test_extracts_fenced_json(self, tmp_path):
        def fake_run(args, timeout=None):
            return _completed('```json\n{"is_bug_fix": true, "reason": "ok"}\n```')

        client = LLMClient(make_settings(tmp_path, llm_backend="claude_cli"))
        client._backend._runner = fake_run
        decision = client.judge_bug_fix(make_commit())
        assert decision.is_bug_fix is True

    def test_timeout_then_retry_success(self, tmp_path, monkeypatch):
        calls: list[int] = []
        sleeps: list[float] = []

        def fake_run(args, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(args, timeout)
            return _completed('{"is_bug_fix": true, "reason": "ok"}')

        def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr("bsa.agents.base.time.sleep", fake_sleep)
        client = LLMClient(make_settings(tmp_path, llm_backend="claude_cli"))
        client._backend._runner = fake_run
        decision = client.judge_bug_fix(make_commit())
        assert decision.is_bug_fix is True
        assert len(calls) == 2
        assert sleeps == [1.0]

    def test_all_timeout_degrades(self, tmp_path, monkeypatch):
        calls: list[int] = []
        sleeps: list[float] = []

        def fake_run(args, timeout=None):
            calls.append(1)
            raise subprocess.TimeoutExpired(args, timeout)

        def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr("bsa.agents.base.time.sleep", fake_sleep)
        client = LLMClient(make_settings(tmp_path, llm_backend="claude_cli", llm_max_retries=3))
        client._backend._runner = fake_run
        decision = client.judge_bug_fix(make_commit())
        assert decision.is_bug_fix is False
        assert decision.recognition_source == "pending:claude-agent"
        assert len(calls) == 3
        assert sleeps == [1.0, 2.0]

    def test_non_json_output_degrades(self, tmp_path):
        def fake_run(args, timeout=None):
            return _completed("抱歉，无法回答")

        client = LLMClient(make_settings(tmp_path, llm_backend="claude_cli"))
        client._backend._runner = fake_run
        decision = client.judge_bug_fix(make_commit())
        assert decision.is_bug_fix is False
        assert decision.recognition_source == "pending:claude-agent"

    def test_nonzero_exit_degrades(self, tmp_path):
        def fake_run(args, timeout=None):
            return _completed("", returncode=1, stderr="claude: error")

        client = LLMClient(make_settings(tmp_path, llm_backend="claude_cli"))
        client._backend._runner = fake_run
        decision = client.judge_bug_fix(make_commit())
        assert decision.is_bug_fix is False
        assert decision.recognition_source == "pending:claude-agent"


class TestObservation:
    def test_logs_each_call(self, tmp_path, monkeypatch, caplog):
        caplog.set_level(logging.INFO, logger="bsa.agents")
        client = LLMClient(make_settings(tmp_path, llm_backend="api"))
        monkeypatch.setattr(
            client._backend,
            "complete",
            lambda prompt, schema: schema(is_bug_fix=True, reason="null deref"),
        )
        client.judge_bug_fix(make_commit())
        assert any("judge_bug_fix" in r.getMessage() for r in caplog.records)
        assert any("backend=api" in r.getMessage() for r in caplog.records)


class TestSupportTypes:
    def test_conflict_context_fields(self):
        ctx = make_conflict_context()
        assert ctx.target_branch == "release/1.0"
        assert ctx.conflict_files == ["src/foo.c"]

    def test_build_attribution_literal_enforced(self):
        with pytest.raises(ValidationError):
            BuildAttribution(category="bogus", reason="x", files_to_fix=[])
        ok = BuildAttribution(category="environment", reason="x", files_to_fix=[])
        assert ok.category == "environment"

    def test_failed_commit_fields(self):
        fc = make_failed()
        assert fc.failure_summary
        assert fc.changed_files == ["src/foo.c"]
