from pathlib import Path

from bsa.executor.base import CommandExecutor, CompletedProcess
from bsa.executor.exceptions import SafetyViolation


class WhitelistExecutor:
    """Security decorator: only allows git subcommands in ALLOWED_GIT.

    Callers pass subcommand-first args (e.g. ``["fetch", "--all"]``); the
    ``git`` binary prefix is prepended here, so the inner executor runs the
    real ``git fetch --all``. Docker commands go through the executor directly
    (BuildRunner's concern) and bypass this git whitelist.
    """

    ALLOWED_GIT: frozenset[str] = frozenset({
        "fetch",
        "checkout",
        "cherry-pick",
        "log",
        "diff",
        "show",
        "format-patch",
        "worktree",
        "merge-base",
        "status",
        "add",
        "rev-parse",
        "diff-tree",
    })

    def __init__(self, inner: CommandExecutor) -> None:
        self.inner = inner

    def run(
        self,
        args: list[str],
        *,
        cwd: str | Path | None = None,
        timeout_sec: int = 300,
        env: dict[str, str] | None = None,
    ) -> CompletedProcess:
        if not args or args[0] not in self.ALLOWED_GIT:
            cmd = args[0] if args else ""
            raise SafetyViolation(f"git command not whitelisted: {cmd!r}")
        return self.inner.run(
            ["git", *args], cwd=cwd, timeout_sec=timeout_sec, env=env
        )
