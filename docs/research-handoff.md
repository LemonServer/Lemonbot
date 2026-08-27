# Lemonbot 研发沿革、通道决策与工程交接

> 最后更新：2026-08-27（Asia/Shanghai）  
> 文档性质：当前通道研发的单一交接入口  
> 当前结论：核心代理框架可继续使用；企业微信不符合最终聊天目标；Windows 个人微信 UIA
> 已被可访问性层阻塞；下一条优先研发路线是官方 Linux 微信的纯 AT-SPI 自动化。

## 先读这里

接手者应按以下顺序阅读：

1. 本文：理解已经验证过什么、为何改变方向、哪些实验不能重复。
2. [`PLAN.md`](../PLAN.md)：了解最初的绿地重构目标和完整能力边界。其“企业微信生产通道”
   与“Windows 为唯一部署基线”已经不是当前通道决策。
3. [`security.md`](security.md)：不可被通道变化放宽的安全规则。
4. [`operations.md`](operations.md)：当前 Windows 实现的运行、备份、数据和 UIA 门禁操作。
5. [`README.md`](../README.md)：当前工程入口和开发命令。

不要把“代码里已经有某个 connector”理解成“该通道已经适合上线”。当前真正稳定的是核心
事件、存储、策略、模型、记忆和工具框架；聊天通道仍处于选择与验证阶段。

## 项目的初心与不变目标

Lemonbot 起源于 2024 年的个人微信 AI 插件。目标不是做客服群发工具，而是让一个 AI 在长期
记忆、上下文压缩和受控工具支持下，能够持续、自主、诚实地与人聊天，并安全地读取网页、
图片和管理员授权的信息。

“自主”始终表示在预先授权的能力沙箱内自主规划，不表示任意操作系统权限。下列行为无论
换成 Windows、Linux 或其他通道都必须硬拒绝：

- 支付、转账、购买、订阅、红包等金融行为。
- 凭据、MFA、OAuth 授权、账号安全设置、提权和软件安装。
- 永久删除、任意 Shell/代码执行、批量外发、自动加人、拉群和 `@所有人`。
- Hook、DLL/进程注入、私有协议逆向、客户端降级和风控规避。
- 模型自行修改白名单、联系人身份、目标会话、限额或策略结果。

任何出站动作必须先由核心绑定目标并复判策略；无法证明是否已经发送时进入 `unknown`，不得
自动重试。这一原则比“发送成功率”优先级更高。

## 当前工程已经具备什么

截至当前 `main` 的基线提交为 `41df66b`（“修复workflow”）。现有工程不是 2024 原型的简单
修补，而是已经完成绿地重构的大部分基础设施：

- 持久化 inbox/outbox、单会话 FIFO、去重、审计和崩溃恢复。
- `pending → reserved → dispatching → acknowledged` 发送状态机及 `unknown/dead` 终态。
- DeepSeek 优先的 OpenAI-compatible 模型网关、确定性路由和持久化硬预算。
- SQLite WAL、FTS5、分层记忆、摘要、事实来源、承诺和上下文压缩。
- 受限网页读取、图片净化、OCR、智谱视觉和受控文件保险库。
- 白名单、静默期、频率限制、主动任务来源约束、暂停和急停。
- 本地管理台、一次性审批、备份恢复、数据导出及管理员显式删除。
- 假连接器、企业微信连接器和 Windows 个人微信实验连接器。
- Windows UIA 的四阶段持久化门禁：`observe → draft → reply → proactive`。

核心目录可从 `src/lemonbot/` 下的 `domain`、`storage`、`policy`、`models`、`memory`、
`orchestration`、`tools`、`approvals`、`proactive`、`connectors` 和 `supervisor` 开始阅读。

通道更换不应重写核心事件链路：

```text
connector event
  → durable inbox
  → per-conversation FIFO/debounce
  → context + memory retrieval
  → model
  → independent policy decision
  → tool execution
  → outbound rate limit
  → durable outbox
  → connector delivery
  → receipt/audit/memory update
```

## 研发时间线

### 2024：原型阶段

- 实现 AI 接入个人微信的早期插件。
- 验证了“模型可以长期和真实联系人聊天”这一产品方向。
- 原型包含粗糙依赖、历史密钥/日志/浏览器驱动等问题，不作为兼容目标。

### 2026：绿地重构

- 确定 Python 3.12、`uv`、`asyncio`、Pydantic、SQLAlchemy、SQLite、FastAPI 等基础栈。
- DeepSeek 被设为首选文本和工具模型，智谱视觉负责语义读图，RapidOCR 负责本地 OCR。
- 建立核心数据库、记忆、预算、工具、策略、审计、管理台和安全工作进程。
- 清理当前工作树中的旧密钥、私钥、CRX、ChromeDriver 和日志，但按约定不改写旧 Git 历史。
- 修复并锁定 GitHub Actions；CI 在 Windows/Python 3.12 上运行 pytest、Ruff、mypy 和当前树
  Gitleaks 扫描。

### 企业微信评估

最初计划把企业微信智能机器人作为生产通道，原因是它有官方 SDK、稳定 ID、媒体和重连能力，
比个人微信 UI 自动化可靠。

后续确认它不符合核心产品目标：

- 智能机器人面向企业内部或企业配置的业务场景，不等同于个人微信账号。
- 企业微信“自建应用”等接口操作的是企业通讯录和企业会话，不提供一个普通个人微信账号可
  自主与全部个人联系人聊天的等价接口。
- `bot_id` 是企业微信智能机器人配置生成的机器人标识，不是个人微信号，也不能把个人微信
  联系人映射为该机器人的联系人。

因此企业微信连接器代码保留为官方通道参考、回归测试和可选企业部署能力，但不再视为项目
最终聊天通道。

### Windows 个人微信 UIA 实验

为了坚持“不 Hook、不注入、不逆向”，Windows 个人微信只采用 UI Automation，并实现了：

- 独立测试号、`lab` 数据库与生产数据隔离。
- 精确 Windows 用户、账号哈希、可执行路径/SHA-256、客户端版本、窗口和 selector 指纹登记。
- 同名目标拒绝、锁屏/升级/控件漂移停止、发送前复判、发送后回读和 `unknown` 规则。
- 观察、草稿、被动回复、主动消息四阶段门禁。

框架和安全门禁已经存在，但现代微信客户端没有向本机账号暴露所需 UIA 树，所以连接器无法
进入实际观察阶段。

### pywechat 项目评估

调研中遇到两个容易混淆的来源：

1. `wuchaooooo/pywechat-windows-ui-auto`：当前 Lemonbot 只读探针固定参考的 fork，提交为
   `363f9139abd419c1289a27391890c62112589030`。
2. [`Hello-Mr-Crab/pywechat`](https://github.com/Hello-Mr-Crab/pywechat)：活跃上游，微信 4.x
   模块名为 `pyweixin`。2026-08-27 调研时主分支最新可见提交为 `73e09e0`。

两者均没有解决本机账号的 UI 树屏蔽。活跃上游自己的
[`Weixin4.0.md`](https://github.com/Hello-Mr-Crab/pywechat/blob/main/Weixin4.0.md)
已经说明讲述人方案对新账号失效。

没有直接安装或导入上游整包，原因包括：

- 导入 `pyweixin` 会加载动作模块并执行 `pyautogui.FAILSAFE = False`。
- 文本发送使用全局剪贴板、`Ctrl+V` 和 `Alt+S`，返回 `None`，没有可靠发送回执。
- 目标主要按显示名称选择，不能提供 Lemonbot 要求的稳定身份绑定。
- 恰好 2000 字的消息会落在 `<2000` 与 `>2000` 两个分支之外而静默丢失。
- 包含删除好友、清空聊天、拉黑、通话、朋友圈和系统设置等不应暴露给模型的能力。
- 上游已有“搜索目标失败后消息发送到置顶会话”的
  [Issue #286](https://github.com/Hello-Mr-Crab/pywechat/issues/286)。

当前 `lemonbot uia pywechat-probe` 只重新实现了一小组静态 selector；它不导入上游、不激活
窗口、不点击、不输入、不读微信数据库，也不操作剪贴板。

### Linux 新发现

官方 Linux 微信是 Qt 客户端。Qt 在 Linux 上使用 AT-SPI 无障碍桥，并支持通过
`QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1` 启用。与 Windows 当前只暴露渲染外壳不同，已有工程
证据表明 Linux 微信可以暴露会话列表、会话项、输入框和发送按钮。

最有价值的可行性证据是
[`thisnick/agent-wechat`](https://github.com/thisnick/agent-wechat)：它在 Xvfb 中用 AT-SPI
和确定性状态机操作官方 Linux 微信。其文档和 Issue 显示树中可以观察到：

- `list[name="Chats"]` 与当前可视的会话项。
- `Send(S)` 按钮及其相邻的可编辑输入框。
- 登录、确认、聊天窗口和弹窗状态。

这证明“纯 AT-SPI 发送路径”值得实验，但不能直接采用整个 `agent-wechat`。完整项目还会：

- 请求 `SYS_PTRACE`，使用 `seccomp=unconfined`，可选请求 `NET_ADMIN`。
- 使用 Frida/内存偏移提取微信数据库密钥。
- 直接读取 `session.db`、`contact.db`、`message_N.db` 和媒体数据库。
- 使用 Hook 改变会话选择，从而获得稳定 `wxid` 和完整消息历史。

这些行为违反 Lemonbot 的无 Hook/注入/逆向边界。项目还存在可视区域之外会话发送失败的
[Issue #173](https://github.com/thisnick/agent-wechat/issues/173)，以及容器/服务器登录数小时后
触发风控的[报告](https://github.com/thisnick/agent-wechat/issues/168)。它只能作为 AT-SPI
结构和状态机的研究证据，不能作为未经审计的运行时依赖，也不能直接拉取 `latest` 镜像运行。

## Windows 实验的已确认结果

### 实验环境和现象

- Windows 11 x64，现代个人微信进程为 `Weixin.exe`。
- 外层窗口可发现，类名为 `Qt*QWindowIcon`；内部主要是 `MMUIRenderSubWindowHW` 渲染外壳。
- 普通 UIA 遍历只得到 7 个节点。
- `mmui::MainWindow`、会话列表、消息列表、搜索框、输入框、发送按钮、当前聊天标题和
  `mmui::ChatSessionCell` 的匹配数全部为 0。
- Lemonbot 只读探针返回 `accessibility_tree_not_exposed`。

### 讲述人实验

已严格执行过以下顺序：

1. 完全退出微信。
2. 先启动 Windows 讲述人。
3. 再启动微信并登录。
4. 保持讲述人和桌面解锁超过 5 分钟。
5. 重新运行只读 UIA 探针。

结果仍为 7 个节点，所有关键 selector 为 0。不要重复该实验，也不要通过修改讲述人注册表、
冻结/降级客户端、坐标点击或 OCR 点击来绕过。

### 故障层级判断

该问题不是 DeepSeek 配置、`bot_id`、数据库、白名单或 Lemonbot 编排错误。阻塞发生在最底层：

```text
微信/账号没有暴露可访问性树
  → 无法可靠读取会话和消息
  → 无法证明目标联系人
  → 无法安全生成入站事件或提交发送
```

只要 `accessibility_tree_not_exposed` 仍成立，修改业务代码、模型提示词或发送限额都不会使
Windows 通道可用。

## 当前通道决策

| 路线 | 当前状态 | 原因 | 后续动作 |
|---|---|---|---|
| 假连接器 | 可用 | 可验证完整核心链路，无外部副作用 | 保持为 CI/开发基线 |
| 企业微信智能机器人 | 可选但非目标 | 官方稳定，但不等价于个人微信账号 | 保留代码，不作为默认产品方向 |
| Windows 个人微信 UIA | 暂停 | 当前账号只暴露 7 个外壳节点 | 保留探针和门禁，不再投入动作适配 |
| Windows OCR/坐标 | 拒绝 | 目标无法可靠证明，误发风险高 | 不实现 |
| WCFerry/Hook/协议逆向 | 拒绝 | 封号、升级和安全边界冲突 | 不实现 |
| Linux 纯 AT-SPI | 优先验证 | Qt/AT-SPI 有结构化控件证据 | 先做只读探针 |
| Linux AT-SPI + Frida/DB | 拒绝 | 需要 ptrace、密钥提取和逆向数据库 | 不采用完整项目 |

不要删除 Windows 连接器、企业微信连接器或已有门禁。它们仍是测试资产，也为未来微信重新
开放可访问性树或用户选择企业部署保留路径。新增 Linux 通道时应复用 `Connector`、策略、
outbox 和四阶段门禁，而不是另起一个绕过核心的机器人进程。

## 下一位工程师的第一项任务：Linux 只读可行性探针

### 实验环境

建议首次实验使用：

- Ubuntu 24.04 LTS x86_64 虚拟机。
- 正常 GNOME Xorg 图形会话；首轮不要使用 Wayland、Docker、Xvfb、VNC 或云服务器。
- 从 `https://linux.weixin.qq.com/` 获取的官方 Linux 微信安装包，不使用未知镜像或
  第三方重打包；记录版本和安装包 SHA-256。
- 独立测试微信账号、正常住宅/办公网络、固定时区 `Asia/Shanghai`。
- 不安装 Frida，不授予 `SYS_PTRACE`/`NET_ADMIN`，不读取或解密微信数据库。

选择 Xorg 是为了减少 Wayland 对合成输入和窗口控制的额外限制；它不是要求永久停留在 Xorg。
待 AT-SPI 读取路径成立后，再单独评估 Wayland。

### 探针边界

建议新增命令名：

```bash
lemonbot channel linux-atspi-probe
```

第一版只能：

- 枚举微信进程、顶层 frame、控件 role、state、action 和结构关系。
- 计算去除可见文本后的结构签名。
- 订阅 AT-SPI 的 window、children、text、state 和 property change 事件。
- 报告固定控件是否存在，不输出联系人、群名、消息或草稿正文。

第一版禁止：

- 点击、聚焦、输入、粘贴、发送、滚动或切换会话。
- 读取 `~/Documents/xwechat_files` 等微信数据目录。
- 截取或保存包含聊天内容的全屏截图。
- 启动 Docker 镜像或加载外部 MCP/Skill。

可参考 Qt 的
[`QAccessible`](https://doc.qt.io/QT-6/qaccessible.html) 和 GNOME 的
[`Atspi.EventListener`](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/class.EventListener.html)
官方文档。可以研究 `agent-wechat` 的 selector 与状态机概念，但不要复制无明确许可的代码，
也不要引入其数据库和 Hook 工具。

### 必须回答的八个问题

探针完成后需要用脱敏 JSON 和人工观察回答：

1. 微信是否稳定暴露完整 AT-SPI 应用树，而不是只有顶层 frame。
2. 是否存在会话列表、当前聊天标题、消息列表、输入框和发送按钮。
3. 新消息到来时是否产生可订阅事件，还是必须低频重新扫描。
4. 私聊消息是否能区分自己/对方，群聊是否能得到发送者。
5. 控件树是否只包含当前可视的会话和消息。
6. 同名联系人或群聊是否有可用于唯一登记的非显示名属性。
7. 窗口最小化、锁屏、重新登录和客户端重启后结构是否保持稳定。
8. 不读微信数据库时，图片/文件能否通过可访问性动作安全接收或保存。

### 判定门槛

| 探针结果 | 允许进入的下一阶段 |
|---|---|
| 只能看到顶层窗口 | 停止 Linux 路线，不做视觉点击替代 |
| 可见输入框/按钮，但消息不可读 | 只允许人工选中会话后的草稿辅助 |
| 可读消息，但发送者/目标不唯一 | 只读观察，不允许发送 |
| 可唯一绑定目标、读取入站及回读出站气泡 | 实现 `draft`，之后逐级门禁 |
| 发送后只能看到输入框清空/按钮禁用 | 仍不能记为 `acknowledged` |

“发送按钮变灰”只能说明输入区状态变化，不能证明消息送到了正确会话。可靠确认至少需要同时
验证当前会话身份、最新自己的消息气泡内容/方向以及时间窗口；任一项不确定都进入 `unknown`。

## Linux connector 的设计约束

若探针通过，建议新增 `LinuxWeChatAtspiBackend`，并沿用现有
`PersonalWeChatConnector` 的高层状态机和门禁。不要让 AT-SPI worker 直接调用模型或拥有
Lemonbot 数据库。

推荐边界：

```text
Lemonbot core
  ├─ owns policy, allowlist, inbox/outbox, rate limits and durable state
  └─ length-prefixed validated IPC
       └─ low-privilege Linux AT-SPI worker
            ├─ observes one enrolled WeChat process/session
            ├─ returns sanitized UI facts/events
            └─ performs one already-authorized exact action
```

必须保留：

- 独立测试号和独立 `lab` 数据库/附件库/密钥命名空间。
- `observe → draft → reply → proactive` 的持久化逐级晋级。
- 客户端包 SHA-256、版本、Linux 用户、会话类型和结构签名登记。
- 同名目标拒绝、登录验证/锁屏/结构漂移立即停止。
- 出站提交前策略复判、限额、静默期和 `reason_event_id`。
- 发送状态未知时停止，不自动重新规划或重发。

需要重新设计而不是照搬 Windows 的部分：

- Windows Credential Manager/DPAPI 应替换为 Linux Secret Service/libsecret 或独立受限密钥
  worker；密钥仍不得进入 TOML、数据库和日志。
- Windows Job Object 应替换为 systemd user service、独立用户/组、`NoNewPrivileges`、资源
  限制和最小文件系统权限。
- `pystray`、Win32 锁屏/进程/文件身份检查需要 Linux 等价实现。
- UIA selector bundle 应升级为平台无关 schema，而不是把 AT-SPI role 硬塞进 Windows 字段。

## 不要重复的弯路

- 不要再次尝试“先开讲述人、等五分钟、再登录微信”；本机已严格验证失败，上游也确认该
  方法对部分新账号失效。
- 不要安装完整 `pywechat127` 来验证 selector；导入本身会扩大动作面。
- 不要把 OCR/视觉点击当作 Windows UIA 的无害回退。看见文字不等于能证明稳定会话身份。
- 不要使用老微信、冻结版本、Web 微信、WCFerry、DLL 注入、Frida、ptrace 或数据库密钥提取。
- 不要直接运行 `agent-wechat:latest`；其默认能力和容器权限超出本项目边界。
- 不要为了“先跑起来”绕过 outbox 或把外部项目的自动回复线程直接连接到模型。
- 不要把显示名当稳定 `chat_id`；同名时必须拒绝。
- 不要因企业微信代码已经完成而把它重新定义成个人微信的等价产品。

## 当前工作树和验证状态

编写本文时工作树包含尚未提交的 Windows pywechat 只读探针相关变更：

```text
M  .gitignore
M  PLAN.md
M  README.md
M  docs/operations.md
M  pyproject.toml
M  src/lemonbot/cli.py
M  uv.lock
?? docs/research-handoff.md
?? src/lemonbot/connectors/pywechat_probe.py
?? tests/unit/test_pywechat_probe.py
```

这些改动属于当前研发成果，不应被 `git reset --hard` 或覆盖。最后一次完整验证结果为
`238 passed, 3 skipped`，变更源码的 mypy 检查通过；接手后仍应在自己的 checkout 重新运行：

```powershell
uv sync --all-extras --locked
uv run pytest
uv run ruff check .
uv run mypy src
```

上游 `Hello-Mr-Crab/pywechat` 的浅克隆曾因网络连接重置失败；研究结论来自 GitHub 当前源码、
提交、Issue 和 PyPI 页面。`.vendor-review/` 只是被忽略的审计临时目录，不能被视为已固定或
已审核的依赖。

## Linux 路线的完成定义

只有满足以下条件，才可以把 Linux 路线从“突破口”升级为“实验连接器”：

- 官方客户端在目标虚拟机稳定运行，包来源、版本和 SHA-256 已登记。
- 无 Frida、ptrace、Hook、数据库解密和超额容器权限。
- AT-SPI 能事件化读取白名单会话的入站消息，并能给出来源与方向。
- 目标会话在发送前后可唯一证明；同名和不可见目标安全停止。
- 发送后可以回读自己的精确气泡；不确定状态正确进入 `unknown`。
- 锁屏、升级、登录验证、VM 挂起和结构变化全部 fail closed。
- 观察和草稿阶段分别完成测试，不能跳级到回复。
- 回复配额、静默期、暂停、急停和出站崩溃测试继续通过。
- 至少用独立测试号持续运行 24–72 小时，没有重复发送或错误会话发送。

在此之前，README 或发布说明不得宣称 Lemonbot 已支持生产级个人微信自动聊天。

## 关键资料

- 当前上游 Windows UIA 研究：[Hello-Mr-Crab/pywechat](https://github.com/Hello-Mr-Crab/pywechat)
- 上游对微信 4.x UI 树限制的说明：
  [Weixin4.0.md](https://github.com/Hello-Mr-Crab/pywechat/blob/main/Weixin4.0.md)
- Windows 误发问题：[pywechat Issue #286](https://github.com/Hello-Mr-Crab/pywechat/issues/286)
- Linux AT-SPI 可行性证据：[thisnick/agent-wechat](https://github.com/thisnick/agent-wechat)
- Linux 可视区域发送问题：
  [agent-wechat Issue #173](https://github.com/thisnick/agent-wechat/issues/173)
- Qt Accessibility：[QAccessible](https://doc.qt.io/QT-6/qaccessible.html)
- AT-SPI 事件接口：
  [Atspi.EventListener](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/class.EventListener.html)
- 微信及腾讯协议入口：[腾讯政策与协议](https://www.tencent.com/zh-cn/policies/)
