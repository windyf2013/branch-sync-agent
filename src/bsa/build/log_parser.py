import re

_ERROR_LINE = re.compile(r"\berror:", re.IGNORECASE)
_ROOTFS_MARKER = "Make rootfs success"
_MAX_KEEP_ERRORS = 500
_TAIL_KEEP_ERRORS = 100


def _artifact_pattern(model: str) -> re.Pattern[str]:
    return re.compile(rf"MSG{re.escape(model)}_(?:[^\s]*_)?SYSTEM_[^\s]*\.bin")


def extract_errors(
    log: str, *, max_chars: int = 3000, context_lines: int = 20
) -> list[str]:
    """Extract ``error:`` lines with surrounding context (decision 26).

    Only ``error:`` / ``Error:`` lines are failure evidence; ``fatal:`` /
    ``warning:`` / ``note:`` lines are long-standing noise and are never
    treated as failures. Each returned item is one block: the error line plus
    ``context_lines`` of context before and after it. Total output is capped
    at ``max_chars``; when more than 500 errors exist the first 500 and the
    last 100 are kept.
    """
    lines = log.splitlines()
    error_idx = [
        i for i, line in enumerate(lines) if _ERROR_LINE.search(line) is not None
    ]
    if len(error_idx) > _MAX_KEEP_ERRORS:
        error_idx = error_idx[:_MAX_KEEP_ERRORS] + error_idx[-_TAIL_KEEP_ERRORS:]

    blocks: list[str] = []
    used = 0
    for idx in error_idx:
        start = max(0, idx - context_lines)
        block = "\n".join(lines[start : idx + context_lines + 1])
        remaining = max_chars - used
        if len(block) <= remaining:
            blocks.append(block)
            used += len(block)
        else:
            if remaining > 0:
                blocks.append(block[:remaining])
            break
    return blocks


def has_success_marker(log: str, model: str) -> bool:
    """True when the log shows a compile-success marker (decision 26).

    Matches ``Make rootfs success`` or an artifact name
    ``MSG<model>_*_SYSTEM_*.bin`` in a convert line. Multi-segment artifact
    names (e.g. ``MSG<model>_<X>_SYSTEM_*``) are not currently matched; extend
    `_artifact_pattern` if real logs need them.
    """
    if _ROOTFS_MARKER in log:
        return True
    return _artifact_pattern(model).search(log) is not None


def artifact_success_marker(log: str, model: str) -> bool:
    """True when a ``MSG<model>_*_SYSTEM_*.bin`` artifact appears in the log."""
    return _artifact_pattern(model).search(log) is not None
