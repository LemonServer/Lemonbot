# Linux Observe 运行手册

当前实现和实机结论的短版入口见 [`linux-wechat-current-status.md`](linux-wechat-current-status.md)。

> 当前停机门禁（2026-09-05）：AT-SPI 无法证明微信 4.1.1.8 的消息方向或群发送者，自动发送也
> 尚未被独立证明。
> `linux-atspi-enroll` 与 `wechat_atspi` runtime 均被代码拒绝。本文的 enrollment、worker 和
> Observe 章节仅保留为未来门禁重新评审时的操作参考，当前不得执行部署。

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

`linux-atspi-semantic-probe` 在进程内生成 self/inbound canary；群聊另生成一个必须由同一对端紧接
发送的 continuation canary。探针以有限周期、只读地重新读取结构发现 canary。Ubuntu 的 AT-SPI
Python 事件绑定曾出现原生崩溃，因此语义探测不注册事件监听器；账号登记短语与当前聊天标题由
隐藏输入读取，只用于本次内存匹配；报告不含真实值或 canary。

`linux-atspi-testing-action-probe --confirm-testing` 只在操作者已经手动打开 `testing` 群时研究
动作面。它跳过输入框名称以及列表/消息行子树的可见文本，只输出白名单化的标题、输入框、发送
控件 selector/role 和结构摘要；`actions_performed` 必须为 0。它不聚焦、不输入、不点击、不发送，
通过也不构成发送授权。

## 独立 testing 发送研究

本节说明已有命令，不属于只读探针流程，也不授权执行发送。当前优先级见
[`PLAN_linux.md`](../PLAN_linux.md)，实验结果统一见
[`linux-wechat-current-status.md`](linux-wechat-current-status.md)。内部 argparse 开关不是稳定公共 API。

已有独立的 `linux-atspi-testing-send-canary` 研究命令。它不接受消息文本，只能生成一条
`LB26_SEND_*` 随机 canary；要求当前标题精确为 `testing`、标题/输入/发送控件均为
`SHOWING+VISIBLE`、操作者确认输入框为空、动作面摘要在写入草稿后仍未改变。动作名必须被明确分类；实机
微信 4.1.1.8 暴露的唯一动作是 `SetFocus`，它不是发送激活。研究路径只有在调用 `SetFocus` 后
证明按钮确实为 `FOCUSED`，并再次验证标题和适用的草稿证据后，才允许生成一次 Return 键事件。
既有草稿只有严格匹配本工具生成的 `LB26_SEND_*` 时才可恢复；其他非空草稿一律拒绝。进入最终
提交事件后，无论异常或超时都标记未知且禁止重试。AT-SPI 回读仍不能证明方向，所以结果固定
`direction_proven=false`、`acknowledged=false`，不得接入 connector 或 outbox。运行真实发送前必须
由操作者对“当前在 testing 群发送一条随机 canary”作当次明确授权：

```bash
uv run lemonbot channel linux-atspi-testing-send-canary \
  --confirm-testing-send --confirm-empty-draft --timeout-seconds 20
```

### 历史实验说明（截至 2026-09-03）

实机首次尝试只写入了草稿；旧实现误把 `SetFocus` 当作发送动作，消息并未发送。人工清空后
又证明 visually empty 的 Qt 编辑器仍暴露固定非空 accessible placeholder，因此该值只能分类为
`unclassified`，不能证明存在草稿。命令现在还要求 `--confirm-empty-draft`，把操作者对空框的当次
确认作为独立门禁；早期版本要求 AT-SPI 精确回读，后续增加了下述较弱的研究证据分支。

后续实机又证明 Qt 的 setter 可以返回成功并改变可见编辑区，但 accessible name/Text 快照保持不变；
因此 testing-only 路径允许以“操作者当次确认空框 + setter 成功回执”作为较弱的草稿证据。它不满足
connector 门禁。发送按钮 `SetFocus` 经最多 2 秒轮询可以证明 `FOCUSED`；输入框及其子树则完全
不暴露 `FOCUSED`。一次按钮焦点 + Enter 实验中，键盘事件返回成功，但 20 秒内 transcript 没有
canary，草稿仍为 `unclassified`，结果为 `unknown`；在操作者人工确认 canary 仍在输入区前，禁止
尝试其他按键或重发。

在人工确认 canary 仍留在输入区后，后续逐项排除了发送按钮焦点下的正确 Return、Space 和 Alt+S；
这些事件均被 AT-SPI 接受，但微信不发送。一次 `Component.grab_focus() → Alt+S` 后曾观察到旧
canary transcript 命中，但操作者随后澄清该消息是人工发送，不能归因于自动路径。使用全新 canary
复测后，`grab_focus()` 仍返回成功，实际输入光标却未进入草稿区，精确 transcript 回读为 0，确认
自动发送失败。操作者说明人工 Alt+S 只有在草稿光标闪烁时才有效；但即使操作者先手动建立该
焦点，当前 `LOCKMODIFIERS → S → UNLOCKMODIFIERS` 合成方式仍未产生新 transcript 命中。因此
“真实光标焦点”和“真实 Alt 按下序列”都是未解决条件，不能用于 connector enrollment。

## 历史语义登记条件（当前不可启用）

原纯 AT-SPI 设计要求每种聊天有两份 `passed=true` 报告，并满足：

- self 与 inbound canary 均恰好匹配一个节点，item 结构签名不同。
- 两者属于同一应用、同一 transcript，正文相对路径稳定。
- 当前 header 节点唯一且路径稳定。
- 群聊存在唯一的非显示文本身份属性；`name`、`description` 不可作为 sender ID。两轮由同一
  对端发送 canary，其加盐身份摘要也必须一致。
- 两轮覆盖微信重启；全部实验覆盖一次锁屏/解锁。

当前私聊和群聊均不满足条件。任何条件不满足都不要人工编辑报告“修复”；不要运行 connector。

## Enrollment 与配置

当前 `linux-atspi-enroll` 固定安全拒绝并且不创建输出。下列要求只描述未来重新开放门禁的最低
条件，不是绕过当前门禁的方法。

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

当前不要安装或启动正式 Lemonbot AT-SPI 服务。只读探针仍可由本地图形会话中的操作者手动
运行。

微信 accessibility unit 是非 root 的 per-user service。不要加入仅适用于 system manager、或
需要 unprivileged user namespace 才能在 user manager 中工作的 `ProtectKernel*`、
`ProtectControlGroups`、`ProtectSystem`；目标 VM 会在执行微信前以 `218/CAPABILITIES` 失败。
该账号本身无内核日志、模块加载和系统目录写权限；unit 仍保留 `NoNewPrivileges`、
`PrivateTmp`、`RestrictSUIDSGID`、`RestrictRealtime`、资源限制和严格 umask。

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

本节是尚未获准运行的设计行为，不是当前运行状态。

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

## 视觉校准研究

视觉实验必须由本地图形会话中的用户通过 xdg-desktop-portal 明确授权。不得使用 SSH 后台
ScreenshotArea/InteractiveScreenshot 的失败路径，也不得绕过 Wayland。截图只允许存在内存或
受控临时区并在分析后删除；聊天截图不得发送给云视觉模型。允许持久化的结果仅限脱敏布局摘要、
门禁原因和会话内加盐的 `unverified_display_sender`。详细规范见
[`visual-calibration.md`](visual-calibration.md)。

### 历史快照：2026-09-01 实机进度

以下仅记录当日进度，不代表当前累计实验次数；后续结果见当前状态文档。

- 官方微信手动启动后，结构探针唯一匹配应用，遍历 555 节点且 0 错误。
- 当前 `testing` 标题唯一；Qt 的两条发送标签实际绑定同一个 Action。过滤
  `SHOWING+VISIBLE` 并对 Action selector 去重后，聊天输入与发送控件形成唯一动作面，实测
  `actions_performed=0`。
- Portal 经用户明确选择一个微信窗口后取得 2 帧 `1718×878 RGBA`；未包含光标、未落盘像素。
- 三条群 canary 均唯一命中且 header 可证明。Qt 的消息 item 几何是同原点的累计高度；视觉入口
  现在只接受带前一兄弟矩形、同 transcript 且顺序受限的报告，并以高度差生成不重叠行区间。
- 尚未生成通过的首轮视觉报告。一次诊断因 Portal 授权超时安全停止。
- testing 专用发送代码已通过 fake AT-SPI 测试，但真实发送被外层安全审查拒绝，实机发送次数仍为
  0；必须取得上述当次明确授权后才能运行。
