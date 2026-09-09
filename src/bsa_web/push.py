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

from bsa.build.runner import _sanitize_container_name
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


def _valid_worktree_dir(path: Path) -> bool:
    """True when path 是可用的 git worktree（.git gitdir 可解析）。

    与 V1 ``graph.nodes._is_valid_worktree`` 同判定：有效 worktree 的 ``.git``
    是文件且 ``gitdir:`` 指向存在的目录，或是目录（老式布局）。已 remove 的
    worktree 残留目录 gitdir 悬空 → 无效，禁止当作本 target 的 worktree。
    """
    dot_git = path / ".git"
    if not dot_git.exists():
        return False
    if dot_git.is_dir():
        return True
    text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
    if not text.startswith("gitdir:"):
        return False
    return Path(text[len("gitdir:") :].strip()).is_dir()


def _checked_out_branch(worktree: str) -> str | None:
    """返回 worktree 当前检出分支名；detached HEAD 或不可读返回 None。"""
    try:
        proc = subprocess.run(
            ["git", "-C", worktree, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    branch = proc.stdout.strip()
    return None if branch == "HEAD" else branch


def worktree_belongs_to_target(
    worktree: str, target: str, cycle_id: str | None = None
) -> str | None:
    """确认 worktree 属于 target（含周期），返回原因或 None。

    三重绑定（invariant：绝不从无法确认归属 target 的 worktree 推送）：
    ① 目录存在且为有效 git worktree（.git gitdir 可解析）；
    ② 路径尾部命名 ``<target>-<cycle_id>``（与 V1 ``prepare_worktree`` 一致，
       排除 stale/错指到其他仓库的投影路径）；
    ③ 检出分支与 target 一致（detached HEAD 无法确认时跳过，靠 ①② 兜底）。
    """
    if not worktree:
        return "worktree 不存在"
    path = Path(worktree)
    if not path.exists():
        return f"worktree 不存在：{worktree}"
    if not _valid_worktree_dir(path):
        return f"worktree 不是有效 git 仓库：{worktree}"
    if cycle_id is not None and not str(path).endswith(
        f"{target}-{_sanitize_container_name(cycle_id)}"
    ):
        return f"worktree 路径与目标/周期不匹配：{path}"
    branch = _checked_out_branch(worktree)
    if branch and branch != target:
        return f"worktree 检出分支 {branch}，与目标 {target} 不一致"
    return None


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


def check_push_gates(
    projection, target, *, forbidden, status_clean, cycle_id=None
) -> list[str]:
    """四道闸预检，返回未过闸原因列表（空=通过）。

    ① 分支状态 SUCCESS；② worktree 存在且绑定 target/周期（worktree↔target
    三重绑定校验）；③ 目标不在 forbidden_branches；④ worktree ``git status
    --porcelain`` 干净（status_clean 由调用方注入）。

    闸② 的绑定周期以投影自报周期（``projection["cycle_id"]``）为准：retained
    重跑线程（rerun-*）的 state 在写入时即来源周期（见 rerun.py
    ``_rerun_retained``：``run_sync_command(cycle_id=来源周期, thread_id=rerun
    线程)``），其 worktree 命名 ``<target>-<来源周期>``，与 URL/请求体携带的
    rerun 线程 id 不同。用投影自报周期校验才能通过真实 worktree 归属；请求体
    cycle_id 仍用于加载投影与审计。
    """
    reasons = []
    branch = (projection.get("branch_results") or {}).get(target)
    if branch is None:
        reasons.append(f"投影中不存在分支 {target}")
        return reasons
    if branch.get("status") != "SUCCESS":
        reasons.append(f"分支 {target} 状态 {branch.get('status')}，非 SUCCESS")
    worktree = branch.get("worktree_path") or ""
    binding = worktree_belongs_to_target(
        worktree, target, projection.get("cycle_id") or cycle_id
    )
    if binding is not None:
        reasons.append(binding)
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
