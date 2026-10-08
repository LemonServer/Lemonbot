# Lemonbot

Lemonbot 是 Ubuntu 桌面虚拟机中的个人微信代理实验项目。当前主线通过固定版
agent-wechat，在 `testing` 群实现合成指令的接收、发送者识别、持久化与固定回复。
需要人工登录并保持目标群前台，不调用模型或工具。

## 当前状态（2026-10-08）

- **新标记收发通过：**一次 POST、一条精确确认的 self ACK；连续两条对端消息分别回复
  一次，sender 一致；self 标记被读取但不回复。
- 旧 UNKNOWN 保留不重发。已执行一次保留卷容器重启，API 身份稳定，但前台待人工恢复；
  重启新指令、切走恢复和三十分钟观察尚未完成。
- 默认关闭的 wechat_agent runtime 代码及模拟测试已实现，实机启用等待完整通道验收。
  PR #171 冻结，原生 AT-SPI 门禁不变。

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

2026-10-08 全仓 467 项测试通过，Ruff、mypy（111 个源文件）和锁文件检查通过。
存在一条依赖层 Starlette/httpx 弃用警告。正式配置保持 `runtime.connector = "fake"`、
`wechat_atspi.enabled = false`、`models.provider = "disabled"` 和 `vision.enabled = false`。
正式连接器默认关闭；本阶段不修改数据库 schema，不启用模型、工具、自动导航、多群或媒体。
