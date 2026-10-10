# Lemonbot

**可直接运行（限定本机已绑定的 testing 群与一个白名单私聊）。**
群聊仅真正 @ 本机账号时回复，私聊自动回复新文本；两会话自动导航、DeepSeek Flash
回复与持久上下文实机通过。程序重启后群回答“青柠”、私聊回答“纸船”，各一次发送尝试、
一条精确回执，无旧消息重放。服务保持运行，仓库默认仍关闭。
工具、主动消息与长期记忆提取关闭；每日 5 元、每月 100 元预算及原发送限流保留。
2026-10-10 优化及扩容后两条实测约 13 秒（小样本）；宿主重启仍需人工登录及恢复服务。
旧 UNKNOWN 一条保留不重发，固定镜像、微信版本与原卷不变，PR #171 继续冻结。

操作与验收见 [conversation 手册](docs/wechat-agent-conversation.md)。

## 开始使用

离线验证无需微信或 Docker：

```bash
uv sync --all-extras --locked
uv run --locked python -m lemonbot.research.agent_wechat_lab demo
```

输出明确标记 `simulated_http_not_live`；模拟通过不代表实机通过。
实机启动和暂停恢复按 [固定环境与恢复手册](docs/environment-baseline.md)执行。
双会话日常运行与暂停恢复见 [conversation 手册](docs/wechat-agent-conversation.md)。
旧单群 canary 操作见 [runtime 手册](docs/wechat-agent-runtime.md)，不要与日常入口同时运行。

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

2026-10-10 全仓 540 项测试通过，Ruff、mypy（116 个源文件）和锁文件检查通过。
存在一条依赖层 Starlette/httpx 弃用警告。正式配置保持 `runtime.connector = "fake"`、
`wechat_atspi.enabled = false`、`models.provider = "disabled"` 和 `vision.enabled = false`。
正式连接器仓库默认关闭；本机私有 lab 配置启用双会话导航与 DeepSeek 文本回复。
数据库 schema 未修改，工具、多群和媒体关闭。性能结果见 [延迟优化报告](docs/wechat-latency-optimization.md)。
