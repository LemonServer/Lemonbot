# 上下文高级运维

来源诊断、备份、迁移与配置回退参考。已有运行库已在阶段 8 迁移，不重复执行迁移或付费预检。当前状态见[交接页](../../context-memory-implementation.md)。

## 只读检查

在 `/home/ega-agx-orin/Lemonbot` 中以桌面用户执行。主配置始终是仓库
`config/lemonbot.toml`，密钥继续留在系统密钥环。以下工具不读密钥、不调用模型、
不打开网页、不发送消息、不迁移数据库，也不启动嵌入 worker。

```bash
uv run --locked python -m lemonbot.memory.unified.operations doctor --config config/lemonbot.toml
```

输出开关、配置中的 Flash 名称、预算、两阶段输入输出限制、配置的嵌入后端/精度。
`device_probe=not_run` 明确表示没有验证实际 CUDA；归档启用时检查现存数据库 schema、
外键、完整性、容量及模型制品/worker 是否准备好。关闭新流程不要求创建数据库或安装模型。
部署 doctor 也包含此项，但只有部署 doctor 才检查实际绑定和微信环境。
Orin 部署检查继续使用 `scripts/wechat_setup.py doctor --experimental-arm64` 的已支持路线，
不把通用旧 doctor 的 AMD64 平台判断当作 ARM64 微信通道验收。

对**显式指定的既有测试库、备份副本或已停止的运行库**检查状态：

```bash
# 请先将变量设为该文件的实际绝对路径，勿猜测路径。
uv run --locked python -m lemonbot.memory.unified.operations status --database "$LEMONBOT_CONTEXT_DATABASE"
```

默认不输出聊天内容、账号或会话 ID；显示表计数、schema、收发状态、全局暂停及暂停通道数、
UNKNOWN 数量、ACK 本地维护积压、整理任务状态、费用/缓存统计。暂停还须结合现有控制
脚本的 `status` 检查目标暂停和连接器状态；数据库计数不是实时服务 readiness。
缺库、缺表或旧 schema 报错，不偷偷创建或升级库。

来源诊断只接受工程师从本地记录取得的**精确内部 scope/ID**，禁止用显示名猜测。
这些参数和结果不应粘贴到公共报告，也不要求最终用户核对哈希：

```bash
uv run --locked python -m lemonbot.memory.unified.operations source \
  --database "$LEMONBOT_CONTEXT_DATABASE" \
  --channel "$LEMONBOT_CONTEXT_CHANNEL" --account "$LEMONBOT_CONTEXT_ACCOUNT" \
  --conversation "$LEMONBOT_CONTEXT_CONVERSATION" --kind group \
  --message-id "$LEMONBOT_CONTEXT_MESSAGE"
# 或用 --memory-id 替换 --message-id；私聊必须使用 --kind private。
```

结果包含来源是否合格、记忆是否有效、更正链/最新版本是否可用、观察结果和附件元数据。
出站原消息会重新核验持久 ACK 与 receipt；附件元数据状态不等于原图字节已验证。
需要查看原文时使用已有 archive `inspect --include-content`，仅在本机私有终端操作。

检索诊断已有入口现在同时包含原始记录、派生记忆与更正标注：

```bash
uv run --locked python -m lemonbot.memory.unified.retrieval_cli query \
  --config config/lemonbot.toml --database "$LEMONBOT_CONTEXT_DATABASE" \
  --index "$LEMONBOT_CONTEXT_TEST_INDEX" \
  --channel "$LEMONBOT_CONTEXT_CHANNEL" --account "$LEMONBOT_CONTEXT_ACCOUNT" \
  --conversation "$LEMONBOT_CONTEXT_CONVERSATION" --kind group \
  --current-id "$LEMONBOT_CONTEXT_MESSAGE"
```

此命令会更新指定的派生 sidecar；用独立测试路径，不能覆盖运行索引。
默认隐藏候选正文及更正正文；`--include-content` 才在本机显示。
`--cuda` 才显式启动本地 CUDA worker；失败时报告关键词/近期候选降级，绝不静默 CPU 推理。
实际 CUDA 验证也可使用 [检索验收工具](context-retrieval.md) 的非私人合成样例。

## 备份、导出与删除

先用现有 `scripts/wechat_bot_restart.py --stop` 暂停、排空并停止服务。
离线导出沿用 `lemonbot data export`；备份沿用 `lemonbot backup`，均传入同一主配置。
ZIP 包含完整 profile 数据库（包括所有新表、费用、暂停、UNKNOWN）和 profile 对象目录，
权限为 0600；不含密钥环、配置、绑定、日志、Docker 卷或派生索引。
配置、白名单、私有状态目录和 Docker 卷须按原运维流程另外备份并私下保存。

恢复使用 `lemonbot restore`，持有 profile 运行锁，校验完整性/哈希并原子切换 DB 和对象。
恢复时清除该 profile 的标准 `context-index/<profile>/index.sqlite3` 及 SQLite sidecar，
避免旧索引绑定旧 inode；下一次检索重建。危险的符号链接或硬链接导致拒绝切换。
原始归档、UNKNOWN、整理终态、费用和暂停原样恢复，不能借恢复自动清除暂停或重发。
恢复旧时间点的备份可能回退后续业务状态；阶段 8 有新发送后，优先用下面的配置回退。

`lemonbot data delete-conversation` 仍只用于管理员**明确要求永久删除**某一会话，
从白名单移除且停止程序后才可用；它不是升级或排错手段。本轮只用合成库演练删除。
删除覆盖原始消息/片段/观察/引用、派生记忆/更正、内容/主题缓存、ACK proof 与确认标记，
并清理标准 profile 派生索引，其他会话的原始记录保留、索引可重建。
只移除没有其他附件记录或归档引用的对象，保留跨会话共享文件。
整理任务删除来源列表并匿名化 scope，保留日期/终态/费用；模型阶段统计及全局预算账本保留，
不会通过删除会话重置每日整理额度。操作报告区分删除行和脱敏账目行。
自行建立在其他位置的诊断索引不受标准 profile 管理；删除前须清理自己的私有副本/诊断缓存。
文件删除、VACUUM 和 secure_delete 不承诺清除磁盘快照、备份或第三方存储的历史副本。

## 迁移演练与阶段 8 切换

阶段 7 验证从 `20260825_0006` 备份副本升级到 `20261015_0008`、重复导入、保留旧记录、
UNKNOWN 与暂停，以及关闭新流程后继续使用兼容旧上下文。该步骤为当时的副本演练；本机后续已在阶段 8 完成生产迁移。

备份副本继续使用 [归档说明](../../context-memory-implementation.md) 的 `migrate-copy`：
只允许 profile backups 目录内独立文件，拒绝活动 DB、硬链接和重定向环境变量。

**以下仅供首次切换新 profile 的工程师参考；已迁移的本机无需重跑。** 顺序为：

1. 核对配置/白名单/绑定/数据位置和备份，暂停、排空并停止；未排空不得继续。
2. 保持三个新开关关闭，在已停止的 profile 上运行：

   ```bash
   uv run --locked python -m lemonbot.memory.unified.cli migrate-offline \
     --config config/lemonbot.toml --confirm-stopped
   ```

   入口只选择主配置指定的 DB，拒绝别名/非本人文件，持有运行锁，要求持久暂停且没有
   pending/processing inbox 或 pending/reserved/dispatching outbox。先生成唯一 0600 备份，
   再升级 schema、按现有精确绑定导入；不重绑、不改收发状态、不删除 UNKNOWN。
   重复执行幂等，每次仍独立备份。失败时保持停止/暂停，保留备份和部分新增归档供诊断。
3. 把本轮运行白名单限制为已授权 testing 与私聊“18”，私下备份原名单；其他目标不参与
   新流程验收。配置 CUDA/TensorRT、新归档/上下文；旧提取器关闭，主动发送仍关闭。
   首轮保持 `consolidation_enabled=false`；云端整理需后续单独启用验收，不能标为实机通过。
4. 运行只读 doctor 和已有 CUDA 合成验证；确认预算 5 元/日、100 元/月、整理上限
   0.50 元/日、Flash 配置、附件和工具状态。CUDA 无法推理则不把阶段 8 CUDA 门槛算通过。
5. 在独立合成上下文、现有模型预算路线中验证真实两阶段 API 格式/用量/缓存，不发送微信。
   该有限付费预检在阶段 8 实施并执行，须计入同一全局预算，不能独立开无限额测试账本。
6. 启动但保留暂停，按绑定/目标 doctor 校验后显式恢复两目标，执行下一节清单。
   服务与队列仍沿用唯一发送路径，依据唯一程序回执判断，不重复发送已处理样例。
7. 通过后记录真实后端/模型版本、费用、延迟及通过范围，保持服务运行；其他白名单目标
   暂不自动恢复到新流程。失败则执行配置回退，不扩大测试范围或追加长时间观察。

## 有限实机验收清单（阶段 8 进行中）

- 私聊：一张新的非私人图片 → 细节追问 → 切换无关文字话题 → 明确引用返回旧图。
- 文字与链接：原文/公开链接追问；分享卡片遇验证或登录页时如实说明正文不可读。
- 群聊：普通消息不回复；另一位成员成员选择真 @，可关联群内普通消息且标明作者。
- 隔离：私聊不能读取群内记录，群不能读取私聊记录，其他群/账号不会进入候选。
- 暂停排空并安全重启：暂停和 UNKNOWN 保持，不回放停机消息；显式恢复后记忆可用。
- 每条需要回复的消息最多回复一次；模型/工具费用、缓存与分阶段耗时有记录。

不要求用户检查哈希；不向其他会话发测试消息；不把标题可读写成公众号正文可读。

## 配置回退

使用相同的 schema 兼容 checkout，暂停排空停止后将 `enabled`、`archive_enabled`、
`consolidation_enabled` 设为 false，再安全启动、核对绑定与目标并显式恢复旧流程。
保留新归档、缓存来源、暂停、费用账本及全部已产生的 UNKNOWN，不运行 schema downgrade，
也不直接切回不认识 `0007/0008` 的旧标签。不用删除会话或恢复更早 DB 来解决回答质量问题。
精确复原配置/白名单时须沿用主配置绝对路径并检查服务入口；密钥和绑定保持原位。


## 阶段 8 有限付费预检与当前维护

本机已完成下列预检，**无需重跑**：

```bash
uv run --locked python -m lemonbot.memory.unified.preflight \
  --config config/lemonbot.toml --confirm-paid-preflight
```

仅在服务停止、持久暂停、inbox/outbox 排空后可运行；与实际服务共享预算库和隔离模型 worker，
合成消息留在临时库，最多四次生成，不导航、不发送、不读取私人聊天作为模型输入。
固定 attempt 标记在调用前持久保存；已执行/失败/崩溃后的再次运行会拒绝，不删除标记绕过。
报告包含分类/回答实际 tokens、缓存、耗时及本地估算费用；不是供应商账单。

新服务启动时的 `unified_context_startup` 只记录实际 worker ready 后端与模型版本，
`degraded=true` 表示关键词/近期候选降级，不能据此宣称 CUDA 通过。查看时仅筛选该诊断行，
不要导出整段含聊天内容的 journal。只读 doctor 的 `device_probe=not_run` 含义保持不变。

本机白名单当前限于 testing 和“18”；完整旧名单保存在私有备份中，不自动恢复其他目标。
普通群消息进入本群档案，但不触发回复；群内其他成员真 @ 可关联档案，作者仍保留。
私聊不能借用群记录，群不能借用私聊记录；明确引用历史图片仍受附件有效期限制。
设备恢复沿用 [重启手册](../../restart-jetson-orin.md)，不会自动重跑迁移或付费预检。


## 诊断要点

当前限额及控制命令见[运维手册](../../operations.md)，已通过/待验证项见[当前交接](../../context-memory-implementation.md)。
候选存在不代表原图已提供；只有实际校验、模型请求携图才能说明原图可用。
筛选 `context_classification_input`、`context_selection`、`context_classification_rejected`、
`context_budget_overflow` 可检查候选裁剪与选择，日志不含正文或原始标识。
`unified local ACK maintenance incomplete` 只应恢复本地维护，不重发、不重复模型调用。
