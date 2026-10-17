# 技术参考

日常操作返回[文档入口](../README.md)。本目录供开发、排错和发布时按需阅读。
当前验收状态统一见[实施交接](../context-memory-implementation.md)，下面的阶段测量只反映记录当时。

## 统一上下文

| 专题 | 内容 |
|---|---|
| [分类与回答](context/context-flow.md) | 两次请求、来源校验、附件与预算 |
| [混合检索](context/context-retrieval.md) | 关键词、向量、近期候选及诊断 |
| [CUDA 嵌入](context/embedding-worker.md) | 固定模型、独立 worker、引擎构建与设备测量 |
| [整理与缓存](context/context-consolidation.md) | 确认后维护、更正、证据有效期与整理子预算 |
| [高级运维](context/context-memory-operations.md) | 来源检查、备份迁移、配置回退 |

## 模型与发布

- [提示词维护](models/system-prompt.md)
- [价格记录与预算口径](models/deepseek-pricing.md)
- [1.0 能力证据](releases/release-1.0.md) · [发布说明](releases/release-1.0-notes.md)
- [发布流程](releases/releasing.md)
