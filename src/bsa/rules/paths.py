from __future__ import annotations


def is_public_file(path: str, public_dirs: list[str]) -> bool:
    """True when path 落在公共目录下 → 该 commit 编译降级全量编译（决策 2）。"""
    return any(
        (d.endswith("/") and path.startswith(d)) or (not d.endswith("/") and path == d)
        for d in public_dirs
    )
