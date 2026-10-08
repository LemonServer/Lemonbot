# 项目文档索引

更新：2026-10-08。当前结论是**单条实机闭环通过，连续验收因回执歧义暂停**。
完整里程碑尚未通过，不进入正式 runtime 集成。

## 当前文档

| 文档 | 用途 |
|---|---|
| [项目 README](../README.md) | 当前能力和离线入口 |
| [Linux 计划](../PLAN_linux.md) | 唯一现行执行顺序；后半部保留历史设计 |
| [固定环境与恢复](environment-baseline.md) | 实际版本、制品锁、原卷恢复、暂停与数据保留 |
| [复现报告](agent-wechat-reproduction.md) | agent-wechat 调研、实验步骤与按日期记录的结果 |
| [通道状态](linux-wechat-current-status.md) | 当前摘要与原生路线的历史事实 |
| [安全边界](security.md) | 正式 runtime、原生探针和独立实验的不同作用域 |
| [操作与 Observe 参考](operations.md) | 当前入口及原生研究/未来 Observe 的历史操作 |

## 固定制品与实机证据

制品与配置在 [research/agent-wechat](../research/agent-wechat/README.md)，Python 依赖以
[uv.lock](../uv.lock) 为准。以下证据只包含版本、状态、计数和脱敏比较：

| 记录 | 证明范围 |
|---|---|
| [登录恢复](evidence/agent-wechat-login-recovered-20261006.json) | 当前账号登录、聊天列表及 VNC；未证明新账号二维码或宿主重启 |
| [首轮收发](evidence/agent-wechat-first-e2e-20261006.json) | 对端收到，但自动确认 UNKNOWN，不重发 |
| [单条闭环](evidence/agent-wechat-single-e2e-20261006.json) | 新标记自动 ACKNOWLEDGED 且对端确认 |
| [连续复核](evidence/agent-wechat-continuous-followup-20261008.json) | API 存在重复标记和回执，程序暂停；原因未确定 |
| [磁盘清理](evidence/disk-cleanup-20261008.json) | 仅清理缓存/未使用的淘汰扩展，保留微信数据与镜像 |

## 历史设计与研究

- [研发交接](research-handoff.md)：历史架构和路线沿革。
- [最初重构计划](../PLAN.md)：旧 Windows/企业微信方案，非当前部署说明。
- [AT-SPI 身份研究](atspi-identity-research.md)与[证据](evidence/atspi-identity-20260909.json)：人工打开资料卡可读 ID，自动消息绑定未证明；当前暂停开发。
- [Portal 视觉校准](visual-calibration.md)：默认关闭的独立研究，未完成两轮实机验收。

历史章节里的“当前”“下一步”仅适用于章节注明日期。恢复环境先读取版本基线，恢复实验先读取
暂停原因和 Linux 计划；不把旧部署命令或单元测试结果当作实机验收结论。
