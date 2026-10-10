# 项目文档索引

更新：2026-10-10。testing 真 @ 与一个白名单私聊的模型回复、上下文隔离及程序重启延续已通过实机验收，服务持续运行。
系统提示词采用英文维护，长期人格方向与当前能力边界明确分开。

## 当前文档

| 文档 | 用途 |
|---|---|
| [项目 README](../README.md) | 当前能力和离线入口 |
| [Linux 计划](../PLAN_linux.md) | 唯一现行执行顺序；后半部保留历史设计 |
| [固定环境与恢复](environment-baseline.md) | 实际版本、制品锁、原卷恢复、暂停与数据保留 |
| [复现报告](agent-wechat-reproduction.md) | agent-wechat 调研、实验步骤与按日期记录的结果 |
| [通道状态](linux-wechat-current-status.md) | 当前摘要与原生路线的历史事实 |
| [runtime 手册](wechat-agent-runtime.md) | 默认关闭的新入口、独立数据库、暂停恢复与许可状态 |
| [群与私聊运行手册](wechat-agent-conversation.md) | 日常双会话模式、通知与忙碌门禁、实机验收及历史故障 |
| [延迟优化](wechat-latency-optimization.md) | 阶段测量、同机收发与硬件对照，明确待验收项 |
| [白名单管理](wechat-whitelist.md) | 按群名称、联系人显示名配置，启动自动同步 |
| [系统提示词](system-prompt.md) | 最终英文提示词、人格方向、记忆与能力边界 |
| [DeepSeek 价格与余额](deepseek-pricing.md) | 官方单价、保守预算及账户余额查询 |
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
| [受控单条](evidence/agent-wechat-controlled-single-20261008.json) | 一次 POST、一条新 self ACK、对端确认 |
| [连续与 self](evidence/agent-wechat-continuous-self-20261008.json) | 两条分别回复一次，self 已读但不回复 |
| [重启收发](evidence/agent-wechat-restart-e2e-20261008.json) | 新指令各一次 ACK，sender 稳定，UNKNOWN 不重发 |
| [本轮 runtime 阶段](evidence/agent-wechat-runtime-stage-20261008.json) | 代码及限定实机验收通过，包含程序重启、暂停恢复和观察 |
| [独立入口稳定观察](evidence/agent-wechat-lab-soak-20261008.json) | 实际 1804.7 秒，52 次采样，计数稳定 |
| [正式首条收发](evidence/agent-wechat-runtime-first-e2e-20261008.json) | 一次 POST、一条精确确认 ACK |
| [正式重启收发](evidence/agent-wechat-runtime-restart-e2e-20261009.json) | 程序重启后新指令回复一次，身份稳定 |
| [持久暂停](evidence/agent-wechat-runtime-pause-restart-20261009.json) | 暂停跨程序重启保留，管理台可用 |
| [暂停指令与恢复](evidence/agent-wechat-runtime-paused-command-20261009.json) | 暂停指令不处理，显式恢复建立基线 |
| [恢复后收发](evidence/agent-wechat-runtime-resume-e2e-20261009.json) | 新指令回复一次 |
| [正式稳定观察](evidence/agent-wechat-runtime-soak-20261009.json) | 22 次正常采样、约 12 分钟，用户提前结束并接受，完整三十分钟未完成 |
| [磁盘清理](evidence/disk-cleanup-20261008.json) | 仅清理缓存/未使用的淘汰扩展，保留微信数据与镜像 |

## 历史设计与研究

- [研发交接](research-handoff.md)：历史架构和路线沿革。
- [最初重构计划](../PLAN.md)：旧 Windows/企业微信方案，非当前部署说明。
- [AT-SPI 身份研究](atspi-identity-research.md)与[证据](evidence/atspi-identity-20260909.json)：人工打开资料卡可读 ID，自动消息绑定未证明；当前暂停开发。
- [Portal 视觉校准](visual-calibration.md)：默认关闭的独立研究，未完成两轮实机验收。

历史章节里的“当前”“下一步”仅适用于章节注明日期。恢复环境先读取版本基线，恢复实验先读取
暂停原因和 Linux 计划；不把旧部署命令或单元测试结果当作实机验收结论。
