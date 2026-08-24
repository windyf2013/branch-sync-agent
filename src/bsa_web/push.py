"""推送执行链：四道闸预检 + 受限 push 执行器 + 结果回显。

任务 15。推送是高风险人工动作，仅由 operator 端点点名触发（``POST /api/push``），
本模块无任何自动推送路径。四道闸（分支 SUCCESS / worktree 存在 / 目标不在
forbidden_branches / worktree 干净）全部通过后才执行受限 ``git push origin
HEAD:<target>``（禁 --force）；非 fast-forward 失败回显"远端已前进，请重新同步"。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from bsa.executor.base import CompletedProcess
from bsa.executor.exceptions import SafetyViolation
from bsa.executor.subprocess import SubprocessExecutor
from bsa.rules import load_safety_rules

_SAFE_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-/]*$")
_NON_FF_MARKERS = ("non-fast-forward", "rejected", "fetch first", "stale info")


def _bundled_rules_dir() -> Path:
    """定位打包内的 safety_rules.yaml 目录（与 V1 factory 同策略）。"""
    import importlib.resources

    try:
        resource = importlib.resources.files("bsa.rules")
        if resource.is_dir():
            return Path(str(resource))
    except (ModuleNotFoundError, FileNotFoundError):
        pass
    return Path(__file__).resolve().parent.parent / "bsa" / "rules"


def load_forbidden_branches() -> list[str]:
    """读 V1 safety_rules.yaml 的 forbidden_branches（数据驱动安全红线）。"""
    rules = load_safety_rules(_bundled_rules_dir() / "safety_rules.yaml")
    return rules.forbidden_branches


def worktree_is_clean(worktree: str) -> bool:
    """worktree 上跑只读 ``git status --porcelain``；非空输出视为不干净。

    只读命令，subprocess 直跑 git 即可，不经受限 executor（executor 仅放行 push）。
    """
    if not worktree or not Path(worktree).exists():
        return False
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and not proc.stdout.strip()


def check_push_gates(projection, target, *, forbidden, status_clean) -> list[str]:
    """四道闸预检，返回未过闸原因列表（空=通过）。

    ① 分支状态 SUCCESS；② worktree 存在；③ 目标不在 forbidden_branches；
    ④ worktree ``git status --porcelain`` 干净（status_clean 由调用方注入）。
    """
    reasons = []
    branch = (projection.get("branch_results") or {}).get(target)
    if branch is None:
        reasons.append(f"投影中不存在分支 {target}")
        return reasons
    if branch.get("status") != "SUCCESS":
        reasons.append(f"分支 {target} 状态 {branch.get('status')}，非 SUCCESS")
    worktree = branch.get("worktree_path") or ""
    if not worktree or not Path(worktree).exists():
        reasons.append(f"worktree 不存在：{worktree}")
    if target in forbidden:
        reasons.append(f"目标分支 {target} 在 forbidden_branches 中，禁止推送")
    if not status_clean:
        reasons.append("worktree 存在未提交改动")
    return reasons


def _is_valid_target(target: str) -> bool:
    """目标分支名校验：字母数字开头、仅 [A-Za-z0-9_.-/]，禁空格/斜杠穿越/双点。"""
    if not target or target.startswith("/") or ".." in target or "//" in target:
        return False
    return bool(_SAFE_TARGET_RE.fullmatch(target))


class PushExecutor:
    """受限执行器：白名单仅 ``push``，argv 形状与目标名强校验，禁 --force。

    ``run`` 收到完整 argv（含 ``git`` 前缀），校验通过后交给 inner 执行。
    """

    def __init__(self, inner=None) -> None:
        self.inner = inner if inner is not None else SubprocessExecutor()

    def run(
        self,
        args: list[str],
        *,
        cwd: str | Path | None = None,
        timeout_sec: int = 300,
        env: dict[str, str] | None = None,
    ) -> CompletedProcess:
        self._validate(args)
        return self.inner.run(args, cwd=cwd, timeout_sec=timeout_sec, env=env)

    def _validate(self, args: list[str]) -> None:
        if not args or args[0] != "git":
            raise SafetyViolation(f"push 仅允许 git 命令: {args!r}")
        if len(args) < 2 or args[1] != "push":
            raise SafetyViolation(f"git command not whitelisted: {args[1:]!r}")
        if len(args) != 4:
            raise SafetyViolation(
                f"push argv 形状非法（仅 git push origin HEAD:<target>）: {args!r}"
            )
        if args[2] != "origin":
            raise SafetyViolation(f"push 远端非法: {args[2]!r}")
        for flag in args:
            if flag in ("-f", "--force"):
                raise SafetyViolation("禁止 --force 推送")
        refspec = args[3]
        if not refspec.startswith("HEAD:"):
            raise SafetyViolation(f"push refspec 非法: {refspec!r}")
        target = refspec[len("HEAD:"):]
        if not _is_valid_target(target):
            raise SafetyViolation(f"push 目标分支非法: {target!r}")


def _is_non_fast_forward(text: str) -> bool:
    lowered = text.lower()
    return any(m in lowered for m in _NON_FF_MARKERS)


def execute_push(executor, worktree: str, target: str) -> tuple[int, str]:
    """执行 ``git push origin HEAD:<target>``（cwd=worktree），返回 (rc, 消息)。

    rc=0 成功；非 fast-forward 失败回显"远端已前进，请重新同步"。
    """
    argv = ["git", "push", "origin", f"HEAD:{target}"]
    proc = executor.run(argv, cwd=worktree)
    if proc.returncode == 0:
        return 0, "推送成功"
    combined = (proc.stderr or "") + (proc.stdout or "")
    if _is_non_fast_forward(combined):
        return proc.returncode, "远端已前进，请重新同步"
    return proc.returncode, (proc.stderr or proc.stdout or "推送失败").strip()
