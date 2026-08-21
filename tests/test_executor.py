import inspect
import os
from pathlib import Path
from typing import Protocol, get_type_hints

import pytest
from pydantic import ValidationError

from bsa.executor.base import CommandExecutor, CompletedProcess
from bsa.executor.exceptions import BsaError, DomainError, InfrastructureError, SafetyViolation
from bsa.executor.fake import FakeExecutor
from bsa.executor.subprocess import SubprocessExecutor
from bsa.executor.whitelist import WhitelistExecutor


class TestExceptionHierarchy:
    def test_domain_error_is_bsa_error(self):
        assert issubclass(DomainError, BsaError)

    def test_infrastructure_error_is_bsa_error(self):
        assert issubclass(InfrastructureError, BsaError)

    def test_safety_violation_is_bsa_error(self):
        assert issubclass(SafetyViolation, BsaError)

    def test_all_are_exceptions(self):
        for cls in [BsaError, DomainError, InfrastructureError, SafetyViolation]:
            assert issubclass(cls, Exception)

    def test_siblings_are_not_subclasses_of_each_other(self):
        assert not issubclass(SafetyViolation, DomainError)
        assert not issubclass(SafetyViolation, InfrastructureError)
        assert not issubclass(DomainError, InfrastructureError)
        assert not issubclass(InfrastructureError, DomainError)


class TestCompletedProcess:
    def test_construct_with_valid_data(self):
        p = CompletedProcess(returncode=0, stdout="hello\n", stderr="")
        assert p.returncode == 0
        assert p.stdout == "hello\n"
        assert p.stderr == ""

    def test_required_fields(self):
        for field in ["returncode", "stdout", "stderr"]:
            data = {"returncode": 0, "stdout": "o", "stderr": "e"}
            del data[field]
            with pytest.raises(ValidationError):
                CompletedProcess(**data)

    def test_round_trip_json(self):
        p = CompletedProcess(returncode=1, stdout="a", stderr="b")
        restored = CompletedProcess.model_validate_json(p.model_dump_json())
        assert restored == p


class TestCommandExecutorProtocol:
    def test_is_a_protocol(self):
        assert issubclass(CommandExecutor, Protocol)

    def test_run_signature(self):
        hints = get_type_hints(CommandExecutor.run)
        assert hints["args"] == list[str]
        assert hints["cwd"] == str | Path | None
        assert hints["timeout_sec"] is int
        assert hints["env"] == dict[str, str] | None
        assert hints["return"] == CompletedProcess

    def test_timeout_default_is_300(self):
        sig = inspect.signature(CommandExecutor.run)
        assert sig.parameters["timeout_sec"].default == 300


class TestSubprocessExecutor:
    def test_runs_real_command_and_captures_stdout(self):
        result = SubprocessExecutor().run(["echo", "hello"])
        assert result.returncode == 0
        assert result.stdout.strip() == "hello"
        assert result.stderr == ""

    def test_nonzero_returncode_does_not_raise(self):
        result = SubprocessExecutor().run(["sh", "-c", "exit 3"])
        assert result.returncode == 3

    def test_captures_stderr(self):
        result = SubprocessExecutor().run(["sh", "-c", "echo oops >&2"])
        assert result.returncode == 0
        assert "oops" in result.stderr

    def test_timeout_raises_infrastructure_error(self):
        with pytest.raises(InfrastructureError):
            SubprocessExecutor().run(
                ["python3", "-c", "import time; time.sleep(30)"], timeout_sec=1
            )

    def test_cwd_passed_through(self, tmp_path):
        result = SubprocessExecutor().run(["pwd"], cwd=tmp_path)
        assert result.returncode == 0
        assert result.stdout.strip() == str(tmp_path)

    def test_env_passed_through(self):
        env = {**os.environ, "BSA_TEST_VAR": "works"}
        result = SubprocessExecutor().run(["sh", "-c", "echo $BSA_TEST_VAR"], env=env)
        assert result.returncode == 0
        assert result.stdout.strip() == "works"


class TestWhitelistExecutor:
    def test_allows_whitelisted_git_commands(self):
        inner = FakeExecutor()
        executor = WhitelistExecutor(inner)
        for cmd in ["fetch", "checkout", "cherry-pick", "log", "diff", "show",
                    "format-patch", "worktree", "merge-base", "status", "add",
                    "rev-parse", "diff-tree"]:
            result = executor.run([cmd, "--foo"])
            assert result.returncode == 0
        assert len(inner.calls) == 13

    @pytest.mark.parametrize("cmd", ["reset", "push", "rm", "rebase", "merge", "clean", "gc"])
    def test_rejects_non_whitelisted_commands(self, cmd):
        executor = WhitelistExecutor(FakeExecutor())
        with pytest.raises(SafetyViolation):
            executor.run([cmd, "x"])

    def test_rejects_empty_args(self):
        executor = WhitelistExecutor(FakeExecutor())
        with pytest.raises(SafetyViolation):
            executor.run([])

    def test_forwards_args_with_git_prefix_to_inner(self):
        inner = FakeExecutor()
        WhitelistExecutor(inner).run(["fetch", "--all", "--prune"])
        assert inner.calls[0][0] == ["git", "fetch", "--all", "--prune"]

    def test_forwards_kwargs_to_inner(self, tmp_path):
        inner = FakeExecutor()
        env = {"PATH": "/usr/bin"}
        WhitelistExecutor(inner).run(["status"], cwd=tmp_path, timeout_sec=10, env=env)
        args, kwargs = inner.calls[0]
        assert args == ["git", "status"]
        assert kwargs["cwd"] == tmp_path
        assert kwargs["timeout_sec"] == 10
        assert kwargs["env"] == env

    def test_allowed_git_is_frozenset_and_excludes_writes(self):
        assert isinstance(WhitelistExecutor.ALLOWED_GIT, frozenset)
        for dangerous in ["push", "reset", "rebase", "merge", "clean", "gc"]:
            assert dangerous not in WhitelistExecutor.ALLOWED_GIT


class TestFakeExecutor:
    def test_preset_responses_consumed_in_order(self):
        executor = FakeExecutor([
            CompletedProcess(returncode=0, stdout="one", stderr=""),
            CompletedProcess(returncode=1, stdout="", stderr="boom"),
        ])
        first = executor.run(["a"])
        second = executor.run(["b"])
        assert first.returncode == 0
        assert first.stdout == "one"
        assert second.returncode == 1
        assert second.stderr == "boom"

    def test_records_calls(self):
        executor = FakeExecutor()
        executor.run(["fetch", "--all"], cwd="/repo", timeout_sec=60, env={"K": "v"})
        executor.run(["status"])
        assert len(executor.calls) == 2
        args, kwargs = executor.calls[0]
        assert args == ["fetch", "--all"]
        assert kwargs["cwd"] == "/repo"
        assert kwargs["timeout_sec"] == 60
        assert kwargs["env"] == {"K": "v"}
        assert executor.calls[1][0] == ["status"]

    def test_callable_response(self):
        def respond(args, kwargs):
            return CompletedProcess(returncode=0, stdout=f"ran {args[0]}", stderr="")

        executor = FakeExecutor([respond])
        result = executor.run(["log"])
        assert result.stdout == "ran log"

    def test_default_empty_response_when_exhausted(self):
        executor = FakeExecutor()
        result = executor.run(["log"])
        assert result == CompletedProcess(returncode=0, stdout="", stderr="")
