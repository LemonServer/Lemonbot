# 本机重启后开启 Lemonbot

适用设备：Jetson AGX Orin，桌面用户 `ega-agx-orin`，仓库 `/home/ega-agx-orin/Lemonbot`。
设备重启后需要人工恢复桌面、密钥环和微信登录；尚未完成整机重启实机验收。
首次安装看[Orin 部署](deployment-jetson-orin.md)，平时改配置看[日常运维](operations.md)。

## 关机或重启设备前

```bash
cd /home/ega-agx-orin/Lemonbot
export PATH="/home/ega-agx-orin/.local/bin:$PATH"
uv run --locked python scripts/wechat_bot_restart.py --stop
```

看到 `Bot stopped after draining` 后再重启。排空失败时先处理故障，不强杀正在发送的任务。

## 设备启动后

按顺序执行，每一步成功后再继续。全程以桌面用户运行，只有 Docker 子进程按需使用 `--sudo`。

### 1. 登录桌面、解锁密钥环

确认网络和日常使用的桌面代理已恢复。已有 API 密钥无需重新录入。

```bash
cd /home/ega-agx-orin/Lemonbot
export PATH="/home/ega-agx-orin/.local/bin:$PATH"
```

以下命令使用本仓库配置；如设置了 `LEMONBOT_CONFIG`，先确认它指向预期文件。

### 2. 恢复微信和本地搜索

```bash
uv run --locked python scripts/wechat_bot_restart.py --stop
uv run --locked python scripts/wechat_setup.py environment --experimental-arm64 --sudo
uv run --locked python scripts/search_setup.py environment --sudo
uv run --locked python scripts/search_setup.py status --sudo
```

复用原镜像和卷；搜索应显示 `running: true`。不要重新 bind 或删除数据。
当前用户已有 Docker 权限时可省略 `--sudo`；不要把整个 uv 命令以 sudo 运行。

### 3. 打开微信，确认原账号已登录

```bash
uv run --locked python scripts/wechat_setup.py open-vnc --experimental-arm64 --sudo
```

默认浏览器会打开已认证页面，本机 noVNC 端口为 **6176**。不要用旧的 `6174/vnc/`。
若出现二维码，用原机器人账号扫码；打开 testing 及需要同步的已授权会话，等待历史同步。
noVNC 已连接不代表微信已登录。同账号在旧机器运行时，先安全停止旧机器人和旧微信容器。

### 4. 刷新服务入口并检查

```bash
uv run --locked python scripts/wechat_setup.py install-services --experimental-arm64 --sudo
systemctl --user restart lemonbot-wechat-metadata.service
uv run --locked python scripts/wechat_setup.py doctor --experimental-arm64 --sudo
```

刷新服务入口可更新容器 IP。所有必要检查通过后继续；身份或绑定错误保持暂停，不重新绑定绕过。

### 5. 启动并显式恢复

```bash
uv run --locked python scripts/wechat_bot_restart.py
# 确认本次白名单报告没有 startup_failed，目标均精确匹配且可读后：
uv run --locked python scripts/wechat_conversation_control.py resume
uv run --locked python scripts/wechat_conversation_control.py status
```

正常应为 `ok: true`、`paused: false`、`target_pause_reasons: []`，空闲时 `queue_depth: 0`。
新出现 UNKNOWN 时停止排查，不补发。恢复会建立新基线，不补回停机消息。
可以关闭终端和 noVNC 页面，保留桌面登录会话。

## 出错时

| 现象 | 处理 |
|---|---|
| `uv` 找不到 | 重新执行第 1 步的 PATH 设置 |
| Docker 权限不足 | setup/search 命令使用 `--sudo`；仍失败先由管理员检查 Docker |
| noVNC 404、`AlreadyRunningError` | 保持机器人停止，重新运行 `open-vnc`，不删除锁文件 |
| `NotLoggedIn`、密钥环检查失败 | 登录原账号、解锁密钥环后重试 doctor |
| `MessageTableMissing` | 打开相应授权会话等待同步 |
| 账号/回执歧义、排空超时 | 保持暂停，按[故障表](operations.md#故障处理)定位，保留全部状态 |

程序平时安全重启、白名单修改、日志和数据回退统一看[日常运维](operations.md)。
恢复现有上下文无需重复迁移或付费预检；当前验收范围见[开发交接](context-memory-implementation.md)。
