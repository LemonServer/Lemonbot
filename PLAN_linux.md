# Lemonbot Linux 微信方案

> 2026-08-29 实测状态：Ubuntu 24.04 GNOME Wayland + 官方微信 4.1.1.8 已通过第一阶段
> AT-SPI 只读快照验证。共读取 535 个结构化节点，能识别 list/list item、Text、
> EditableText 和发送控件；尚未验证消息方向、群发送者、稳定会话身份或发送回执，因此仍
> 禁止模型接入和任何微信动作。部署细节见 `docs/research-handoff.md`。

## 总体决策

- 采用“整套 Lemonbot + 官方 Linux 微信”同驻 Ubuntu 24.04 x86_64 桌面 VM，不采用 Windows 核心与 Linux worker 分体。
- 首个交付目标为 `observe → draft`；被动回复和主动消息必须后续逐级解锁。
- 使用纯 AT-SPI：禁止 Frida、ptrace、Hook、微信数据库读取、协议逆向、坐标点击、键盘模拟、剪贴板和 OCR 点击。
- `agent-wechat` 只作为可行性证据，不运行、不依赖、不复制其代码。Qt 和 Ubuntu 均确认 Linux GUI 可通过 AT-SPI 暴露结构与事件，但微信实际暴露程度仍需探针验证。[Qt QAccessible](https://doc.qt.io/qt-6/qaccessible.html)、[Ubuntu AT-SPI](https://ubuntu.com/desktop/docs/en/24.04/explanation/accessibility-stack/)、[GNOME EventListener](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/class.EventListener.html)
- 个人微信自动化仍存在非零封号风险，只允许独立测试号，不宣称生产可用。

## 分阶段实施

### 1. 只读可行性探针

新增：

```bash
lemonbot channel linux-atspi-probe
```

固定实验环境（实机以已验证的 Wayland 会话为准）：

- Ubuntu 24.04 Desktop、GNOME 本地图形会话；当前登记环境为 Wayland。
- 专用无 sudo 用户、独立微信测试号、NAT 网络，无端口转发、共享目录、共享剪贴板、Docker、Xvfb、VNC/RDP 或 systemd linger。
- 从[微信 Linux 官网](https://linux.weixin.qq.com/)安装官方包，记录包名、版本、SHA-256、ELF build-id 和真实可执行文件。
- 通过专用桌面入口仅为微信设置 `QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1`。

探针代码不包含任何动作接口，只允许：

- 枚举微信进程、frame、role、state、interface、action 和父子关系。
- 计算剔除可见文本后的 UI 结构签名。
- 订阅 window、children、text、state、property 事件。
- 输出脱敏 JSON：控件是否存在、角色统计、事件种类和属性哈希，不输出联系人、群名或消息正文。
- 由测试人员手动切换会话、收发唯一 canary 文本，探针只观察方向、发送者结构和新旧气泡关系。

判定规则：

- 只有顶层 frame：终止 Linux 路线。
- 消息、方向或群发送者不可读：不得接入模型。
- 目标只能依赖显示名：最多 `observe`。
- 目标在当前会话内结构唯一但重启后不稳定：最多 `draft`。
- 只有稳定非显示名属性、入站事件、方向和精确出站气泡均可证明，才允许实施 `reply`。
- 只看到可视区域会话时不滚动、不猜测；该限制已有真实项目报告支持。[viewport 问题](https://github.com/thisnick/agent-wechat/issues/173)

### 2. Linux 平台基础

探针通过后再迁移核心：

- 保留 SQLite、记忆、策略、预算、inbox/outbox、浏览器、视觉和管理台。
- 新增 `SecretStoreFactory`：
  - Windows 使用 Credential Manager。
  - Linux 使用固定版本 `secretstorage` 对接 [Freedesktop Secret Service](https://specifications.freedesktop.org/secret-service/latest/)。
  - Keyring 锁定时服务启动失败；不得回退到环境变量或明文文件。
  - **2026-08-29 已实现平台工厂与锁定时 fail-closed；待真实 lab keyring 联调。**
- 新增 Linux worker supervisor：
  - 使用固定参数的 `systemd-run --user --pipe --wait --collect`，不经过 Shell。
  - AT-SPI worker 再由 bubblewrap 隔离网络、Home、数据库、附件库和密钥。
  - 通过过滤后的 D-Bus proxy 只开放 AT-SPI、login1 和锁屏状态，不开放 Secret Service。
  - transient unit 或安全属性不可用时，正式 connector 拒绝启动。
- `lemonbot.service` 绑定 `graphical-session.target`，启用 `NoNewPrivileges`、只读系统、精确可写目录、资源限制和 `KillMode=control-group`。
- Linux 不依赖托盘；使用管理台、CLI 和桌面快捷方式提供暂停及急停。急停先写入持久 sentinel，再停止服务；重启后保持暂停，必须执行显式确认才能恢复。
- 新建 Linux `lab.db`，不自动导入或合并 Windows 实验数据。

### 3. Connector 与 worker

公开的 `Connector.events/deliver/health` 保持不变。将现有个人微信安全 broker 泛化为平台无关实现，Windows UIA 与 Linux AT-SPI 仅作为 backend。

新增配置：

```toml
[runtime]
connector = "wechat_atspi"

[wechat_atspi]
enabled = false
stage = "observe"
expected_linux_uid = 1000
expected_linux_user = "lemonlab"
expected_session_type = "x11"
expected_executable_path = "/..."
expected_executable_sha256 = ""
enrolled_client_version = ""
account_fingerprint = ""
ui_signature = ""
enrollment_bundle_path = ""
enrollment_bundle_sha256 = ""
allow_chat_ids = []
admin_sender_ids = []
event_debounce_ms = 500
reconcile_seconds = 15
```

约束：

- 仅允许 `profile="lab"`，逻辑 channel 继续使用 `wechat_personal_lab`。
- 私有 enrollment bundle 权限为 `0600`，保存随机 `target_ref`、本地 chat ID、聊天类型和稳定 AT-SPI 身份属性；显示名称只作人工标签。
- 任一账号、客户端、包哈希、结构签名或 enrollment 变化，都将阶段持久重置为 `observe`。

worker 使用现有 1 MiB 长度前缀 JSON，并增加固定消息：

```text
wechat_atspi.init / ready
wechat_atspi.inspect / snapshot
wechat_atspi.prepare / prepared
wechat_atspi.send / send_result
wechat_atspi.event
wechat_atspi.error
worker.shutdown / stopped
```

worker 只接受核心映射后的 `target_ref`，不能接受模型指定的联系人、selector、channel 或权限。未知消息、错误 request ID、队列溢出和 worker 重启都会毒化当前实例并暂停通道。

### 4. 入站与发送语义

入站消息：

- AT-SPI 事件只作为触发器；防抖后重新读取结构化消息节点。
- 初次连接只建立 transcript baseline，不把屏幕历史重新当作新消息。
- 只产生方向明确的文本事件；群聊必须读取发送者。
- 低频校准仅检查当前可见、已登记会话，不主动滚动。
- worker 重启或 transcript tail 无法唯一对齐时停止发事件，并要求人工重新建立 baseline。
- 停机期间消息首版不保证回补，以避免重复回复。

发送必须依次执行：

1. 唯一验证 UID、图形会话、锁屏、账号、进程、文件哈希、版本和 UI 签名。
2. 根据 enrollment 找到且只找到一个目标，并用当前 header 复核。
3. 记录发送前 transcript tail。
4. 仅通过 AT-SPI `EditableText` 写入，随后逐字读回。
5. 副作用前再次执行策略、白名单、暂停、静默期和配额判定。
6. 标记 `commit_state=started` 后只调用一次控件 action。
7. 等待新消息节点。
8. 只有同一目标出现发送前不存在、方向为自己、正文完全相同的新气泡，才返回 `acknowledged`。

发送 action 前失败为 `failed`；action 开始后的超时、崩溃、目标变化或证据不足一律为 `unknown`，永久禁止自动重发。

### 5. 能力上线顺序

- `observe`：运行 24 小时，只持久化可证明的入站，不调用模型。
- `draft`：运行 24 小时，生成本地草稿，不操作微信输入框。
- `reply`：只回复五分钟内、同一会话、未处理的白名单事件；继续使用既有个人微信限额，持续验证 72 小时。
- `proactive`：只有 AT-SPI 能稳定导航并证明 off-viewport 目标时才实现；否则永久保持关闭。
- 图片和文件不进入首版。文本链路稳定后，仅在微信暴露可证明的官方保存动作时保存到隔离区，再接现有 OCR/智谱视觉流程；否则明确标记“不支持读取”，不使用数据库或截图绕过。

## 测试与验收

- Ubuntu CI 运行全部跨平台测试；Windows CI 保留原 UIA 回归。
- fake AT-SPI 树覆盖节点缺失、同名、off-viewport、群发送者缺失、事件乱序、弹窗和 transcript 截断。
- IPC 覆盖非法 JSON、重复字段、超限帧、未知类型、错误 correlation 和事件洪水。
- 在发送每一步注入超时、锁屏、目标切换、worker kill 和进程重启；提交后的异常必须进入 `unknown`。
- 验证 AT-SPI worker无法联网、读取 Lemonbot/微信数据库、Home 或 Secret Service。
- 日志、配置、数据库、备份、进程参数和环境中不得出现 API 密钥或聊天 enrollment 明文。
- 锁屏、休眠、远程会话、Wayland、客户端更新、验证弹窗、多窗口和结构漂移全部阻止发送并持久暂停。
- 以测试号连续运行 `observe → draft → reply`，72 小时内不得出现重复发送、错误会话发送或 `unknown` 自动重发。
