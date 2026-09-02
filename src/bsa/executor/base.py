from pathlib import Path
from typing import Protocol

from pydantic import BaseModel


class CompletedProcess(BaseModel):
    returncode: int
    stdout: str
    stderr: str


class CommandExecutor(Protocol):
    """Runs an external command and returns its captured output.

    Implementations: SubprocessExecutor (production), FakeExecutor (tests),
    WhitelistExecutor (security decorator). Callers pass subcommand-style
    args; the git binary prefix is the WhitelistExecutor's responsibility.
    """

    def run(
        self,
        args: list[str],
        *,
        cwd: str | Path | None = None,
        timeout_sec: int = 300,
        env: dict[str, str] | None = None,
        stream_to: str | Path | None = None,
    ) -> CompletedProcess: ...
