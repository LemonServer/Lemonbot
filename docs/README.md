# 文档入口

日常只需要前两份。遇到具体问题再进入对应专题，不必按目录逐篇阅读。

| 场景 | 文档 |
|---|---|
| 本机重启后启动 | [Orin 重启手册](restart-jetson-orin.md) |
| 状态、暂停、配置、故障处理 | [日常运维](operations.md) |
| 修改和确认白名单 | [白名单](wechat-whitelist.md) |
| 图片、网页、搜索与时间 | [能力说明](read-capabilities.md) |
| 新设备首次安装 | [部署入口](deployment.md) · [Orin ARM64](deployment-jetson-orin.md) |
| 当前开发进度与下一步 | [实施交接](context-memory-implementation.md) · [开发规范](../CONTRIBUTING.md) |
| 权限、隐私与数据边界 | [安全说明](security.md) |

## 按需查阅

- [技术参考](reference/README.md)：上下文实现、CUDA、诊断、模型计费、发布流程。
- [历史记录](history/README.md)：旧部署实验、逐阶段过程及排错快照，不作为当前操作指令。
- [脱敏证据](evidence/)：按日期保存的测试/设备数据；当前验收结论见[实施交接](context-memory-implementation.md)。

维护规则：操作步骤只维护在对应手册；当前状态只更新交接页；长篇调试过程归档到 history，
不再把每次执行日志追加到日常手册。密钥、原始账号标识、聊天正文和认证链接不写入文档。
