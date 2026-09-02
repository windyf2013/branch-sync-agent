# decision Specification Delta

## MODIFIED Requirements

### Requirement: 规则层先行 + LLM 兜底

classify 机器规则无法判定的 commit，由 Sync Decision Agent 兜底判定。

#### Scenario 规则无法判定 is_bug_fix 的 commit。

- **当** classify 机器规则无法判定某 commit 是否为 bug-fix
- **则** 交 Sync Decision Agent（LLM）只判定 is_bug_fix
- **且** 判定按 SHA 缓存（agent_judgments），后续周期命中缓存不再调 LLM
- **且** 支持人工在 judgments 文件中覆盖判定，人工覆盖优先级最高
- **且** LLM 不可用时降级为人工审核（degrade_to_manual，默认 true）

#### Scenario: 人工覆盖贯通全量 commit（G9）

- **当** 检测阶段对每个 commit 判定 is_bug_fix
- **则** 加载 `judgments.json` 人工覆盖并传入 classify，使人工覆盖对**全量** commit 生效
- **且** 人工覆盖优先级最高，可覆盖机器已判定的 bug-fix / 非 bug-fix 标记
- **且** 覆盖生效于下一次决策执行，不追溯改写已冻结的 decisions.json

### Requirement: 覆盖判定 CLI

提供人工覆盖判定的命令，经该命令写入 judgments.json。

#### Scenario: 人工覆盖判定

- **当** 调用 `bsa override <sha> [--is-bug-fix] [--risk]`
- **则** 将 sha 的判定写入 judgments.json，人工覆盖优先级最高（V1 既有语义）
- **且** 执行时持有全局 flock，避免与周期决策的 judgments 写入竞争

#### Scenario: 只写 risk 不覆盖 is_bug_fix

- **当** 调用 `bsa override <sha> --risk high`（未传 `--is-bug-fix`）
- **则** judgments 中该 sha 只记录 `risk`，**不写入 `is_bug_fix` 键**
- **且** classify 读取时 `is_bug_fix` 键缺失视为「未覆盖」，继续走机器规则/LLM，不误判为 `false`
- **且** SyncDecisionAgent 读取时同样不短路——键缺失（只写 risk）时交 LLM 判 is_bug_fix，仅把 entry 的 risk 当覆盖应用，不把 `needs_agent` 压成 False

#### Scenario: 清除覆盖判定

- **当** 调用 `bsa override <sha> --clear`
- **则** 删除 judgments 中该 sha 条目，并用 `message`+`patch_id` 现算 `fp:<fingerprint>` 键一并删除（fp 是派生别名键，无需维护一一对应映射）
- **且** sha 不可达、patch_id 查不出时降级只删 sha 键、接受 fp 残留（罕见路径）
- **且** 清除后该 commit 回到「未判定」，下次决策让 LLM 重判
- **且** `--clear` 与 `--is-bug-fix`/`--risk` 互斥

### Requirement: 覆盖生效时机

覆盖判定生效于下一次决策执行，不追溯改写已冻结结论。

#### Scenario: 下次决策生效

- **当** judgments.json 被人工覆盖后
- **则** 下一次决策执行（cron 周期或重跑重判）读到新判定并据此得出结论
- **且** 已冻结的 decisions.json（上一周期结论）不被追溯改写，保持审计一致性

### Requirement: 四态结论由规则输出

对每个 (commit, 目标分支) 对，由 conclude 确定性规则输出同步状态结论。

#### Scenario 对每个 (commit, 目标分支) 对判定同步状态。

- **当** commit 通过 classify 判定为 bug-fix
- **则** 用 conclude 确定性规则输出四态：`NeedSync / AlreadyIncluded / ManualReview / OutOfScope`
- **且** 四态结论全部由规则输出，LLM 不直接输出四态、不覆盖规则结论
- **且** 同一 commit 对不同目标分支可有不同结论

#### Scenario: ManualReview 结论标注结构化成因

- **当** conclude 规则返回 ManualReview 结论
- **则** 该结论携带 `cause` 字段，取值为成因枚举（`pending`/`severity_gate`/`fix_missing`/`function_renamed`/`similarity_gray`/`symbols_missing`/`unknown_branch_type`）
- **且** `cause` 是纯标注，不改变判定结果、不改变优先级
- **且** 平台据此精确渲染人工项操作控件（override 仅对 `pending`/`severity_gate`/`fix_missing` 有效，其余成因提示用确认继续/放弃）

#### Scenario: 人工覆盖与四态结论的分层边界

- **当** 人工覆盖（override）作用于 is_bug_fix / risk
- **则** override 覆盖 classify 层的分类与风险，压过规则与 LLM
- **且** 四态结论由规则层按 git 快照独立判定，客观结论（已包含/不同产品线）不接受 override
- **且** 人工对四态的拍板通过「确认继续（直同步）/放弃」实现，而非 override
