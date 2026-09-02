"""V1 `bsa report` 的投影封装。

Web 层实时渲染依赖：每次请求经子进程只读调用 `bsa report <cycle> --json`，
不缓存。子进程失败（返回码非 0 / 超时 / 输出损坏）一律返回 None，平台容错。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime

from bsa.report.projection import cycle_summary
from bsa.scheduler.cycle import list_cycle_records

__all__ = [
    "load_cycle",
    "latest_completed_cycle",
    "list_cycles",
    "latest_cycle_start",
    "cycle_summary",
]


def load_cycle(log_dir: str, cycle_id: str) -> dict | None:
    """子进程调 `bsa report <cycle> --json`，返回解析后的 JSON；失败返回 None。

    环境变量注入 ``LOG_DIR``（pydantic-settings 字段名大写映射，无前缀，已核实
    非 ``BSA_LOG_DIR``）。env 优先级高于 ``.env`` 文件，注入可覆盖任何 .env 的
    LOG_DIR；其余必填设置继承进程环境（生产同 env 部署）或 .env。
    """
    env = dict(os.environ)
    env["LOG_DIR"] = str(log_dir)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "bsa.cli", "report", cycle_id, "--json"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def latest_completed_cycle(log_dir: str) -> str | None:
    """取状态非 running 且 started_at 最近的周期 id；无则 None。"""
    records = [
        r for r in list_cycle_records(log_dir) if r.get("status") != "running"
    ]
    return records[-1]["cycle_id"] if records else None


def list_cycles(log_dir: str) -> list[dict]:
    """周期记录列表，按 started_at 倒序（最新在前）。"""
    return list(reversed(list_cycle_records(log_dir)))


def latest_cycle_start(log_dir: str) -> datetime | None:
    """最近完成周期（非 running）的 started_at，归一化为无时区 UTC。

    引擎写 ``cycle.json`` 的 ``started_at`` 是本地墙钟（``datetime.now().isoformat``，
    无时区，如 ``2026-09-01T00:00:04``，CST=UTC+8）；``tasks.created_at`` 是 UTC-aware
    ISO。两者比较前必须统一到 UTC-naive——否则偏移 8 小时，当天任务会被误归档。

    引擎与 web 同机同目录（systemd WorkingDirectory 一致），系统本地时区即引擎时区，
    故 naive 时间按系统本地时区解释。无已完成周期 / started_at 缺失/损坏返回 None
    （调用方按「不过滤」处理）。
    """
    records = [
        r for r in list_cycle_records(log_dir) if r.get("status") != "running"
    ]
    if not records:
        return None
    started = records[-1].get("started_at")  # records 已按 started_at 升序
    if not started:
        return None
    try:
        dt = datetime.fromisoformat(started)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive 本地墙钟 → 附系统本地时区
    return dt.astimezone(UTC).replace(tzinfo=None)
