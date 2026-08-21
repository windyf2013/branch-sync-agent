from collections.abc import Callable
from pathlib import Path
from typing import Any

from bsa.executor.base import CompletedProcess

Response = CompletedProcess | Callable[[list[str], dict[str, Any]], CompletedProcess]


class FakeExecutor:
    """Test executor: returns preset responses and records calls.

    Responses are consumed in order; each may be a CompletedProcess or a
    callable ``(args, kwargs) -> CompletedProcess``. When responses run out,
    an empty success is returned.
    """

    def __init__(self, responses: list[Response] | None = None) -> None:
        self.responses = list(responses) if responses else []
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(
        self,
        args: list[str],
        *,
        cwd: str | Path | None = None,
        timeout_sec: int = 300,
        env: dict[str, str] | None = None,
    ) -> CompletedProcess:
        kwargs: dict[str, Any] = {"cwd": cwd, "timeout_sec": timeout_sec, "env": env}
        self.calls.append((args, kwargs))
        if self.responses:
            response = self.responses.pop(0)
            if callable(response):
                return response(args, kwargs)
            return response
        return CompletedProcess(returncode=0, stdout="", stderr="")
