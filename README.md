# Lemonbot 2026

> 通道研发状态（2026-08-29）：企业微信不符合最终的个人聊天目标；现代 Windows 微信在
> 当前测试账号上没有暴露可用 UIA 树。官方 Linux 微信 4.1.1.8 已在 Ubuntu 24.04 Wayland
> 上通过纯 AT-SPI 只读快照验证，但消息语义和发送路径尚未开放。完整证据和接手步骤见
> [研发沿革与工程交接](docs/research-handoff.md)。

Lemonbot 当前实现是一个面向 Windows 11 x64 的、能力受控的自主聊天代理。它以 DeepSeek
API 作为默认文本与工具模型；现有企业微信和个人微信 UI Automation 连接器予以保留，但
不再把企业微信视为最终默认通道，个人微信连接器仍完全隔离并默认关闭。

> 这不是“让模型随意控制电脑”的程序。所有外部动作都必须经过独立策略引擎；支付、
> 转账、购买、订阅、账号安全、凭据、提权、安装、永久删除、任意命令执行及批量外发
> 永久禁止。个人微信自动化仍有非零风控和封号风险。

## 当前能力

- 持久化 inbox/outbox、逐会话串行处理、去重、审计和崩溃恢复。
- DeepSeek 优先的 OpenAI-compatible 模型网关及硬预算。
- SQLite WAL、FTS5 长期记忆、摘要和来源追踪。
- 企业微信官方长连接适配器、假连接器和个人微信实验性 UIA 适配器。
- 隔离工作进程中的只读 HTTPS 浏览器与图片净化/OCR/智谱视觉、路径受限文件保险库。
- 附件写入前磁盘余量熔断，默认保留至少 1 GiB，不自动清理原始数据。
- 固定清单 MCP、白名单、静默期、限频、主动任务来源约束和全局急停。
- 仅监听回环地址的本地管理台、诊断、备份、数据导出与显式删除命令。

## 快速开始

需要 Windows 11 x64、Python 3.12 x64 和 [uv](https://docs.astral.sh/uv/)。

```powershell
uv sync --all-extras
uv run playwright install chromium
Copy-Item config/lemonbot.example.toml "$env:LOCALAPPDATA\Lemonbot\config.toml"
uv run lemonbot doctor
```

密钥不会写入 TOML。Windows 使用 Credential Manager；Linux 使用已由图形登录解锁的
Freedesktop Secret Service，后台进程不会弹出解锁提示或退回明文文件：

```powershell
uv run lemonbot secret set deepseek_api_key
uv run lemonbot secret set zhipu_api_key
```

先在配置中填写模型价格和每日/月度预算，再启动：

```powershell
uv run lemonbot run
```

默认管理地址为 `http://127.0.0.1:8765`。初次登录令牌只会显示在本机控制台或托盘，
不会写入日志。没有真实企业微信凭据时，可将 `runtime.connector = "fake"` 进行端到端验证。

## 个人微信实验门禁

个人微信只允许 `profile = "lab"` 和独立测试号，并依次提升以下四个阶段：

- `observe`：只持久化观察到的事件，不调用模型、不创建 outbox、不发送。
- `draft`：生成并保存供人工检查的草稿，不创建 outbox、不操作发送控件。
- `reply`：只回复白名单会话中已经观察到的入站消息，禁止主动发送。
- `proactive`：在 `reply` 之上允许具备来源事件的已授权主动任务，仍受静默期和配额约束。

UIA 没有通用选择器。请复制
[`config/wechat_uia_selectors.example.json`](config/wechat_uia_selectors.example.json)，在目标虚拟机上完成账号、客户端版本和 UI 树登记后再启用。样例中的 `__ENROLL__` 值故意不能匹配真实控件；未登记时会安全停止。详见[运行手册](docs/operations.md#个人微信-uia-登记与分阶段上线)。

微信 4.x 可先用审计过的 pywechat selector 子集执行只读兼容性探测；该命令不会导入上游
PyAutoGUI/剪贴板动作，也不会点击、输入或发送：

```powershell
uv run lemonbot uia pywechat-probe --process-name Weixin.exe
```

配置中的 `stage` 只是管理员请求的上限。实际阶段还受 lab 数据库中的持久化门禁约束，首次
运行和任何登记指纹变化都会回到 `observe`。每次只允许晋级一级：

```powershell
uv run lemonbot uia promote --to draft --config <path> --confirm
```

## 管理员数据操作

停止 Lemonbot 后，可以导出当前 profile 的一致性数据归档，或显式永久删除一个精确会话：

```powershell
uv run lemonbot data export --config <path> --output <archive.zip>
uv run lemonbot data delete-conversation <channel> <chat-id> --config <path> --confirm
```

导出沿用可校验的 backup format v1，包含当前 profile 的 SQLite 原始记录与附件对象，但不含
Credential Manager、配置或日志。删除命令只存在于管理员 CLI，不注册为模型工具；详情和
不可恢复边界见[运行手册](docs/operations.md#管理员数据导出与显式删除)。

## 安全边界

- 生产与实验通道使用不同数据库、附件目录、白名单和密钥命名空间。
- 网页、聊天、OCR、图片和工具结果一律视为不可信内容。
- 浏览器只允许公开 HTTPS GET/HEAD，阻断私网、回环地址、异常端口和重定向绕过。
- 个人微信仅支持独立测试号；不含 Hook、DLL 注入、协议逆向、WCFerry 或风控规避。
- 任何出站状态不确定的消息进入 `unknown`，不自动重发。

停止服务并人工核对真实会话后，可使用 `lemonbot outbox unknown` 和
`lemonbot outbox resolve` 关闭不确定记录；该流程不会把记录重新排队。

更多部署和威胁模型见 [docs/operations.md](docs/operations.md)、
[docs/security.md](docs/security.md) 与 [docs/research-handoff.md](docs/research-handoff.md)。

## 开发

```powershell
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mypy src
```

GitHub CI 在 Windows Server 2025 和 Ubuntu 24.04 / Python 3.12 上按 `uv.lock` 安装并执行
pytest 与 Ruff；mypy 由 Windows job 执行。
另有只扫描当前检出树的 Gitleaks 作业。旧 Git 历史按约定不改写，因此历史审计与当前树
门禁应分开理解。

旧版原型中的密钥、私钥、浏览器扩展、驱动和日志已从当前工作树移除；按项目约定未改写
旧 Git 历史。旧凭据即使已失效，也不应再次使用。

## Linux 微信只读探针

Ubuntu 24.04 安装发行版的 `python3-gi`、`gir1.2-atspi-2.0` 和 `at-spi2-core`，为官方微信
启用 Qt accessibility 后，可在本地图形会话执行：

```bash
uv run lemonbot channel linux-atspi-probe
```

命令只输出结构、接口和固定控件计数，不输出聊天文本，也不执行任何动作。当前它只是
connector 上线前的 `observe` 探针，不会接入 DeepSeek 或发送微信消息。
