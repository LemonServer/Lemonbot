# 日常运维

本机仓库：`/home/ega-agx-orin/Lemonbot`。以桌面用户运行，使用 Python 3.12 和 `uv.lock`。
整机或微信容器重启后走[本机重启手册](restart-jetson-orin.md)，首次安装走[部署入口](deployment.md)。

## 查看、暂停和安全重启

```bash
cd /home/ega-agx-orin/Lemonbot
uv run --locked python scripts/wechat_conversation_control.py status
uv run --locked python scripts/wechat_conversation_control.py pause
uv run --locked python scripts/wechat_conversation_control.py resume
# 安全停止：暂停并等待在途任务完成。
uv run --locked python scripts/wechat_bot_restart.py --stop
# 安全启动/重启：打印本次白名单报告，暂停保留。
uv run --locked python scripts/wechat_bot_restart.py
```

`resume` 只在身份、目标和环境正常时执行。重启后先核对报告、doctor，再显式 resume。
排空最多等待 90 秒，失败时不强杀发送任务。暂停跨重启保留，停机消息不重放。

## 修改哪个文件

| 内容 | 位置 |
|---|---|
| 模型、预算、功能、运行数据路径 | `config/lemonbot.toml` |
| 群和联系人白名单 | 主配置 `[wechat_agent].whitelist_file` 指定的文件，本机为 `config/wechat-whitelist.local.toml` |
| SearXNG 引擎设置 | `config/searxng/settings.local.yml` |
| API 密钥 | 系统密钥环，通过 `lemonbot secret set ... --profile lab` 隐藏输入 |
| 数据库、附件、绑定和暂停 | 主配置指定的私有状态/数据目录，不迁入仓库 |

所有入口选择顺序：显式 `--config` → `LEMONBOT_CONFIG` → 当前 checkout 的
`config/lemonbot.toml`，与终端当前目录无关。仓库外安装保留系统用户配置目录默认值。
本机配置文件 Git 忽略；缺少主配置会报错，不自动读旧私有配置。

修改配置后安全重启生效；更换主配置路径时，先用同一路径重新 `install-services`。
**白名单不自动合并、不热加载**；改名、同步和生效核对见[白名单手册](wechat-whitelist.md)。
SearXNG YAML 修改后需要重新执行 `search_setup.py environment`；部署锁要求先停止机器人。
能力边界见[图片和只读工具](read-capabilities.md)。

本机用户选择关闭十分钟/小时短时限额；每会话滚动 24 小时 30 条、全局 50 条仍生效，
模型预算为 5 元/日、100 元/月。限额包括待发送及 UNKNOWN，不只统计成功回复。
修改上下文开关或回退时保留这些设置，勿用早期完整配置覆盖后续用户选择。

## 检查和日志

```bash
uv run --locked python scripts/wechat_setup.py doctor --experimental-arm64 --sudo
systemctl --user status lemonbot-wechat-conversation.service lemonbot-wechat-metadata.service --no-pager
journalctl _UID="$(id -u)" _SYSTEMD_USER_UNIT=lemonbot-wechat-conversation.service -n 60 --no-pager
```

ARM64 用上面的 doctor；AMD64 省略 `--experimental-arm64`。`--sudo` 只作用于 Docker 子进程，
当前用户有权限时可省略。日志可能包含会话名，仅在本机查看，不公开完整日志或认证 URL。
当前上下文状态见[交接页](context-memory-implementation.md)；来源、检索和 ACK 维护诊断见
[高级运维](reference/context/context-memory-operations.md)。doctor 成功不代表内容验收通过。

## 故障处理

| 原因或现象 | 处理 |
|---|---|
| `ManualPause` / `ExplicitResumeRequired` | doctor 和启动报告通过后 resume |
| `RuntimeConfigMissing` | 恢复选定主配置，或显式 `--config`；不重新 bind |
| `NotLoggedIn` / noVNC 失效 | 按重启手册打开新认证页面，登录原账号 |
| `MessageTableMissing` | 打开对应授权聊天，等待本地同步 |
| 名称不存在或不唯一 | 修改精确名称/备注，安全重启同步；不模糊匹配 |
| 桥连接或 snapshot 失败 | 检查容器、磁盘、元数据服务；容器重启后刷新服务入口 |
| `AccountChanged` / `AnchorLost` / 身份歧义 | 保持暂停，调查原账号及绑定，不覆盖信任锚 |
| 密钥环、模型或预算失败 | 解锁密钥环，检查模型余额/本地预算；不循环付费重试 |
| 群不回复 | 检查真实成员选择 @、白名单、暂停、预算；普通消息和手打 @ 不触发 |
| `AmbiguousReadback` / UNKNOWN | 保留现场，不改 pending，不自动补发或按正文合并 |
| `ACK maintenance incomplete` | 先查发送是否已 ACK；修复本地维护，不重复发送或调用模型 |

## 数据与回退

保留微信卷、绑定、私有 SQLite/附件、费用账本和 UNKNOWN。停机排空后做一致性备份；
不要复制不一致的 WAL，也不要为了排错删除 DB 或暂停文件。
本机目录为 `~/.local/state/lemonbot/jetson-orin/` 与 `~/.local/share/Lemonbot-wechat-orin/`。

统一上下文回退使用能识别当前 schema 的 checkout：停机后关闭
`[context_memory]` 的 `enabled`、`archive_enabled`、`consolidation_enabled`，
启动、doctor、显式恢复。保留新存档及全部发送状态，不降级 schema，不恢复早期 DB 覆盖新 ACK。
详细备份、迁移和回退命令集中在[高级运维](reference/context/context-memory-operations.md)。
