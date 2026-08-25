# reporting Specification (delta)

## ADDED Requirements

### Requirement: 候选 commit 只读 CLI

提供按源分支列出候选 commit 的只读命令，供平台新建同步选择。

#### Scenario: 列出源分支候选 commit

- **当** 调用 `bsa commits <src> [--limit N]`
- **则** 返回源分支最近 N 条 commit（默认 50），每条含 sha、提交说明、提交时间
- **且** 列表平铺，不做"已同步/已包含"过滤（用户勾选即信任）
- **且** 命令只读，不写任何状态、不改任何文件

#### Scenario: 运行中/失败容错

- **当** 源分支不存在或 git 命令失败
- **则** 返回非零退出码与错误信息，平台提示"候选 commit 加载失败"
- **且** 不影响平台其他操作
