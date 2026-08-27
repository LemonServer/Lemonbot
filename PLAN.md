# Lemonbot 2026 重构计划（DeepSeek API 优先）

> 历史基线说明（2026-08-27）：本文保留最初获批的绿地重构方案。核心架构和安全边界仍然
> 有效，但“企业微信作为生产通道”和“Windows 个人微信 UIA 为主要实验路线”已被后续实证
> 调整。当前通道结论、Windows 失败实验和 Linux AT-SPI 下一步见
> [`docs/research-handoff.md`](docs/research-handoff.md)。

## 总体方案

将现有代码视为原型并进行绿地重构，目标是在 Windows 11 x64 虚拟机中运行一个具备长期记忆、网页访问、图片理解和受控工具调用能力的自主聊天代理。

采用双通道隔离：

- 生产通道：企业微信智能机器人，使用[官方 Python SDK](https://github.com/WecomTeam/wecom-aibot-python-sdk)。
- 实验通道：独立测试账号上的个人微信 Win32/UI Automation。
- 两个通道使用独立数据库、附件库、密钥和白名单，禁止自动共享信息。
- 个人微信自动化无法保证零封号风险；微信现行规则禁止未经授权的自动操作，因此不使用 Hook、注入、协议逆向、WCFerry、旧客户端降级或 Web 微信模拟方案。[腾讯协议入口](https://www.tencent.com/zh-cn/policies/)。

“完全自主”定义为在预先授权的能力沙箱中自主规划和执行，而不是获得任意系统权限。支付、转账、购买、订阅、账号安全设置、凭据/MFA、软件安装、提权、永久删除、任意 Shell/代码执行、批量加人或群发全部硬拒绝。

## 技术架构与接口

### 运行栈

- Python 3.12 x64、`uv` 锁定依赖、`asyncio`。
- Pydantic v2、FastAPI、Jinja2/HTMX、SQLAlchemy 2、Alembic、SQLite。
- Playwright 负责隔离网页访问；当前版本以 Windows 11 为支持基线，不再支持 Windows 7。[Playwright 系统要求](https://playwright.dev/docs/intro)。
- RapidOCR/ONNX Runtime 负责本地 OCR。
- `uiautomation + pywin32` 直接实现个人微信适配。
- `pystray` 提供托盘状态、暂停和紧急停止。
- 不引入 LangChain/LlamaIndex，核心调度、策略和记忆链路保持可审计。

事件链路固定为：

`连接器事件 → 持久化 inbox → 单会话 FIFO/防抖 → 上下文与记忆检索 → 模型 → 独立策略判定 → 工具执行 → 输出限流 → 持久化 outbox → 发送 → 回执、审计与记忆更新`

核心进程拥有数据库、队列、策略和任务状态；企业微信、个人微信 UIA、模型、浏览器和视觉分别运行在受限工作进程中。进程使用长度前缀 JSON 管道通信，所有消息经过 Pydantic 校验，不使用 Pickle、内部开放端口或 Shell 拼接，并用 Windows Job Objects 限制子进程生命周期。

公开稳定接口：

```python
class Connector:
    async def events(self) -> AsyncIterator[InboundEvent]: ...
    async def deliver(self, message: OutboundMessage) -> DeliveryReceipt: ...
    async def health(self) -> ConnectorHealth: ...

class ModelBackend:
    async def generate(self, request: ModelRequest) -> ModelResponse: ...
    async def embed(self, texts: list[str]) -> list[list[float]] | Unsupported: ...
    def count_tokens(self, messages: list[Message]) -> int: ...
    def capabilities(self) -> ModelCapabilities: ...

class Tool:
    def manifest(self) -> ToolManifest: ...
    async def invoke(self, context: ToolContext, arguments: dict) -> ToolResult: ...

class Policy:
    async def evaluate(self, action: ProposedAction) -> PolicyDecision: ...
```

`PolicyDecision` 只有 `AUTO`、`APPROVE_ONCE`、`ENROLL`、`DENY`。任何具有外部副作用的动作在真正提交前必须再次判定，模型不能修改联系人身份、目标会话、权限或策略结果。

### DeepSeek 优先模型网关

默认模型供应商为 DeepSeek，使用 OpenAI Chat Completions 兼容接口和 `https://api.deepseek.com`：

- `deepseek-v4-flash`：普通聊天、摘要、事实提取、记忆整理、意图和路由判断。
- `deepseek-v4-pro`：复杂工具规划、长时自主任务以及管理员显式 `/deep` 请求。
- 路由由确定性规则决定，模型不能自行升级到高成本模型。
- 不再使用已弃用的 `deepseek-chat` 或 `deepseek-reasoner`；模型名和能力启动时通过健康检查确认。[DeepSeek 官方文档](https://api-docs.deepseek.com/zh-cn/)。
- 工具参数始终执行本地 JSON Schema 校验，不依赖 beta 严格模式。[工具调用文档](https://api-docs.deepseek.com/zh-cn/guides/tool_calls)。
- 思考模式工具调用按官方协议在同一次调用链中回传必要字段，但隐藏推理不写入日志、数据库或聊天记录。[思考模式文档](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode)。
- 超时或失败只允许在尚未产生副作用前切换后端；副作用状态不明时不得重新规划或盲目重试。
- 保留通用 OpenAI-compatible 后端，后续可接 Ollama、llama.cpp server 或 LM Studio；本地模型最初只做兼容测试。[Ollama 兼容接口](https://docs.ollama.com/api/openai-compatibility)。

DeepSeek V4 按纯文本模型使用。图片流程为：

`附件隔离 → 文件头/尺寸/像素检查 → 解码沙箱 → 去元数据和缩放 → RapidOCR → 智谱视觉 → 作为不可信事实交给 DeepSeek`

视觉模型固定为智谱 `glm-4.6v-flash`。[模型文档](https://docs.bigmodel.cn/cn/guide/models/free/glm-4.6v-flash)。智谱不可用时退化到 OCR，并明确告诉用户无法完成语义读图，不虚构图像内容。

DeepSeek 与智谱密钥分别存入 Windows Credential Manager/DPAPI，不出现在配置文件、数据库、日志或管理页面响应中。启用云模型前必须配置每日和每月硬预算、单任务 token 上限及价格表；调用模型是唯一默认允许的金钱消耗，其他金融行为一律拒绝。

### 数据、记忆和上下文压缩

生产使用 `prod.db`，实验使用 `lab.db`。SQLite 开启 WAL、外键、迁移和定期一致性检查；搜索使用 FTS5 trigram/BM25。[WAL](https://www.sqlite.org/wal.html)、[FTS5](https://www.sqlite.org/fts5.html)。

永久保存：

- 原始事件、消息、回复和发送回执。
- 工具请求、策略决定、审批、失败和任务状态。
- 附件按 SHA-256 内容寻址存储。
- 最近对话、分段摘要、长期事实/偏好/承诺、工具执行经历。

派生记忆必须记录来源消息、模型、提示词版本、置信度和 `supersedes` 关系。上下文按“当前事件、最近轮次、相关摘要、相关事实、未完成承诺”组合，并在达到模型窗口阈值前压缩。跨联系人、跨群或跨生产/实验通道检索默认拒绝。

第一版不依赖向量数据库；FTS5 足以离线工作。以后配置独立 embedding 后端时，采用会话内精确余弦搜索与 BM25 的 RRF 融合。原始数据不自动删除，只有管理员可以导出或显式删除；模型没有删除权限。磁盘不足时报警并暂停大附件接收，不静默清理历史。

## 自主能力与微信行为

### 能力沙箱

- 浏览器：每任务创建非持久化 Playwright Context，不读取个人浏览器 Cookie；第一版仅允许公开 HTTPS GET/HEAD。
- 网络：阻止 localhost、私网、链路本地地址、非标准端口、`file:`/`data:`、DNS 重绑定和重定向绕过。
- 下载：进入隔离区，禁止执行；仅允许经过验证的文本、网页和图片进入模型。
- 文件：只访问管理员配置的目录；默认只读，写入必须新建或版本化，不能覆盖。
- MCP：仅加载管理员固定版本、固定命令和固定能力的服务器；新发现的工具默认禁用，必须映射为 `ToolManifest` 后才能使用。
- 网页、OCR、图片、聊天和工具输出全部视为不可信数据，不能把其中的指令提升为系统指令；遵循提示注入隔离原则。[OWASP 指南](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)。

数据分为 `PUBLIC`、`CONVERSATION(channel, chat_id)`、`PRIVATE_LOCAL`、`SECRET`。默认只能读取当前会话的数据，`SECRET` 永远不能发送给模型或联系人。

### 企业微信生产通道

- 官方长连接 SDK，支持单聊、群聊、媒体接收、重连和 `msgid` 去重。
- 第一版发送完整最终回复，不使用逐 token 流式刷屏。
- `enter_chat` 只发送管理员预先批准的固定欢迎语。
- 联系人和群以稳定 ID 识别，显示名称只用于展示。
- AI 始终透明表明机器人身份。

### 个人微信实验通道

- 默认关闭，仅允许独立测试账号。
- 使用 UIA 事件监听加低频校准扫描，OCR 仅作回退。
- 每次发送前校验当前 Windows 用户、微信账号、窗口句柄、目标会话和编辑框；发送后读取界面确认。
- 客户端升级、锁屏、登录验证、同名会话、控件漂移或目标不确定时立即停止。
- 上线门禁依次为：观察模式 → 只生成草稿 → 白名单被动回复 → 白名单主动消息。
- 不冻结或降级微信客户端，不模拟高频人工行为，不追求规避风控。

### 白名单、主动聊天与限额

时区固定 `Asia/Shanghai`，主动消息静默期为 23:00–08:00。

企业微信默认：

- 回复：每会话 10 分钟 6 条、每小时 30 条、每天 100 条；全局每天 500 条。
- 主动消息：每会话 6 小时 1 条、每天 3 条；全局每天 30 条。

个人微信默认：

- 回复：每会话 10 分钟 3 条、每小时 10 条、每天 30 条；全局每天 50 条。
- 主动消息初始关闭；通过门禁后每会话 12 小时 1 条、每天 2 条，全局每天 10 条。

单事件最多运行 5 分钟、8 次模型调用、20 次工具调用、10 次页面导航和 3 次下载。每个事件只生成一个逻辑回复，最多拆成两段，每段不超过 1500 字。

主动任务只能来源于管理员日程、用户订阅或对话中已保存的明确承诺，并必须携带 `reason_event_id`；AI 不能凭空创建自我唤醒循环。托盘和管理台提供全局暂停、按通道暂停和紧急停止。

Outbox 状态固定为：

`pending → reserved → dispatching → acknowledged`

异常进入 `unknown` 或 `dead`；`unknown` 必须人工核对，绝不自动重发。

## 实施顺序

1. 清理并搭建新工程：移除当前树中的旧密钥、私钥、CRX、ChromeDriver 和日志，建立锁定依赖、迁移、CI、密钥扫描和配置样例，但按用户要求不重写旧 Git 历史。
2. 完成核心事件模型、SQLite、inbox/outbox、策略引擎、审计、暂停机制和可崩溃恢复的假连接器纵向切片。
3. 接入企业微信官方 SDK，完成白名单、媒体隔离、去重、重连和发送回执。
4. 接入 DeepSeek 网关、确定性模型路由、预算器、工具调用协议和智谱视觉后端。
5. 完成分层记忆、上下文压缩、FTS5 检索、承诺追踪和数据导出/显式删除。
6. 加入受限 Playwright、图片/OCR、文件保险库和固定 MCP 工具。
7. 加入事件驱动的主动消息调度、静默期、配额和失败恢复。
8. 实现个人微信 UIA，并逐级通过观察、草稿、回复和主动发送门禁。
9. 完成本地管理台、托盘、备份恢复、运行手册及命令：
   `lemonbot doctor|run|backup|restore|install-startup`。

管理台只监听 `127.0.0.1`。托盘产生一次性登录令牌，换取 HttpOnly/SameSite Cookie；校验 Host、Origin 和 CSRF，不提供公网部署模式。

## 测试与验收

自动测试包括：

- DeepSeek 模拟服务器覆盖普通文本、JSON、工具调用、思考模式、限流、超时和模型不可用。
- 智谱视觉模拟服务器覆盖图片、OCR 回退、损坏图片和超大像素图片。
- 策略属性测试证明未知权限默认拒绝，硬禁止行为无法被提示词绕过。
- 企业微信脱敏事件覆盖重复消息、乱序、断线重连和媒体失败。
- 假 UIA 树覆盖 DPI、主题、窗口切换、锁屏、客户端版本变化、同名联系人和发送结果未知。
- 安全测试覆盖聊天/DOM/OCR 提示注入、SSRF、DNS 重绑定、路径逃逸、图片炸弹、密钥诱饵以及跨联系人和跨通道泄漏。
- 在 outbox 每个状态强制杀进程，验证不会丢失记录或重复发送。
- 故障测试覆盖数据库忙、磁盘不足、模型中断、浏览器崩溃、虚拟机挂起和 UIA 失效。
- 使用用户提供的企业微信环境和个人微信测试号分阶段联调，最终持续运行 24–72 小时。

完成标准：

- 重启后能恢复对话、记忆、任务和未确认发送状态。
- 上下文始终不超过模型限制，历史事实和承诺可带来源召回。
- 白名单、频率、静默期、预算和暂停均在副作用前生效。
- 能阅读安全网页和图片，但网页内容不能扩大权限或泄露数据。
- 已确认消息不重复发送，发送状态不明时停止。
- UIA 无法证明目标正确时不操作。
- 磁盘和数据库中不存在明文 API 密钥。
- 原始数据永久保留；空间不足时报警和限流，不自动删除。

## 已确定的默认假设

- 部署环境改为 Windows 11 x64 虚拟机，不支持 Windows 7。
- DeepSeek 是首选文本和工具模型；智谱 `glm-4.6v-flash` 是首选视觉模型。
- 云 API 优先，本地模型仅保留兼容接口，初期不作为上线前提。
- 生产聊天渠道是企业微信智能机器人，不是个人微信客服接口。
- 个人微信仅用独立测试号并接受非零封号风险。
- 已暴露的旧密钥已失效；清理当前代码树，但不改写 Git 历史。
- 用户将提供企业微信测试环境和个人微信测试账号。
- 管理台不对局域网或公网开放。
