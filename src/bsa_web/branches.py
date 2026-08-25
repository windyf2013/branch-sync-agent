"""从 branch.md 提取 RCIOS 仓库分支列表，供工作台源/目标分支下拉框使用。

branch.md 采用多仓库清单（inventory）格式：

    ## 1 RCIOS代码库
    - 路径：rcios

    ### 1.1 组网产品分支
    - br_v4.33_5200_CU_develop_20260518

仓库归属由 ``- 路径：<repo>`` 标记界定；RCIOS 仓库路径为 ``rcios``。
本模块只返回 RCIOS 仓库下的分支名（产品线小节内 bullet），供 SSR 渲染。
"""

from __future__ import annotations

import re
from pathlib import Path

_PATH_BULLET_RE = re.compile(r"^\s*-\s+(?:路径|path|Path|PATH)\s*[：:]\s*(\S+)")
_BRANCH_RE = re.compile(r"^\s*-\s+(\S+)")
_RCIOS_PATHS = frozenset({"rcios"})

_REPO_HEADER_RE = re.compile(r"^##\s+")


def rcios_branch_names(branch_file: str | Path) -> list[str]:
    """读取 branch.md 并返回 RCIOS 仓库的分支名列表（去重排序）。

    branch.md 缺失/不可读时返回空列表（平台容错，不阻塞工作台渲染）。
    """
    path = Path(branch_file)
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return _rcios_names_from_text(text)


def _rcios_names_from_text(text: str) -> list[str]:
    names: set[str] = set()
    collecting = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _REPO_HEADER_RE.match(line):
            collecting = False
            continue
        path_match = _PATH_BULLET_RE.match(line)
        if path_match is not None:
            collecting = path_match.group(1).lower() in _RCIOS_PATHS
            continue
        if collecting:
            branch_match = _BRANCH_RE.match(line)
            if branch_match is not None:
                names.add(branch_match.group(1))
    return sorted(names)
