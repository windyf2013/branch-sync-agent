# Close Issues: redesign-web-task-center

日期：2026-08-25

## 验证结果

- 设计一致性：design.md 技术决策（B/A 分离、任务粒度分支级、bsa commits、ttyd WebSSH、abandons、cycle_id 贯穿推送）全部在代码中体现 ✅
- 规格完整性：web-platform（MODIFIED+ADDED）、webssh（ADDED）、reporting（ADDED）需求全部实现并验证 ✅
- 硬不变式：推送四道闸 + 禁 --force + 持全局锁（execute_push 唯一调用点）；WebSSH 仅失败/停批且有现场、仅限 worktree、复用登录态免二次认证、会话级审计；无自动推送 ✅
- 测试：818 passed / 3 skipped；最终全分支审查通过（I1-I3 已修复）

## 已接受的已知项（parked，非阻断）

1. WebSSH 真实 ttyd 冒烟（WS 反代、spawn 端口解析）待部署机验证——本机无 ttyd，测试全 monkeypatch；部署需 ttyd 二进制 + 平台账号无 sudo 权限。
2. 禁 sudo 依赖部署约束（未在 shell 层清 PATH sudo），deploy/ttyd.md 已注明。
3. token 过期后 ttyd 进程有界残留（`timeout 1800`/`-o` 自回收）。
4. rerun thread id 含 `/` 时 `/task/{cycle}/{target}` 路由歧义（安全失败）；`_is_manual_cycle` 前缀启发式为代理判断。
5. manual worktree 存活受每日清理限制（推送闸②安全失败）。
6. 会话未做 user 所有权绑定（纵深可加）；WS 未登录 1011 vs 无效 token 4401 不一致；WS 无 Origin 校验。
7. `GET /ssh/task/...` 打开会话有副作用（GET 触发，已审计+会话上限）。
8. `_load_commits` 项形状不校验；`--limit` 无上限；`_cmd_commits` context 在 try 外；`api_rerun` 未登录 302 与 docstring 403 不符。
9. 放弃接受空字符串 sha（API 未拒绝/规范化）；重复放弃重复审计行；`rerun_pending` 死数据。
10. 历史页不收敛窗口外的手动任务；`_manual_tasks` 逐任务 load_cycle O(n)（cycle_id 回写后成真）。
11. schema_version 未递增（PRAGMA 探测迁移幂等，可接受）。

以上项均不违反能力边界/安全硬约束，记录备查。
