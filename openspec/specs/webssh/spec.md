# webssh Specification

## Purpose
平台为失败/停批分支任务提供浏览器内受限 WebSSH 终端，映射到该任务的 worktree 现场，供维护人员在线处理冲突与编译问题；终端受目录与权限约束，复用平台鉴权并留痕审计。

## Requirements

### Requirement: 受限终端入口

WebSSH 终端映射到任务 worktree，目录与权限受限。

#### Scenario: 终端进入工作现场

- **当** 操作者打开某任务详情页的 WebSSH 窗口
- **则** 终端以固定入口启动：`cd <worktree> && exec bash`，会话工作目录锁定在该任务 worktree
- **且** 终端仅限该 worktree 目录操作，禁 sudo、禁出目录
- **且** 平台运行账号为非维护类超管、无 sudo 密码权限（部署约束），sudo 天然不可用

#### Scenario: 开放范围

- **当** 判断某任务是否提供 WebSSH
- **则** 仅"有 worktree 现场且状态为失败/停批（FAILED/PARTIAL/MANUAL）"的分支任务开放
- **且** ManualReview 待确认项（决策类）不开放 WebSSH

### Requirement: 鉴权与审计

WebSSH 复用平台登录鉴权，操作留痕。

#### Scenario: 鉴权复用

- **当** 客户端建立 WebSSH 连接
- **则** 必须携带平台会话凭证，鉴权失败拒绝连接
- **且** 会话鉴权复用平台登录态，不另设独立口令

#### Scenario: 终端审计

- **当** WebSSH 会话进行
- **则** 记录会话开始/结束（操作人、时间、worktree、时长）为审计记录
- **且** 记录终端命令但做脱敏（过滤明显的密码/密钥内容，如含 password=/token=/私钥行）
- **且** 命令审计粒度可配置；敏感内容不进入平台日志展示
