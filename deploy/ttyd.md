# ttyd（WebSSH 受限终端）部署说明
#
# 作用：失败/停批任务经平台反代的受限终端。每会话实例：
#   ttyd -o -p 0 -W -i 127.0.0.1 -- timeout 1800 bash -c "cd <worktree> && exec bash"
#   - `-i 127.0.0.1`：仅本机监听，对外只经平台 WS 反代暴露；
#   - `-o`：单客户端，断开即退出，杜绝空闲会话残留；
#   - `timeout 1800`：空闲 30 分钟自动回收；
#   - `cd <worktree>`：会话固定进入该任务自己的工作树。

## 安装（两种方式任选）

### 方式 A：apt（Debian/Ubuntu，版本较旧但够用）
    sudo apt-get install ttyd

### 方式 B：官方 release 单二进制（推荐，版本新）
    # 从 https://github.com/tsl0922/ttyd/releases 下载对应平台 tarball，
    # 解压后把 ttyd 放到 /usr/local/bin/ 并 chmod +x。
    # 示例（x86_64）：
    #   curl -L -o ttyd.x86_64 https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64
    #   sudo install -m 0755 ttyd.x86_64 /usr/local/bin/ttyd

验证：`ttyd --version` 能打印版本号即可。无需 systemd 单元——ttyd 由
bsa-web（uvicorn 工作进程）按需 spawn，随平台进程存活。

## 安全边界（G1/G5/G6）

1. 仅本机监听：平台以 `ws://127.0.0.1:<随机端口>/` 反代，用户永远不直连 ttyd；
   nginx 无需额外 WS location（平台 / 反代已含 WS）。
2. 免二次认证：`/ssh/{token}` 与 `/ssh/ws/{token}` 均先过平台登录态，token
   15 分钟过期且与会话绑定，仅作寻址。
3. 禁 sudo：**依赖部署约束**——平台账号（bsa）无 sudo 密码，故终端内 `sudo`
   会要求密码而不可用；ttyd 以 bsa 用户身份运行，无法越权。若还需加固，
   可在 shell 启动前过滤 PATH 中的 sudo，当前实现未额外处理，靠该部署约束兜底。
4. 审计：open/close 必记会话级审计（user/cycle/target/worktree/时长）。
   命令级原始终端日志（ttyd `-l <file>`，本地落盘、不进平台日志）属可选开关，
   当前版本未启用；如需，在 spawn 命令加 `-l /var/log/bsa/ttyd/<token>.log`
   并由 logrotate 轮转（注意其含敏感命令，务必不进平台日志与审计库）。
5. 会话上限：单用户并发 8 个；超限拒绝新会话。

## 回滚

停止 bsa-web 即同时回收全部 ttyd 实例（ttyd 为平台子进程，进程组随平台退出）。
无需额外清理。
