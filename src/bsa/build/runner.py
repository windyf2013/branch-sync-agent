import shlex
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from bsa.build.log_parser import artifact_success_marker, extract_errors, has_success_marker
from bsa.config.settings import Settings
from bsa.executor.base import CommandExecutor, CompletedProcess

_BUILD_EXEC_TIMEOUT_SEC = 3600


class BuildResult(BaseModel):
    model: str
    returncode: int
    log_path: Path
    succeeded: bool
    errors: list[str]


class BuildRunner:
    """Runs RCIOS cross-compiles inside a docker container (decision 1).

    Docker commands go through the injected executor directly — never through
    the git WhitelistExecutor. ``is_public_file`` is injected from
    rules/paths.is_public_file so callers can downgrade to a full (clean)
    compile when a changed file is public (decision 2).
    """

    def __init__(
        self,
        executor: CommandExecutor,
        settings: Settings,
        is_public_file: Callable[[str], bool] | None = None,
        *,
        cycle_id: str | None = None,
    ) -> None:
        self.executor = executor
        self.settings = settings
        self.is_public_file = is_public_file
        self.cycle_id = cycle_id

    def _container_name(self) -> str:
        if self.cycle_id:
            return f"{self.settings.docker_container_prefix}-{self.cycle_id}"
        return self.settings.docker_container_prefix

    def _docker_prefix(self) -> list[str]:
        return self.settings.docker_prefix.strip().split()

    def build_commit(
        self,
        worktree: Path,
        model: str,
        *,
        clean: bool,
        module: str | None,
        log_path: Path | None = None,
    ) -> BuildResult:
        """Build one model in docker: run -d (kept alive), exec build script, rm -f.

        ``log_path`` when provided is the per-commit log destination; the graph
        layer must pass ``{log_dir}/build/<branch>/<commit>/build.log`` to
        preserve the decision-26 audit trail. Otherwise a flat
        ``{log_dir}/build_{model}.log`` is used.
        """
        container = self._container_name()
        prefix = self._docker_prefix()

        self.executor.run(
            [
                *prefix,
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "-v",
                f"{worktree}:{self.settings.docker_mount_workspace}",
                "-v",
                "/usr/local:/usr/local",
                self.settings.docker_image,
                "sleep",
                "infinity",
            ]
        )

        script_dir = self.settings.build_script_dir.rstrip("/")
        # docker exec 默认工作目录是容器启动目录(/)；必须显式 -w 到挂载点，
        # 否则 cd build/platform/RTL9617C 在 / 下失败 (真机测试: No such file or directory)
        mount = self.settings.docker_mount_workspace.rstrip("/")
        steps = [f"cd {shlex.quote(script_dir)}", "code_update.sh -d"]
        if clean:
            steps.append("RTL9617C_build.sh clean")
        build_cmd = f"RTL9617C_build.sh {shlex.quote(model)}"
        if module:
            build_cmd = f"{build_cmd} {shlex.quote(module)}"
        steps.append(build_cmd)

        try:
            proc: CompletedProcess = self.executor.run(
                [
                    *prefix,
                    "docker",
                    "exec",
                    "-w",
                    mount,
                    container,
                    "bash",
                    "-c",
                    " && ".join(steps),
                ],
                timeout_sec=_BUILD_EXEC_TIMEOUT_SEC,
            )
        finally:
            self.executor.run([*prefix, "docker", "rm", "-f", container])

        log = proc.stdout + ("\n" if proc.stderr else "") + proc.stderr
        log_path = log_path or Path(self.settings.log_dir) / f"build_{model}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(log, encoding="utf-8", errors="replace")

        return BuildResult(
            model=model,
            returncode=proc.returncode,
            log_path=log_path,
            succeeded=proc.returncode == 0,
            errors=self.parse_errors(log),
        )

    def parse_errors(self, log_text: str) -> list[str]:
        return extract_errors(log_text)

    def is_success(self, result: BuildResult) -> bool:
        """Three-check success: artifact produced + log marker + container state."""
        try:
            log = result.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        container_ok = result.returncode == 0
        marker_ok = has_success_marker(log, result.model)
        artifact_ok = artifact_success_marker(log, result.model)
        return container_ok and marker_ok and artifact_ok
