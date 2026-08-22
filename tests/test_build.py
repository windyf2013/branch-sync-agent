from pathlib import Path

import pytest

from bsa.build.log_parser import extract_errors, has_success_marker
from bsa.build.runner import BuildResult, BuildRunner
from bsa.config.settings import Settings
from bsa.executor import CompletedProcess, FakeExecutor
from bsa.executor.exceptions import InfrastructureError

SPEC_LOG = (
    Path(__file__).resolve().parent.parent / "spec" / "rcios-compiling-log-info.md"
).read_text(encoding="utf-8")


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        repo_path="/srv/rcios",
        branch_file="/srv/rcios/branch.md",
        worktree_root="/srv/wt",
        llm_model="m",
        llm_api_key="k",
        llm_base_url="u",
        docker_image="rcios-build:latest",
        docker_mount_workspace="/workspace/rcios",
        build_script_dir="build/platform/RTL9617C",
        mail_sender="s",
        mail_recipients=["ops@x.com"],
        log_dir=str(tmp_path / "logs"),
    )


def ok(stdout: str = "", rc: int = 0) -> CompletedProcess:
    return CompletedProcess(returncode=rc, stdout=stdout, stderr="")


class TestExtractErrors:
    def test_real_spec_log_yields_no_errors(self):
        assert extract_errors(SPEC_LOG) == []

    def test_warning_fatal_note_noise_is_not_error(self):
        log = (
            "fatal: not a git repository: '.../FleetConntrackDriver/.git'\n"
            "make[1]: warning: implicit declaration of function 'foo'\n"
            "note: '#pragma message: some note'\n"
            "  CC [M]  somefile.o\n"
        )
        assert extract_errors(log) == []

    def test_error_line_extracted_with_context(self):
        lines = [f"line {i:02d}" for i in range(60)]
        lines[25] = "/x/foo.c:12:1: error: implicit declaration of function 'bar'"
        result = extract_errors("\n".join(lines), context_lines=10)
        assert len(result) == 1
        block = result[0]
        assert "error: implicit declaration" in block
        assert "line 15" in block
        assert "line 35" in block
        assert "line 00" not in block

    def test_total_chars_capped(self):
        log = "\n".join(f"/x/f{i}.c: error: something wrong here {i}" for i in range(50))
        result = extract_errors(log, max_chars=3000)
        assert sum(len(b) for b in result) <= 3000

    def test_keeps_first_500_and_last_100_when_many_errors(self):
        log = "\n".join(f"error: line {i}" for i in range(700))
        result = extract_errors(log, context_lines=0, max_chars=10**7)
        assert len(result) == 600
        assert "error: line 0" in result
        assert "error: line 499" in result
        assert "error: line 500" not in result
        assert "error: line 599" not in result
        assert "error: line 600" in result
        assert "error: line 699" in result

    def test_case_insensitive_error_marker(self):
        assert extract_errors("make[1]: Error: failure in target\n") != []


class TestHasSuccessMarker:
    def test_real_success_log_is_success(self):
        assert has_success_marker(SPEC_LOG, "5200") is True

    def test_make_rootfs_success_marker(self):
        assert has_success_marker("Make rootfs success\n", "5200") is True

    def test_model_specific_artifact_marker(self):
        log = "convert file rcios.bin to MSG5200_SYSTEM_4.33.204_20260820.bin success!\n"
        assert has_success_marker(log, "5200") is True
        assert has_success_marker(log, "9999") is False

    def test_convert_line_without_middle_segment_is_success(self):
        log = "convert file rcios.bin to MSG5200_SYSTEM_4.33.204_20260820.bin success!\n"
        assert has_success_marker(log, "5200") is True

    def test_success_bang_convert_marker(self):
        log = "convert file a.bin to MSG5200_DECRYPT_SYSTEM_4.33.204_20260820.bin success!\n"
        assert has_success_marker(log, "5200") is True

    def test_failure_log_is_not_success(self):
        log = "/x/foo.c:12: error: implicit declaration of function 'bar'\n"
        assert has_success_marker(log, "5200") is False


class TestParseErrors:
    def test_fatal_only_log_is_not_failure(self):
        fatal_log = (
            "fatal: not a git repository: '/workspace/rcios/.../FleetConntrackDriver/.git'\n"
            "  CC [M]  drivers/net/ethernet/realtek/rtl86900/FleetConntrackDriver/"
            "core/rtk_fc_driver.o\n"
        )
        runner = BuildRunner(FakeExecutor(), make_settings(Path("/tmp")))
        assert runner.parse_errors(fatal_log) == []

    def test_error_line_with_context(self):
        lines = [f"line {i}" for i in range(30)]
        lines[15] = "/x/foo.c:10: error: undeclared identifier 'x'"
        runner = BuildRunner(FakeExecutor(), make_settings(Path("/tmp")))
        result = runner.parse_errors("\n".join(lines))
        assert len(result) == 1
        assert "undeclared identifier" in result[0]
        assert "line 5" in result[0]
        assert "line 29" in result[0]


class TestBuildCommit:
    def test_docker_sequence_with_sudo_and_module(self, tmp_path):
        settings = make_settings(tmp_path)
        settings.docker_prefix = "sudo "
        executor = FakeExecutor([ok(), ok(stdout="build log"), ok()])
        runner = BuildRunner(executor, settings, cycle_id="20260821")
        result = runner.build_commit(tmp_path / "wt", "5200", clean=False, module="rtk_api")
        import os

        ssh_expected = []
        ssh = Path(os.path.expanduser("~/.ssh"))
        if ssh.is_dir():
            ssh_expected = [
                "-v",
                f"{ssh}:/home/ubuntu/.ssh:ro",
            ]
        assert executor.calls[0][0] == [
            "sudo",
            "docker",
            "run",
            "-d",
            "--name",
            "rcios-sync-20260821",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            *ssh_expected,
            "-v",
            f"{tmp_path / 'wt'}:/workspace/rcios",
            "-v",
            "/usr/local:/usr/local",
            "rcios-build:latest",
            "sleep",
            "infinity",
        ]
        assert executor.calls[1][0][:5] == [
            "sudo",
            "docker",
            "exec",
            "-w",
            "/workspace/rcios",
        ]
        assert executor.calls[1][0][-1] == (
            "cd /workspace/rcios/build && ./code_update.sh -d "
            "&& cd /workspace/rcios/build/platform/RTL9617C "
            "&& ./RTL9617C_build.sh 5200 rtk_api"
        )
        assert executor.calls[2][0] == ["sudo", "docker", "rm", "-f", "rcios-sync-20260821"]
        assert result.model == "5200"
        assert result.log_path.read_text() == "build log"

    def test_clean_before_build(self, tmp_path):
        executor = FakeExecutor([ok(), ok(), ok()])
        runner = BuildRunner(executor, make_settings(tmp_path), cycle_id="c1")
        runner.build_commit(tmp_path / "wt", "5200", clean=True, module=None)
        inner = executor.calls[1][0][-1]
        assert inner == (
            "cd /workspace/rcios/build && ./code_update.sh -d "
            "&& cd /workspace/rcios/build/platform/RTL9617C "
            "&& ./RTL9617C_build.sh clean && ./RTL9617C_build.sh 5200"
        )

    def test_no_sudo_prefix(self, tmp_path):
        executor = FakeExecutor([ok(), ok(), ok()])
        runner = BuildRunner(executor, make_settings(tmp_path), cycle_id="c1")
        runner.build_commit(tmp_path / "wt", "5200", clean=False, module=None)
        assert executor.calls[0][0][0] == "docker"
        assert executor.calls[1][0][0] == "docker"
        assert executor.calls[2][0][0] == "docker"

    def test_docker_goes_directly_not_through_git_whitelist(self, tmp_path):
        executor = FakeExecutor([ok(), ok(), ok()])
        runner = BuildRunner(executor, make_settings(tmp_path), cycle_id="c1")
        runner.build_commit(tmp_path / "wt", "5200", clean=False, module=None)
        assert len(executor.calls) == 3
        for args, _ in executor.calls:
            assert "docker" in args
            assert args[0] != "git"

    def test_build_result_fields_and_log_written(self, tmp_path):
        log = "Make rootfs success\n"
        executor = FakeExecutor([ok(), ok(stdout=log), ok()])
        runner = BuildRunner(executor, make_settings(tmp_path), cycle_id="c1")
        result = runner.build_commit(tmp_path / "wt", "5200", clean=True, module=None)
        assert isinstance(result, BuildResult)
        assert result.model == "5200"
        assert result.returncode == 0
        assert result.succeeded is True
        assert result.log_path.exists()
        assert result.log_path.read_text() == log
        assert result.errors == []

    def test_failing_exec_still_builds_result_and_cleans_up(self, tmp_path):
        exec_err = CompletedProcess(returncode=2, stdout="", stderr="make: *** [foo] Error 2")
        executor = FakeExecutor([ok(), exec_err, ok()])
        runner = BuildRunner(executor, make_settings(tmp_path), cycle_id="c1")
        result = runner.build_commit(tmp_path / "wt", "5200", clean=False, module=None)
        assert result.returncode == 2
        assert result.succeeded is False
        assert len(executor.calls) == 3

    def test_exec_timeout_always_cleans_up_container(self, tmp_path):
        def raise_timeout(args, kwargs):
            raise InfrastructureError("docker exec timed out")

        executor = FakeExecutor([ok(), raise_timeout])
        runner = BuildRunner(executor, make_settings(tmp_path), cycle_id="c1")
        with pytest.raises(InfrastructureError):
            runner.build_commit(tmp_path / "wt", "5200", clean=False, module=None)
        assert len(executor.calls) == 3
        assert executor.calls[-1][0] == ["docker", "rm", "-f", "rcios-sync-c1"]

    def test_custom_log_path_written(self, tmp_path):
        log = "Make rootfs success\n"
        executor = FakeExecutor([ok(), ok(stdout=log), ok()])
        runner = BuildRunner(executor, make_settings(tmp_path), cycle_id="c1")
        target = tmp_path / "logs" / "c1" / "build" / "main" / "abc123" / "build.log"
        result = runner.build_commit(
            tmp_path / "wt", "5200", clean=False, module=None, log_path=target
        )
        assert result.log_path == target
        assert target.read_text() == log

    def test_model_and_module_are_shell_quoted(self, tmp_path):
        executor = FakeExecutor([ok(), ok(), ok()])
        runner = BuildRunner(executor, make_settings(tmp_path), cycle_id="c1")
        runner.build_commit(tmp_path / "wt", "5200; echo pwned", clean=False, module="x$(id)")
        inner = executor.calls[1][0][-1]
        assert "'5200; echo pwned'" in inner
        assert "'x$(id)'" in inner

    def test_public_file_injection(self, tmp_path):
        seen = []

        def is_public(path: str) -> bool:
            seen.append(path)
            return path == "plat/route/x.c"

        runner = BuildRunner(
            FakeExecutor(), make_settings(tmp_path), is_public_file=is_public
        )
        assert runner.is_public_file is not None


class TestIsSuccess:
    def test_three_checks_pass(self, tmp_path):
        log_path = tmp_path / "build.log"
        log_path.write_text(
            "Make rootfs success\n"
            "convert file rcios.bin to MSG5200_SYSTEM_4.33.204_20260820.bin success!\n"
        )
        result = BuildResult(
            model="5200", returncode=0, log_path=log_path, succeeded=True, errors=[]
        )
        runner = BuildRunner(FakeExecutor(), make_settings(tmp_path), cycle_id="c1")
        assert runner.is_success(result) is True

    def test_nonzero_returncode_fails(self, tmp_path):
        log_path = tmp_path / "build.log"
        log_path.write_text(
            "Make rootfs success\n"
            "convert file rcios.bin to MSG5200_SYSTEM_4.33.204_20260820.bin success!\n"
        )
        result = BuildResult(
            model="5200", returncode=1, log_path=log_path, succeeded=False, errors=[]
        )
        runner = BuildRunner(FakeExecutor(), make_settings(tmp_path), cycle_id="c1")
        assert runner.is_success(result) is False

    def test_no_success_marker_fails(self, tmp_path):
        log_path = tmp_path / "build.log"
        log_path.write_text("/x/foo.c:12: error: implicit declaration of function 'bar'\n")
        result = BuildResult(
            model="5200", returncode=0, log_path=log_path, succeeded=False, errors=[]
        )
        runner = BuildRunner(FakeExecutor(), make_settings(tmp_path), cycle_id="c1")
        assert runner.is_success(result) is False

    def test_no_artifact_marker_fails(self, tmp_path):
        log_path = tmp_path / "build.log"
        log_path.write_text("Make rootfs success\n")
        result = BuildResult(
            model="5200", returncode=0, log_path=log_path, succeeded=False, errors=[]
        )
        runner = BuildRunner(FakeExecutor(), make_settings(tmp_path), cycle_id="c1")
        assert runner.is_success(result) is False

    def test_missing_log_file_fails(self, tmp_path):
        result = BuildResult(
            model="5200",
            returncode=0,
            log_path=tmp_path / "missing.log",
            succeeded=False,
            errors=[],
        )
        runner = BuildRunner(FakeExecutor(), make_settings(tmp_path), cycle_id="c1")
        assert runner.is_success(result) is False
