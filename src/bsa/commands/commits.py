from bsa.executor.exceptions import InfrastructureError
from bsa.git.service import GitService


def list_candidate_commits(git: GitService, src: str, limit: int = 50) -> list[dict]:
    """列出源分支最近的候选 commit（只读），每项含 sha/message/committed_at。

    只经 GitService 的 executor 走白名单 git 子命令（rev-parse/log），
    不写任何状态；src 或 log 失败时抛 InfrastructureError。
    """
    ref: str | None = None
    for candidate in (f"origin/{src}", src):
        check = git.executor.run(
            ["rev-parse", "--verify", "--quiet", candidate], cwd=git.repo_path
        )
        if check.returncode == 0:
            ref = candidate
            break
    if ref is None:
        raise InfrastructureError(f"branch not found: {src}")

    result = git.executor.run(
        ["log", "-n", str(limit), "--format=%H|%s|%aI", ref], cwd=git.repo_path
    )
    if result.returncode != 0:
        detail = result.stderr.strip()
        msg = f"cannot list commits for {ref}"
        if detail:
            msg = f"{msg}: {detail}"
        raise InfrastructureError(msg)

    commits: list[dict] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        commits.append({"sha": parts[0], "message": parts[1], "committed_at": parts[2]})
    return commits
