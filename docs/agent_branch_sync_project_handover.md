# Agent 学习与 Branch Sync Agent 项目交接文档

## 一、当前学习进度

目标不是单纯学习 LangGraph API，而是建立完整的 Agent 工程思维。

目前已经完成 Agent 基础知识和主要工程机制，约 90%。

### 已掌握

1. LLM / ChatModel
   - ChatOpenAI
   - DeepSeek API
   - Model invoke
   - token usage / reasoning token / cache

2. LangGraph 基础
   - State
   - Node
   - Edge
   - Conditional Edge
   - Router
   - Graph invoke
   - State Update

3. State 理解
   - Graph 是整体执行框架
   - Node 是执行具体逻辑的函数
   - Node 读取 State，并返回 State Update
   - Graph 根据 State Update 更新 State
   - Router 本质是根据 State 决定下一条路径

4. LLM Router
   - LLM 负责分类/判断
   - Router 决定 Workflow 路径
   - 理解 Workflow 与 Agent 的区别

5. Tool Calling
   - Tool 定义
   - Tool Calling
   - ToolNode
   - 多 Tool
   - ToolMessage / AIMessage
   - Tool Call ID 与消息链对应关系

6. 一个重要工程认识

   LLM 即使“有能力”完成某件事情，也不代表工程上一定应该让它自己完成。

   应该区分：

   LLM 决策能力
        ↓
   Workflow 控制
        ↓
   Policy 限制
        ↓
   Tool 执行

7. Workflow vs Agent

   Workflow：
   工程师决定流程

   Agent：
   LLM 在允许的范围内决定下一步

8. Policy
   - Tool 权限
   - 高风险操作
   - Human Approval
   - 明确区分：
     “这个 Tool 是否允许使用”
     和
     “这一次具体 Tool Call 是否允许执行”

9. Memory / Checkpoint
   - LangGraph Checkpoint
   - SQLite
   - 中断后恢复
   - 理解 Checkpoint 保存的是执行状态，而不是简单聊天记录
   - 实际遇到过 checkpoint 导致旧 Tool Message / Tool Call 状态干扰后续运行的问题

10. Human-in-the-loop
    - Interrupt
    - Command resume
    - Human Approval
    - Tool 执行前暂停

11. Agent 工程可靠性
    - Retry
    - Timeout
    - Error Handling
    - 参数验证
    - 幂等性
    - UNKNOWN 状态
    - 不要把所有异常交给 LLM
    - 确定性问题尽量由代码处理

12. Observability
    - State
    - Node
    - Router
    - AIMessage
    - ToolMessage
    - Tool Call
    - Tool Result
    - Human Approval
    - 完整执行轨迹
    - Agent Debug 思路

## 二、最终形成的 Agent 工程模型

LLM 负责“想”，Workflow 负责“管”，Policy 负责“限制”，Tool 负责“做”，State 负责“记”，Checkpoint 负责“续”，Observability 负责“看”，Reliability 负责“扛”。

整体架构：

    USER
      │
      ▼
    LLM
    理解/推理/决策
      │
      ▼
    Agent
    决定下一步做什么
      │
      ▼
    Workflow
    流程/状态/路由
      │
      ├──────────────┐
      ▼              ▼
    Policy         Human
                   Approval
      │              │
      └──────┬───────┘
             ▼
           Tool
           真实世界能力
             │
             ▼
        External System

旁边贯穿：
State / Checkpoint / Retry / Timeout / Validation / Logging / Observability

## 三、原来的结课项目已经取消

原来的 SDWAN/CPE Agent 项目已经明确放弃。

原因：项目本身已经没有实际意义。

不再继续围绕它做综合项目。

## 四、新的综合项目：Branch Sync Agent

用户背景：用户从事项目管理，希望开发一个用于代码分支维护的 Agent。

### 项目目标

定期检测指定列表中的分支合入记录，判断每个 Commit 是否需要同步到其他分支。

如果需要同步，则自动执行：

1. 在指定 Workspace 拉取代码库
2. 切换目标分支
3. cherry-pick commit
3a. 如果冲突，解决冲突
4. 编译版本
4a. 如果编译报错，分析并解决
5. 生成 diff-patch
6. 发送检测/维护报告

## 五、建议的整体 Workflow

    Scheduler
        │
        ▼
    Commit Detector
        │
        ▼
    Sync Decision Agent
        │
        ├──────────────┐
        ▼              ▼
       NO             YES
        │              │
      Report           ▼
                Sync Workflow
                       │
                  Git Prepare
                       │
                  Cherry-pick
                       │
                ┌──────┴──────┐
                ▼             ▼
               OK          CONFLICT
                │             │
                │       Conflict Agent
                │             │
                └──────┬──────┘
                       │
                     Build
                       │
                ┌──────┴──────┐
                ▼             ▼
               OK           FAILED
                │             │
                │        Build Agent
                │             │
                └──────┬──────┘
                       │
                Generate Patch
                       │
                Maintenance Report

## 六、核心设计原则

这个项目不要设计成一个“大 Agent”。

错误方式：

    用户：帮我同步分支

    LLM：
    自己决定拉代码
    自己决定切分支
    自己 cherry-pick
    自己解决冲突
    自己编译
    自己改代码
    自己生成 patch

正确方式：

    Workflow
        ↓
    规定必须经过哪些阶段
        ↓
    Agent
        ↓
    只在复杂决策点发挥能力
        ↓
    Tool
        ↓
    执行确定性操作

即：

> Workflow 控制流程，Agent 处理复杂判断，Tool 执行具体动作。

## 七、真正需要 Agent 的地方

### 1. Sync Decision Agent

输入：

    source branch
    target branch
    commit message
    commit diff
    changed files
    历史同步记录
    分支同步策略

输出结构化结果：

    {
        "need_sync": True,
        "reason": "...",
        "target_branches": [
            "release/2.4"
        ],
        "risk": "medium"
    }

然后由 Workflow 决定是否进入同步流程。

不要让 LLM 直接控制后续 Git 操作。

### 2. Conflict Agent

当：

    git cherry-pick
        ↓
    CONFLICT

进入 Agent。

Agent 获取：

    conflicted files
    conflict markers
    original commit
    source branch
    target branch
    相关代码上下文

然后：

    分析冲突
      ↓
    提出解决方案
      ↓
    修改
      ↓
    检查
      ↓
    继续 cherry-pick

高风险情况下可以加入 Human Approval。

### 3. Build Agent

当：

    build
      ↓
    FAILED

进入 Build Agent。

判断：

    编译错误
      ↓
    ├── 本次 cherry-pick 引入
    ├── 原分支已有问题
    ├── 环境问题
    └── 无法自动修复

如果可以修：

    修改
      ↓
    重新编译

否则：

    STOP
      ↓
    报告
      ↓
    人工处理

## 八、Workspace 建议

强烈建议：不要直接让 Agent 修改主工作区。

采用独立 Git Worktree：

    Main Workspace
          │
          └── 不允许 Agent 随便修改

    Agent Worktree
          │
          ├── pull
          ├── checkout
          ├── cherry-pick
          ├── conflict resolution
          ├── build
          └── patch

这样即使 Agent 搞错，也不会直接破坏开发人员的主工作区。

## 九、核心 State 初步方向

大致包含：

    task_id
    repository
    workspace
    source_branch
    target_branch
    commit
    commit_message
    sync_decision
    target_branches
    current_target_branch
    git_status
    conflicts
    conflict_resolution
    build_status
    build_errors
    build_fix_attempts
    patch_path
    report
    status

任务生命周期：

    DETECTED
        ↓
    ANALYZING
        ↓
    NEED_SYNC
        ↓
    REPOSITORY_READY
        ↓
    BRANCH_READY
        ↓
    CHERRY_PICKING
        ↓
    CONFLICT
        ↓
    CONFLICT_RESOLVED
        ↓
    BUILDING
        ↓
    BUILD_FAILED
        ↓
    BUILD_FIXED
        ↓
    PATCH_GENERATED
        ↓
    COMPLETED

失败：

    FAILED

无法确认：

    UNKNOWN

尤其需要关注：

> Tool timeout ≠ 操作一定没有发生。

例如 rebase/cherry-pick/build 超时，不能简单认为失败后立即重试。

## 十、报告

最终报告应说明：

    检测时间
    Repository
    Source Branch
    Target Branch

    Commit:
        abc123

    Commit Message:
        xxx

    Sync Decision:
        YES

    Reason:
        xxx

    Cherry-pick:
        SUCCESS

    Conflict:
        YES
        resolved by Agent

    Build:
        SUCCESS

    Patch:
        xxx.patch

    Final Status:
        COMPLETED

失败时：

    Final Status:
        FAILED

    Failure Stage:
        BUILD

    Reason:
        xxx

    Agent Attempts:
        3

    Recommendation:
        Human intervention required

## 十一、新会话的开发路线

### Phase 1：项目定义

#### 30.1 项目边界

明确：

- Repository
- Branch List
- Source / Target Branch
- Commit 检测范围
- 同步规则
- Workspace
- Build Command
- Patch 输出
- Report 输出
- 通知方式

#### 30.2 明确哪些是 Workflow，哪些是 Agent

这是本项目最重要的架构设计。

### Phase 2：数据模型

#### 31. State 设计

设计：

    Task
    Commit
    Branch
    SyncDecision
    GitOperation
    Conflict
    Build
    Patch
    Report

### Phase 3：确定性 Git Workflow

#### 32.

先不加 LLM。

完成：

    检测 Commit
      ↓
    准备 Worktree
      ↓
    切换分支
      ↓
    cherry-pick
      ↓
    判断成功/冲突
      ↓
    build
      ↓
    patch

先把确定性流程跑通。

### Phase 4：加入 Sync Decision Agent

#### 33.

让 LLM 判断：

    这个 Commit 是否需要同步？
    应该同步到哪些分支？
    为什么？
    风险是什么？

使用 Structured Output。

### Phase 5：Conflict Agent

#### 34.

加入：

    Conflict Detection
      ↓
    Conflict Agent
      ↓
    Resolution
      ↓
    Validation

设计：

    最大尝试次数
    人工介入
    失败恢复

### Phase 6：Build Agent

#### 35.

加入：

    Build
      ↓
    Error Analysis
      ↓
    Fix
      ↓
    Rebuild

限制：

    max_attempts
    timeout
    allowed_paths

### Phase 7：Checkpoint / Resume

#### 36.

加入 SQLite Checkpoint：

    任务执行到一半
      ↓
    程序退出
      ↓
    重新启动
      ↓
    恢复
      ↓
    继续执行

### Phase 8：Human Approval / Policy

#### 37.

定义：

    哪些操作自动执行
    哪些操作必须人工批准
    哪些操作禁止

例如：

    读取 → 自动
    创建 Worktree → 自动
    cherry-pick → 自动
    解决简单冲突 → 可自动
    修改核心代码 → 人工批准
    生产分支 → 人工批准
    删除/强制操作 → 禁止

具体规则后面根据实际 Git 流程确定。

### Phase 9：Observability + Report

#### 38.

建立：

    Execution Log
    Task History
    Decision Log
    Tool Log
    Agent Reasoning Summary
    Build Log
    Patch
    Maintenance Report

不需要记录 LLM 私有 Chain-of-Thought；记录可审计的决策理由、输入摘要、输出结构和执行结果即可。

### Phase 10：Scheduler / Production 化

#### 39.

最后加入：

    定时检测
      ↓
    任务队列
      ↓
    并发控制
      ↓
    Checkpoint
      ↓
    报告
      ↓
    通知

最终形成真正可长期运行的：

> Branch Sync Agent

## 十二、新会话第一句话建议

> 我们继续 Branch Sync Agent 项目。前面的 Agent/LangGraph 基础已经学完，现在不要再重复教学，直接进入项目设计。目标是：定期检测指定分支的合入 Commit，判断是否需要同步到其他分支；需要时在独立 Workspace/Worktree 中完成 pull、checkout、cherry-pick、冲突处理、编译、编译错误处理、diff-patch 生成和维护报告。架构原则是 Workflow 控制确定性流程，Agent 只负责复杂判断，Tool 执行 Git/Build 等操作。请从 Phase 1：项目边界与总体架构开始，先设计，不要急着写代码。
