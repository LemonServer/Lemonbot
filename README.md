# Lemonbot

Lemonbot 是 Ubuntu 桌面虚拟机中的个人微信代理实验项目。当前主线通过固定版
agent-wechat，在 `testing` 群实现合成指令的接收、发送者识别、持久化与固定回复。
需要人工登录并保持目标群前台，不调用模型或工具。

## 当前状态（2026-10-08）

- **单条实机闭环通过：**入站一条、发送一次、精确回读 self 回复并自动记录
  `ACKNOWLEDGED`，对端确认收到。
- **连续验收受阻：**已有测试的 API 中出现相同标记的多条消息和回执，实验程序以
  `AmbiguousReadback` 暂停；对应 `UNKNOWN` 不重发，重复记录原因未定位。
- 容器/程序重启、前台切走恢复及三十分钟连续运行尚未完成本轮验收。
- PR #171 冻结，保留历史固定镜像和原数据卷。原生 `wechat_atspi` runtime 与
  enrollment 仍关闭；独立实验入口没有加入正式 runtime。

## 开始使用

离线验证无需微信或 Docker：

```bash
uv sync --all-extras --locked
uv run --locked python -m lemonbot.research.agent_wechat_lab demo
```

输出明确标记 `simulated_http_not_live`；模拟通过不代表实机通过。
实机启动和暂停恢复按 [固定环境与恢复手册](docs/environment-baseline.md)执行。
当前实验已暂停，恢复发送须重新核验前台并使用全新标记。

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

2026-10-08 全仓 448 项测试通过，Ruff、mypy（108 个源文件）和锁文件检查通过。
存在一条依赖层 Starlette/httpx 弃用警告。正式配置保持 `runtime.connector = "fake"`、
`wechat_atspi.enabled = false`、`models.provider = "disabled"` 和 `vision.enabled = false`。
本阶段不启用正式机器人，不修改产品数据库 schema，不自动导航或处理多群与媒体。
