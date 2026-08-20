---
name: branch-maintenance-report
description: BMA 报告（分支维护报告）— 分析 branch.md 清单中各分支合入状态，生成 Need Sync / Already Included / Manual Review 结论并发送邮件。适用于「BMA报告」「生成bma报告」「bma」「分支维护报告」「Need Sync」等。
---

# Branch Maintenance Report

## 职责边界

| 来源 | 用途 | 路径 |
| --- | --- | --- |
| **业务仓** | 脚本、配置、branch.md 清单、报告/state/judgments、邮件 | `.claude/scripts/`；输出默认 `ai_reports/` |
| **`--repo`** | git 操作（fetch/log/show） | 业务 git 根绝对路径 |

**禁止**：

- **禁止**另写 `ai_reports/bma_config.runtime.yaml` 覆盖 `mail_phase` / `mail_to`；用 `.claude/scripts/branch_maintenance_config.yaml`（`prod`→`mail_to` 全量；`test`→仅 `yangfu@raisecom.com`）。
- 不接受 remote 全分支列表作为分支来源。

**必须**：

- `--from-branch-md` → `.claude/scripts/branch.md`。
- `--config` → `.claude/scripts/branch_maintenance_config.yaml`。
- `--repo` → 业务 git 根；pending 的 `git show` 也在该路径执行。
- `--output` 可省略（默认 `ai_reports/`）。

## 文件清单

| 文件 | 路径 | 说明 |
| --- | --- | --- |
| 脚本 | `.claude/scripts/branch_maintenance_report.py` | BMA 主程序 |
| 配置 | `.claude/scripts/branch_maintenance_config.yaml` | 仓库范围、邮件、阈值等 |
| 分支清单 | `.claude/scripts/branch.md` | 待分析的分支列表 |
| 输出报告 | `ai_reports/branch_maintenance_*.html` | 生成的 HTML 报告 |
| 状态文件 | `ai_reports/branch_maintenance_state.json` | 扫描窗口基线（自动维护） |
| 判定记录 | `ai_reports/bma_agent_judgments.json` | pending bug-fix 判定结果 |

## 用途

将用户输入的维护分析意图转成 CLI 参数并执行 Branch Maintenance Agent（BMA）：

- **分支来源**：`.claude/scripts/branch.md`（`--from-branch-md`）。
- **Git 目标**：`--repo <业务仓绝对路径>`；默认会 `git fetch --all --prune`。
- **仓库范围（当前阶段）**：config `inventory_repo_paths` 默认仅 `[rcios]`，只分析 inventory 中「路径」为 `rcios` 的条目；WLAN/XPON/FTTO/ROS 等其它库暂不处理。清空该列表可恢复全库。
- **扫描窗口（默认）**：未传 `--since`/`--until` 时，自动取**上一完整日 22:00～22:00**（时区 +08:00）。
- **结论类型**：Need Sync / Already Included / Manual Review / Out of Scope。

## 参数映射规则（G7）

| CLI 标志 | 用户意图 / 触发短语 | 默认行为 |
| --- | --- | --- |
| `--config <path>` | 「用 xxx 配置」 | `.claude/scripts/branch_maintenance_config.yaml` |
| `--repo <path>` | 「业务仓路径」 | **必须**：业务 git 根绝对路径 |
| `--skip-fetch` | 「跳过 fetch」 | 缺省会先 `git fetch --all --prune` |
| `--output <dir>` | 「报告输出到 xxx」 | `ai_reports/` |
| `--from-branch-md` / `--branch-file` | 「branch 清单」 | `.claude/scripts/branch.md` |
| `--mail-enabled` | 「生成后发邮件」 | 沿用 config（当前 `mail_enabled: true`，`mail_phase: prod`） |
| `--mail-enabled false` | 「不要发邮件」 | 显式关闭 |
| `--mail-dry-run` | 「只校验邮件不发送」 | `mail_send_subflow.py --dry-run` |

说明：

- **未传 `--mail-enabled`**：沿用 config。
- **`--mail-enabled` 无参数**：等价 `true`。
- **`mail_phase: test`**：仅 `yangfu@raisecom.com`。**`mail_phase: prod`**：使用 `mail_to` 全量。
- 邮件失败仅 `[WARN]`，不回滚 HTML。
- BMA **不修改**业务源码、**不改写** `branch.md`、**不** cherry-pick。

### 1. 仓库范围

- **当前默认**：`inventory_repo_paths: [rcios]`；组件库与其它产品库 `[SKIP]`。
- **`--repo`**：只绑定 git 根，**不**清空白名单。
- 恢复全库：将 `inventory_repo_paths` 置空。

### 2. Fetch 与基线

- 默认 `git fetch --all --prune`；可用 `--skip-fetch`。
- 默认时间窗上一完整 `22:00~22:00`（+08:00）。
- Bug Fix 判定：pending 在业务仓 `git show`；结论写入 `ai_reports/bma_agent_judgments.json` 后重跑。

### 3. 输出

- 报告落在 `ai_reports/`（可由 `--output` 覆盖）。

### 4. 邮件

- payload/结果在 `ai_reports/`；收件人由 `mail_phase` / `mail_to` 决定。

## 执行流程

1. 识别业务仓路径、skip-fetch、邮件意图。
2. 组装 argv（以业务仓根为工作目录）：

   ```bash
   python3 ".claude/scripts/branch_maintenance_report.py" \
     --config ".claude/scripts/branch_maintenance_config.yaml" \
     --from-branch-md ".claude/scripts/branch.md" \
     --repo "<业务仓绝对路径>" \
     --output "ai_reports"
   ```

   勿生成 `ai_reports/bma_config.runtime.yaml`。
3. 解析退出码与 `[OK]`/`[FAIL]`/`[WARN]`。
4. **检查 pending**：脚本输出中如有 `pending agent judgments: N`（N > 0），邮件已被自动跳过。此时**必须**继续处理 pending，不得直接结束：
   - 读取 `ai_reports/branch_maintenance_*_pending_agent.json`
   - 在 `--repo` 路径下逐条 `git show <sha>`
   - 判定每条 commit 是否为 `is_bug_fix`
   - 将判定结果**追加**写入 `ai_reports/bma_agent_judgments.json`（JSON object，key 为 commit SHA，value 含 `is_bug_fix` / `reason`）
   - 同参重跑 BMA 脚本 → pending 归零后自动发送邮件
5. **确认交付**：pending=0 且 mail handoff `[OK]` 后任务方可结束。

退出码：`0` 全成功；`1` 部分失败；`2` 配置错误。

## 返回载荷

```json
{
  "command": "python3 .claude/scripts/branch_maintenance_report.py --repo <业务仓绝对路径> ...",
  "exit_code": 0,
  "reports": [
    {
      "repo_id": "rcios",
      "status": "ok",
      "report_path": "ai_reports/branch_maintenance_rcios_20260729_130000.html"
    }
  ],
  "failures": [],
  "scan_window": {
    "mode": "daily_2200",
    "state_file": "ai_reports/branch_maintenance_state.json"
  },
  "mail": {
    "enabled": true,
    "phase": "prod",
    "to_emails": ["yuhui@raisecom.com", "yangfu@raisecom.com"],
    "status": "SUCCESS",
    "payload_path": "ai_reports/bma_mail_send_payload.runtime.json",
    "result_path": "ai_reports/bma_mail_send_result.json"
  }
}
```

## 命令模板

### A. 标准（当前仅 RCIOS）

```bash
python3 ".claude/scripts/branch_maintenance_report.py" \
  --config ".claude/scripts/branch_maintenance_config.yaml" \
  --from-branch-md ".claude/scripts/branch.md" \
  --repo "<业务仓绝对路径>" \
  --output "ai_reports"
```

### B. 跳过 fetch

```bash
python3 ".claude/scripts/branch_maintenance_report.py" \
  --config ".claude/scripts/branch_maintenance_config.yaml" \
  --from-branch-md ".claude/scripts/branch.md" \
  --repo "<业务仓绝对路径>" \
  --output "ai_reports" \
  --skip-fetch
```

### C. 自定义输出 / 关闭邮件

```bash
... --output "ai_reports/bma"
... --mail-enabled false
```

## 示例

### 示例 1：生成维护报告

以业务仓根为工作目录，执行模板 A。

### 示例 2：用户只给业务仓路径

仍用 `.claude/scripts/branch.md` + `.claude/scripts/branch_maintenance_config.yaml`；仅改 `--repo`。

## Dry-run 自检清单

- [ ] 脚本/config/清单均来自 `.claude/scripts/`；输出到 `ai_reports/`。
- [ ] G7：`--config` / `--repo` / `--from-branch-md` / `--output` / `--skip-fetch` / `--mail-enabled`。
- [ ] 白名单：当前默认仅 `rcios`。
- [ ] Fetch：未说明则默认 fetch。
- [ ] 边界：不承诺 cherry-pick / 改业务代码 / 改 branch.md。
- [ ] **未**写 runtime config。

## 注意事项

- 部分仓失败时 exit 可能为 `1`；须区分 per-repo status。
- 邮件失败不删除已生成 HTML。
