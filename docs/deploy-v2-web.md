# BSA V2 Web 工作台部署指南

> 适用版本：V2 Web 分支维护工作台（端口 8888，nginx TLS 反代）。
> 本文档面向部署/运维人员；架构与需求见 `docs/v2-web-platform-requirements.md`。

## 1. 架构总览

```
浏览器 ──443──> nginx（TLS 终结 + 登录限速）──> 127.0.0.1:8888（bsa-web systemd 单元）
                                                     │
                                                     ├─ LOG_DIR/platform.sqlite3（平台库：会话/任务/审计）
                                                     ├─ LOG_DIR/access.log（结构化请求日志，logrotate）
                                                     └─ V1 CLI 子进程（bsa sync/rerun/report/override）
```

- 平台与 V1 Agent **同机部署、独立进程**；平台崩溃不影响 V1 cron。
- 端口契约：平台监听 `BSA_WEB_PORT`（默认 8888），nginx 对外 443 → 反代 127.0.0.1:8888。

## 2. 部署步骤

```bash
# 1. 创建运行用户与目录
sudo useradd --system --home /srv/bsa --shell /usr/sbin/nologin bsa
sudo mkdir -p /srv/bsa/logs /srv/bsa/worktrees /srv/bsa/backup
sudo chown -R bsa:bsa /srv/bsa

# 2. 部署代码（V1 + V2 同一 checkout）
sudo -u bsa git clone <repo> /srv/bsa/bsa
cd /srv/bsa/bsa
sudo -u bsa uv sync --extra dev   # 或只装运行依赖：uv sync

# 3. 环境变量
sudo cp .env.example /etc/bsa-web.env
sudo chmod 600 /etc/bsa-web.env
sudo vim /etc/bsa-web.env          # 填 SECRET_KEY / BSA_USERS / V1 路径等

# 4. systemd
sudo cp deploy/bsa-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bsa-web
systemctl status bsa-web

# 5. nginx（替换占位后）
sudo cp deploy/nginx.conf /etc/nginx/conf.d/bsa-web.conf
sudo vim /etc/nginx/conf.d/bsa-web.conf
sudo nginx -t && sudo systemctl reload nginx

# 6. 验证
curl -fsS http://127.0.0.1:8888/healthz        # → {"status":"ok"}
curl -fsS https://<BSA_HOST>/healthz           # → {"status":"ok"}（nginx 层）
```

## 3. 环境变量清单

见 `.env.example`（含注释）。要点：

| 变量 | 必填 | 说明 |
|---|---|---|
| `SECRET_KEY` | 是 | 会话/CSRF 签名密钥，缺失则拒绝启动 |
| `BSA_USERS` | 是 | 静态账号 JSON 字典 `{username: "bcrypt哈希:角色"}`，operator=操作者 / viewer=查看者；生成示例见 `.env.example` |
| `LOG_DIR` | 否 | 平台库与访问日志根目录，默认 `logs` |
| `BSA_WEB_PORT` | 否 | 监听端口，默认 8888 |
| `SESSION_TTL_SEC` | 否 | 会话有效期（秒），默认 28800 |
| `COOKIE_SECURE` | 否 | HTTPS 下必须 `true` |
| `REPO_PATH`/`WORKTREE_ROOT`/`BRANCH_FILE` | 是（V1） | V1 仓库与 worktree，平台子进程/每日周期使用 |
| `LLM_*` | 是（V1） | LLM 决策配置 |
| `DOCKER_IMAGE`/`DOCKER_MOUNT_WORKSPACE`/`BUILD_SCRIPT_DIR` | 是（V1） | 编译验证配置 |
| `MAIL_*` | 是（V1） | 报告邮件（首次部署建议 `MAIL_DRY_RUN=true` 验证） |

改 env 后：`sudo systemctl restart bsa-web`。

## 4. 定时任务（crontab）

每日 cron 需配置三件事：V1 每日周期、数据保留+备份、worktree 清理。

```cron
# V1 每日同步周期（时间按贵司维护窗口，如 22:00）
0 22 * * *  bsa  cd /srv/bsa/bsa && /srv/bsa/bsa/.venv/bin/uv run python -m bsa.cli run-cycle >> /srv/bsa/logs/cron.log 2>&1

# 数据保留 + 滚动备份（L2 超龄日志清理 + platform.sqlite3/V1 状态打包，保留 7 份）
30 3 * * *  bsa  cd /srv/bsa/bsa && /srv/bsa/bsa/.venv/bin/uv run python -m bsa_web.retention --log-dir /srv/bsa/logs --backup-dir /srv/bsa/backup --keep-days 30 --keep 7 >> /srv/bsa/logs/maintenance.log 2>&1

# worktree 清理（超期即弃，只留当前周期；V1 周期开头也会清理）
# 0 21 * * *  bsa  cd /srv/bsa/bsa && /srv/bsa/bsa/.venv/bin/uv run python -m bsa.cli <清理命令> >> /srv/bsa/logs/cron.log 2>&1
```

> 说明：`bsa run-cycle` 周期开头会清理上期 worktree（V1 现有行为）；若需独立触发，用 `bsa rerun --fresh` 语义或显式清理命令。

### 日志轮转（logrotate）

平台访问日志为 JSON 行文件（`LOG_DIR/access.log`，`WatchedFileHandler` 自动重开），配合以下配置轮转：

```conf
# /etc/logrotate.d/bsa-web
# 注意：不要用 copytruncate。平台用 WatchedFileHandler，靠 logrotate 先 rename
# 当前文件、随后写入时按 inode 变化自动重开新文件；copytruncate 不换 inode，
# 会导致 handler 在截断后从旧偏移继续写 NUL 字节。
/srv/bsa/logs/access.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
}
```

进程 stdout 另由 journald 采集（`journalctl -u bsa-web`），按 journal 自身策略保留。

## 5. 健康检查与监控

- `GET /healthz`：平台 DB 可打开/可写（`SELECT 1`），正常返回 `{"status":"ok"}`；DB 故障返回 **503**。
- systemd `Restart=always` 自动拉起；可加 Systemd 探活或外部探针：

```bash
# 监控探活示例（失败告警）
curl -fsS http://127.0.0.1:8888/healthz || echo "bsa-web unhealthy" | mail -s "BSA-WEB DOWN" ops@example.com
```

## 6. 升级说明

```bash
cd /srv/bsa/bsa
sudo -u bsa git pull --ff-only           # 拉取新版本
sudo -u bsa uv sync                      # 依赖变更时
# 平台库 schema 变更会自动迁移（init_db 幂等 + schema_version）
sudo systemctl restart bsa-web
curl -fsS http://127.0.0.1:8888/healthz  # 确认就绪
```

- V1 代码与平台同仓同发布；升级后建议跑一次 `bsa validate-config` 校验 V1 配置。
- 平台只读消费 V1 投影、写入走 V1 CLI，**升级不影响运行中的 V1 周期**；但建议避开周期窗口重启。

## 7. 回滚

```bash
# 1. 停新版本
sudo systemctl stop bsa-web

# 2. 切回上一版本
cd /srv/bsa/bsa && sudo -u bsa git checkout <上一版本 tag/commit>
sudo -u bsa uv sync
sudo systemctl start bsa-web

# 3. nginx 配置回滚
sudo cp deploy/nginx.conf.rbak /etc/nginx/conf.d/bsa-web.conf   # 若有备份
sudo nginx -t && sudo systemctl reload nginx
```

- 数据回滚：平台库/V1 状态每日备份于 `/srv/bsa/backup/backup-*.tgz`（保留 7 份）。恢复示例：

```bash
sudo systemctl stop bsa-web
ls -t /srv/bsa/backup/backup-*.tgz | head -1   # 选最近一份
sudo tar -xzf <所选备份> -C /srv/bsa/          # 覆盖 logs/ 下同名文件
sudo systemctl start bsa-web
```

- **恢复限制**：worktree 不纳入备份（超期即弃），恢复后需对目标分支 `重新同步`（平台触发 `rerun --fresh`）。

## 8. 端到端冒烟清单（人工验收）

> 自动化链路（登录→工作台→触发同步→轮询→推送→审计）已在 `tests/web/` 用 TestClient 覆盖；
> 上线前按以下清单在真实环境人工走一遍：

1. `curl -fsS http://127.0.0.1:8888/healthz` → `{"status":"ok"}`。
2. 浏览器访问 `https://<BSA_HOST>`，http 访问自动跳 https。
3. 用查看者账号登录：可看工作台/历史/详情，**无**触发同步/推送按钮，直接调 API 返回 403。
4. 用操作者账号登录：工作台显示当前周期总览与待办区。
5. 触发一次同步（源+目标），任务区显示进度，轮询到 succeeded。
6. 对 SUCCESS 分支点推送 → 确认框（commit 范围/patch 摘要）→ 确认 → 结果回显。
7. 审计页可见登录、触发同步、推送记录（操作人/时间/分支/结果）。
8. `tail LOG_DIR/access.log` 为 JSON 行（method/path/status/duration_ms/user），无敏感信息。
9. 连续错误登录 5 次后触发 nginx 429（登录限速生效）。
10. 手动执行保留 job，确认 `backup-*.tgz` 生成、超龄 build/run.log 被清理。
