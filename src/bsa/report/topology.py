"""同步拓扑（源 → 目标配对）的纯数据投影。

`HomologousSet`（同源集）只在 ``GraphContext.matrix`` 里以内存对象形式存在，图
invoke 结束后随 ctx 丢弃。报告与邮件渲染发生在 invoke 之后，届时只拿得到扁平的
``sources`` / ``targets`` 两个无配对列表 —— 「哪个业务分支喂哪个主分支」就此丢失。

本模块把 ``ctx.matrix`` 投影成可进 LangGraph checkpoint 的纯 dict/list/str：不引入
任何配对逻辑，语义仍是 ``build_matrix`` 那一份（不变量 #1），只做形状转换。
"""

from __future__ import annotations

from typing import Any

from bsa.rules.branch_md import HomologousSet


def matrix_to_topology(matrix: list[HomologousSet] | None) -> list[dict[str, Any]]:
    """``[HomologousSet]`` → ``[{"section", "sources", "targets"}]``。

    - 只取分支名，丢掉 ``BranchRef`` 的 branch_type（报告不展示它，且 pydantic 模型
      进 checkpoint 需要扩 serde allowlist，纯数据不需要）。
    - ``sorted()`` 保序：分支文件的书写顺序不该影响报告呈现。
    - 空矩阵返回 ``[]``。这不是静默降级 —— ``detect_commits`` 已为「零同步边」写
      ``errors["branch_matrix"]`` 响亮报错（不变量 #1/#13），此处只如实投影。
    """
    return [
        {
            "section": hs.section,
            "sources": sorted(ref.name for ref in hs.sources),
            "targets": sorted(ref.name for ref in hs.need_sync_targets),
        }
        for hs in matrix or []
    ]
