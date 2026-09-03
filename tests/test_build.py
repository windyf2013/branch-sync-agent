from pathlib import Path

import pytest

from bsa.build.log_parser import extract_errors, has_success_marker
from bsa.build.runner import BuildResult, BuildRunner
from bsa.config.settings import Settings
from bsa.executor import CompletedProcess, FakeExecutor
from bsa.executor.exceptions import InfrastructureError
from bsa.rules.build_rules import BuildType

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

    def test_git_identity_fatal_is_hard_error(self):
        # 真机基线编译失败：容器无 git 身份 → strongswan make add_patch Error 128。
        log = (
            "hint: Using 'master' as the name for the initial branch.\n"
            "Author identity unknown\n"
            "*** Please tell me who you are.\n"
            "fatal: unable to auto-detect email address (got 'ubuntu@x.(none)')\n"
            "Committer identity unknown\n"
            "fatal: unable to auto-detect email address (got 'ubuntu@x.(none)')\n"
            "make[2]: *** [Makefile:58: add_patch] Error 128\n"
            "Plat make failed\n"
        )
        result = extract_errors(log)
        joined = "\n".join(result)
        assert "unable to auto-detect email" in joined
        assert "identity unknown" in joined
        assert "Plat make failed" in joined
        assert "Error 128" in joined

    def test_benign_fatal_not_a_git_repository_still_ignored(self):
        log = (
            "fatal: not a git repository: '.../FleetConntrackDriver/.git'\n"
            "  CC [M]  drivers/net/ethernet/realtek/rtl86900/FleetConntrackDriver/src/x.o\n"
        )
        assert extract_errors(log) == []


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


class TestDockerGitconfigArgs:
    def test_mounts_gitconfig_when_present(self, tmp_path, monkeypatch):
        from bsa.build.runner import _docker_gitconfig_args

        home = tmp_path / "home"
        home.mkdir()
        gitconfig = home / ".gitconfig"
        gitconfig.write_text("[user]\n\tname = yangfu\n", encoding="utf-8")
        monkeypatch.setattr("os.path.expanduser", lambda p: str(gitconfig))
        assert _docker_gitconfig_args(make_settings(tmp_path)) == [
            "-v",
            f"{gitconfig}:/home/ubuntu/.gitconfig:ro",
        ]

    def test_no_args_when_gitconfig_missing(self, tmp_path, monkeypatch):
        from bsa.build.runner import _docker_gitconfig_args

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr("os.path.expanduser", lambda p: str(home / ".gitconfig"))
        assert _docker_gitconfig_args(make_settings(tmp_path)) == []

    def test_root_user_targets_root_home(self, tmp_path, monkeypatch):
        from bsa.build.runner import _docker_gitconfig_args

        home = tmp_path / "home"
        home.mkdir()
        gitconfig = home / ".gitconfig"
        gitconfig.write_text("[user]\n\tname = yangfu\n", encoding="utf-8")
        monkeypatch.setattr("os.path.expanduser", lambda p: str(gitconfig))
        settings = make_settings(tmp_path)
        settings.docker_user = "root"
        assert _docker_gitconfig_args(settings) == [
            "-v",
            f"{gitconfig}:/root/.gitconfig:ro",
        ]


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
        gitconfig_expected = []
        gitconfig = Path(os.path.expanduser("~/.gitconfig"))
        if gitconfig.is_file():
            gitconfig_expected = [
                "-v",
                f"{gitconfig}:/home/ubuntu/.gitconfig:ro",
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
            *gitconfig_expected,
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
        # 模块编译不跑 code_update.sh -d（那是全量前置，重拉独立子仓库）
        assert executor.calls[1][0][-1] == (
            "cd /workspace/rcios/build/platform/RTL9617C "
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
            "&& ./RTL9617C_build.sh 5200 clean && ./RTL9617C_build.sh 5200"
        )

    def test_customer_model_selects_operator_script(self, tmp_path):
        executor = FakeExecutor([ok(), ok(), ok()])
        runner = BuildRunner(executor, make_settings(tmp_path), cycle_id="c1")
        runner.build_commit(tmp_path / "wt", "2600_CMCC", clean=False, module=None)
        inner = executor.calls[1][0][-1]
        assert (
            "cd /workspace/rcios/build/platform/RTL9617C "
            "&& ./RTL9617C_build_cmcc.sh 2600"
        ) in inner

    def test_explicit_build_type_selects_custom_script(self, tmp_path):
        executor = FakeExecutor([ok(), ok(), ok()])
        runner = BuildRunner(
            executor,
            make_settings(tmp_path),
            cycle_id="c1",
            build_types={"5200B": BuildType(script="X86.sh", product="5200B")},
        )
        runner.build_commit(tmp_path / "wt", "5200B", clean=False, module=None)
        inner = executor.calls[1][0][-1]
        assert "./X86.sh 5200B" in inner

    def test_build_streams_docker_output_to_log_path(self, tmp_path):
        def respond(args, kwargs):
            if kwargs.get("stream_to"):
                Path(kwargs["stream_to"]).write_text(
                    "streamed build output", encoding="utf-8"
                )
            return CompletedProcess(returncode=0, stdout="", stderr="")

        executor = FakeExecutor([ok(), respond, ok()])
        runner = BuildRunner(executor, make_settings(tmp_path), cycle_id="c1")
        log_path = tmp_path / "logs" / "build.log"
        runner.build_commit(
            tmp_path / "wt", "5200", clean=False, module=None, log_path=log_path
        )
        assert log_path.read_text(encoding="utf-8") == "streamed build output"
        assert executor.calls[1][1]["stream_to"] == str(log_path)

    def test_build_parses_errors_from_log_file_when_streamed(self, tmp_path):
        # stream_to 时 SubprocessExecutor 返回空输出，错误必须从落盘的日志解析。
        def respond(args, kwargs):
            if kwargs.get("stream_to"):
                Path(kwargs["stream_to"]).write_text(
                    "fatal: unable to auto-detect email address\n"
                    "make[2]: *** [Makefile:58: add_patch] Error 128\n",
                    encoding="utf-8",
                )
            return CompletedProcess(returncode=2, stdout="", stderr="")

        executor = FakeExecutor([ok(), respond, ok()])
        runner = BuildRunner(executor, make_settings(tmp_path), cycle_id="c1")
        log_path = tmp_path / "logs" / "build.log"
        result = runner.build_commit(
            tmp_path / "wt", "5200", clean=False, module=None, log_path=log_path
        )
        assert result.errors != []
        joined = "\n".join(result.errors)
        assert "unable to auto-detect email" in joined
        assert "Error 128" in joined

    def test_unknown_model_without_build_types_legacy_inference(self, tmp_path):
        executor = FakeExecutor([ok(), ok(), ok()])
        runner = BuildRunner(executor, make_settings(tmp_path), cycle_id="c1")
        runner.build_commit(tmp_path / "wt", "2600_CMCC", clean=False, module=None)
        inner = executor.calls[1][0][-1]
        assert "./RTL9617C_build_cmcc.sh 2600" in inner

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

    def test_module_multi_token_quoted_separately(self, tmp_path):
        # 数据驱动 module（如 "component wlan"）逐段 quote 成独立 argv。
        executor = FakeExecutor([ok(), ok(), ok()])
        runner = BuildRunner(executor, make_settings(tmp_path), cycle_id="c1")
        runner.build_commit(tmp_path / "wt", "5200", clean=False, module="component wlan")
        inner = executor.calls[1][0][-1]
        assert inner.endswith(" component wlan")

    def test_module_multi_token_special_chars_quoted(self, tmp_path):
        executor = FakeExecutor([ok(), ok(), ok()])
        runner = BuildRunner(executor, make_settings(tmp_path), cycle_id="c1")
        runner.build_commit(tmp_path / "wt", "5200", clean=False, module="component x$(id)")
        inner = executor.calls[1][0][-1]
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

    def test_module_build_success_by_returncode_only(self, tmp_path):
        # 模块级编译（component cell 等）只编单个组件，不产 rootfs / MSG 产物，
        # 退出码 0 即为成功——三重校验会让干净编译的模块被误判 FAILED。
        log_path = tmp_path / "build.log"
        log_path.write_text("GENERATE executable file cell\nmake: Leaving directory\n")
        result = BuildResult(
            model="5200", returncode=0, log_path=log_path, succeeded=True,
            errors=[], module="component cell",
        )
        runner = BuildRunner(FakeExecutor(), make_settings(tmp_path), cycle_id="c1")
        assert runner.is_success(result) is True

    def test_module_build_nonzero_returncode_fails(self, tmp_path):
        log_path = tmp_path / "build.log"
        log_path.write_text("/x/cell.c:10: error: undeclared identifier\n")
        result = BuildResult(
            model="5200", returncode=2, log_path=log_path, succeeded=False,
            errors=["error: undeclared identifier"], module="component cell",
        )
        runner = BuildRunner(FakeExecutor(), make_settings(tmp_path), cycle_id="c1")
        assert runner.is_success(result) is False
