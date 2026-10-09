# testing 群与单一白名单私聊

当前为开发验收阶段，不能以模拟通过代替可直接运行。固定镜像、微信版本、原卷与密钥保存方式不变。
旧 canary 模式与数据库保留；本模式使用独立数据目录，不迁移 UNKNOWN，不启用工具、主动任务、媒体或长期记忆提取。

## 接收与触发

固定版上游事件源码的 WebSocket 是 TODO，未提供可用的消息推送。Lemonbot 自有容器桥使用 Linux
inotify 监听当前账号消息数据库/WAL 的写入，并通过 token 认证的 localhost 长轮询通知宿主。
这是数据库变更通知，不是精确的会话事件；通知不含正文、账号或会话 ID，收到后只增量读取两个绑定会话。
通知突发合并，每分钟补偿核对一次。启动建立历史基线，不补抓停机消息；通知桥重启仍按锚点核对，无法对齐即暂停。
通知等待位于导航锁之外，因此空闲等待不会阻塞发送或管理台恢复。
读取、模型处理、导航和发送忙碌期间仅保留 pending 通知，不重入新处理；空闲后合并读取。
模型处理使用单独的使能门禁，避免与管理台策略锁互相等待；忙时不能恢复使能，暂停必须显式恢复。
存在待确认 outbox 时不开始下一个模型轮次，避免下一条追问缺少已经生成但尚未送达的回答。

testing 只接受消息 source 顶层 atuserlist 对本机原始微信 ID 的精确匹配；手打昵称、引用内 @、其他人、@所有人均不触发。
本机 loggedInUser 带目录后缀，不能直接与 atuserlist 比较。桥从绑定群内唯一 self 发送者确认原始 ID，
核对其与当前登录目录标识的完整边界关系；不能唯一确认时停止。账号引用算法仍沿用原登录字段。
固定版 API 不暴露这个字段，因此桥只在容器内解密并解析 source，输出绑定消息 ID 和 mention 布尔值。
按消息 ID 查询避免 API 和元数据读取之间的新消息造成 offset 漂移。密钥、原始 source 和数据库均不导出。
其他会话可能已被上游账号同步，不能宣称底层账号数据获得群级隔离。

## 导航、发送与上下文

读取不依赖前台；发送串行执行：绑定内部 ID → 打开会话 → 唯一名称与前台语义标题复核 → 一次 POST →
精确确认唯一新 self 消息。导航失败暂停对应会话；身份变化、缺失或 UNKNOWN 暂停整个通道。
历史同文消息允许存在，新消息仍按不同 ID 处理。歧义和超时不自动重发；暂停不会因程序重启解除。
群上下文按群＋成员隔离，私聊按联系人隔离；只有确认送达的助手回复进入后续上下文。
非触发群消息、自发消息、非白名单消息不调用模型，也不进入模型上下文。模型失败不重复调用。

采用 DeepSeek Flash，8,192 输入上下文、512 输出 tokens、2,000 字符回复上限。
全天被动响应，保留原发送限流，每日 5 元、每月 100 元。价格与官方余额接口见 [计费说明](deepseek-pricing.md)。

## 本机入口

默认关闭示例：[conversation TOML](../config/wechat-agent-conversation.example.toml)。
私有绑定为 state 下 conversation-binding.json，两个白名单 ID 只保存于本机受限目录。
必须包含用户确认后的 operator_confirmed=true；技术绑定不等于用户确认。
2026-10-09 用户已确认 testing＋此前测试对端的私聊；首次登记漏掉确认步骤，现已补齐并增加启动守卫。
本机验收配置为 `~/.local/state/lemonbot/agent-wechat-lab/conversation-runtime.toml`，
数据库为 `~/.local/share/Lemonbot-wechat-conversation`；旧实验与正式 canary 数据保留。

扫码登录后，安装或恢复独立通知桥（仅重启桥，不重启微信）：

```bash
uv run --locked python scripts/agent_wechat_conversation_env.py
```

桥代码和绑定保存在容器 /data；宿主桥端口仅 127.0.0.1:6175，容器端仅允许宿主网关且要求 token。
容器重启后需重新执行上述入口，尚未验证宿主重启自动恢复。

正常 runtime 入口与控制：

```bash
uv run --locked lemonbot run --config ~/.local/state/lemonbot/agent-wechat-lab/conversation-runtime.toml
uv run --locked python scripts/wechat_conversation_control.py status
uv run --locked python scripts/wechat_conversation_control.py pause
uv run --locked python scripts/wechat_conversation_control.py resume
```

恢复重新核验两个目标并重建基线。日常 user service 为 lemonbot-wechat-conversation.service；
`systemctl --user status|restart|stop lemonbot-wechat-conversation.service` 管理进程。
实验入口和正式 runtime 继续共享 run.lock，不能同时发送。

## 验收记录（2026-10-09）

- 自动往返私聊与 testing、唯一内部 ID 与语义标题绑定：实机通过，无发送。
- 容器 source 可读取并解压，通知接口 ready、无消息内容：实机只读通过。
- 首批群消息没有触发：登录字段带目录后缀，source 使用原始 ID。修复后原合成群消息已精确识别真 @，尚未证明新指令回复。
- 首批私聊已入站并调用模型；验收期间为修复 HTTP 日志而重启，正在发送的任务变为 UNKNOWN，精确 self 回读为零。保持一次尝试、不自动重发；不能记作送达。
- 首批读取时发生 MessageWindowChanged；增加最多三次只读重试，保留旧锚点，不重试模型或 POST。表定位缓存仅存于容器内存，每次仍核对账号。
- 初次预检 HTTP 日志曾含目标私聊内部 ID。修复输出过滤覆盖 httpx/httpcore 子日志器；旧日志保留于本机，不导出原文。
- Flash 模型极小请求和官方余额查询：实机通过，不经过微信发送。
- 全仓 508 项自动化测试、Ruff、mypy（114 源文件）及锁检查通过；模拟消息不计为实机收发。
- 两批六条消息验收：首批未通过；复测仍未触发，现已持久暂停，UNKNOWN 保留。第一批普通群消息、真 @ 群消息、私聊短词、私聊追问；
  第二批程序重启后群/私聊各追问一次。总计预期五次回复，原限流不放宽。

当前归类为代码通过、实机受阻；修复后的新消息闭环与重启上下文尚待验证。
旧“正式接入通过”只指单群 canary，不能自动扩展到本模式。

## 新消息未触发的收敛记录

2026-10-09 08:15–08:33 UTC，用户确认 noVNC 中可见新 @ 消息，但绑定群 API
和普通只读 SQL 查询均返回 42 条，最新时间仍为 07:46:19 UTC；inbox 仍只有首批的一条私聊。
通知接口 ready 且 sequence=204，但写入通知不能等价为目标群收到消息。
限定微信进程的文件描述符确认它打开了同一消息库；另外两个消息文件未读到绑定表，查询错误不作其他聊天证据。
目标库处于 WAL 模式，WAL 存在有效提交标记；不能因此断言目标群的新消息已经提交。

记录的固定源码使用 immutable=1 查询活动库；独立 SQLite 模拟对照中，immutable 读到 0 条而
普通只读读到 WAL 已提交的 1 条。这证明该读取方式存在风险，**不是本次故障的完整定位**：
本机普通 SQLCipher 只读同样返回旧消息。参见 [固定源码](https://github.com/thisnick/agent-wechat/blob/96c2e90c497d999bf5189a134a9b3b293731f42e/packages/agent-server-rust/src/tools/wechat_db.rs)。

当前单一阻塞是界面新消息与绑定数据库可见结果不一致。下一项仅核对实际 UI 会话绑定与
WCDB/WAL 读视图，不重启登录、不更换镜像、不要求用户重发，不将通知存活记为收取成功。
通道已显式持久暂停，旧 UNKNOWN 的一次尝试保留；本模式尚不能直接运行。
