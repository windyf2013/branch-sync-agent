import shlex
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from bsa.build.log_parser import artifact_success_marker, extract_errors, has_success_marker
from bsa.config.settings import Settings
from bsa.executor.base import CommandExecutor, CompletedProcess

_BUILD_EXEC_TIMEOUT_SEC = 3600


def _resolve_build_script(model: str) -> tuple[str, str]:
    """Resolve a required-model id to (build script, product arg).

    Model format ``2600`` (no customer) or ``2600_CMCC`` (customer suffix).
    Customer selects the operator script (RTL9617C_build_cmcc.sh etc.) which
    exports CUSTOMER; without customer the generic script is used. 真机测试:
    config 文件是 2600_CMCC.config 等, 必须指定 customer 才能生成配置.
    """
    parts = model.split("_", 1)
    product = parts[0]
    if len(parts) > 1 and parts[1].strip():
        customer = parts[1].strip()
        script = f"RTL9617C_build_{customer.lower()}.sh"
    else:
        script = "RTL9617C_build.sh"
    return script, product


def _docker_user_args(settings: Settings) -> list[str]:
    """Compute docker --user args.

    Empty docker_user → host uid:gid (avoid container-root writing host files
    as root; 真机测试 77805 root files blocked cleanup). Explicit "root" or
    "uid:gid" overrides.
    """
    user = (settings.docker_user or "").strip()
    if not user:
        import os

        user = f"{os.getuid()}:{os.getgid()}"
    return ["--user", user]


def _docker_ssh_args(settings: Settings) -> list[str]:
    """Mount host ~/.ssh into the container so code_update.sh can git-clone
    components via SSH.

    真机测试: --user 后容器内用户无 .ssh, code_update 的 git clone 报
    'Host key verification failed' → 组件拉不到 → 编译失败. 挂载宿主
    SSH key + known_hosts (只读) 解决.
    """
    import os

    ssh = Path(os.path.expanduser("~/.ssh"))
    if not ssh.is_dir():
        return []
    user = (settings.docker_user or "").strip()
    if user == "root":
        container_home = "/root"
    else:
        container_home = "/home/ubuntu"
    return ["-v", f"{ssh}:{container_home}/.ssh:ro"]


def _docker_gitconfig_args(settings: Settings) -> list[str]:
    """Mount host ~/.gitconfig into the container so in-build git operations
    (e.g. strongswan Makefile 的 ``git init && git commit && git am``) have a
    committer identity.

    真机测试: --user 后容器内用户无 git 身份, ``git commit`` 报
    'Author identity unknown / unable to auto-detect email address' →
    make add_patch Error 128 → 基线编译失败. 挂载宿主 ~/.gitconfig (只读) 解决.
    """
    import os

    gitconfig = Path(os.path.expanduser("~/.gitconfig"))
    if not gitconfig.is_file():
        return []
    user = (settings.docker_user or "").strip()
    if user == "root":
        container_home = "/root"
    else:
        container_home = "/home/ubuntu"
    return ["-v", f"{gitconfig}:{container_home}/.gitconfig:ro"]


class BuildResult(BaseModel):
    model: str
    returncode: int
    log_path: Path
    succeeded: bool
    errors: list[str]
    # 模块编译标记：非 None 时本次为模块级编译（不产 rootfs / MSG 产物），
    # is_success 只认 returncode（见下）。
    module: str | None = None


class BuildRunner:
    """Runs RCIOS cross-compiles inside a docker container (decision 1).

    Docker commands go through the injected executor directly — never through
    the git WhitelistExecutor. ``is_public_file`` is injected from
    rules/paths.is_public_file so callers can downgrade to a full (clean)
    compile when a changed file is public (decision 2).
    """

    def __init__(
        self,
        executor: CommandExecutor,
        settings: Settings,
        is_public_file: Callable[[str], bool] | None = None,
        *,
        cycle_id: str | None = None,
        build_types: dict[str, object] | None = None,
    ) -> None:
        self.executor = executor
        self.settings = settings
        self.is_public_file = is_public_file
        self.cycle_id = cycle_id
        self.build_types = build_types or {}

    def _resolve_script(self, model: str) -> tuple[str, str]:
        """型号 → (build 脚本, 产品参数)：显式 build_types 优先，旧格式推断兜底。"""
        bt = self.build_types.get(model)
        if bt is not None:
            return bt.script, bt.product
        return _resolve_build_script(model)

    def _container_name(self) -> str:
        if self.cycle_id:
            return f"{self.settings.docker_container_prefix}-{self.cycle_id}"
        return self.settings.docker_container_prefix

    def _docker_prefix(self) -> list[str]:
        return self.settings.docker_prefix.strip().split()

    def build_commit(
        self,
        worktree: Path,
        model: str,
        *,
        clean: bool,
        module: str | None,
        log_path: Path | None = None,
    ) -> BuildResult:
        """Build one model in docker: run -d (kept alive), exec build script, rm -f.

        ``log_path`` when provided is the per-commit log destination; the graph
        layer must pass ``{log_dir}/build/<branch>/<commit>/build.log`` to
        preserve the decision-26 audit trail. Otherwise a flat
        ``{log_dir}/build_{model}.log`` is used.
        """
        container = self._container_name()
        prefix = self._docker_prefix()

        self.executor.run(
            [
                *prefix,
                "docker",
                "run",
                "-d",
                "--name",
                container,
                *_docker_user_args(self.settings),
                *_docker_ssh_args(self.settings),
                *_docker_gitconfig_args(self.settings),
                "-v",
                f"{worktree}:{self.settings.docker_mount_workspace}",
                "-v",
                "/usr/local:/usr/local",
                self.settings.docker_image,
                "sleep",
                "infinity",
            ]
        )

        script_dir = self.settings.build_script_dir.rstrip("/")
        # docker exec 默认工作目录是容器启动目录(/)；必须显式 -w 到挂载点，
        # 否则 cd build/platform/RTL9617C 在 / 下失败 (真机测试: No such file or directory)
        mount = self.settings.docker_mount_workspace.rstrip("/")
        # code_update.sh 在 build/ 目录（不在 platform/RTL9617C 里）：
        # 先 cd build 拉插件，再 cd script_dir 编译（真机测试:
        # cd RTL9617C 后 code_update.sh: command not found）。
        build_root = f"{mount}/build"
        # code_update.sh -d（删掉并重新 clone voip/xpon/wlan/ac/ponolt 独立子仓库）
        # 是全量编译的前置准备：模块编译只编主 rcios 仓库里已随 worktree 存在的
        # 目录，不需要重拉子仓库，故仅在 full 编译（clean 或未解析到模块）时执行。
        is_full = clean or not module
        steps = []
        if is_full:
            steps.append(f"cd {shlex.quote(build_root)}")
            steps.append("./code_update.sh -d")
        steps.append(f"cd {shlex.quote(f'{mount}/{script_dir}')}")
        build_script, product = self._resolve_script(model)
        if clean:
            steps.append(f"./{build_script} {shlex.quote(product)} clean")
        build_cmd = f"./{build_script} {shlex.quote(product)}"
        if module:
            # module 来自 build_rules.yaml 的数据驱动配置（非用户输入），可含多 token
            # （如 "component wlan"），逐段 quote 成独立 argv 再拼回 shell 字符串。
            for tok in shlex.split(module):
                build_cmd = f"{build_cmd} {shlex.quote(tok)}"
        steps.append(build_cmd)

        streaming = log_path is not None
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc: CompletedProcess = self.executor.run(
                [
                    *prefix,
                    "docker",
                    "exec",
                    "-w",
                    mount,
                    container,
                    "bash",
                    "-c",
                    " && ".join(steps),
                ],
                timeout_sec=_BUILD_EXEC_TIMEOUT_SEC,
                stream_to=str(log_path) if streaming else None,
            )
        finally:
            self.executor.run([*prefix, "docker", "rm", "-f", container])

        # stream_to 时输出已实时落盘 build.log；未传 log_path（或注入 executor
        # 返回了捕获输出）时按 stdout/stderr 组装。
        log = proc.stdout + ("\n" if proc.stderr else "") + proc.stderr
        log_path = log_path or Path(self.settings.log_dir) / f"build_{model}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log or not streaming:
            log_path.write_text(log or "", encoding="utf-8", errors="replace")
        # streaming 时 SubprocessExecutor 返回空 stdout/stderr（输出直接落盘），
        # 必须从落盘的日志文件解析错误，否则 errors 恒为空 → 平台侧看不到失败原因。
        errors_text = log
        if streaming and not log:
            try:
                errors_text = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                errors_text = ""
        return BuildResult(
            model=model,
            returncode=proc.returncode,
            log_path=log_path,
            succeeded=proc.returncode == 0,
            errors=self.parse_errors(errors_text),
            module=module,
        )

    def parse_errors(self, log_text: str) -> list[str]:
        return extract_errors(log_text)

    def is_success(self, result: BuildResult) -> bool:
        """Three-check success: artifact produced + log marker + container state.

        模块级编译（``result.module`` 非 None）只编单个 component，不产 rootfs /
        ``MSG<model>_*_SYSTEM_*.bin`` 产物，故不适用 marker/artifact 三重校验；
        唯一可信的成功信号是 ``make`` 退出码为 0（编译错误必然非零）。否则模块
        编译永远被判失败，进而误触发 fix_build → LLM 归因（每 commit 模块编译引入）。
        """
        if result.module:
            return result.returncode == 0
        try:
            log = result.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        container_ok = result.returncode == 0
        marker_ok = has_success_marker(log, result.model)
        artifact_ok = artifact_success_marker(log, result.model)
        return container_ok and marker_ok and artifact_ok
