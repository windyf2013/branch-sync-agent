import re

_ERROR_LINE = re.compile(r"\berror:", re.IGNORECASE)
# 硬失败行：不满足 ``error:`` 但必然致命的证据。刻意排除 ``fatal: not a git
# repository``（内核构建里长期存在的噪声，构建会继续）。只收构建系统自己的
# 失败声明与 git 身份缺失这两类确定终止编译的行；不匹配 ``make ***``——
# ``make clean`` 阶段对尚未 clone 的组件会打 ``make[1]: *** ... No such file
# or directory. Stop.`` 这类可忽略噪声。
_HARD_ERROR_LINE = re.compile(
    r"fatal: unable to auto-detect email"
    r"|(?:author|committer) identity unknown"
    r"|\bplat make failed\b",
    re.IGNORECASE,
)
_ROOTFS_MARKER = "Make rootfs success"
_MAX_KEEP_ERRORS = 500
_TAIL_KEEP_ERRORS = 100


def _artifact_pattern(model: str) -> re.Pattern[str]:
    # 产物名大小写由构建工具决定（如 product "2600m" 产出 MSG2600M_*.bin），引擎
    # 不可控 → 大小写不敏感匹配，否则真成功会被误判失败（真机 2600m 基线根因）。
    return re.compile(
        rf"MSG{re.escape(model)}_(?:[^\s]*_)?SYSTEM_[^\s]*\.bin", re.IGNORECASE
    )


def extract_errors(
    log: str, *, max_chars: int = 3000, context_lines: int = 20
) -> list[str]:
    """Extract ``error:`` lines with surrounding context (decision 26).

    Only ``error:`` / ``Error:`` lines are failure evidence; ``fatal:`` /
    ``warning:`` / ``note:`` lines are long-standing noise and are never
    treated as failures. ``_HARD_ERROR_LINE`` (git 身份缺失、make 顶层失败)
    是例外：这些行不含 ``error:`` 却必然终止编译。Each returned item is one
    block: the error line plus ``context_lines`` of context before and after
    it. Total output is capped at ``max_chars``; when more than 500 errors
    exist the first 500 and the last 100 are kept.
    """
    lines = log.splitlines()
    error_idx = [
        i
        for i, line in enumerate(lines)
        if _ERROR_LINE.search(line) is not None or _HARD_ERROR_LINE.search(line) is not None
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
    ``MSG<model>_*_SYSTEM_*.bin`` in a convert line (大小写不敏感——构建工具可能
    把 product 大写产出 MSG<MODEL>_*.bin)。Multi-segment artifact names
    (e.g. ``MSG<model>_<X>_SYSTEM_*``) are not currently matched; extend
    `_artifact_pattern` if real logs need them.
    """
    if _ROOTFS_MARKER in log:
        return True
    return _artifact_pattern(model).search(log) is not None


def artifact_success_marker(log: str, model: str) -> bool:
    """True when a ``MSG<model>_*_SYSTEM_*.bin`` artifact appears in the log
    (大小写不敏感)。"""
    return _artifact_pattern(model).search(log) is not None
