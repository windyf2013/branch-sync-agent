# Proposal: add-branch-sync-agent

## 做什么

构建 Branch Sync Agent：定期检测 RCIOS 代码库指定分支的合入 commit，判断是否需要同步到其他目标分支；对需要同步的 commit 在独立 Git worktree 中自动执行 cherry-pick、冲突解决、多型号编译验证、diff-patch 生成，最终输出 HTML 维护报告并通过邮件发送。

## 为什么

- 手动将 bug-fix 类 commit 从开发分支同步到各产品线分支，工作重复且易漏
- 冲突处理、编译失败修复高度依赖对代码的理解，适合 LLM 介入
- 需要可审计的执行记录（检测/决策/执行各阶段），供维护人员审核

## 成功标准

1. 每天定时触发，一个完整周期内完成：检测 → 决策 → 逐分支同步 → 编译验证 → 生成 patch → 邮件报告
2. 检测阶段能识别 bug-fix 类 commit 并输出四态结论：`NeedSync / AlreadyIncluded / ManualReview / OutOfScope`
3. 规则层（classify + conclude）先于 LLM 判定，规则能确定的场景不调用 LLM
4. `NeedSync` 的 commit 自动执行完整同步流程；`ManualReview` 只进报告不执行
5. 每 commit 编译一次（按受影响模块 × 全型号），所有型号必须通过
6. 单个分支或 commit 失败不拖垮整个周期，失败必出报告 + 邮件
7. Agent 不推送任何分支：权限上限为同步+解决冲突+解决编译报错+生成 patch，推送必须由人工执行（能力边际，硬约束）
8. 只生成 patch，不推送远程；不在主工作区操作，只用独立 worktree
9. 空检（无 commit 或全部无需同步）也发邮件

## 边界（不在范围内）

- **Agent 不推送分支**：推送是能力边际，永远由人工执行；v2 web 平台也只提供人工操作入口
- 不修改主工作区，全部操作在独立 worktree 内
- 不处理 SDWAN/CPE 旧项目
- v2 的分支维护平台（web）本 change 不做，仅预留存储/路径演进空间
- 多 repo 管理本 change 不做，配置结构预留扩展

## 现有约束

- 技术栈：Python 3.13 + LangGraph + SQLite checkpoint + OpenAI 兼容 LLM（可配置）
- 目标仓库：嵌入式 C（RCIOS），GitLab SSH 认证，branch.md 同源矩阵清单在业务仓内
- branch.md 同源矩阵（5 个产品线 section）：组网产品 / FTTR-B / 政企网关 / 4.34 产品主线 / 路由器类，各 section 内分支为同源集合，源分支 → 目标分支在该集合内判定
- 编译环境：Docker 容器 `rcios-build-env:ubuntu24.04`，挂载 worktree 到 `/workspace/rcios`
- 产品架构：RTL9617C（默认，当前唯一支持）/ RTL9607F / X86 / EN7529
- 型号：5200（默认）/ 2600；运营商脚本 _ct/_cmcc/_cu/_gj
- 构建命令：`RTL9617C_build.sh [product-type] [function-name]`，编译前 `code_update.sh -d` + clean
- 容错四层：节点级隔离 / LLM 级（timeout+重试+降级）/ Tool 级（幂等+git 真实状态）/ 周期级
- 容错硬要求：workflow 不能因部分 node 或 LLM 出错而崩溃
- 邮件先支持 dry-run，真实 SMTP 后续补
- 工具链：pytest + ruff，允许新增依赖
- 真实仓库路径/密钥不硬编码，用占位符 + 环境变量/独立配置注入
- **运行模式：无人值守服务器 cron 任务，不存在交互式人工审批；所有安全控制靠代码层强制 + 规则文件红线 + 事后审核**

## 安全边际模型（五道闸门，无人值守版）

Agent 具备真实世界的写权限（git worktree、代码修改、docker 编译），安全必须靠代码层强制而非人工监督。五道闸门：

1. **能力闸门（前置锁死）**：Git 命令白名单（禁止 push / reset --hard / rebase / merge / clean -fdx / gc / submodule 更新）；文件编辑工具只允许编辑"当前任务允许的文件集合"；编译命令参数白名单（型号/模块枚举限定）。
2. **正确性闸门**：编译验证（已有）；修改合理性检查（修改规模、删除量、未定义符号检测）；每次 Agent 修改生成独立 diff 留存供审核。
3. **禁止规则（规则文件红线，LLM 不可覆盖）**：规则层定义不可修改路径（核心目录 / vendor / 第三方）、禁同步分支、必验证型号清单。
4. **审批闸门（全部事后）**：推送是硬约束，Agent 永不推送，只能由人工执行（唯一不可自动化的动作）；所有 Agent 代码修改事后邮件审核；ManualReview 项进报告。
5. **审计闸门**：Agent 结构化输出 + 每次代码修改的 diff 全部落盘，报告可查，出问题可追溯。


## grill-me 决策记录## grill-me 决策记录

### 决策 1：Docker 编译执行方式

- **问题**：后台 cron 进程执行编译时，`sudo docker run -it` 的交互式 TTY 与 sudo 权限如何处理？
- **用户回答**：宿主机不添加 sudo 权限无法执行 docker run，对 docker 其他用法不熟悉，接受更好方案。
- **结论**：
  - 代码设计采用 **`docker run -d`（detached）+ `docker exec` 执行编译 + 完成后 `docker rm -f`**，容器名绑定 `rcios-sync-{cycle-id}` 保证幂等（同周期重复执行先 rm -f 再重建）。
  - Docker 命令前缀（`sudo` / 无）封装为可配置项，不阻塞设计。
  - **待确认环境项（运维侧，非本 change 代码）**：需将运行用户加入 `docker` 组或配置 sudo 免密，否则 cron 后台执行会因 sudo 密码提示卡死。


### 决策 2：多型号编译的验证范围与失败语义

- **问题**：改动公共头文件/核心目录时，仅按模块编译可能掩盖真实错误；某型号失败时如何继续？
- **结论**：
  - **公共文件全量编译**：commit 改动文件命中公共/核心目录（如 `include/`、公共头文件、Makefile 等）时，降级为全量编译，不按模块；其余按受影响模块编译。
  - **失败即停该 commit**：某型号编译失败 → 该 commit 立即进入 Build Agent 修复后重编，修复通过前不继续跑剩余型号；Build Agent 确认无法修复时走 fail-fast 关联性判断。


### 决策 3：fail-fast 关联性判断（三层方案）

- **问题**：某 commit 冲突无法解决或编译无法修复时，如何判断后续 commits 是否与该 commit 关联，决定继续同步还是停批？
- **结论**（采纳用户提出的"文件级 + 区域级"思路，整合为三层）：
  - **第 1 层 文件级（确定性规则）**：失败 commit F 与后续 Ci 的 changed_files 无交集 → 无关，继续同步 Ci；有交集 → 进第 2 层。
  - **第 2 层 区域级（确定性规则）**：同一文件内 F 与 Ci 的改动行区域（diff 行号区间）相距很远（间隔 > 阈值）→ 无关，继续同步；区域接近/重叠 → 进第 3 层。
  - **第 3 层 LLM（兜底）**：输入 = F 的失败摘要 + Ci 的 message + 交集文件/区域；有关联 → 停批，无关 → 继续同步。
  - **降级**：第 3 层 LLM 调用失败 → 保守停批。
  - 阈值（行间隔、文件交集定义）在实现阶段以配置参数形式落地。


### 决策 4：Conflict Agent 修改边界与回滚

- **问题**：LLM 解决冲突时可能越界修改冲突文件之外的内容；修复失败时如何回滚？
- **结论**：
  - **仅冲突文件**：Tool 层只允许编辑 `git diff --name-only --diff-filter=U`（unmerged 文件）列表内的文件；修改后校验仅冲突文件有改动、其余工作区干净；越界修改拒绝该轮并重试（计入 max_attempts）。
  - **快照回滚**：每轮修改前对冲突文件做快照，本轮修改无效/越界则恢复快照重来。
  - **质量底线（用户要求）**：冲突解决不得机械删除冲突标记了事，必须理解功能实现与合入目的综合判断如何修改；Agent 输入必须含原始 commit 完整 diff、commit message、源/目标分支上下文、冲突文件完整内容；修改方案须说明"冲突为何如此解决、保留哪边意图、依据"。禁止无理由丢弃任一边改动。
  - **验证深度**：resolve_conflict 阶段只做语法层校验（冲突标记消失 + `git diff --check` 干净）；语义正确性交给 build 阶段编译验证，形成"冲突解决 → 编译 → 修复"闭环。


### 决策 5：Build Agent 修改边界与归因处置

- **问题**：Build Agent 修复编译错误时能改哪些文件？归因分类后哪些情况允许自动修复？
- **结论**：
  - **修改边界 = commit 文件 + 报错文件**：允许编辑本次同步 commit 的 changed_files + 编译错误日志定位到的文件（如 `dhcp.c:123: error`）；若报错文件不在 commit 改动范围内，则倾向归因分类而非直接修改。
  - **归因处置**：仅"本次 cherry-pick 引入"的错误允许自动修复；"原分支已有问题 / 环境问题 / 无法自动修复"只报告不修改。
  - 归因分类沿用交接文档四类：本次引入 / 原分支已有 / 环境问题 / 无法自动修复。


### 决策 6：agent_judgments 缓存策略

- **问题**：LLM 对 pending commit 的 is_bug_fix 判定持久缓存后，如何避免错误判定被永久锁定？
- **结论**：
  - **缓存 + 人工覆盖**：缓存仅用于同一 SHA 去重（不重复调用 LLM）；报告始终展示判定来源（`agent:bug-fix` / `agent:not-bug-fix` / `machine:*`）；支持人工在 judgments 文件中覆盖某 SHA 的判定，人工覆盖优先级最高。
  - 审计闭环：缓存的判定及其来源在报告中可见，人工可通过覆盖修正。


### 决策 7：fail-fast 停批作用域（跨分支隔离 + 分支内按 commit 判断）

- **问题**：同一 commit 跨多个目标分支时，某分支 fail-fast 是否影响其他分支？分支内如何停？
- **结论**（两维度，明确区分）：
  - **跨分支维度（分支级隔离）**：commit 在某目标分支失败，只影响该分支，其他目标分支继续各自的批次；分支间历史差异独立。
  - **分支内维度（按 commit 关联性判断）**：失败 commit F 在分支内停止后，对**后续 commits Ci 逐个**按决策 3 三层方案判断关联性——相关（同文件+区域接近 或 LLM 判关联）→ Ci 也停；无关（不同文件/区域远/LLM 判无关）→ Ci 继续同步。
  - **不做"整批一刀切停"**：只有 F 及与 F 相关的后续 commits 停，无关 commits 继续。


### 决策 8：patch 组织（按分支独立）

- **问题**：同一 commit 同步到多个目标分支时，patch 如何组织？
- **结论**：**按目标分支独立生成**。每份 patch 在对应分支的 worktree 内执行 `git format-patch <base>..<HEAD>`，基于该分支历史；命名含 cycle_id / 目标分支 / commit 范围。报告列出每份 patch 及其包含的 commits。
  - 多分支 patch 之间内容重复**无影响**：每份 patch 是独立面向各自目标分支的应用产物，重复是物理必然，各分支维护者只取自己那份。


### 决策 9：worktree 生命周期（周期内全保留）

- **问题**：周期结束后 worktree 如何清理？
- **结论**：**周期内全保留**。正常与失败分支的 worktree 都保留，直到超过一个周期（1 天）后删除。理由：v2 维护平台需支持用户通过 web 操作执行推送，推送依赖 worktree 的真实 cherry-pick 结果；周期=1 天，磁盘影响可控。
  - **v1 阶段同样按周期保留**：预留 v2 web 演进，且保留一天成本可接受，v1→v2 过渡平滑。
  - worktree 命名含 cycle_id，便于按周期定位与清理。


### 决策 10：推送能力边界（硬约束）

- **问题**：推送目标分支的能力归入哪个阶段？
- **结论**：**推送必须留给人工处理，是能力边际（硬约束，不可配置）**。
  - Agent 权限上限 = 同步解析 + 解决冲突 + 解决编译报错 + 生成 patch，**到此为止**；Agent 永不推送分支。
  - patch 环节是故意设计的强制人工介入安全闸门（"费力不讨好的 patch 环节"正是此意）。
  - v1 产物 = patch + 保留的 worktree（人工复核现场）；推送由人工手动执行。
  - v2 web 平台"操作推送" = 给人工的操作入口，推送动作仍由人触发，Agent 只提供可推送现场与命令建议，**不改变能力边界**。


### 决策 11：空检邮件内容（扫描摘要式）

- **问题**：空检邮件发什么才不是噪音，而是对"Agent 在跑"的证明？
- **结论**：**扫描摘要式**：扫描窗口、扫描分支数、新 commit 数、无需同步的 commit 及其理由（审计闭环）、警告（如分支 fetch 失败）、下次周期时间。
  - **邮件正文必须显示必要信息，不能只靠 HTML 附件**；即使附件携带详细 HTML 报告，正文也要可读的摘要。


### 决策 12：检测扫描模型（固定时间窗，取代增量基线）

- **问题**：检测 commit 用什么扫描模型？崩溃恢复时如何保证不丢 commit？
- **用户关键修正**：采用固定时间窗，基线概念不存在。
- **结论**：**固定时间窗扫描，无增量基线**。
  - 每次 cron 触发扫描**上一完整日 22:00~22:00（+08:00）** 的 commit（与参考实现默认一致）；时间窗边界 22:00 为发布节奏节点。
  - 每次扫描**幂等**：同一窗口扫几遍结果一致；崩溃恢复零负担，无状态文件、无"首日建基线"特殊处理。
  - **跨周期重复检测靠 git 真实状态去重**：已应用的 commit（`git log target` 可见）、已判定的 commit（agent_judgments 缓存）自然不重复处理。
  - **原决策 12（基线周期成功才推进）作废**。
  - 正确性仍遵循：`Tool timeout ≠ 操作一定没有发生`，恢复后以 git 真实状态为准。


### 决策 13：HTML 报告模板基线

- **问题**：报告的呈现结构以什么为基线？
- **结论**：**以参考 skill 的 `branch_maintenance_report.html` 模板为基础，扩展执行阶段区块**。
  - 保留：四态结论徽章体系、KPI 总览、commit 卡片 + 过滤/搜索、按目标分支汇总、风险汇总、页脚"不自动推送变更"声明。
  - 扩展：cherry-pick 执行结果、编译结果（按 commit × 型号矩阵）、Build Agent 归因与修复记录、fail-fast 停批原因、patch 路径（每分支一份、链接包含的 commits）、**Action Required 区块**（人工要动手的事项：停批/未修复/推送命令建议）。
  - 邮件正文显示摘要（决策 11），HTML 附件承载明细。


### 决策 14：编译并发模型

- **问题**：多个目标分支 + 多型号的编译如何控制并发？
- **结论**：**编译全局串行（默认并发=1，可配置参数）**。交叉编译吃满资源，并发多个大编译互相拖慢甚至 OOM；单周期 4-5 小时基准下串行最稳，并发度做成可配置（默认 1）。
  - cherry-pick 等轻量操作可在编译间隙并行处理其他分支，不阻塞；时长由编译主导。


### 决策 15：LLM 选型与配置

- **问题**：三个 Agent 用什么模型？失败降级如何配置？
- **结论**：
  - **三 Agent 共用同一可配置模型**，配置留 `agents.<name>.model` 覆盖项（v1 简化，日后可分开）。
  - 降级参数化：`llm.timeout_sec`、`llm.max_retries`、`llm.degrade_to_manual`（默认 true，LLM 不可用时降级全人工审核，确定性流程仍可运行）。
  - 模型名/API key/Base URL 走环境变量（`LLM_MODEL` / `LLM_API_KEY` / `LLM_BASE_URL`），不硬编码。


### 决策 16：分支执行顺序

- **问题**：周期内多个目标分支的执行顺序与依赖？
- **结论**：**v1 分支间无强制依赖**。各分支从自身 tip 独立 checkout worktree，cherry-pick 基于各自历史；分支独立处理，靠编译串行自然排队。分支依赖配置（parent 声明）留后续，v1 不做（YAGNI）。


### 决策 17：git fetch 失败容错

- **问题**：fetch 是检测前置，失败时如何处理？
- **结论**：**指数退避重试（次数/间隔可配置）→ 超过次数后周期提前结束，发失败报告邮件**。
  - 报告区分错误类型：认证错误（SSH key 失效，提示人工检查）vs 网络超时（临时抖动）。
  - 失败不推进任何状态（固定时间窗模式无状态），下个周期 cron 自动重试。


### 决策 18：安全边际模型（无人值守 + 五道闸门）

- **问题**：Agent 有真实写权限（git worktree/代码修改/docker 编译），无人值守 cron 下如何保证安全？
- **用户关键纠正**：这是服务器自动化任务，**不会有人审批**。所有审批都在事后审核；可通过规则文件指定路径的代码不允许修改。
- **结论**：**五道闸门模型（见上方「安全边际模型」章节）**。
  - 闸门 1 能力闸门（前置锁死）：Git 命令白名单、文件编辑白名单、编译参数白名单。
  - 闸门 2 正确性闸门：编译验证 + 修改合理性检查 + 修改 diff 独立留存。
  - 闸门 3 禁止规则：规则文件定义不可修改路径 / 禁同步分支 / 必验证型号，LLM 不可覆盖。
  - 闸门 4 审批闸门（全部事后）：推送硬约束人工执行；所有 Agent 代码修改事后邮件审核；**无事前人工批准**（无人值守，交互式 human-in-the-loop 不适用）。
  - 闸门 5 审计闸门：Agent 结构化输出 + 修改 diff 全部落盘可追溯。
  - 修正：交接文档 Phase 8 的"人工批准"语义在本项目改为"事后审核 + 规则文件红线"，不采用交互式审批。


### 决策 19：规则集结构（拆两文件）

- **问题**：规则集同时承载决策规则（classify/conclude）与安全红线（闸门 3），如何组织？
- **结论**：
  - 拆为两个文件：`rules/decision_rules.yaml`（决策规则，可被 LLM 兜底）+ `rules/safety_rules.yaml`（安全红线，LLM 不可覆盖，代码层强制）。
  - **安全红线采用纯数据驱动 + 通用 SafetyEnforcer 校验器**：Agent 的编辑/执行工具在操作前主动检查红线，命中即拒绝；新增红线只改 yaml 不碰代码。
  - safety_rules 字段：`forbidden_paths`（禁改路径）、`required_models`（必验证型号）、`forbidden_branches`（禁同步分支）、`max_single_edit_lines`（单次修改规模红线）。
  - decision_rules 字段：`classify`（机器 bugfix/notbugfix 标记、issue-id 模式、路径规则）、`conclude`（相似度阈值、目标类型）、`branch_mapping`（分支类型覆盖项）。
  - 修正（决策 35/36/37 联动）：不做生命周期策略；不做 develop_backfill；branch_mapping 仅分支类型覆盖，不含生命周期覆盖。


### 决策 20：feature/personal 分支矩阵排除

- **问题**：feature 类分支的 commit 如何处理？
- **结论**：**矩阵层面排除**。feature / personal 分支在 branch.md 同源矩阵构建时直接排除（非源非目标），检测阶段不扫描其 commit；**不设 force_manual_categories 强制人工规则**。开发流语义：feature → merge → develop → release，feature 分支本身不直接跨分支同步。


### 决策 21：classify 规则完整移植

- **问题**：classify 机器规则移植多少？
- **用户关键信息**：参考 skill 的目标代码库与当前项目相同（均为 RCIOS）。
- **结论**：**完整移植参考实现的 classify 机器规则**（`[BUG]` / `:bug:` / `fix:` / 中文修复词 / issue-id(CQ/BUG/JIRA) / cherry-pick 标记 + 版本号 / chore/docs / AI IGNOR / openspec-only 排除），连同 golden-set 测试一起移植。这些规则本就是针对 RCIOS commit 习惯定制且已验证。后续新 commit 模式在 golden-set 补测试迭代。


### 决策 22：detect_commits 阶段设计

- **问题**：检测阶段的具体执行细节？
- **结论**：
  - fetch 用 `git fetch --all --prune`（SSH），照搬参考。
  - commit 元数据/性能保护照搬参考：单 commit 超 500 文件 / 2 万行 / 12 万字符则截断或跳过 diff。
  - **branch.md 人工维护**（Agent 只读不写），每次周期重新读取解析（活清单，不缓存），解析结果进 checkpoint。
  - 窗口固定 22:00~22:00（决策 12），重复检测靠 git 真实状态去重。


### 决策 23：sync_decision 阶段设计

- **问题**：决策阶段的具体执行细节？
- **结论**：
  - **判定粒度逐 commit 逐目标分支**：同一 commit 对不同分支可有不同结论（历史不同）。
  - **四态结论全部由 conclude 确定性规则输出**；LLM 只判定 is_bug_fix，不直接输出四态，不覆盖规则结论（规则先行，确定性问题不给 LLM）。
  - is_bug_fix 按 SHA 判定一次并缓存（决策 6），各分支结论由缓存 + 确定性 conclude 算出。
  - 性能保护照搬参考（target_message_cache / patch-id / issue-id / 文件相似度缓存）。


### 决策 24：prepare_worktree + cherry_pick 阶段设计

- **问题**：worktree 准备与 cherry-pick 的执行细节？
- **结论**：
  - worktree 从目标分支**远程最新 tip**（`origin/<branch>`，优先 remote-tracking）检出，照搬参考 `branch_tip` 语义。
  - 批次内 commits 按合入顺序（`git log --reverse`）cherry-pick。
  - cherry-pick 前用 `merge-base --is-ancestor` 确认未应用（git 真实状态去重，照搬参考）。
  - **cherry-pick 变空提交视为成功跳过**：状态记为 `EMPTY`（已应用，与 spec/design 术语统一），不报错不计失败，继续下一个。


### 决策 25：build 阶段设计

- **问题**：编译验证的具体执行细节？
- **结论**：
  - **编译在目标分支 worktree 上执行**（cherry-pick 之后），验证"cherry-pick 到该分支后的代码能否编译通过"。docker 容器挂载宿主 worktree 到 `/workspace/rcios`，`docker exec` 内执行 `cd build/platform/RTL9617C/ && code_update.sh -d && RTL9617C_build.sh clean && RTL9617C_build.sh <型号> [功能]`。
  - **批次内每 commit 编译的是累积状态**：commit1 cherry-pick → 编译 → commit2 cherry-pick → 编译（含 commit1+2）→ ... 验证"引入该 commit 后编译通过"。
  - **clean 粒度 = 批次开头 clean 一次，commit 间增量编译**；仅当某 commit 改动公共文件（决策 2 降级全量编译）时才额外 clean + 全量编译。
  - 型号矩阵执行序：按 `required_models` 列表顺序，失败即停该 commit（决策 2），不继续跑剩余型号。


### 决策 26：build 日志解析与成功判定（基于真实样例）

- **问题**：嵌入式编译日志如何解析错误、判定成败？（依据 `spec/rcios-compiling-log-info.md` 真实日志）
- **结论**：
  - **错误提取参数（用户调整）**：错误行前后各 **20 行**上下文（路径过深输出行多，10 行不够）；总量 ≤3000 行；错误 >500 条取前 500 + 末尾 100。
  - **区分 error / warning / note**：日志中 `warning:`（cast 警告、implicit declaration 警告）与 `#pragma message` note 是长期规范噪声，**不进 LLM 错误输入**；仅提取 `error:` / `Error:` 行作为编译失败证据。
  - **关键陷阱：`fatal:` 未必致命**。日志开头的 `fatal: not a git repository: '...FleetConntrackDriver/.git'` 是内核构建 make 探测 .git 的非致命警告，其后编译继续并可成功。错误解析**不得**把任何含 `fatal:` 的行直接判为编译失败，须结合后续是否还有 `error:` 与退出码综合判定。
  - **成功判定（三查，参考真实成功日志）**：
    1. 产物存在且完整：匹配 `MSG<型号>_*_SYSTEM_*.bin` / `Make rootfs success` 后的产物（真实命名含型号+版本+日期）。
    2. 日志成功标志：`Make rootfs success` / `... success!` / 产物 convert 行。
    3. 容器状态：仍在跑 → 等待；已退出 → 看退出码。
  - 编译日志落盘 `logs/<cycle_id>/build/<branch>/<commit>/build.log`（按周期→分支→commit 分层，问题 11 定案），供审计与人工复核。


### 决策 27：generate_patch 阶段设计

- **问题**：patch 的生成语义与内容？
- **结论**：
  - **patch = `git format-patch <原tip>..<当前HEAD>`**：base = 该分支同步前的原始 tip（`origin/<branch>` 检出点），HEAD = cherry-pick 完成后的 worktree 状态。
  - **patch 包含冲突解决时的修改**（Conflict Agent 的改动已进 git add / cherry-pick commit）：patch 是该分支实际可应用的最小改动集，正是目标产物。
  - 每分支独立生成（决策 8），命名含 cycle_id / 分支 / commit 范围，落盘与报告关联。


### 决策 28：report 阶段设计

- **问题**：报告与邮件的发送时机？
- **结论**：**周期末统一发一封**。周期结束（全部完成或提前失败结束）后汇总发一封邮件；一个周期一封，收件人每天只看一封；中途失败提前结束也发（含已执行分支的部分结果）。
  - 报告三区块（Action Required / 已成功同步 / 检测信息，决策 13）+ 邮件正文摘要 + HTML 附件（决策 11）。


### 决策 29：Conflict Agent 详细设计

- **问题**：Conflict Agent 的执行循环与边界？
- **结论**：
  - **输入**：原始 commit 完整 diff/message/changed_files、unmerged 冲突文件列表、冲突标记内容、源/目标分支、相关代码上下文。
  - **安全预检**：冲突文件命中 `forbidden_paths` → 禁止自动解决，该 commit 转人工（进 Action Required，不自动处理）。
  - **循环**：快照 → LLM 分析（理解功能实现 + 合入目的）→ Apply（仅 unmerged 文件，SafetyEnforcer 强制）→ 校验（冲突标记消失 + `git diff --check` 干净 + 仅冲突文件改动 + 未触红线）→ 通过则 `git add` 继续，失败则回滚快照重试。
  - **max_attempts = 3**（交接文档 Agent Attempts: 3 先例），用尽转该分支 fail-fast 关联判断（决策 3）。
  - 修改 diff 独立留存（闸门 5），报告 Action Required 区展示。


### 决策 30：Build Agent 详细设计

- **问题**：Build Agent 的执行循环与边界？
- **结论**：
  - **输入**：编译失败信息（型号、退出码、错误摘要：error 行 + 20 行上下文 ≤3000 行）、本次 commit changed_files、safety_rules、环境信息。
  - **归因分类**（决策 5）：本次引入 / 疑似原分支已有（确定性验证：切原 tip 编译对比）/ 环境问题 / 无法修复。仅"本次引入"进入修复循环，其余报告不修改。
  - **安全预检**：报错文件命中 `forbidden_paths` → 转人工。
  - **修复循环**：快照 → LLM 修复 → Apply（仅 commit 文件 + 报错文件，SafetyEnforcer 强制）→ 重编（增量）→ 通过则继续剩余型号，失败则回滚重试。
  - **max_attempts = 3**，用尽转 fail-fast 关联判断（决策 3）。
  - 修改 diff 独立留存（闸门 5），报告 Action Required 区展示。


### 决策 31：State 数据契约与批次固化

- **问题**：State 的完整数据结构？分支批次何时固化？
- **结论**：
  - **决策先行固化批次**：决策阶段算完全部四态结论、固化所有分支批次（detected_commits / classifications / decisions / batches），执行阶段只按固定批次顺序跑。checkpoint 恢复清晰、报告可预估工作量。
  - State 顶层：`cycle_id / scan_window / branch_md_version / detected_commits / classifications / decisions / batches / current_target / current_commit / branch_results / status / errors / report`。
  - 子结构：`CommitInfo`（sha/message/author/changed_files/patch_text/symbols/patch_id/issue_ids/源分支/同源段）、`SyncDecision`（仅 is_bug_fix + reason + source）、`Conclusion4`（四态 + evidence + confidence）、`BranchResult`（worktree_path/逐 commit 结果/patch_path/stop_reason）、`CommitResult`（cherry_pick 态/冲突解决/build 逐型号/build_agent 记录）、`BuildOutcome`（OK/FAILED/SKIPPED）。
  - `errors: dict[node, ErrorRecord]` 承载节点级容错（闸门 5 审计 + 容错四层）。


### 决策 32：fail-fast 关联判断触发条件汇总

- **问题**：哪些情况触发 fail-fast 关联判断？哪些情况直接转人工？
- **结论**：
  - **触发 fail-fast 关联判断**（max_attempts 用尽后，走决策 3 三层方案）：
    1. Conflict Agent 3 轮无法解决冲突；
    2. Build Agent 3 轮无法修复编译错误。
  - **不触发关联判断，直接转人工**（进 Action Required，该 commit 标记需人工）：
    3. safety_rules 命中 forbidden_paths（冲突文件或报错文件在禁改路径内）。
  - **统一停批语义（决策 7 修正版）**：转人工或 fail-fast 均只停止当前失败 commit；分支内后续 commits 按决策 3 三层方案逐个判断关联性，相关停、无关继续；其他目标分支不受影响（跨分支隔离）。


### 决策 33：技术栈评审（可扩展 / 解耦 / 容错三维）

- **问题**：bsa 项目自身的技术栈与结构如何定，才能满足可扩展、解耦、容错？
- **结论**：
  - **LLM 集成层：langchain-openai `ChatOpenAI`**（与 LangGraph/ToolNode 原生兼容，DeepSeek 走 base_url，pyproject 锁定主版本防生态漂移）。不采用 openai SDK 自适配（需手写消息链，违背解耦）。
  - **State：外层 TypedDict（LangGraph 原生 state + reducer）+ 内嵌 pydantic（domain 校验）**，职责分离。
  - **引入 `CommandExecutor` 抽象层**（评审 3 关键修正）：`executor/` 含 CommandExecutor 接口 + SubprocessExecutor（生产）/ FakeExecutor（测试）/ WhitelistExecutor（安全白名单装饰）。GitService / BuildRunner 构造注入 executor → 单测零真实命令、换执行环境只换 executor、超时/重试/白名单集中在 executor 层（同时解决解耦/容错/安全）。
  - **Checkpoint：`langgraph-checkpoint-sqlite`（SqliteSaver）**，每 cycle 一个 thread_id；v2 web 平台读同一 SQLite。
  - **包结构**：`executor / config / domain / rules / git / build / mail / agents / graph / scheduler / store / cli.py`；依赖单向 `domain ← executor ← git/build/mail ← agents ← graph`，config 只读被用。
  - **容错三关卡**：节点边界（node_wrapper → state.errors）、LLM（统一 LLMClient 封装 timeout/重试/降级）、外部命令（executor 层）。
  - **安全编码**：WhitelistExecutor 强制 git 命令白名单；文件编辑工具接受"允许文件集合"参数 + SafetyEnforcer 校验 forbidden_paths；配置/密钥全走 pydantic-settings + 环境变量；Agent 工具不暴露通用 shell。


### 决策 34：模块实现契约（subagent 派发约束）

- **问题**：分模块交由 subagent 实现时，按什么约束执行？
- **结论**：
  - **技术栈**：Python 3.13 + uv；ruff lint/format + pytest；pydantic v2；langgraph + langchain-openai + pydantic-settings + PyYAML + python-dotenv；langgraph-checkpoint-sqlite。
  - **错误处理**：异常分层 `BsaError`（DomainError 业务状态走状态转移不抛异常 / InfrastructureError 节点边界捕获写 state.errors / SafetyViolation 强制拒绝转人工）；任何 node 不允许裸异常冒泡出 graph。
  - **测试约定**：规则层 golden-set；Git/Build 注入 FakeExecutor 零真实命令；模块单测独立；graph 集成 mock 服务层；TDD（先失败测试）。
  - **日志审计**：每周期 `logs/<cycle_id>/`（run.log 结构化运行日志 + decisions.json 判定结果 + `build/<branch>/<commit>/build.log` 编译输出，分层组织）；结构化日志（node/stage/commit/branch/耗时/结果）；Agent 修改 diff 独立落盘。
  - **安全**：见决策 33 安全编码条。
  - **扩展性约束**：多 repo 配置结构预留（v1 单 repo）；v2 web 平台读同一 SQLite。

### 决策 35：分支语义修正（基于分支管理规范）

- **问题**：`spec/分支管理规范.md` 定义了分支前缀血缘体系，同源矩阵如何对齐？
- **用户关键信息**：同产品线需互相同步，但一般同产品线不会出现多个 develop 分支；分支命名及关系参考 `spec/分支管理规范.md`。
- **结论**：
  - **同源判定 = 双源结合**：产品线归属 = branch.md section（人工标注，如 1.1 组网 / 1.2 FTTR-B / 1.3 政企网关 / 1.4 4.34 主线 / 1.5 路由器类）；同步方向 = 前缀血缘（develop 父 → 前缀下子 release/fix）。
  - **同步方向 = develop 父 → 子 release/fix**（规范 5.1 语义），源为 develop，目标为其前缀下派生的 release/fix；非参考实现的"源 develop/release/fix 皆可"。
  - **branch.md 是唯一权威分支来源**（真实状态，以它为准，不管远端）；release/fix 分支后续加入 branch.md。
  - **边界风险记录**：参考实现 `resolve_branch_type` 纯关键词匹配，`br_v4.33_5200_sdwan_release_feature_quantum_20260625` 会误判为 feature（实为 release 阶段特性分支）。当前 branch.md 无此类分支不影响主流程；后续若出现，须在分支类型解析时处理"release_feature"歧义。

### 决策 36：develop_backfill 定案

- **问题**：v1 是否启用 develop_backfill（develop 分支作为同步目标）？
- **结论**：**不启用**。同步目标是 release/fix（develop 父 → 子 release/fix），develop 只作源不作目标。与决策 35 前缀血缘方向一致；同产品线不会出现多个 develop 分支，不存在 develop→develop 互同步场景。

### 决策 37：lifecycle 不做

- **问题**：v1 是否实现分支生命周期过滤（active/frozen/preview/eol）？
- **结论**：**不做**。全部分支默认 active，conclude 不判 lifecycle；**移除 `CommitAnalysis.has_high_priority_rule` 字段**（原为 frozen 分支放行用，联动移除）。

### 决策 38：时间窗计算职责与默认参数

- **问题**：默认 22:00~22:00 窗口在哪计算？manual-scan 如何覆盖？
- **结论**：**config 只管读配置**（`scan_since/scan_until` 默认 None = 未指定）；**detect_commits node 计算默认窗口**（上一完整日 22:00~22:00 +08:00，业务逻辑放 node）；manual-scan 传 `--since/--until` 时通过 Settings 传入覆盖默认窗口。

### 决策 39：LLM 后端可选切换（二选一：自研 langchain-openai 或 claude CLI）

- **问题**：交付 Agent（Sync Decision/Conflict/Build）的 LLM 后端用什么？
- **用户最终决策**：**可选，不是同时执行**——自研 agent 按原计划实现（langchain-openai），同时提供可选 `claude -p "prompt"` 后端；**同一时间只启用一个后端，通过配置选择，二者互斥**。
- **用户关键信息**：底层模型同为 DeepSeek API；claude harness 稳定（重试/超时/认证已处理好），自研封装是未知风险；宿主机 claude 已配置好可直接调用。
- **技术事实**：当前 claude CLI（2.1.220）用 `claude -p "prompt"`（非 `-s`）；强约束 prompt 下 JSON 输出可靠（已验证 3/3）。
- **结论**：
  - **LLMClient 单一后端，配置切换（二选一，互斥）**：`llm.backend = "api" | "claude_cli"`，**每个进程/周期只实例化所选的那一个后端，绝不两者同时执行**。
    - `api`（默认）：langchain-openai `ChatOpenAI`（决策 33 原方案，DeepSeek 走 base_url），schema 强制、token 观测、per-agent 模型覆盖。
    - `claude_cli`（可选）：封装 `claude -p` 子进程，prompt 强约束 JSON + 健壮解析器（json.loads → 提取重试 → 降级安全默认）；用宿主配置模型，忽略 per-agent 模型覆盖。
  - **LLMClient 接口 4 方法不变**，后端实现隔离在内部；未来加新后端不破坏接口。**切换只影响选用的实现，不产生双执行**。

  - **配置**：Settings 增加 `llm_backend`（默认 "api"）+ `claude_cli_path`（默认 "claude"）；`llm_model/llm_api_key/llm_base_url` 保留（api 后端用）；`agents.<name>.model` 覆盖项保留（api 后端支持，claude_cli 后端忽略）。
  - **观测**：api 后端用 token usage；claude_cli 后端记录耗时 + 输入/输出摘要（补偿）。
  - **主图仍用 LangGraph 编排**（checkpoint/流程），节点内 LLM 调用走 LLMClient；LLM 不调工具（工具执行在 bsa 代码层）。



