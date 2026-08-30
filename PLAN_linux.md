# Lemonbot Linux-only 下一阶段计划：语义验证 → Observe

> 状态更新（2026-08-31）：AT-SPI-only 已在微信 4.1.1.8 上被实机证伪。self/peer 消息行同构且
> 无稳定方向或 sender 属性，语义报告正确为 `passed=false`。enrollment 和 connector runtime
> 已硬关闭。下文 Observe connector 内容保留为原设计目标，不代表当前可启用能力。当前获准的
> 下一步仅是独立、默认关闭的 Portal 视觉校准研究，见
> [`docs/visual-calibration.md`](docs/visual-calibration.md)。

## 1. 总体目标

- 唯一运行平台改为 Ubuntu 24.04 x86_64、GNOME Wayland，与官方 Linux 微信同驻一台桌面虚拟机。
- 本阶段交付止于个人微信只读 `observe`：接收并持久化白名单私聊和测试群消息，不调用模型、不生成草稿、不操作微信、不产生出站消息。
- 完整移除 Windows UIA、pywechat 探针、托盘、Job Object、Credential Manager、计划任务启动器、Windows 依赖和 Windows CI。
- 移除企业微信 SDK、connector、配置、密钥项、限额和测试；核心只保留 `fake` 与 `wechat_atspi` connector。
- 不迁移 Windows 数据；Linux 继续使用独立 `lab.db`。配置升级为 `schema_version=2`，旧配置明确拒绝，不静默转换。

## 2. Linux 平台与公开接口

- 主运行环境继续由 `uv` 锁定；为 AT-SPI worker 单独建立基于 `/usr/bin/python3 --system-site-packages` 的最小虚拟环境，只安装锁定的 worker 包和 Pydantic，从 Ubuntu 获取 `gi/AT-SPI`。
- Linux 密钥只使用 Secret Service。Observe 模式不检查 DeepSeek 密钥；保留 DeepSeek 网关供后续 Draft 阶段使用。
- 配置接口改为：

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
expected_executable_sha256 = ""
enrolled_client_version = "4.1.1.8"
account_fingerprint = ""
ui_signature = ""
enrollment_bundle_path = ""
enrollment_bundle_sha256 = ""
allow_target_refs = []
event_debounce_ms = 500
reconcile_seconds = 15
```

- `Connector` 和 `ModelBackend` 接口保持不变；新增 `DisabledModelBackend`，任何误调用都立即失败并写审计。
- AT-SPI IPC 增加严格 Pydantic 消息：`init`、`ready`、`baseline`、`event_hint`、`snapshot`、`health`、`error`、`shutdown`。不存在输入、点击、导航或发送类型。
- 管理台和暂停接口统一使用逻辑 channel `wechat_personal_lab`，不再暴露 `wechat_uia` 等平台实现名称。
- 新增持久化 transcript cursor，保存每个 `target_ref` 的滚动链哈希、尾部指纹、状态和最后事件 ID；不在 cursor 中重复保存正文。

## 3. AT-SPI 语义门控与 Observe Connector

### 语义探针

新增命令：

```bash
lemonbot channel linux-atspi-semantic-probe --kind private
lemonbot channel linux-atspi-semantic-probe --kind group
```

- 每次生成一次性合成 canary，由实验账号和对端账号手动发送；群聊使用专用测试群。
- 通过 `EventListener.register_with_app` 只监听微信，事件仅触发 500ms 防抖后的有限结构重读。
- 探针只报告角色、接口、结构路径、事件数量、方向判定、发送者节点存在性和 canary 是否命中。
- 不输出或持久化真实聊天正文、联系人、群名和 canary；非 canary 文本只在内存中比较。
- 私聊和群聊分别完成两轮测试，覆盖微信重启及锁屏/解锁。
- 若入站/自己发送方向无法稳定区分，私聊门控失败；若群发送者无法稳定识别，群聊单独保持禁用。完整阶段验收要求两者均通过。

### Enrollment 与入站

- Enrollment bundle 权限必须为 `0600`，保存随机 `target_ref`、聊天类型、稳定 AT-SPI 属性和必要的不可逆标题指纹，不保存显示名称作为身份依据。
- Observe 只读取当前可见且已登记的会话，不自动导航、滚动或补抓后台会话。
- 切换到另一个已登记会话时先建立 baseline，屏幕上已有历史消息全部视为旧消息；只接收 baseline 后到达的消息。
- AT-SPI 事件只是提示，正文必须通过重新读取结构确认；只接收方向明确的入站文本，自己发送的消息丢弃。
- 群消息必须带稳定 `sender_ref`；只有显示名称或结构歧义时暂停该群。
- 使用 transcript 尾部唯一对齐和链式事件 ID 去重；重启后无法唯一对齐时暂停而不是猜测或回放。
- 原始入站正文在通过白名单和方向校验后保存到 `lab.db`，但不会发送给模型、网络服务或日志。
- `deliver()` 在本阶段固定返回 `observe_only`；runtime 不启动模型、工具、主动任务、outbox dispatcher、浏览器或视觉 worker。

## 4. 隔离、部署与运维

- 实现 Linux supervisor：固定参数调用 `systemd-run --user`，再使用 bubblewrap 和 xdg-dbus-proxy 启动 AT-SPI worker，全程不经过 Shell。
- worker 只可访问只读运行包、必要系统库和过滤后的 AT-SPI bus；禁止网络、Home、仓库配置、数据库、附件库、微信数据目录和 Secret Service。
- 如果 systemd 安全属性、bubblewrap、D-Bus 过滤或微信应用身份无法验证，connector 拒绝启动。
- 保留并完善微信 accessibility user service；新增 `lemonbot.service`，绑定图形会话，不启用 linger。
- 用 `lemonbot install-service --config ...` 替换 `install-startup`。
- 增加 `lemonbot emergency-stop` 和 `lemonbot resume --confirm`。急停写入持久 sentinel、终止 worker；重启后仍暂停，恢复时重新建立 baseline，不补抓停机消息。
- `doctor` 检查 Ubuntu/Wayland、UID、微信包和哈希、AT-SPI、systemd 单元、安全沙箱、enrollment 权限及数据库；Observe 模式不要求云 API 密钥。
- README、运行手册和安全文档改为 Linux-first；原 Windows/企业微信研发记录保留为明确标注的历史档案。

## 5. 测试与验收

- Ubuntu CI 执行锁文件检查、全部 pytest、Ruff、严格 mypy 和密钥扫描。
- 删除 Windows、UIA、pywechat 和企业微信测试；新增 fake AT-SPI 树、事件乱序、事件洪水、同名会话、窗口切换、弹窗、结构漂移、群发送者缺失和 cursor 对齐测试。
- IPC 覆盖非法 JSON、额外字段、错误 request ID、未知消息类型、超限帧和 worker 异常退出。
- 验证 worker 无法联网或读取 Home、`lab.db`、微信数据和 Secret Service。
- 在 baseline、事件处理、数据库提交和进程重启各阶段强制终止，证明不会重放历史、重复记录或产生出站消息。
- VM 实机验收：
  - 私聊与测试群语义门控均通过微信重启和锁屏/解锁测试。
  - 连续 Observe 24 小时，无历史回放、自己消息、重复事件、跨会话记录或群发送者误判。
  - 模型调用数、工具调用数和 outbox 记录均为零。
  - 暂停、急停和重启保持 fail-closed。
  - 日志、探针报告和配置中不存在聊天正文、联系人名称或 API 密钥。

Observe 验收后再单独规划 Linux Draft 阶段：启用 DeepSeek API 生成本地草稿，但仍不触碰微信输入框；Reply、主动聊天、图片和文件能力继续作为后续独立安全门控。

## 6. 当前风险门禁

- AT-SPI 能发现应用、header 和 canary，不等于能证明消息方向或发送者身份。
- 私聊 self/peer 均为全宽、无属性、无子节点的同构 `list item`；群聊也没有稳定 sender 属性。
- 不允许 enrollment，不允许启动 `wechat_atspi` connector，不允许人工伪造结构签名。
- 显示标签、左右位置、气泡颜色和消息顺序都不是身份；任何不确定性都必须停止处理。
- 只有至少两轮 canary 校准并覆盖微信重启和锁屏/解锁后，布局才可作为方向线索；仍不得用于
  白名单、管理员或权限判定。
