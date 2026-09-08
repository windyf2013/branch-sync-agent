import hashlib
import time
from pathlib import Path

from bsa.domain.models import CherryPickResult
from bsa.executor.base import CommandExecutor, CompletedProcess
from bsa.executor.exceptions import InfrastructureError

MAX_CHANGED_FILES = 500
MAX_PATCH_LINES = 20_000
MAX_PATCH_CHARS = 2_000_000

_AUTH_ERROR_MARKERS = (
    "permission denied",
    "authentication failed",
    "could not read username",
    "host key verification failed",
)
_NETWORK_ERROR_MARKERS = (
    "could not resolve host",
    "connection timed out",
    "operation timed out",
    "could not read from remote",
    "network is unreachable",
    "no route to host",
)


def _classify_fetch_failure(stderr: str) -> str:
    """Classify a git fetch failure: ``auth``, ``network``, or ``unknown``."""
    text = stderr.lower()
    if any(marker in text for marker in _AUTH_ERROR_MARKERS):
        return "auth"
    if any(marker in text for marker in _NETWORK_ERROR_MARKERS):
        return "network"
    return "unknown"


class GitService:
    """Git operations for the sync workflow, run through an injected executor.

    Commands are passed as subcommand-first args without the ``git`` prefix; a
    whitelisted executor prepends the binary. Docker commands never pass here.
    """

    fetch_retry_count = 3
    fetch_retry_base_sec = 30

    def __init__(self, executor: CommandExecutor, repo_path: Path) -> None:
        self.executor = executor
        self.repo_path = repo_path

    def _run(self, args: list[str], *, error_msg: str | None = None) -> CompletedProcess:
        result = self.executor.run(args, cwd=self.repo_path)
        if result.returncode != 0:
            msg = error_msg or f"git command failed ({result.returncode}): {' '.join(args)}"
            detail = result.stderr.strip()
            if detail:
                msg = f"{msg}: {detail}"
            raise InfrastructureError(msg)
        return result

    def fetch_branch(self, branch: str) -> None:
        """git fetch origin <branch>：仅更新单个源分支的远端引用（快，失败即抛）。"""
        result = self.executor.run(["fetch", "origin", branch], cwd=self.repo_path)
        if result.returncode != 0:
            kind = _classify_fetch_failure(result.stderr)
            raise InfrastructureError(
                f"git fetch origin {branch} failed ({kind}): {result.stderr.strip()}"
            )

    def fetch_all(self) -> None:
        """git fetch --all --prune with exponential-backoff retries."""
        for attempt in range(1, self.fetch_retry_count + 1):
            result = self.executor.run(["fetch", "--all", "--prune"], cwd=self.repo_path)
            if result.returncode == 0:
                return
            if attempt < self.fetch_retry_count:
                time.sleep(self.fetch_retry_base_sec * (2 ** (attempt - 1)))
        kind = _classify_fetch_failure(result.stderr)
        raise InfrastructureError(
            f"git fetch --all --prune failed after {self.fetch_retry_count} attempts "
            f"({kind}): {result.stderr.strip()}"
        )

    def branch_tip(self, branch: str) -> tuple[str, str]:
        """Resolve a logical branch to (resolved_ref, tip_sha), preferring origin/."""
        resolved: str | None = None
        for candidate in (f"origin/{branch}", branch):
            check = self.executor.run(
                ["rev-parse", "--verify", "--quiet", candidate], cwd=self.repo_path
            )
            if check.returncode == 0:
                resolved = candidate
                break
        if resolved is None:
            raise InfrastructureError(f"branch not found: {branch}")
        tip = self._run(["rev-parse", resolved], error_msg=f"cannot resolve ref {resolved}")
        return resolved, tip.stdout.strip()

    def commits_in_window(self, since: str, until: str, ref: str) -> list[str]:
        args = [
            "log",
            "--reverse",
            "--first-parent",
            f"--since={since}",
            f"--until={until}",
            "--format=%H",
            ref,
        ]
        result = self._run(args, error_msg=f"cannot list commits for {ref}")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def commit_metadata(self, sha: str) -> tuple[str, str, str]:
        result = self._run(["log", "-1", "--format=%an|%aI|%B", sha])
        parts = result.stdout.split("|", 2)
        if len(parts) < 3:
            return "", "", result.stdout.strip()
        return parts[0], parts[1], parts[2]

    def _parent_sha(self, sha: str) -> str | None:
        """第一父 SHA（merge commit 取 %P 首个，即主线父）；根 commit 返回 None。"""
        result = self.executor.run(["log", "-1", "--format=%P", sha], cwd=self.repo_path)
        if result.returncode != 0:
            return None
        return result.stdout.split()[0] if result.stdout.split() else None

    def _numstat(self, sha: str) -> tuple[int, int] | None:
        """Return (file_count, total_lines) or None when the commit is too large."""
        result = self.executor.run(
            ["diff-tree", "--no-commit-id", "--numstat", "-r", sha], cwd=self.repo_path
        )
        if result.returncode != 0:
            return None
        file_count = 0
        total_lines = 0
        for line in result.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) < 2:
                continue
            file_count += 1
            if file_count > MAX_CHANGED_FILES:
                return None
            try:
                added = 0 if parts[0] == "-" else int(parts[0])
                deleted = 0 if parts[1] == "-" else int(parts[1])
            except ValueError:
                continue
            total_lines += added + deleted
            if total_lines > MAX_PATCH_LINES:
                return None
        return file_count, total_lines

    def changed_files(self, sha: str) -> list[str]:
        parent = self._parent_sha(sha)
        if parent is None:
            return []
        # 对第一父求 diff（决策 16 变体）：merge commit 经 ``diff-tree -r <first_parent>
        # <sha>`` 拿到主线聚合改动文件，而非 ``git show`` 的合流 diff（对 merge 恒空）。
        result = self._run(
            ["diff-tree", "--no-commit-id", "--name-only", "-r", parent, sha]
        )
        names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return names[:MAX_CHANGED_FILES]

    def commit_patch(self, sha: str, max_chars: int = MAX_PATCH_CHARS) -> str:
        parent = self._parent_sha(sha)
        if parent is None or self._numstat(sha) is None:
            return ""
        result = self._run(
            ["diff-tree", "--no-commit-id", "-p", "-r", parent, sha]
        )
        return (result.stdout or "")[:max_chars]

    def patch_id(self, sha: str) -> str | None:
        parent = self._parent_sha(sha)
        if parent is None or self._numstat(sha) is None:
            return None
        result = self._run(
            ["diff-tree", "--no-commit-id", "-p", "-r", parent, sha]
        )
        return _stable_patch_id(result.stdout)

    def diff_stat(self, sha: str) -> dict[str, int] | None:
        """第一父聚合变更统计 ``{files, insertions, deletions}``。

        与 ``changed_files``/``commit_patch``/``patch_id`` 同源（第一父聚合），
        单独计算、不设截断上限——超大 commit 即使 ``patch_text`` 因超限为空，
        ``diff_stat`` 仍保留文件数与增删行统计。根 commit（无父）返回 None。
        """
        parent = self._parent_sha(sha)
        if parent is None:
            return None
        result = self._run(
            ["diff-tree", "--no-commit-id", "--numstat", "-r", parent, sha]
        )
        files = 0
        insertions = 0
        deletions = 0
        for line in result.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) < 2:
                continue
            files += 1
            if parts[0] != "-":
                insertions += int(parts[0])
            if parts[1] != "-":
                deletions += int(parts[1])
        return {"files": files, "insertions": insertions, "deletions": deletions}

    def file_exists(self, ref: str, path: str) -> bool:
        result = self.executor.run(["rev-parse", "--verify", f"{ref}:{path}"], cwd=self.repo_path)
        return result.returncode == 0

    def show_file(self, ref: str, path: str) -> str | None:
        result = self.executor.run(["show", f"{ref}:{path}"], cwd=self.repo_path)
        if result.returncode != 0:
            return None
        return result.stdout

    def is_ancestor(self, sha: str, ref: str) -> bool:
        result = self.executor.run(
            ["merge-base", "--is-ancestor", sha, ref], cwd=self.repo_path
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise InfrastructureError(
            f"merge-base --is-ancestor {sha} {ref} failed: {result.stderr.strip()}"
        )

    def committer_email(self, sha: str) -> str:
        """commit 的提交者邮箱（直提场景 author==committer，即合入人）；取不到返回空串。"""
        result = self.executor.run(
            ["log", "-1", "--format=%ce", sha], cwd=self.repo_path
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    def merge_committer_emails(self, sha: str, branch: str) -> list[str]:
        """把 ``sha`` 合入 ``branch`` 主线的 merge 的 committer（真正的「合入人」）。

        git 没有独立「合入人」字段：业务分支上的叶子 commit 常 author==committer（直提），
        此时合入人就是 committer（调用方用 ``committer_email`` 即可）；而经个人分支 merge
        进来的 commit，其引入 merge 的 committer 才是合入人。判定：沿 branch first-parent
        从旧到新，找第一个「第二父含 sha 且第一父不含 sha」的 merge——该 merge 正是把
        sha 首次带上主线的那个。直提 commit 无引入 merge，返回空（不报错）。

        ``sha..branch`` 把候选 merge 限定为 sha 之后的历史：扫窗口 commit 都在 branch
        近期，候选 merge 极少；若 sha 是直提（本身已在 first-parent 线上），候选区恒空
        （无 merge 在其后），零开销。绝不从 branch 顶端扫到根（RCIOS 多年历史数千 merge，
        逐条 subprocess 判会挂死）。
        """
        result = self.executor.run(
            ["log", "--first-parent", "--merges", "--reverse", "--format=%H", f"{sha}..{branch}"],
            cwd=self.repo_path,
        )
        if result.returncode != 0:
            return []
        merges = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        for merge in merges:  # 旧→新:第一个引入者即 sha 首次上主线之处
            in_second = (
                self.executor.run(
                    ["merge-base", "--is-ancestor", sha, f"{merge}^2"], cwd=self.repo_path
                ).returncode
                == 0
            )
            if not in_second:
                continue
            in_first = (
                self.executor.run(
                    ["merge-base", "--is-ancestor", sha, f"{merge}^1"], cwd=self.repo_path
                ).returncode
                == 0
            )
            if not in_first:
                email = self.committer_email(merge)
                return [email] if email else []
        return []

    def add_worktree(self, branch: str, path: Path) -> None:
        self._run(["worktree", "add", str(path), branch], error_msg=f"cannot add worktree {path}")

    def remove_worktree(self, path: Path) -> None:
        self._run(
            ["worktree", "remove", "--force", str(path)],
            error_msg=f"cannot remove worktree {path}",
        )

    def list_worktrees(self) -> list[Path]:
        """List registered worktree paths (``git worktree list --porcelain``)."""
        result = self._run(["worktree", "list", "--porcelain"])
        paths: list[Path] = []
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                paths.append(Path(line[len("worktree ") :].strip()))
        return paths

    def cherry_pick(self, sha: str) -> CherryPickResult:
        result = self.executor.run(["cherry-pick", sha], cwd=self.repo_path)
        if result.returncode == 0:
            return CherryPickResult(status="OK")
        combined = f"{result.stdout}\n{result.stderr}"
        if "empty" in combined or "nothing to commit" in combined:
            self._run(
                ["cherry-pick", "--skip"],
                error_msg="cannot clear stuck cherry-pick sequencer",
            )
            return CherryPickResult(status="EMPTY", conflict_files=[])
        conflicts = self.unmerged_files()
        if conflicts:
            return CherryPickResult(status="CONFLICT", conflict_files=conflicts)
        return CherryPickResult(status="FAILED")

    def cherry_pick_continue(self) -> None:
        """Finish an in-progress cherry-pick after a resolved conflict.

        ``--no-edit`` reuses the original commit message so the sequencer
        commits without opening an editor; this clears the sequencer and makes
        ``HEAD`` advance so the sync patch is non-empty (C1).
        """
        self._run(
            ["cherry-pick", "--continue", "--no-edit"],
            error_msg="cannot finish cherry-pick after conflict resolution",
        )

    def unmerged_files(self) -> list[str]:
        result = self._run(["diff", "--name-only", "--diff-filter=U"])
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def diff_check(self) -> bool:
        result = self.executor.run(["diff", "--check"], cwd=self.repo_path)
        return result.returncode == 0

    def stage(self, paths: list[str]) -> None:
        self._run(["add", *paths], error_msg="cannot stage files")

    def format_patch(self, base: str, head: str, out_dir: Path, prefix: str) -> Path:
        result = self._run(["format-patch", "--stdout", f"{base}..{head}"])
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / prefix
        out_path.write_text(result.stdout or "", encoding="utf-8")
        return out_path

    def snapshot(self, paths: list[str]) -> dict[str, str]:
        snapshots: dict[str, str] = {}
        for rel in paths:
            path = self.repo_path / rel
            if path.is_file():
                snapshots[rel] = path.read_text(encoding="utf-8")
        return snapshots

    def restore(self, snapshots: dict[str, str]) -> None:
        for rel, content in snapshots.items():
            path = self.repo_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def status(self) -> str:
        return self._run(["status", "--porcelain"]).stdout


def _stable_patch_id(diff_text: str) -> str:
    """Compute git's stable patch-id for a diff, matching ``git patch-id --stable``.

    Replicates git builtin/patch-id.c: header lines are dropped, hunk/content
    lines are hashed with all whitespace removed, and hunks accumulate via
    byte-wise carry addition. ``patch-id`` is not whitelisted and the executor
    has no stdin support, so the algorithm is reproduced here.
    """

    def accumulate(acc: bytearray, digest: bytes) -> bytearray:
        carry = 0
        for i in range(20):
            carry += acc[i] + digest[i]
            acc[i] = carry & 0xFF
            carry >>= 8
        return acc

    def parse_hunk_count(text: str, i: int) -> tuple[int, int]:
        n = i
        while n < len(text) and text[n].isdigit():
            n += 1
        return (int(text[i:n]) if n > i else 0), n

    def scan_hunk_header(raw: str) -> tuple[int, int]:
        before, n = parse_hunk_count(raw, 4)
        if n < len(raw) and raw[n] == ",":
            before, n = parse_hunk_count(raw, n + 1)
        if n == 4 or n + 1 >= len(raw) or raw[n] != " " or raw[n + 1] != "+":
            return 0, 0
        after, m = parse_hunk_count(raw, n + 2)
        if m < len(raw) and raw[m] == ",":
            after, m = parse_hunk_count(raw, m + 1)
        if m == n + 2:
            return 0, 0
        return before, after

    ctx = hashlib.sha1()
    result = bytearray(20)
    patchlen = 0
    before = after = -1
    diff_is_binary = False
    pre_oid = post_oid = ""

    for raw in diff_text.splitlines(keepends=True):
        if (
            not raw.startswith(("commit ", "From "))
            and raw.startswith("\\ ")
            and len(raw) > 12
        ):
            continue
        if patchlen == 0 and not raw.startswith("diff "):
            continue
        if before == -1:
            if raw.startswith(("GIT binary patch", "Binary files")):
                diff_is_binary = True
                before = 0
                ctx.update(pre_oid.encode())
                ctx.update(post_oid.encode())
                result = accumulate(result, ctx.digest())
                ctx = hashlib.sha1()
                continue
            if raw.startswith("index "):
                oid1_end = raw.find("..")
                oid2_end = raw.find(" ", oid1_end + 2) if oid1_end != -1 else -1
                if oid2_end == -1:
                    oid2_end = len(raw) - 1
                if oid1_end != -1 and oid2_end != -1:
                    pre_oid = raw[6:oid1_end]
                    post_oid = raw[oid1_end + 2 : oid2_end]
                continue
            if raw.startswith("--- "):
                before = after = 1
            elif not (raw[:1] and raw[0].isalpha()):
                break
        if diff_is_binary:
            if raw.startswith("diff "):
                diff_is_binary = False
                before = -1
            continue
        if before == 0 and after == 0:
            if raw.startswith("@@ -"):
                before, after = scan_hunk_header(raw)
                continue
            if not raw.startswith("diff "):
                break
            result = accumulate(result, ctx.digest())
            ctx = hashlib.sha1()
            before = after = -1
        if raw[:1] in ("-", " "):
            before -= 1
        if raw[:1] in ("+", " "):
            after -= 1
        cleaned = "".join(c for c in raw if not c.isspace())
        ctx.update(cleaned.encode())
        patchlen += len(cleaned)

    result = accumulate(result, ctx.digest())
    return result.hex()
