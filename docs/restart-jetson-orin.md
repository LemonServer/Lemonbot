# Orin 设备重启后启动 Lemonbot

适用于当前已部署的这台 Jetson AGX Orin，桌面用户为 `ega-agx-orin`，工程位于
`/home/ega-agx-orin/Lemonbot`。以下是日常恢复流程；首次安装见
[Orin 部署手册](deployment-jetson-orin.md)。

日常修改的配置都在仓库内：主配置为 `config/lemonbot.toml`，白名单为
`config/wechat-whitelist.local.toml`。以下命令默认使用这份主配置；若设置了
`LEMONBOT_CONFIG`，会优先使用该路径，也可以通过 `--config` 明确指定。
API 密钥仍在密钥环中，绑定和聊天数据仍在原私有目录。

若启用了本地网页搜索，登录桌面并恢复宿主代理后按第 2 步检查搜索环境。
搜索容器默认随 Docker 恢复，只监听 `127.0.0.1:6177`，不需要 Tavily 密钥。
图片、链接、搜索和时间开关都在主配置 `[conversation]` 中；详见
[能力使用说明](read-capabilities.md)。

目前 Lemonbot 用户服务没有启用开机自动启动，需要登录桌面后人工恢复。已有实机验收覆盖微信通道、
群聊和私聊回复、桥与机器人重启；整台设备重启后的恢复尚未实机验收。

## 重启设备前：先安全停止

正常重启或关机前，在桌面终端执行：

```bash
cd /home/ega-agx-orin/Lemonbot
export PATH="/home/ega-agx-orin/.local/bin:$PATH"
uv run --locked python scripts/wechat_bot_restart.py --stop
```

看到 `Bot stopped after draining; persistent pause and all data preserved.` 后，
再通过 Ubuntu 菜单重启或关机。脚本先暂停、等待正在处理的任务完成，然后停止机器人。
如果排空失败，先处理原因，不要强杀发送任务或删除数据。

## 设备重启后：按顺序执行

**每一步成功后再继续。遇到错误时停在该步，按下方故障表处理。**
下面的命令都在同一个桌面终端里执行。

### 1. 登录桌面并准备终端

登录 `ega-agx-orin` 的 Ubuntu 桌面，确认网络可用。若系统提示解锁“登录”密钥环，
输入桌面登录密码完成解锁；自动登录桌面时尤其要留意这一点。
已有 DeepSeek API 密钥保存在密钥环中，正常重启后无需重新录入。

```bash
cd /home/ega-agx-orin/Lemonbot
export PATH="/home/ega-agx-orin/.local/bin:$PATH"
```

### 2. 确保机器人停止，再恢复微信环境

```bash
uv run --locked python scripts/wechat_bot_restart.py --stop
```

成功后执行：

```bash
uv run --locked python scripts/wechat_setup.py environment --experimental-arm64 --sudo
```

这会检查固定 ARM64 镜像、启动已有微信容器，并从保存的绑定恢复容器内元数据桥。
现有镜像和数据卷会被复用，不需要每次重装。

命令中的 `--sudo` 仅让 Docker 子进程使用管理员权限；如有提示，在本机终端输入
系统密码。不要在整个 `uv` 命令前加 `sudo`。这样也能避开重启后 Docker socket
临时授权失效的问题。若当前用户已有 Docker 权限，可以省略各 setup 命令的 `--sudo`。

当前已启用本地搜索。先确认桌面代理已恢复，再执行：

```bash
uv run --locked python scripts/search_setup.py environment --sudo
uv run --locked python scripts/search_setup.py status --sudo
```

这会复用已核验的搜索镜像和仓库内 YAML 配置，不重新安装，也不需要搜索密钥。
状态应显示 `"running": true`；网页搜索还依赖代理和外部搜索引擎可用。

### 3. 打开微信桌面，确认原账号登录

```bash
uv run --locked python scripts/wechat_setup.py open-vnc --experimental-arm64 --sudo
```

命令会在本机默认浏览器打开已认证的 noVNC 页面。当前 Orin 的 noVNC 端口是
`6176`；不要使用旧的 `http://127.0.0.1:6174/vnc/` 地址，也不要手工复制认证 token。

在浏览器里的微信桌面检查：

- 如果已登录原来的机器人账号，继续下一步。
- 如果出现二维码，用原来的机器人微信账号扫码并在手机确认登录，等待聊天同步。
- 打开 `testing` 群；如其他已授权会话尚未同步，也先手动打开并等待同步。

noVNC 显示“已连接”只表示远程桌面连通，仍要确认微信实际已登录。
日常重启不用重新绑定账号，也不用发送初始化或验收消息。
如果同一个微信账号又在旧机器运行，先安全排空并停止旧机器的机器人和微信容器，
避免两个客户端争抢登录。

### 4. 刷新服务配置并检查环境

容器重启后 IP 可能变化，因此每次恢复都重新生成用户服务配置：

```bash
uv run --locked python scripts/wechat_setup.py install-services --experimental-arm64 --sudo
```

成功后启动元数据转发服务：

```bash
systemctl --user restart lemonbot-wechat-metadata.service
```

然后执行检查：

```bash
uv run --locked python scripts/wechat_setup.py doctor --experimental-arm64 --sudo
```

**只有所有检查均为 `"ok": true`，且命令成功退出，才继续启动机器人。**
检查失败时按输出的原因处理，再运行 doctor。不要通过重新 bind 或删除绑定文件绕过失败。

### 5. 启动机器人，查看本次启动报告

```bash
uv run --locked python scripts/wechat_bot_restart.py
```

这个脚本也可用于启动已停止的服务，会等待并打印本次启动的白名单报告。
确认没有 `startup_failed` 或目标检查失败，并看到“暂停仍保留”的提示，再继续。
启动成功仍会保留之前的暂停，需要下一步明确恢复。

### 6. 解除暂停并确认已就绪

```bash
uv run --locked python scripts/wechat_conversation_control.py resume
```

成功后查看状态：

```bash
uv run --locked python scripts/wechat_conversation_control.py status
```

正常就绪时应看到 `"ok": true`、`"paused": false`，且
`"target_pause_reasons": []`。静闲时 `queue_depth` 应为 `0`。
`unknown_outbox` 是未确认发送的历史计数；本轮部署验收结束时为 `0`。
如果新出现 UNKNOWN，应暂停并排查，不要补发或修改其状态。

完成后可以关闭终端和 noVNC 浏览器页面，保留桌面登录会话；服务由用户 systemd 管理。
后续仅处理现有白名单的新消息，群聊仍需通过成员选择真正 @ 机器人。
恢复会重新建立接收基线，不会补回停机期间的消息。

## 常用操作

以下命令均在工程目录、以桌面用户运行：

| 目的 | 命令 |
|---|---|
| 查看状态 | `uv run --locked python scripts/wechat_conversation_control.py status` |
| 暂停回复 | `uv run --locked python scripts/wechat_conversation_control.py pause` |
| 恢复回复（先确认身份和环境正常） | `uv run --locked python scripts/wechat_conversation_control.py resume` |
| 安全停止，准备关机 | `uv run --locked python scripts/wechat_bot_restart.py --stop` |
| 仅重启机器人 | `uv run --locked python scripts/wechat_bot_restart.py`，检查报告后再 `resume` |
| 再次打开微信桌面 | 先安全停止机器人，再执行第 3 步的 `open-vnc`；完成后按第 4–6 步检查并恢复。 |

如果只是机器人程序重启、微信容器没有重启且 doctor 正常，可使用“仅重启机器人”。
整台设备或微信容器重启后，执行上面的完整流程。

修改白名单时，编辑 `/home/ega-agx-orin/Lemonbot/config/wechat-whitelist.local.toml`，
先手动打开新增的已授权会话，再安全重启机器人同步名单，检查启动报告、运行 doctor，
全部通过后执行 `resume`。名单不会自动热加载，`resume` 本身也不会重新加载。
若 doctor 仅报告 `whitelist_binding` 不匹配且刚修改过名单，需要先安全重启同步；
不要重新 bind。详见 [白名单管理](wechat-whitelist.md)。

## 启动失败时怎么处理

| 现象 | 处理 |
|---|---|
| `uv: command not found` | 重新执行第 1 步的 `export PATH=...`。 |
| `RuntimeConfigMissing` | 检查仓库内 `config/lemonbot.toml`，或明确传 `--config`；旧目录的配置不会自动加载。 |
| Docker 权限错误 | 使用本手册中的 setup `--sudo`；不要把机器人改为 root 运行。 |
| Docker daemon 无法连接 | 在桌面终端执行 `sudo docker version` 检查；若服务端不可用，请管理员恢复 Docker 后重试。 |
| noVNC 404 或页面断开 | 先安全停止机器人，再运行第 3 步的 `open-vnc`；使用新打开的认证页面，不要使用 `6174/vnc/` 旧地址。 |
| `AlreadyRunningError` | `open-vnc` 等部署命令需要部署锁；先安全停止机器人，再重试。若另一个部署命令仍在运行，等待它结束，不删除锁文件。 |
| `NotLoggedIn` | 在 noVNC 中扫码登录原账号，等待同步后重新运行 doctor。 |
| 密钥环锁定、`secret_store` / `model_key` 失败 | 解锁桌面密钥环后重试 doctor；无需删除或重新创建已有密钥。 |
| 元数据连接失败 | 登录正常后重新执行第 4 步，刷新容器 IP 和转发服务。 |
| `MessageTableMissing` | 手动打开报错的已授权会话，等待消息同步，再检查。 |
| `ManualPause` / `ExplicitResumeRequired` | doctor 和启动报告通过后，执行第 6 步。 |
| 账号/绑定不一致、消息身份歧义 | 保持暂停，核对原账号及报错原因；不要猜目标、覆盖绑定或换账号重绑。 |
| 排空超时、UNKNOWN、错误目标或重复发送 | 停止恢复；保留现场和数据，排查后再决定下一步。UNKNOWN 不自动重发。 |
| 服务正常但没有回复 | 检查暂停状态、白名单、群聊真实 @、模型余额和预算；不用在其他会话测试。 |

需要查看日志时，在本机执行：

```bash
systemctl --user status lemonbot-wechat-conversation.service lemonbot-wechat-metadata.service lemonbot-wechat-novnc.service --no-pager
journalctl _UID=1000 _SYSTEMD_USER_UNIT=lemonbot-wechat-conversation.service -n 60 --no-pager
```

本机的 `journalctl --user -u ...` 可能查不到日志，因此这里按用户 UID 和服务名查询。
日志可能包含会话名称，只在本机查看，不要公开整份日志或认证 URL。

保留 `~/.local/state/lemonbot/jetson-orin/`、`~/.local/share/Lemonbot-wechat-orin/`
以及微信容器的两个数据卷。聊天上下文和暂停状态会保留；不要通过删除 DB、卷、绑定
或暂停文件解决启动问题。更多原因码见 [运维手册](operations.md)。


## 统一上下文（阶段 8 验收中）

本机目前仅 testing 群和私聊“18”开启新归档、TensorRT FP16 检索及 DeepSeek 两阶段回答；
云端长期整理关闭。按本手册恢复时沿用 `config/lemonbot.toml`，不要重复迁移或执行付费预检。
先检查 doctor，再明确 resume；GPU 后端以本次 `unified_context_startup` 为准，
仅配置写着 tensorrt 不代表设备可用。失败时按[上下文运维手册](context-memory-operations.md)
关闭三个新开关、排空重启回退，保留新数据库、记忆、费用及 UNKNOWN。


本机已按用户选择将 `[limits.wechat_reply]` 的 `per_10_minutes`、`per_hour` 设为 0，
日常不再限制短时连续对话。每会话每日 30、全局每日 50 及日/月 5/100 元预算仍生效。
只回退上下文开关时保留该限额选择；不要直接用更早完整配置覆盖后续用户设置。
