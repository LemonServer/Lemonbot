# 安全模型

Lemonbot 假定聊天消息、网页、附件、OCR、视觉描述、模型输出和工具结果均可能包含恶意
提示。LLM 只能提出动作，不能批准动作；目标会话、联系人身份、数据范围和预算由核心进程
绑定，不能由模型参数覆盖。

## 永久禁止

支付、购买、转账、订阅、红包、凭据和 MFA、OAuth 授权、账号安全设置、安装、提权、
注册表或防护设置修改、模型/工具发起的永久删除、任意 Shell/代码执行、批量发送、自动
加好友、拉群与 `@所有人`。这些规则不能由联系人、管理员聊天消息、网页或模型绕过。
唯一的数据删除入口是服务离线时由本机管理员直接运行、带 `--confirm` 的 CLI；它不注册为
模型工具，也不提供 HTTP 接口。

## 通道与数据

- `prod` 只运行官方企业微信通道，`lab` 只运行个人微信 UIA 实验通道。
- 两者使用不同数据库、对象目录和 Credential Manager 命名空间。
- 数据等级为 `PUBLIC`、`CONVERSATION`、`PRIVATE_LOCAL`、`SECRET`。
- 默认只允许检索当前通道和当前稳定会话 ID 的数据；`SECRET` 不进入提示词。

## 个人微信

UI Automation 不代表腾讯授权，也不能保证账号安全。该适配器默认关闭，只能用于独立
测试号，并按观察、草稿、被动回复、主动消息逐级开放。登录验证、锁屏、版本或控件树
变化、同名会话、目标不确定或发送结果不明时一律停止。不实现 Hook、注入、协议逆向、
客户端降级或风控规避。

## 文本模型工作进程

DeepSeek/OpenAI-compatible 文本模型可以通过 `IsolatedModelBackend` 放入独立工作进程。
核心进程只持有 `ProviderConfig.secret_name` 这一 Credential Manager 查找名和持久化预算器；
实际密钥由子进程按 `prod`/`lab` 命名空间读取，不通过参数、环境变量或 IPC 返回。当前隔离
只覆盖文本 `ModelBackend`，视觉后端仍需单独迁移，不能误认为已经隔离。

集成入口如下；创建后将 `model.aclose` 注册到运行时关闭流程即可：

```python
from lemonbot.models import IsolatedModelBackend, ModelWorkerConfig

model = await IsolatedModelBackend.create(
    config=ModelWorkerConfig(
        profile=settings.profile,
        provider=provider_config,
        verify_models_on_startup=True,
    ),
    budget=persistent_budget,
)
```

代理在调用前由核心预算器预留最坏成本。IPC 超时、取消、损坏响应或子进程中断都使该 worker
永久失效；调用状态按未知处理并保守计费，不自动重启、重试或切换模型。通信只接受 1 MiB
以内的长度前缀 JSON 和严格 Pydantic 模型，stderr 被丢弃且不会写入日志。Windows Job
Object 限制内存和进程生命周期；Python 可执行文件与模块参数由代码固定，不能来自聊天或
模型输出。

## 固定 MCP 能力

MCP 默认整体关闭；每个 server 和每个 tool 也分别默认关闭。启用项必须固定可执行文件的
绝对路径和 SHA-256、无 shell 的参数数组、工作目录、MCP 协议版本、服务端返回的精确名称
与版本，以及本地 JSON Schema。进程由 `WorkerSupervisor` 创建并纳入 Windows Job Object；
只继承最小系统环境，核心不会通过参数、环境或 IPC 向它传递 API Key，并在服务关闭时连同
子进程一起终止。Job Object 不是 Windows 身份或文件权限沙箱；MCP 仍以 Lemonbot 的 Windows
用户运行，因此只能登记已审核、可信的服务，并应在专用虚拟机和专用低权限用户中运行。

每个已登记工具映射为 `mcp.<server>.<local-tool>`。`read_only=true` 映射到 `mcp_read`，只在
固定 scope 已由核心授予时自动执行；写工具映射到 `mcp_write`，每次都进入持久化的一次性
审批。`enrolled`、`approved`、permission 与 scope 等代理自有字段既不能出现在工具 schema
中，也不能由模型参数传入。工具返回最多保留配置的字节数，带有明确的 `UNTRUSTED` 标记；
错误正文、stderr 和服务端异常数据不会写入聊天或日志。写调用超时、管道损坏或服务端报告
错误时按副作用状态未知处理，不能自动重试。

## 漏洞报告

报告中不要附带真实 API Key、聊天记录、Cookie 或账号信息。发现疑似密钥泄漏时，先在
服务端撤销并轮换凭据，再进行调查。
