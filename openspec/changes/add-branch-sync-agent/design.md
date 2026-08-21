# Design: add-branch-sync-agent

## 概述

Branch Sync Agent：无人值守服务器 cron 任务，定期检测 RCIOS 代码库各分支合入的 bug-fix commit，判定是否需要同步到其他目标分支；对需要同步的 commit 在独立 Git worktree 中自动执行 cherry-pick、冲突解决、多型号编译验证、diff-patch 生成，输出 HTML 报告 + 邮件。安全采用五道闸门模型，所有代码修改事后审核、推送永远人工。

## 总体架构

LangGraph 主图（单周期 = 单 thread_id = 单 checkpoint）控制流程与状态；确定性执行层（GitService / BuildRunner / MailService）执行操作；3 个 Agent 子图（Sync Decision / Conflict / Build）只在复杂判断点介入；规则层（classify + conclude 四态）先于 LLM。

```
Scheduler (cron 每日) → 主图 cycle-<YYYY-MM-DD>
  detect_commits → sync_decision → (无 NeedSync → 报告空检)
    → 逐分支批次: prepare_worktree → cherry_pick → [conflict → Conflict Agent]
    → build 多型号 → [fail → Build Agent] → generate_patch
  → 汇总 HTML 报告 + 邮件
```

### 模块依赖（单向）

```
domain ← executor ← git/build/mail ← agents ← graph
      config 被所有模块只读使用
```

### 技术栈

- Python 3.13 + uv；ruff + pytest；pydantic v2
- langgraph + langchain-openai（ChatOpenAI，DeepSeek 走 base_url，锁主版本）
- pydantic-settings + PyYAML + python-dotenv
- langgraph-checkpoint-sqlite（SqliteSaver，每 cycle 一个 thread_id）

## 核心设计决策

### 1. 安全边际模型（五道闸门，无人值守）

1. **能力闸门**：WhitelistExecutor 强制 git 命令白名单；文件编辑工具只接受"允许文件集合"；编译命令参数枚举白名单。
2. **正确性闸门**：编译验证 + 修改合理性检查（规模/删除量/未定义符号）+ 修改 diff 独立留存。
3. **禁止规则**：safety_rules.yaml（forbidden_paths / required_models / forbidden_branches / max_single_edit_lines），LLM 不可覆盖，SafetyEnforcer 强制。
4. **审批闸门（全事后）**：推送硬约束人工执行；所有 Agent 代码修改事后邮件审核；无交互式审批。
5. **审计闸门**：Agent 结构化输出 + 修改 diff 全落盘可追溯。

### 2. 规则集（拆两文件）

- `rules/safety_rules.yaml`：安全红线（数据驱动，SafetyEnforcer 通用校验器，LLM 不可覆盖）。
- `rules/decision_rules.yaml`：classify（机器 bugfix/notbugfix 标记、issue-id、路径规则）+ conclude（相似度阈值、目标类型、生命周期）+ branch_mapping。
- classify 规则完整移植参考实现（同 RCIOS 代码库），带 golden-set 测试。

### 3. 检测与决策

- **固定时间窗 22:00~22:00（+08:00）**，无增量基线，幂等；重复检测靠 git 真实状态去重。
- 同源矩阵：branch.md 人工维护，每次周期重新解析；feature/personal 分支矩阵排除。
- 四态结论（NeedSync / AlreadyIncluded / ManualReview / OutOfScope）**全部由 conclude 确定性规则输出**；LLM 只判 is_bug_fix（按 SHA 缓存 + 人工可覆盖）。
- 性能保护照搬参考（大 commit 截断、target cache、patch-id/issue-id 等价识别）。

### 4. 同步执行

- worktree 从目标分支远程 tip 检出，按合入顺序逐 commit cherry-pick；空提交成功跳过；冲突走 Conflict Agent。
- Conflict Agent：仅冲突文件 + 快照回滚 + 语法层校验（冲突标记消失 + `git diff --check`）+ 质量底线（理解功能与合入目的，禁止机械删标记）；max_attempts=3。
- 编译：批次开头 clean，commit 间增量；公共文件改动降级全量编译；型号矩阵按 required_models 顺序，失败即停该 commit。
- Build Agent：错误行 + 20 行上下文（≤3000 行）；`fatal:` 未必致命（须结合 error 行与退出码）；成功判定三查（产物/日志标志/容器状态）；仅 commit 文件 + 报错文件；max_attempts=3。
- fail-fast 三层关联判断（文件级/区域级确定性 + LLM 兜底；跨分支隔离 + 分支内按 commit 逐个判断关联性，只停当前失败 commit 及相关后续，无关继续；LLM 失败保守停批）。
- patch = `git format-patch <原tip>..<HEAD>`，含冲突解决修改，每分支独立。

### 5. 报告与邮件

- 周期末统一发一封；三区块（Action Required / 已成功同步 / 检测信息）。
- 邮件正文摘要 + HTML 附件；空检也发（扫描摘要式）。
- HTML 以参考模板为基础，扩展执行阶段区块。

## State 数据契约

外层 TypedDict（LangGraph state）+ 内嵌 pydantic（domain 校验）。

```
TaskState:
  cycle_id, scan_window, branch_md_version
  detected_commits: list[CommitInfo]
  classifications: dict[sha, SyncDecision]     # is_bug_fix + source
  decisions: dict[sha, dict[target, Conclusion4]]
  batches: dict[target_branch, list[sha]]      # 按合入顺序，决策先行固化
  current_target, current_commit
  branch_results: dict[target_branch, BranchResult]
  status, errors: dict[node, ErrorRecord], report
```

子结构：CommitInfo / SyncDecision / Conclusion4 / BranchResult / CommitResult / BuildOutcome(OK|FAILED|SKIPPED)。

## 模块接口契约（subagent 派发契约）

依赖批次（并行派发分组）：
```
批次 0（并行）：domain + config + executor
批次 1（并行）：rules + git + build + mail
批次 2（并行）：sync_decision + conflict + build 三 Agent
批次 3（串行）：graph 主图 + cli + scheduler
```
跨模块接口（类名/方法签名/参数/返回类型）如下；模块内实现自由，subagent 遵守本契约即可。

### 批次 0：domain

```python
# domain/models.py（纯 pydantic，零外部依赖）
class CommitInfo(BaseModel):
    sha: str
    message: str
    author: str
    committed_at: str            # ISO8601
    changed_files: list[str]
    patch_text: str
    symbols: list[str]
    patch_id: str | None
    issue_ids: list[str]
    source_branch: str
    homologous_section: str

class SyncDecision(BaseModel):
    sha: str
    is_bug_fix: bool
    reason: str | None
    recognition_source: str      # 枚举统一（参考实现）：machine:[BUG] / machine:fix: / machine:issue / machine:cherry-pick / machine:version-bump / machine:chore-docs / machine:docs-only / agent:bug-fix / agent:not-bug-fix / pending:claude-agent / not-included
    needs_agent: bool

class Conclusion4(BaseModel):
    kind: Literal["NeedSync","AlreadyIncluded","ManualReview","OutOfScope"]
    evidence: list[str]
    confidence: Literal["high","medium","low"]

class ConflictResolution(BaseModel):
    files: list[str]
    diff: str                    # 解决冲突产生的修改 diff
    agent_reason: str            # 冲突为何如此解决（质量底线，决策 4）

class CherryPickResult(BaseModel):
    status: Literal["OK","CONFLICT","EMPTY","FAILED"]
    # EMPTY = 空提交视为成功跳过（决策 24）；CONFLICT 时 conflict_files 非空
    conflict_files: list[str] = []

class BuildOutcome(BaseModel):
    model: str
    status: Literal["OK","FAILED","SKIPPED"]
    # SKIPPED = 因前面型号失败（决策 2 失败即停）而未执行该型号编译，非正常通过
    log_path: str | None
    errors: list[str]            # 提取的错误行（error: 行，不含 warning）
    agent_attempts: int
    fix_diff: str | None

class CommitResult(BaseModel):
    sha: str
    cherry_pick: Literal["OK","CONFLICT","FAILED","EMPTY"]
    # EMPTY = cherry-pick 变空提交，视为成功跳过（决策 24，spec 的 already-applied 同一概念）
    conflict_resolution: ConflictResolution | None
    build: dict[str, BuildOutcome]   # key = model

class BranchResult(BaseModel):
    target_branch: str
    worktree_path: str
    status: Literal["SUCCESS","PARTIAL","FAILED","MANUAL"]
    # SUCCESS = 批次内全部 commit 成功；PARTIAL = 部分成功部分失败；FAILED = 全部失败；MANUAL = forbidden_paths 转人工
    commits: list[CommitResult]
    patch_path: str | None
    stop_reason: str | None

class ErrorRecord(BaseModel):
    node: str
    error: str
    ts: str

class Report(BaseModel):
    cycle_id: str
    html_path: Path
    summary: dict[str, Any]          # 邮件正文摘要：窗口/分支数/commit 数/结论计数/警告
    action_required: list[dict[str, Any]]   # Action Required 区块数据（决策 13）
    decisions_json_path: Path
```

### 批次 0：config

```python
# config/settings.py（pydantic-settings，环境变量注入）
class Settings(BaseSettings):
    repo_path: str               # 宿主机仓库路径（REPO_PATH）
    branch_file: str             # branch.md 路径
    worktree_root: str           # worktree 父目录
    llm_backend: Literal["api","claude_cli"] = "api"  # 决策 39：可选后端，二选一（api=langchain-openai 默认 / claude_cli=claude -p），同一时间只启用一个
    llm_model: str               # LLM_MODEL（api 后端用；claude_cli 后端忽略，用宿主配置）
    llm_api_key: str             # LLM_API_KEY（api 后端用）
    llm_base_url: str            # LLM_BASE_URL（api 后端用，DeepSeek 走此）
    claude_cli_path: str = "claude"  # claude CLI 路径（claude_cli 后端用）
    llm_timeout_sec: int = 60
    llm_max_retries: int = 3
    llm_degrade_to_manual: bool = True
    docker_prefix: str = ""      # "sudo " / ""（DOCKER_PREFIX）
    docker_image: str
    docker_container_prefix: str = "rcios-sync"   # 容器名 = {prefix}-{cycle_id}（决策 1）
    docker_mount_workspace: str  # 容器内挂载点 /workspace/rcios
    build_script_dir: str        # build/platform/RTL9617C/
    max_conflict_attempts: int = 3
    max_build_attempts: int = 3
    mail_dry_run: bool = True
    mail_sender: str
    mail_recipients: list[str]
    log_dir: str                 # logs/<cycle_id>/ 根
    scan_since: str | None = None    # 覆盖默认窗口（manual-scan 用）；默认 None = 由 detect_commits node 计算上一完整日 22:00~22:00（决策 38）
    scan_until: str | None = None
    fetch_retry_count: int = 3       # 决策 17 指数退避
    fetch_retry_base_sec: int = 30
    failfast_region_gap: int = 200   # 决策 3 区域级判定：行区间间隔 > 此值视为无关
    # 注意：型号（required_models）不在 Settings，单一权威源 = safety_rules.yaml（决策 19）

def load_settings() -> Settings  # 读环境变量 + 校验必填
```

### 批次 0：executor

```python
# executor/exceptions.py（公共异常基类，Ruling：所有异常在此定义，供 git/build/agents 引用）
class BsaError(Exception): ...
class DomainError(BsaError): ...           # 业务状态走状态转移，不抛（用于显式标记）
class InfrastructureError(BsaError): ...   # 网络/IO/超时 → 节点边界捕获写 state.errors
class SafetyViolation(BsaError): ...       # 命中安全红线 → 强制拒绝转人工

# executor/base.py
class CompletedProcess(BaseModel):
    returncode: int
    stdout: str
    stderr: str

class CommandExecutor(Protocol):
    def run(self, args: list[str], *, cwd: Path | None = None,
            timeout_sec: int = 300, env: dict[str,str] | None = None) -> CompletedProcess: ...

# executor/subprocess.py
class SubprocessExecutor:  # 生产：subprocess + 超时 + 输出捕获

# executor/whitelist.py
class WhitelistExecutor:   # 安全装饰：命令白名单强制，非白名单 raise SafetyViolation
    ALLOWED_GIT: frozenset[str] = {fetch, checkout, cherry-pick, log, diff,
                                   show, format-patch, worktree, merge-base, status, add, rev-parse, diff-tree}
    # 调用约定（controller ruling）：调用方传 git 子命令 args（如 ["fetch","--all"]），
    # WhitelistExecutor 内部前置 "git" 后转发 inner executor。Docker 命令不经此白名单
    # （BuildRunner 单独处理）。Task 1.2 GitService 必须遵守此约定。

# executor/fake.py（测试用）
class FakeExecutor:        # 预置返回 + 记录调用
```

### 批次 1：rules

```python
# rules/classify.py（移植参考实现逻辑，纯函数）
class Classification(BaseModel):
    is_bug_fix: bool
    recognition_source: str      # 枚举与 SyncDecision 一致（见 agents 模型关系）
    issue_ids: list[str]
    cherry_pick_from: str | None
    reason: str | None
    needs_agent: bool

def classify_commit(message: str, changed_files: list[str], symbols: list[str],
                    patch_text: str, *, sha: str | None = None,
                    agent_judgments: dict[str, Any] | None = None) -> Classification

# rules/conclude.py（移植参考实现，纯函数）
def conclude_pair(source: CommitAnalysis, target: TargetSnapshot, *,
                  similarity_high: float, similarity_low: float) -> Conclusion4
    # 返回 domain 的 Conclusion4（唯一类型，问题 21 定案）
    # 决策 36：develop_backfill 不启用，无此参数；决策 37：不做 lifecycle，TargetSnapshot 无 lifecycle 字段
    # similarity_high/lower 来源：decision_rules.yaml conclude.similarity_high/low（默认 0.90/0.50，参考实现默认，问题 27 定案）

# rules/decision_rules.py（决策规则加载，数据驱动 decision_rules.yaml）

# rules/decision_rules.py（决策规则加载，数据驱动 decision_rules.yaml）
class DecisionRules(BaseModel):
    classify: dict[str, Any]           # 机器 bugfix/notbugfix 标记、issue-id 模式、路径规则（决策 21 完整移植）
    conclude: ConcludeThresholds       # similarity_high/low、目标类型
    branch_mapping: dict[str, str]     # 分支名 → 类型覆盖（决策 19，仅类型覆盖，无生命周期）

class ConcludeThresholds(BaseModel):
    similarity_high: float = 0.90
    similarity_low: float = 0.50
    need_sync_target_types: list[str] = ["release", "fix"]   # 决策 36：无 develop

def load_decision_rules(path: Path) -> DecisionRules

# rules/safety.py（安全红线校验器）
# SafetyRules 数据模型定义（从 safety_rules.yaml 加载，字段见决策 19）
class SafetyRules(BaseModel):
    forbidden_paths: list[str]
    required_models: list[str]           # 型号唯一权威源（问题 14 定案）
    forbidden_branches: list[str]
    max_single_edit_lines: int

def load_safety_rules(path: Path) -> SafetyRules   # 读 safety_rules.yaml

class SafetyEnforcer:
    def __init__(self, safety_rules: SafetyRules): ...
    def check_editable(self, paths: list[str]) -> None    # 命中 forbidden_paths → raise SafetyViolation
    def check_sync_branch(self, branch: str) -> bool      # forbidden_branches → False
    def required_models(self) -> list[str]                # 唯一权威源
    def max_single_edit_lines(self) -> int

# rules/branch_md.py（同源矩阵解析，决策 35 前缀血缘 + section 双源）
def parse_branch_md(text: str) -> BranchMdDocument
def branch_prefix(name: str) -> str                      # 去掉末尾 _YYYYMMDD 日期段（规范 3.2）
def build_matrix(doc) -> list[HomologousSet]
    # 双源判定：同产品线 = branch.md section（人工标注）；同步方向 = develop 父 → 前缀下子 release/fix
    # source = develop 分支；target = 同 section 内、分支前缀命中该 develop 前缀的 release/fix 分支
    # 决策 36：develop 只作源不作目标；决策 20：feature/personal 排除

# rules/paths.py（公共目录判定，数据驱动 decision_rules.yaml path_rules.public_dirs）
def is_public_file(path: str, public_dirs: list[str]) -> bool   # 决策 2：命中 → build 降级全量编译
```

### 批次 1：git

```python
# git/service.py（注入 CommandExecutor）
class GitService:
    def __init__(self, executor: CommandExecutor, repo_path: Path): ...
    def fetch_all(self) -> None                              # git fetch --all --prune，指数退避重试
    def branch_tip(self, branch: str) -> tuple[str, str]     # (resolved_ref, tip_sha)，优先 origin/
    def commits_in_window(self, since: str, until: str, ref: str) -> list[str]
    def commit_metadata(self, sha: str) -> tuple[str, str, str]   # author, committed_at, message
    def changed_files(self, sha: str) -> list[str]           # 大 commit 保护
    def commit_patch(self, sha: str, max_chars: int = 120_000) -> str
    def patch_id(self, sha: str) -> str | None
    def file_exists(self, ref: str, path: str) -> bool
    def show_file(self, ref: str, path: str) -> str | None
    def is_ancestor(self, sha: str, ref: str) -> bool        # merge-base --is-ancestor
    def add_worktree(self, branch: str, path: Path) -> None
    def remove_worktree(self, path: Path) -> None            # 周期清理
    def cherry_pick(self, sha: str) -> CherryPickResult      # OK / CONFLICT / EMPTY / FAILED + 冲突文件
    def unmerged_files(self) -> list[str]                    # git diff --name-only --diff-filter=U
    def diff_check(self) -> bool                             # git diff --check 干净
    def stage(self, paths: list[str]) -> None                # git add（冲突解决/修复后暂存）
    def format_patch(self, base: str, head: str, out_dir: Path, prefix: str) -> Path
    def snapshot(self, paths: list[str]) -> dict[str, str]   # 快照（回滚用）
    def restore(self, snapshots: dict[str, str]) -> None
    def status(self) -> str
```

### 批次 1：build

```python
# build/runner.py（注入 executor + docker 封装）
class BuildRunner:
    def __init__(self, executor: CommandExecutor, settings: Settings,
                 is_public_file: Callable[[str], bool] | None = None): ...
    # is_public_file 注入 rules/paths.is_public_file；命中 → 该 commit 降级全量编译（决策 2）
    def build_commit(self, worktree: Path, model: str, *, clean: bool,
                     module: str | None) -> BuildResult      # docker exec RTL9617C_build.sh
    def is_success(self, result: BuildResult) -> bool        # 三查：产物/日志标志/容器状态
    def parse_errors(self, log_text: str) -> list[str]       # error 行 + 20 行上下文 ≤3000 行

# build/log_parser.py（独立可测）
def extract_errors(log: str, *, max_chars: int = 3000, context_lines: int = 20) -> list[str]
def has_success_marker(log: str, model: str) -> bool         # Make rootfs success / success! / MSG<型号>_*_SYSTEM_*.bin
```

### 批次 1：mail

```python
# mail/service.py（sender 可注入，dry-run 默认）
class MailService:
    def __init__(self, settings: Settings, sender: Callable | None = None): ...
    def send_report(self, subject: str, body: str, html_path: Path,
                    attachments: list[Path]) -> MailResult     # dry-run 不真发
    # 失败仅告警，不影响已生成报告
```

### 批次 2：agents（3 个 LangGraph 子图）

**模型关系**（重要，避免混淆）：
- `Classification`（rules/classify.py 输出）：规则层机器判定结果，含 is_bug_fix / recognition_source / needs_agent。
- `SyncDecision`（domain 模型，Agent 判定输出）：LLM 判定结果，含 sha / is_bug_fix / reason / recognition_source / needs_agent。
- **映射**：规则层能判定的 commit 直接用 Classification 转 SyncDecision（reason 为规则理由）；规则层需 Agent 的（Classification.needs_agent=True）由 LLMClient.judge_bug_fix 产出 SyncDecision。SyncDecision 是 State 中 `classifications[sha]` 的唯一类型。

**支撑类型定义**（agents / rules / mail 契约引用，避免 subagent 各自发明）：

```python
# rules/conclude.py 支撑类型
class CommitAnalysis(BaseModel):      # 一次判定的源 commit 侧输入
    sha: str
    message: str
    changed_files: list[str]
    symbols: list[str]
    patch_text: str
    patch_id: str | None
    issue_ids: list[str]
    recognition_source: str
    source_branch: str
    source_branch_type: str
    homologous_section: str

class TargetSnapshot(BaseModel):      # 一次判定的目标分支侧快照
    branch_name: str
    branch_type: str
    has_source_sha: bool = False
    target_commit_messages: list[str]
    target_patch_ids: set[str]
    target_issue_ids: set[str]
    files_on_target: set[str]
    symbols_on_target: dict[str, bool]
    file_similarity: float | None
    fix_clearly_missing: bool
    function_renamed: bool
    in_same_homologous_set: bool
    # 决策 36：无 develop_backfill_allowed；决策 35：无 cross_product_linked（同源由 section+前缀判定）

# rules/branch_md.py 支撑类型
class BranchRef(BaseModel):
    name: str
    branch_type: str
    section: str

class BranchSection(BaseModel):
    title: str
    branches: list[BranchRef]

class BranchMdDocument(BaseModel):
    title: str | None
    sections: list[BranchSection]
    duplicates: list[dict]

class HomologousSet(BaseModel):
    section: str
    sources: list[BranchRef]
    need_sync_targets: list[BranchRef]

# agents 支撑类型
class ConflictContext(BaseModel):
    commit: CommitInfo
    conflict_files: list[str]
    conflict_markers: dict[str, str]   # 文件 → 冲突标记内容
    source_branch: str
    target_branch: str
    worktree: Path

class BuildErrorContext(BaseModel):
    commit: CommitInfo
    model: str
    errors: list[str]                  # 提取的错误行
    log_path: Path
    worktree: Path

class BuildAttribution(BaseModel):
    category: Literal["introduced_by_commit","pre_existing","environment","unresolvable"]
    reason: str
    files_to_fix: list[str]

class FailedCommit(BaseModel):
    sha: str
    failure_summary: str
    changed_files: list[str]

# mail 支撑类型
class MailResult(BaseModel):
    status: Literal["ok","skipped","failed"]
    error: str | None
    report_path: Path | None

# build 支撑类型
class BuildResult(BaseModel):
    model: str
    returncode: int
    log_path: Path
    succeeded: bool
    errors: list[str]
```

```python
# agents/base.py（统一 LLM 封装：timeout/重试/降级安全默认；决策 39 可选后端）
class LLMClient:
    """单一后端，按 settings.llm_backend 选择（二选一，互斥，同一时间只启用一个）：
    - api（默认）：langchain-openai ChatOpenAI（DeepSeek 走 base_url），schema 强制 + token 观测，
      per-agent 模型覆盖生效。
    - claude_cli：封装 `claude -p "<强约束 prompt>"` 子进程（用宿主配置的 DeepSeek 模型），
      结构化输出靠 prompt 强约束 + 健壮解析器（json.loads → 提取重试 → 降级安全默认），
      记录调用耗时 + 输入/输出摘要补偿观测；忽略 per-agent 模型覆盖。
    实现要点：构造时按 llm_backend 只实例化所选后端，绝不两者同时执行。
    接口 4 方法不变，后端实现隔离在内部；未来加新后端不破坏接口。
    """
    def __init__(self, settings: Settings): ...
    def judge_bug_fix(self, commit: CommitInfo) -> SyncDecision        # 失败 → degrade ManualReview
    def solve_conflict(self, ctx: ConflictContext) -> ConflictResolution  # 失败 → 转人工
    def classify_build_error(self, ctx: BuildErrorContext) -> BuildAttribution
    def judge_failfast_related(self, failed: FailedCommit, subsequent: list[CommitInfo]) -> bool

# agents/sync_decision.py（子图，只判 is_bug_fix）
class SyncDecisionAgent:
    def __init__(self, llm: LLMClient, judgments_path: Path): ...
    def run(self, pending: list[CommitInfo]) -> dict[str, SyncDecision]  # 按 SHA 缓存 + 人工覆盖优先

# agents/conflict.py（子图）
class ConflictAgent:
    def __init__(self, llm: LLMClient, git: GitService, safety: SafetyEnforcer,
                 max_attempts: int = 3): ...
    def resolve(self, commit: CommitInfo, conflict_files: list[str]) -> ConflictResolution | None
    # None = 3 轮未解决，触发 fail-fast；循环内快照回滚 + SafetyEnforcer 校验

# agents/build_agent.py（子图）
class BuildAgent:
    def __init__(self, llm: LLMClient, git: GitService, runner: BuildRunner,
                 safety: SafetyEnforcer, max_attempts: int = 3): ...
    def fix(self, commit: CommitInfo, errors: list[str], model: str) -> BuildAttribution
    # 归因四类；仅"本次引入"修复；疑似原分支已有 → 切原 tip 编译对比
```

### 批次 3：graph

```python
# graph/state.py（外层 TypedDict，内嵌 pydantic）
class TaskState(TypedDict):
    cycle_id: str
    scan_window: tuple[str, str]
    branch_md_version: str
    detected_commits: list[CommitInfo]
    classifications: dict[str, SyncDecision]
    decisions: dict[str, dict[str, Conclusion4]]
    batches: dict[str, list[str]]
    current_target: str | None
    current_commit: str | None
    branch_results: dict[str, BranchResult]
    status: str
    errors: dict[str, ErrorRecord]
    report: Report | None

# graph/nodes.py（每个 node 由 node_wrapper 包裹，异常写 state.errors）
def node_wrapper(node: Callable) -> Callable   # try/except → state.errors，不冒泡
def detect_commits(state) -> dict
def sync_decision(state) -> dict
def prepare_worktree(state) -> dict
def cherry_pick(state) -> dict
def resolve_conflict(state) -> dict
def build(state) -> dict
def fix_build(state) -> dict
def generate_patch(state) -> dict
def report(state) -> dict

# graph/workflow.py（主图构建，含 conditional edges）
def build_workflow(state: TaskState) -> CompiledGraph   # thread_id = cycle_id，SqliteSaver checkpoint
```

### 批次 3：cli + scheduler

```python
# cli.py
def main(argv) -> int   # run-cycle / status / manual-scan(--since/--until，传入覆盖默认 22:00~22:00 窗口，A 点) / validate-config

# scheduler/cycle.py
def run_cycle(date: str | None = None) -> int   # 构建 graph、invoke、更新周期记录
```

## 容错落地（三关卡）

1. **节点边界**：node_wrapper 包每个 node，try/except → state.errors，不崩溃。
2. **LLM**：统一 LLMClient（timeout / max_retries / degrade_to_manual 安全默认）。
3. **外部命令**：executor 层（超时 / 重试 / WhitelistExecutor 白名单 / fetch 指数退避）。

异常分层：BsaError（DomainError 状态转移不抛 / InfrastructureError 节点边界捕获 / SafetyViolation 强制拒绝转人工）。

## 关键待确认环境项（运维侧）

- 运行用户需加入 docker 组或配置 sudo 免密，否则 cron 后台 docker 命令卡死。
- 真实 SMTP 配置（P1 先 dry-run）。
- 真实仓库路径/SSH 地址注入（环境变量，不硬编码）。

## 扩展性预留

- 多 repo：config 结构 repo 列表化（v1 单 repo）。
- v2 web 平台：同一 SQLite 历史 + worktree 周期内保留。
- 分支依赖配置：v1 不做（YAGNI）。
