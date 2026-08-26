"""任务工具模块：web 平台侧的任务 DB 直读直写 + 测试用执行器别名。

解耦架构下 web 是纯管理/UI 层：
- 提交 = ``enqueue_task`` 写 tasks 表（queued），由独立 ``bsa_web.executor``
  守护进程消费执行（见 executor.py）；web **不持有执行线程、不 spawn CLI**。
- 读取 = ``get_task`` / ``task_detail_url`` 直接查库。

``TaskRunner`` 是 ``TaskExecutor`` 的向后兼容别名，供 web 测试用
（``_install_runner(app, run_func)`` 注入内存 executor，不依赖独立进程）。
生产 app.py 不再实例化 TaskRunner。
"""

from __future__ import annotations

from bsa_web.executor import (
    DEFAULT_TIMEOUT_SEC,
    QUEUED_WAIT_REASON,
    TaskExecutor,
    cleanup_task,
    enqueue_task,
    get_task,
    task_detail_url,
)

TaskRunner = TaskExecutor

__all__ = [
    "DEFAULT_TIMEOUT_SEC",
    "QUEUED_WAIT_REASON",
    "TaskRunner",
    "TaskExecutor",
    "cleanup_task",
    "enqueue_task",
    "get_task",
    "task_detail_url",
]
