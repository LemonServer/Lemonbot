# Lemonbot

运行在 Linux 桌面的微信机器人，使用 DeepSeek Flash 回复白名单私聊和群内真实 @。
支持持久上下文，以及可选的图片理解、公开网页读取、本地搜索和时间工具。

## 从这里开始

| 要做什么 | 阅读 |
|---|---|
| 本机设备重启后开启机器人 | [Orin 重启手册](docs/restart-jetson-orin.md) |
| 查看状态、修改配置或白名单、排错 | [日常运维](docs/operations.md) |
| 首次部署到新设备 | [部署入口](docs/deployment.md) |
| 了解图片、链接和搜索怎么用 | [能力说明](docs/read-capabilities.md) |
| 接续开发或查看验收进度 | [当前交接](docs/context-memory-implementation.md) · [开发规范](CONTRIBUTING.md) |

完整分类目录见 [docs/README.md](docs/README.md)。日常使用不需要阅读历史记录。

## 支持范围

- Ubuntu 24.04 桌面、Python 3.12、Docker、已解锁的桌面密钥环；微信登录及设备重启恢复由人工完成。
- AMD64 保留原 1.0.0 固定环境；Jetson AGX Orin 的 ARM64 原生通道已有两会话实机证据，
  其他设备不据此视为通过。见 [Orin 部署](docs/deployment-jetson-orin.md)。
- 普通群消息不回复；必须通过微信成员选择真正 @。发送超时或回执歧义记为 UNKNOWN，不自动重发。
- 公开示例默认关闭。统一上下文在本机两个授权会话内验收：私聊隔离、同群保留作者共享，
  本地检索使用 CUDA/TensorRT，分类和回答使用 DeepSeek。长期整理与主动消息关闭。
- 新流程整体验收尚未完成；已通过和待验证项目只在[当前交接](docs/context-memory-implementation.md)维护。

可编辑配置在仓库的 `config/lemonbot.toml`，白名单由其中的 `whitelist_file` 指定；
两者 Git 忽略。密钥留在系统密钥环，数据库、附件、绑定和暂停状态留在私有数据目录。

## 无微信演示

```bash
uv sync --locked --extra dev
uv run --locked python -m lemonbot.research.agent_wechat_lab demo
```

该演示使用模拟通道，不代表真实微信验收。安装及启用按部署文档执行。

[MIT](LICENSE) 仅覆盖自有代码；微信及第三方镜像边界见[第三方声明](THIRD_PARTY_NOTICES.md)。
