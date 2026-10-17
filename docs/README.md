# Lemonbot 1.0 documentation

## Three entry paths

1. **First deployment:** [requirements, fixed environment, login, binding, secrets and services](deployment.md).
2. **Daily operations:** [start/stop/restart, whitelist changes, pause, recovery and troubleshooting](operations.md).
3. **Development:** [contribution and architecture](../CONTRIBUTING.md), [agent instructions](../AGENTS.md).

## Current references

| Document | Purpose |
|---|---|
| [Project README](../README.md) | Scope and quickest entry |
| [Jetson AGX Orin](deployment-jetson-orin.md) | Native ARM64 deployment, limited live evidence and acceptance gates |
| [Orin 设备重启启动手册](restart-jetson-orin.md) | 本机重启后的微信登录、服务检查、启动与显式恢复 |
| [Release 1.0](release-1.0.md) | Capability evidence and verification limits |
| [Linux roadmap](../PLAN_linux.md) | Delivered scope and next milestones |
| [两阶段分类与回答](context-flow.md) | 默认关闭的分类/来源校验/回答、图片来源与双阶段预算 |
| [本地混合检索](context-retrieval.md) | 按账号/会话检索、精确引用、索引重建、降级状态与诊断命令 |
| [CUDA 嵌入 worker](embedding-worker.md) | 独立环境、固定制品、引擎构建、设备验证与故障状态 |
| [统一上下文运维与部署准备](context-memory-operations.md) | 只读 doctor/状态/来源、生命周期与阶段 8 清单 |
| [统一上下文与记忆实施记录](context-memory-implementation.md) | 八阶段交接、类型接口、CUDA 优先与新旧流程边界 |
| [Whitelist](wechat-whitelist.md) | Exact names, startup sync and target status |
| [图片与只读工具](read-capabilities.md) | DeepSeek 原生图片、公开正文、本地 SearXNG 与时间工具 |
| [System prompt](system-prompt.md) | Identity, concise style and honest continuity |
| [Model pricing](deepseek-pricing.md) | Recorded official prices, budgets and balance |
| [Latency report](wechat-latency-optimization.md) | Code/hardware measurements; small-sample limits |
| [Security](security.md) | Secrets, permissions and channel boundaries |
| [Third-party notices](../THIRD_PARTY_NOTICES.md) | MIT scope and unresolved upstream licensing |
| [Changelog](../CHANGELOG.md) | Release changes |
| [Releasing](releasing.md) | Exact-tag publishing and verification |

[Historical research](history/README.md) is separate from current operations.
[Evidence](release-1.0.md) contains sanitized results, never credentials or account data.

- [记忆整理与缓存（阶段 6）](context-consolidation.md)：确认后维护、版本更正、整理子预算和本地统计；默认关闭。
