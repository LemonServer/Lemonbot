# 微信白名单配置

本机入口：[config/wechat-whitelist.local.toml](../config/wechat-whitelist.local.toml)。
该文件已从现有 testing 群和测试联系人生成，权限为 0600、已加入 Git 忽略，不会提交联系人姓名。
公开模板：[wechat-whitelist.example.toml](../config/wechat-whitelist.example.toml)。

```toml
version = 1
groups = ["testing", "另一个群名称"]
contacts = ["张三", "李四"]
```

填写微信**会话列表显示的完整名称**，有备注的联系人使用显示的备注名。
不做模糊匹配、不填写微信号，不区分联系人昵称与备注的自动优先级；以 API 返回的显示名为准。
会话需已出现在微信聊天列表中；仅存在通讯录、从未建立会话的联系人不能直接解析。
允许 1–20 个会话，至少一个；群只在真正 @ 本机账号时回复，私聊自动回复新文本。
需要暂时停止所有回复时使用 pause，不用空列表。

## 修改后生效

每次正常启动都会自动解析名称并同步：容器桥读取范围、私有绑定、数据库白名单。
新增项加入，移除项关闭；无须手工修改内部 ID、JSON 或数据库。当前是**启动同步**，没有文件热重载。
正在运行时修改文件不会立即改变白名单，下次启动才生效。

本机服务的常用操作（在项目根目录执行）：

```bash
uv run --locked python scripts/wechat_conversation_control.py pause
uv run --locked python scripts/wechat_conversation_control.py status
# 确认队列为空、模型预留金额为零，且没有正在处理/发送的任务后再重启。
systemctl --user restart lemonbot-wechat-conversation.service
# 等待启动同步完成；若状态返回 ConnectError，先查看下面的日志，不反复重启。
uv run --locked python scripts/wechat_conversation_control.py status
uv run --locked python scripts/wechat_conversation_control.py resume
```

暂停不会因配置同步或重启解除。保留会话的暂停原因继续保留，显式恢复仍会核验账号、
绑定及读取基线；旧 UNKNOWN 不重发，停机期间消息不补抓，原对话上下文不迁移或删除。
若仍有在途任务，不强行重启；先让当前任务结束或检查管理台状态。

启动日志只显示同步数量和固定错误码，不记录配置中的姓名、原始会话 ID 或凭据：

```bash
journalctl --user -u lemonbot-wechat-conversation.service -n 80 --no-pager
```

| 错误码 | 处理 |
|---|---|
| InvalidWhitelistConfig | 检查 TOML、重复名称、总数、文件权限及所有者；禁止符号链接和其他用户可写的文件 |
| WhitelistTargetNotFound | 核对完整显示名，并先在微信建立该会话 |
| WhitelistTargetAmbiguous | 同名会话无法区分；修改联系人备注或群名称再配置 |
| WhitelistTargetKindMismatch | 群填写在 groups，联系人填写在 contacts |
| WhitelistTargetIdentityChanged | 原名称对应的内部 ID 已改变，需人工核验，不能自动重新指向另一人 |
| ChatListLimit | 列表超过当前 1,000 条检查上限；不能用未检查完的列表认定唯一 |
| WhitelistBridgeSyncFailed | 确认自有桥已部署新版本且服务正常，不跳过同步继续运行 |
| AccountChanged / NotLoggedIn | 手动核对并登录原绑定账号；配置名称不能授权切换账号 |
| MessageTableMissing | 会话已在列表中，但本地消息库没有对应表；在微信打开该会话并同步历史或产生一条新消息，再显式 resume |
| MessageTableAmbiguous | 本地消息表匹配不唯一；保留暂停并定位数据来源，不猜测选表 |

上述两种消息表问题只暂停对应会话，已验证可读的其他会话继续运行。
消息表出现后不会自动解除暂停；恢复会重新建立基线，基线中的消息不会补回复。
所有会话不可读时仍暂停整个通道。账号变化、密钥或快照校验失败仍按全局故障处理。
`MessageSnapshotUnavailable` 是快照读取失败的概括错误，不表示必须重新登录或清空数据；
先检查具体读取原因。本机 2026-10-10 已修复新增空会话误触发全局暂停的问题。

失败时不回退到旧名单继续回复，启动失败并保留原证据。修正问题后重新启动；
已有持久暂停仍需显式恢复。绑定变更失败不启动消费或发送循环。

## 运行配置与兼容

本机私有 conversation-runtime.toml 已设置：

```toml
[wechat_agent]
whitelist_file = "/home/lemon/Lemonbot/config/wechat-whitelist.local.toml"
```

其他部署可使用自己的绝对路径或 `~/` 路径，文件归运行用户所有，建议 `chmod 600`。
`whitelist_file = ""` 保留旧的人工绑定方式；只有 conversation 模式支持名称配置，canary 不变。
账号仍以已有私有 account binding 为信任锚，不自动登记另一个账号。

容器桥增加认证内部 `POST /bindings`，只接受同账号的明确会话 ID，先核验后持久化。
已从 self 消息核验的本机标识保存在私有状态中并核对账号边界；更新名单前从原授权范围确认，
新会话不必预先包含本机发言，身份不明仍停止。该标识不进入公开配置或日志。
读周期上限随白名单扩至二十个目标，同库共用严格快照；通知合并、轮转、操作互斥、
真实 @、预算、限流、上下文隔离、唯一新 self 回执及 UNKNOWN 规则均保留。

本机实测仅同步已有 testing 和一个测试联系人：启动自动同步两个目标，暂停保留，
显式恢复后服务运行、队列为空，旧 UNKNOWN 一条未重发。多联系人和新名称的新增、移除、
歧义、身份变化及暂停保留通过模拟和实际 runtime 数据库测试；不宣称其他真实群已完成收发验收。
宿主重启后的手动登录和临时服务恢复条件不变，见 [环境手册](environment-baseline.md)。

检查记录：全仓 562 项测试通过（一次既有依赖弃用警告）；最后补充的身份衔接与多群绑定两项测试单独通过。
Ruff、mypy（117 源文件）及离线锁检查通过；没有放宽模型预算、发送校验或权限。
