import subprocess
from pathlib import Path

from bsa.executor.base import CompletedProcess
from bsa.executor.exceptions import InfrastructureError


class SubprocessExecutor:
    """Production executor: runs args via subprocess with timeout and output capture.

    A nonzero returncode is returned, not raised — callers decide the outcome.
    """

    def run(
        self,
        args: list[str],
        *,
        cwd: str | Path | None = None,
        timeout_sec: int = 300,
        env: dict[str, str] | None = None,
    ) -> CompletedProcess:
        try:
            result = subprocess.run(
                args,
                cwd=cwd,
                timeout=timeout_sec,
                env=env,
                text=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise InfrastructureError(
                f"timeout: command exceeded {timeout_sec}s "
                f"(tool timeout != 操作未发生, check real git state): {' '.join(args)}"
            ) from exc
        return CompletedProcess(
            returncode=result.returncode, stdout=result.stdout, stderr=result.stderr
        )
