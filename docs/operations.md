# Linux Observe 运行手册

## 固定部署环境

- Ubuntu 24.04 Desktop x86_64、GNOME Wayland、本地图形登录。
- 专用非 sudo 用户、独立微信测试号、NAT 网络，无端口转发、共享目录、共享剪贴板或 linger。
- 官方 Linux 微信；当前登记版本为 4.1.1.8，真实程序 `/opt/wechat/wechat`。
- Python 3.12、`uv.lock`、SQLite `lab.db`，时区 `Asia/Shanghai`。

安装系统能力：

```bash
sudo apt install python3-gi gir1.2-atspi-2.0 at-spi2-core \
  bubblewrap xdg-dbus-proxy libglib2.0-bin
gsettings set org.gnome.desktop.interface toolkit-accessibility true
```

不要用脚本登录微信、反复处理验证、冻结客户端版本或绕过风控。客户端升级后停止 Lemonbot，
重新计算包和文件摘要并从语义探针开始复核。

## 只读探针

`linux-atspi-probe` 只输出角色、接口、固定控件计数和剔除可见文本后的结构摘要。节点数和
`Text/EditableText` 只能证明 AT-SPI 可读，不能解锁 connector。

`linux-atspi-semantic-probe` 在进程内生成 self/inbound canary，通过限定到微信应用的
`EventListener.register_with_app` 监听 text、children、property、state 和 window 事件。账号
登记短语与当前聊天标题由隐藏输入读取，只用于本次内存匹配；报告不含真实值或 canary。

每种聊天必须有两份 `passed=true` 报告，并满足：

- self 与 inbound canary 均恰好匹配一个节点，item 结构签名不同。
- 两者属于同一应用、同一 transcript，正文相对路径稳定。
- 当前 header 节点唯一且路径稳定。
- 群聊存在唯一的非显示文本身份属性；`name`、`description` 不可作为 sender ID。两轮由同一
  对端发送 canary，其加盐身份摘要也必须一致。
- 两轮覆盖微信重启；全部实验覆盖一次锁屏/解锁。

任何条件不满足都不要人工编辑报告“修复”。群发送者不可证明时，群聊保持禁用。

## Enrollment 与配置

`linux-atspi-enroll` 比较两轮 private 和两轮 group 报告的完整 candidate。任一 selector、方向
签名、正文路径、sender 属性、账号指纹变化都会拒绝。输出 bundle 只包含随机 target ref、
路径、结构摘要和不可逆 header/account 摘要，权限固定为 `0600`。

配置还必须登记：

```bash
sha256sum /opt/wechat/wechat
dpkg-query -W -f='${Version}\n' wechat
sha256sum /home/lemon/.config/Lemonbot/atspi-enrollment.json
id -u
```

Observe 配置必须同时满足：

```toml
schema_version = 2
profile = "lab"

[runtime]
connector = "wechat_atspi"

[models]
provider = "disabled"

[wechat_atspi]
enabled = true
stage = "observe"
expected_linux_uid = 1000
expected_linux_user = "lemon"
expected_session_type = "wayland"
expected_executable_path = "/opt/wechat/wechat"
worker_python_path = "/home/lemon/.local/share/Lemonbot/atspi-worker/bin/python"
expected_executable_sha256 = "<sha256>"
enrolled_client_version = "4.1.1.8"
account_fingerprint = "<enroll output>"
ui_signature = "<enroll output>"
enrollment_bundle_path = "/home/lemon/.config/Lemonbot/atspi-enrollment.json"
enrollment_bundle_sha256 = "<enroll output>"
allow_target_refs = ["<enroll 输出的随机 private ref>", "<随机 group ref>"]
event_debounce_ms = 500
reconcile_seconds = 15
```

旧 `schema_version=1`、`wecom` 和 `wechat_uia` 配置会被拒绝，不自动迁移。

## Worker 与 systemd

运行 `install-service` 前必须位于仓库根目录。命令会：

1. 在配置数据目录内重建专用 worker venv。
2. 使用 `/usr/bin/python3 --system-site-packages` 取得发行版 `gi/AT-SPI`。
3. 安装固定 worker requirements 与当前仓库构建的 wheel。
4. 安装 `lemonbot-wechat-accessible.service` 和 `lemonbot.service`。
5. 执行 `systemctl --user daemon-reload` 及 `enable --now`。

AT-SPI connector 只在 `lemonbot.service` 内启动。核心先通过 accessibility bus 的
`GetConnectionUnixProcessID` 解析属于当前微信 PID 的唯一 D-Bus 名称，再启动过滤代理。worker
只得到 Registry 和这些微信名称，且由 bubblewrap 隔离网络、Home、数据库、微信数据与
Secret Service。任何命令、systemd 属性、PID 映射或代理 socket 缺失都会 fail closed。

不要启用 user linger。退出图形会话时微信和 Lemonbot 应同时停止。

## Observe 行为

- 只观察当前可见、已经 enrollment 的会话；不会导航或滚动。
- 首次进入目标或恢复后建立 baseline，现有历史不进入 inbox。
- 切换到另一登记目标时重新对齐；未激活期间的消息不保证回补。
- transcript 使用最近 100 项指纹唯一对齐；重复内容导致多重匹配时暂停。
- self 消息不持久化；群入站缺少稳定 `sender_ref` 时暂停群聊。
- 通过验证的新入站保存到 `wechat_personal_lab/<target_ref>`，随后 pipeline 只完成 inbox 并记录
  `model_called=false/outbox_created=false`。
- `deliver()` 不存在动作实现，runtime 也不启动 dispatcher 或主动任务。

## 暂停、急停与恢复

管理台 channel 名称固定为 `wechat_personal_lab`。暂停期间 worker 事件会被安全丢弃并推进
baseline，恢复后不会回放。急停同时写入数据目录 sentinel 并停止 systemd 服务：

```bash
uv run lemonbot emergency-stop --config <path>
uv run lemonbot resume --config <path> --confirm
systemctl --user start lemonbot.service
```

不要手工删除 sentinel、lock 或 cursor。恢复命令会明确提醒重新建立 baseline。

## 数据操作与事故处理

备份、恢复、导出和永久删除必须在服务停止后执行。删除前先从配置和 enrollment 移除 target。
原始消息和附件不自动清理；磁盘不足时报警并停止新附件入口。

事故处理：

- 会话识别、方向或 sender 不确定：急停，保留数据库与四份报告，重新做语义门控。
- 微信更新或账号验证：停止服务，不自动重试登录。
- API Key 泄漏：在供应商处撤销；Observe 本身不应加载任何 API Key。
- worker/proxy 异常：检查 `journalctl --user -u lemonbot.service`，不要改为直连完整 session bus。
- 数据库或磁盘错误：保持停止，先复制 VM/磁盘证据，再进行离线恢复。

完整阶段验收为私聊和测试群均通过门控，并连续 Observe 24 小时：没有历史回放、self 消息、
重复、跨会话记录、群 sender 误判、模型调用、工具调用或 outbox 记录。
