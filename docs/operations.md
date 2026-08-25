# 运行手册

## 推荐部署

- Windows 11 x64 虚拟机，Python 3.12 x64。
- 固定时区 `Asia/Shanghai`，开启系统更新和磁盘加密。
- UIA 实验需要交互桌面保持解锁、固定 DPI/分辨率并禁用休眠；不要使用生产微信账号。
- 数据目录默认 `%LOCALAPPDATA%\Lemonbot`，不要放在网络共享目录。

## 启动前门禁

1. `lemonbot doctor` 全部必需检查通过。
2. 在 Credential Manager 中配置对应 profile 的密钥。
3. 云 API 的价格、每日和月度硬上限均为正数后，才能设置 `budget.enabled=true`。
4. 先使用 `fake` 连接器验证，再启用企业微信；个人微信从 `observe` 开始。
5. 备份 `prod.db`、`lab.db` 与对象目录，执行一次恢复演练。

## 个人微信 UIA 登记与分阶段上线

个人微信自动化只用于 `profile = "lab"` 的独立测试号。它不能保证零封号风险，也不使用
Hook、注入、协议逆向、客户端降级或风控规避。微信出现登录验证、锁屏、升级、同名会话、
控件漂移或目标不唯一时，应保持停止状态，不要通过放宽选择器继续运行。

### 登记环境

1. 在固定的 Windows 11 虚拟机用户下安装并登录测试微信，只保留一个匹配
   `expected_process_name` 的进程和一个主窗口。固定客户端版本、DPI、缩放、主题与分辨率。
2. 将 `config/wechat_uia_selectors.example.json` 复制到 lab 的受控配置位置，例如
   `%LOCALAPPDATA%\Lemonbot\wechat_uia_selectors.json`。不要直接修改仓库样例；登记期间保持
   `wechat_uia.enabled = false` 且 `runtime.connector = "fake"`。
3. 使用本机只读 UIA 检查工具逐项登记精确的 `ControlTypeName`、`AutomationId`、
   `ClassName` 或必要的 `Name`。选择器的所有已填写字段都必须精确相等；避免只用本地化
   文本，也不要把可变消息正文写进选择器。
4. 将 `chat_targets` 的键设置为 Lemonbot 使用的稳定会话 ID，值设置为微信界面中的精确
   会话标签；同名或搜索结果不唯一的会话不能登记。配置中的 `allow_chat_ids` 必须使用相同
   的稳定 ID。
5. 运行 `lemonbot uia inspect` 读取本机登记状态。将规范化的可执行文件绝对路径和内容哈希
   分别写入 `expected_executable_path`、`expected_executable_sha256`，将客户端文件版本写入
   `enrolled_client_version`，将结构签名写入 `enrolled_selector_signature`。路径、哈希、版本
   或结构签名任一变化都必须停机复核，并重新从 `observe` 开始验证。

样例里的每个控件都包含 `__ENROLL__` 占位，因此故意无法匹配真实 UI 树。删除占位并不
等于完成登记；只有能够唯一定位所有必需控件、确认输入框支持可写 `ValuePattern`、并通过
人工回读测试的选择器才可使用。选择器文件不得包含密码、Cookie、会话正文或 API 密钥。

### 账号哈希

`wechat_uia.expected_account` 保存的不是微信号明文，而是账号身份控件 `Name` 属性经过
以下算法得到的 64 位小写十六进制值：

```text
sha256(str(Name).strip().encode("utf-8")).hexdigest()
```

`lemonbot uia inspect` 只输出上述账号哈希、可执行文件路径和哈希、版本、窗口句柄、锁屏
状态和结构签名，不输出账号显示名或其他 UI 文本。只在离线本机计算或比对账号值，确认前后
没有不可见空白；不要把原始 `Name` 写入 TOML、selector 文件、日志、截图文件名或工单。
账号哈希只用于阻止误账号操作，不应被当作密码学匿名化。可执行文件必须位于本地盘的绝对
路径；UNC、设备路径、相对路径、符号链接和 junction 都不能登记。

### 四阶段门禁

阶段只能按顺序提升，每次升级前停止进程、备份 lab 数据库并人工复核上一阶段的审计：

1. `observe`：只接收入站事件并写入审计；不调用模型、不创建草稿/outbox、不点击发送。
2. `draft`：允许模型生成持久化草稿供人工检查；不创建 outbox，也不操作微信发送控件。
3. `reply`：只对本次观察到、目标唯一且位于白名单的入站事件发送被动回复；主动任务拒绝。
4. `proactive`：额外允许管理员日程、用户订阅或已保存承诺产生的任务；每个任务必须携带
   `reason_event_id`，且在提交前再次经过目标绑定、静默期、配额和策略检查。

建议至少完整运行一个业务周期的 `observe`，再用无敏感内容的固定样本验证 `draft`。
任何客户端可执行路径或哈希、Windows 用户、账号哈希、窗口句柄或 selector 签名与登记值
不符时均应回退到 `observe`，不能把 `reconcile_seconds` 调到 5 秒以下，也不能用坐标点击
或剪贴板作为回退。

`wechat_uia.stage` 仅表示配置请求的最高阶段，不能自行解锁发送。Lemonbot 在 `lab.db` 保存
独立的登记指纹和已验证阶段；首次运行、账号/用户/可执行文件/版本/selector/白名单任一登记
变化都会自动重置为 `observe`。完成上一阶段复核后，先把 TOML 的 `stage` 精确改为下一阶段，
保持微信已登录且桌面解锁，再运行：

```powershell
uv run lemonbot uia promote --to draft --config <path> --confirm
uv run lemonbot uia promote --to reply --config <path> --confirm
uv run lemonbot uia promote --to proactive --config <path> --confirm
```

每次命令都会获取运行锁、执行实时只读 UIA preflight，并且只接受紧邻的下一阶段；不能跳级。
晋级失败时不会改变已验证阶段。客户端或登记发生变化后必须重新从 `draft` 逐级验证。

### 启用配置核对

```toml
profile = "lab"

[runtime]
connector = "wechat_uia"

[wechat_uia]
enabled = true
stage = "observe"
expected_account = "<64 lowercase hex characters>"
expected_windows_user = "<exact Windows user>"
expected_process_name = "WeChat.exe"
expected_executable_path = "C:\\Program Files\\Tencent\\WeChat\\WeChat.exe"
expected_executable_sha256 = "<64 lowercase hex characters>"
enrolled_client_version = "<exact file version>"
enrolled_selector_signature = "<64 lowercase hex characters>"
selector_bundle_path = "C:\\Users\\<user>\\AppData\\Local\\Lemonbot\\wechat_uia_selectors.json"
allow_chat_ids = ["<stable-chat-id>"]
reconcile_seconds = 15
```

登记 selector 文件后执行只读检查；配置文件不是 lab profile 时命令会拒绝：

```powershell
uv run lemonbot uia inspect --config <path>
uv run lemonbot doctor --config <path>
```

不要在微信未登录、桌面锁定或控制树尚未稳定时启动 `lemonbot run`。

## 固定 MCP 服务登记

Lemonbot 配置格式是 TOML。先保持 `[mcp].enabled = false`，把 MCP 服务安装到仅管理员可写的
本地目录，离线审核其来源和能力，然后记录可执行文件摘要：

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath "C:\Program Files\ExampleMCP\example-mcp.exe"
```

参照 `config/lemonbot.example.toml` 添加一个 `[[mcp.servers]]` 和固定工具表。必须填写无 shell
的 `arguments` 数组、绝对工作目录、64 位小写 SHA-256、初始化时应返回的精确 server name / 
version，以及根类型为 object 且 `additionalProperties=false` 的 JSON Schema。不要把密钥写入
参数、工作目录文件、schema 或描述；核心不会通过参数、环境或 IPC 向 MCP worker 传递
Lemonbot 的 Credential Manager 密钥。Job Object 只限制生命周期、进程数和内存，不隔离同一
Windows 用户可访问的文件或凭据，所以必须使用专用低权限虚拟机账号并只登记可信服务。

登记顺序为：先启用具体 tool，再启用 server，最后启用 `[mcp]`。启动时任何路径、摘要、协议
版本或服务身份不匹配都会拒绝注册并使启动失败；不会退回 PATH 搜索，也不会自动接受
`tools/list` 中新发现的能力。只读工具获得自己唯一的 read scope；写工具没有自动 scope，
模型请求后必须由本地管理台对精确参数执行一次性审批。升级 MCP 服务后必须停机、重新审核、
更新摘要和期望版本；不能只放宽版本匹配。

输出全部按不可信数据处理并截断到 `max_output_bytes`。如果写调用超时、子进程退出或结果为
错误，审批状态进入 `unknown`；先人工核对外部系统，不要重新批准同一动作。紧急停止或正常
关机都会通过 Job Object 收回 MCP 子进程。

## 备份与恢复

运行时创建一致性备份：

```powershell
uv run lemonbot backup --config <path>
```

恢复必须在 Lemonbot 完全停止时执行，并会先保留当前状态：

```powershell
uv run lemonbot restore <archive.zip> --config <path> --confirm
```

## 管理员数据导出与显式删除

这些命令只存在于本机管理员 CLI，不是模型工具，也没有管理台 HTTP 接口。两者都会获取
当前 profile 的运行锁；如果 Lemonbot 或另一个数据操作仍在运行，命令会拒绝执行。先从
托盘停止服务并确认进程退出，不要通过删除 `.lock` 文件绕过互斥。

### 导出当前 profile

```powershell
uv run lemonbot data export --config <path> --output D:\offline\lab-export.zip
```

导出复用 Lemonbot backup format v1：`manifest.json` 记录 profile、数据库摘要和大小，
`database/<profile>.db` 是通过 SQLite backup API 创建并通过完整性检查的一致快照，
`objects/` 保存该 profile 的附件内容对象。ZIP 成员使用固定相对路径；输出不能位于对象库
或隔离区，也不会覆盖已有文件。对象库中若存在非标准目录、符号链接、非 SHA-256 文件名或
内容摘要不匹配，导出会拒绝执行，避免把误放文件带入归档。

Windows Credential Manager、TOML 配置、selector 文件、日志、运行锁和环境变量不会进入
归档。不过原始聊天、审计、记忆来源/模型/提示词版本元数据、审批的完整工具参数、任务以及
附件本身可能含用户提供的敏感内容；“不含密钥”仅指 Lemonbot 管理的服务凭据。归档应按
敏感个人数据加密、离线保存并限制访问。恢复可直接使用同版本的 `lemonbot restore`。

### 永久删除一个会话

这是破坏性操作，不会自动创建备份。若删除属于一般数据整理，可先执行导出；若目的是隐私
清除，不要创建新的副本，并同时处置所有已有备份、旧导出、虚拟机快照和宿主机备份。

先从 TOML 白名单和 UIA selector 的 `chat_targets` 中移除目标，停止服务，再使用稳定 ID：

```powershell
uv run lemonbot data delete-conversation wecom <stable-chat-id> `
  --config <path> --confirm
```

命令在一个 `BEGIN IMMEDIATE` 事务中删除该精确 `(channel, chat_id)` 的 inbox、outbox、
messages、drafts、approvals、memory、attachments、proactive jobs、allowlist 和原会话审计。
若未来 schema 出现尚未支持的会话作用域表，命令会拒绝执行，避免留下半删除的数据。提交
前会重建 FTS；提交后启用 SQLite `secure_delete`、截断 WAL、执行 `VACUUM` 和完整性检查。

附件对象只有在 `attachments` 中不再被任何其他会话引用时才会删除。路径、摘要或符号链接
校验失败时不会扩大删除范围；若数据库已提交但对象删除因文件系统错误失败，命令以非零状态
退出并要求离线人工核对。

原会话审计会被删除，随后保留一条 `data.delete_conversation` 管理员审计摘要。摘要只包含
操作 ID、各表删除行数及对象清理计数，`chat_id`、消息正文、事件 ID、附件摘要及联系人
显示名均不写入该摘要。若对象清理失败，审计 outcome 为 `partial`。CLI 输出同样只给出操作
ID 和计数。已经存在的归档、旧日志、Git 历史和
外部供应商数据不会被该命令触碰，必须由管理员分别按其保留策略处理。

## 一次性审批状态

`APPROVE_ONCE` 请求会持久化绑定当前 profile、channel、稳定 chat ID、来源 event ID、固定
tool name、action kind、参数结构摘要、完整参数和规范 JSON 的 SHA-256。待审批列表只返回
结构摘要与摘要哈希，不返回完整参数值；完整参数只能由成功的执行 claim 取得。不要把数据
库导出或调试查询的完整参数复制到管理台、日志或工单。

管理员批准会在执行前把 `pending` 原子转换为 `executing` 并签发不可猜测的 claim token；
同一请求不能被两个执行者消费。已知成功记为 `approved`，明确未提交或管理员拒绝记为
`denied`，副作用结果不确定记为 `unknown`。过期请求按 `denied/expired` 关闭。进程恢复时
所有遗留的 `executing` 必须直接转为 `unknown`，绝不重新排队；相同 event、tool、action 和
完整参数哈希的唯一约束也会阻止以新 approval ID 绕过这一规则。

## 不确定出站消息的人工核对

连接器超时、进程在 `dispatching` 时中断或 UI 回读不确定时，outbox 固定进入 `unknown`，不会
自动重试。先停止 Lemonbot，在对应稳定会话中人工核对消息 ID、来源事件、时间和实际内容；
CLI 列表故意不打印消息正文：

```powershell
uv run lemonbot outbox unknown --config <path>
uv run lemonbot outbox resolve <item-id> --as acknowledged `
  --note "已在目标会话确认精确消息" --config <path> --confirm
uv run lemonbot outbox resolve <item-id> --as dead `
  --note "确认未送达并决定放弃，不再发送" --config <path> --confirm
```

`acknowledged` 表示管理员确认已经送达；`dead` 表示终止该消息。两者都是终态，均不能恢复为
`pending`，重复核对也不会覆盖第一次结果。每次操作都会写入 `outbox.reconcile` 审计。

## 事故处理

- 误发风险：立即点击托盘“紧急停止”，保留数据库和日志，不删除 `unknown` outbox。
- API Key 泄漏：在供应商控制台撤销，然后更新 Credential Manager；不要只修改配置。
- 个人微信风控：停止实验 worker，不重试登录或尝试绕过验证。
- 磁盘不足：系统会暂停大附件；扩容或显式导出后由管理员决定删除，程序不自动清理。
- `dispatching` 状态崩溃：重启后标记 `unknown`，由管理员核对真实会话后处理。

## 附件磁盘容量熔断

附件对象库默认要求所在数据盘在完成一个新对象写入后仍至少保留 1 GiB。每次新附件写入前
都会检查 `free_bytes >= 1 GiB + new_object_bytes`；检查发生在创建临时对象文件之前，并由
附件存储锁串行化，避免本进程内多个附件同时越过保留线。内容已存在时不重复计算对象空间，
但熔断已经触发后仍保持暂停，不能靠重复内容绕过状态。

空间不足、无法读取磁盘用量或底层返回磁盘满/配额满时，附件存储抛出明确的
`AttachmentCapacityError`，并通过 `capacity_status` 提供 `paused`、剩余字节、保留字节、
本次所需字节、原因和触发时间。熔断是锁存的：即使空间随后增加，新附件仍被拒绝，直到
管理员扩容或释放明确选定的数据，并调用受控的 `recheck_capacity()`；当前部署可通过停止
服务、处理空间问题并重启来完成同等的重新检查。不要删除 `.lock`、数据库、未知对象或历史
记录来尝试解除熔断。

熔断只阻止新的附件对象进入隔离库；现有附件读取和纯文本消息不受影响。Lemonbot 不会为了
恢复空间而自动删除消息、附件、备份或其他历史。若需要清除数据，只能使用管理员显式删除
流程并理解其备份边界。

`lemonbot doctor` 的 `data-disk-free` 项报告当前 profile 数据盘余量和 1 GiB 附件保留线。
低于保留线时显示 `WARN` 而不是阻止整个文本服务启动；在重新启用附件前必须先解决该警告。
运行中的锁存状态、原因和最近剩余字节同时显示在本地管理台 `/api/status`；附件失败不会阻断
纯文本处理，但最终回复会明确说明有附件未能安全接收或保存。

## CI 与密钥门禁

`.github/workflows/ci.yml` 使用 Windows Server 2025、Python 3.12 和锁定版本的 `uv`，执行
`uv sync --all-extras --locked`、pytest、Ruff 与 mypy。第三方 Actions 均固定为完整提交
SHA，以减少可变标签带来的供应链风险。

Gitleaks 作业故意只检出和扫描当前 revision。旧原型的 Git 历史按项目约定不改写，不能
把“当前树扫描通过”理解为历史中从未存在过凭据；发布仓库前仍应撤销全部旧凭据，并根据
托管平台能力对历史仓库设置访问控制或执行单独的人工历史审计。不要为测试字符串添加宽泛
allowlist，也不要把扫描结果中的疑似密钥复制到 issue 或日志。
