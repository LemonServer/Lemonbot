# Lemonbot

正在推进 testing 真 @ 回复与一个白名单私聊：新增默认关闭的 conversation 模式，
接收采用数据库变更通知唤醒增量读取，支持 DeepSeek Flash、持久上下文与官方余额查看。
自动化检查与实机预检已通过；首批消息验收未通过，已定位并修复真 @ 身份表示和读取并发问题，
复测仍未触发，现已持久暂停，保留一条不重发的 UNKNOWN。修复后收发与重启上下文尚待验证；此前单群 canary 已通过。
操作与结果见 [conversation 手册](docs/wechat-agent-conversation.md)。

> 更新：2026-10-09。正式 runtime 接入验收通过：testing 单群合成指令收发、程序重启、持久暂停及显式恢复均通过，正式入口完成约 12 分钟稳定采样，用户决定提前结束并接受结果（未完成原定三十分钟窗口）。仓库默认配置继续关闭，模型和工具关闭，仍需人工登录并保持 testing 前台。
Lemonbot 是 Ubuntu 桌面虚拟机中的个人微信代理实验项目。当前主线通过固定版
agent-wechat，在 `testing` 群实现合成指令的接收、发送者识别、持久化与固定回复。
需要人工登录并保持目标群前台，不调用模型或工具。

## 当前状态（2026-10-09）

- **正式接入通过：**正常 runtime 入口处理 testing 指令，识别 sender 并持久化，一次 POST、一条精确确认 ACK；模型和工具为零。
- 独立通道连续两条分别回复一次，self 已被读取但不回复；容器和程序重启后身份摘要稳定。
- 正式程序重启后旧消息不重放，新指令回复一次；管理台暂停跨重启保留，暂停指令不处理，显式恢复建立新基线后新指令回复一次。
- 独立入口已完成实际三十分钟观察；正式入口 22 次正常采样覆盖约 12 分钟，用户提前结束并接受结果，未完成原定三十分钟窗口。旧实验 UNKNOWN 保留，不自动重发。
- 仓库默认关闭连接器；范围限前台单群合成指令。PR #171 冻结，原生 AT-SPI 门禁不变。

## 开始使用

离线验证无需微信或 Docker：

```bash
uv sync --all-extras --locked
uv run --locked python -m lemonbot.research.agent_wechat_lab demo
```

输出明确标记 `simulated_http_not_live`；模拟通过不代表实机通过。
实机启动和暂停恢复按 [固定环境与恢复手册](docs/environment-baseline.md)执行。
正式入口与显式恢复见 [runtime 手册](docs/wechat-agent-runtime.md)。恢复发送须核验前台并使用全新标记。

## 文档入口

- [文档索引](docs/README.md)：区分当前操作文档、实机证据和历史方案。
- [Linux 计划](PLAN_linux.md)：当前优先级、剩余验收与止损条件。
- [环境基线](docs/environment-baseline.md)：镜像、微信、依赖锁与持久路径。
- [实机复现记录](docs/agent-wechat-reproduction.md)：实验方法和各阶段结果。
- [安全边界](docs/security.md)：正式 runtime 与独立实验的不同作用域。

## 开发验证

```bash
uv lock --check
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked mypy src
```

2026-10-08 全仓 475 项测试通过，Ruff、mypy（112 个源文件）和锁文件检查通过。
存在一条依赖层 Starlette/httpx 弃用警告。正式配置保持 `runtime.connector = "fake"`、
`wechat_atspi.enabled = false`、`models.provider = "disabled"` 和 `vision.enabled = false`。
正式连接器默认关闭；本阶段不修改数据库 schema，不启用模型、工具、自动导航、多群或媒体。
