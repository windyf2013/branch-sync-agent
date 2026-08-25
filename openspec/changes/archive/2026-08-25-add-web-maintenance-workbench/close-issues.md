# Close Issues: add-web-maintenance-workbench

日期：2026-08-24

## 验证结果

- 设计一致性：design.md 技术决策（投影读/CLI 写、全局 flock、单 target 子图、四道闸推送、8888 端口）全部在代码中体现 ✅
- 规格完整性：web-platform 及 V1 五个 capability 的 ADDED Requirements 全部实现并验证 ✅
- 硬不变式：无自动推送（execute_push 唯一调用点 api/push.py:133）、推送禁 --force（push.py:190-191 显式拒绝）、全局 flock（cycle/override/sync/push 均持锁）、超期即弃、审计 append-only ✅
- 测试：741 passed / 3 skipped；最终全分支代码审查通过

## 已接受的已知项（parked，非阻断）

以下为各任务审查中的 deferred minors，经最终审查分诊为不阻塞合并，留待后续迭代：

1. 投影物理非绝对只读：`bsa report` 经 SqliteSaver 的幂等 DDL 会触碰 state.sqlite3（reporting spec "投影命令不写任何文件" 字面差异，实际不修改周期状态）——建议后续文档化。
2. CLI 层 `_cmd_override` 无 try/except（锁超时/损坏 judgments.json 会裸 traceback）；`_parse_bool` 大小写敏感。
3. runner 无 stale 之外的守护：会话表只增不清理、CSRF token 无 max_age、用户枚举时序侧信道（auth.py 登录无统一假哈希）。
4. 四道闸拒绝（400）不写审计（仅 SafetyViolation 留痕）；`body.shas is None` 跳过防陈旧校验。
5. 平台重启遗留 running 任务恢复已实现（final fix I3），但 tasks 表无长期清理 job。
6. 详情页 target/sha href 未 urlencode（应用级既有模式）；build log 整读再截断；BuildOutcome.fix_diff 未在详情页渲染。
7. retention 不清理根级 build_agent.log；backup `[:-keep]` keep=0 边界；sqlite backup URI 未 percent-encode。
8. 部署文档 LOG_DIR 相对路径 vs 硬编码 /srv/bsa/logs 不一致；systemd 硬编码 8888（BSA_WEB_PORT 声明可配但以 unit 为准）。
9. `_NON_FF_MARKERS` 含 "rejected"，可能把非 fast-forward 之外的分支拒绝误标为"远端已前进"。

以上项均不违反能力边界/安全硬约束，记录备查。
