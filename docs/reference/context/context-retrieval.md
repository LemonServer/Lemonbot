# 本地混合检索与诊断

关键词、CUDA 语义向量和近期候选的实现与本地诊断。设备测量是注明日期的证据，当前部署状态见[交接页](../../context-memory-implementation.md)。

## 数据流和安全边界

`SQLiteMessageArchive.scan_scope` 在一个只读事务中加载同一账号/会话的消息、片段和观察，
校验原始归档完整性；新消息始终与历史候选分开。索引是可重建的独立 SQLite 文件，
不是归档 schema 的新版本，也不替代原始消息。运行数据库不需要为阶段 4 升级。

[HybridContextRetriever](../../../src/lemonbot/memory/unified/retrieval.py) 使用如下过程：

1. 按 channel、账号、会话 ID 和 private/group 全部字段过滤。私聊分别隔离，同群允许
   跨发送者检索，候选保留原作者。不会根据显示名扩大范围。
2. 原始文字、链接/卡片的已有标题和 URL，以及可用且未过期的观察内容生成来源单元。
   单元最多 1000 字符，保存精确 part/observation 和字符起点，长文尾部也可索引。
   URL 仅作为文字，不打开页面；没有下载、OCR 或新云端整理。
3. 中文使用二字切分，非中文词转小写后建立 SQLite FTS5；FTS MATCH 本身含 scope 分区，
   不使用其他会话的 BM25 统计。语义向量只从同 scope、同模型版本读取后计算相似度。
   向量由阶段 3 worker 在 CUDA 上生成；本机相似度计算在 CPU 完成，不运行 CPU 神经网络。
4. 精确引用先按 ID/part 从归档核实，最高优先。其他候选融合关键词、语义及近期召回：
   关键词和语义各取最多 40 个，reciprocal-rank 分数 `1/(60+rank)`；近期权重为 0.25。
   语义默认门槛为 0.5，属于有限候选生成阈值，不能作为“已经相关”的分类结论。
5. 候选返回前重新读取原始来源和观察；内容已变化、缺失或过期时丢弃。原始消息和
   观察不被索引改写。UNKNOWN、失败、未确认助手消息不进入已送达历史。
6. 返回最多 20 个候选，每个消息最多 2 个来源单元；候选 JSON 字符总数默认不超过 16000。
   保留精确来源、作者、原消息时间、观察 ID、召回原因与排序分数。
   分类及回答在阶段 5 实施，不把候选自动拼接成回答上下文。

归档消息 ID 不按正文合并；相同正文不同 ID 仍可分别召回。当前消息和时间在它之后的
消息不作为历史候选。原始消息没有被“摘要”覆盖，候选 summary 是原来源片段。
引用无法解析时在 `unresolved_references` 中返回，不用附近消息或最近图片替代。

## 图片、链接及失效来源

没有描述的图片只生成不可全文/语义搜索的元数据占位。引用精确命中时可以返回占位，
以便后续明确说明未读取；不会因此下载原图。

普通“刚才图片/照片/截图”等明确图片追问可把最近图片占位列为待判断候选。
无关换题不会默认召回最近图片；带新图片的问题不会自动借旧图片，精确引用例外。
已有明确引用时，不补入其他最近图片。图片观察若有相关文字，也可以通过关键词或语义
召回，但仍只是候选，绝不等同于原图已选中或附件访问授权。

已有链接/卡片正文观察可立即检索，不等待长期记忆整理；只有标题时仍只索引标题/URL，
不会伪造正文。网页和图片文字是历史内容，不能授予工具权限。历史 URL 不允许直接成为
新一轮浏览权限；阶段 5 继续执行已有只读工具的授权规则。

原图过期不必删除没有到期的历史描述；若 Observation 自身有过期时间，则到期即从来源
单元和向量索引移除。失败 Observation 不当作成功正文索引。
阶段 5 使用原图前必须再核验精确来源、附件有效期、对象完整性和本轮选择。

## 配置与索引管理

设置继续放在仓库 `config/lemonbot.toml` 的现有节，公共示例和本机运行开关均保持关闭：

```toml
[context_memory]
enabled = false
archive_enabled = false
# 沿用阶段 3 embedding_* 字段及私有 worker/资产目录。
retrieval_max_messages = 10000
retrieval_vector_chunks_per_refresh = 256
retrieval_semantic_min_similarity = 0.5
retrieval_candidate_char_budget = 16000
retrieval_recent_messages = 6
```

首次或增量刷新按范围扫描最多 10000 条消息、50000 个来源单元，超过时明确失败，
不会把不完整范围悄悄替换成完整索引。索引单元不变则复用向量；版本改变后重新生成。
每次刷新默认最多 256 个模型分词块，长单元通过 worker 无损分块、归一化平均向量，
剩余部分显示 `semantic_index_incomplete`，下一次显式刷新继续处理。
查询用于检索的派生文本最多 8000 字符、16 个模型块；超过时报告相应限制，原始 Message
保持不变。关键词查询最多 64 个生成词；首版不承诺任意长文或单字查询都能完整召回。

每次 retrieve 会刷新当前 scope 的派生索引，以反映新增/失效观察，再生成候选。
首版采用有界的完整 scope 扫描，没有将它当作恒定耗时；大范围/积压向量会更慢。
阶段 6 可继续优化局部更新，不能为减少扫描跳过失效来源检查。

索引含原文片段与向量，应放在私有运行目录，目录 0700、索引文件 0600，不加入 Git。
CLI 要求显式指定源归档及独立索引；拒绝源数据库自身、指向源的符号链接/硬链接，
也拒绝复用任意其他 SQLite 数据库。索引 manifest 绑定源路径、设备/inode 和索引版本。
换备份或替换源文件必须选新的私有索引路径，不能强改 manifest 来复用旧内容。

可从保留的原始归档重建索引；遇到未知 schema、损坏数据或身份不匹配时保留旧文件供定位，
选新索引路径重新执行 index，不删除原始归档、绑定、暂停状态或 UNKNOWN。
只做关键词刷新会保留有效的既有向量，之后 --cuda 可继续回填。

## 本地诊断命令

这些命令不会启用机器人。`--database` 使用已有合成数据库或已验证的独立备份副本；
不要为本阶段在生产数据库执行升级/迁移。scope 参数为该副本的精确内部标识，不是显示名。
以下变量由操作者在本机填写；本轮验收只操作临时合成数据库。

```bash
cd /home/ega-agx-orin/Lemonbot
uv run --locked python -m lemonbot.memory.unified.retrieval_cli index \
  --config config/lemonbot.toml \
  --database "$LEMONBOT_ARCHIVE_COPY" --index "$LEMONBOT_RETRIEVAL_INDEX" \
  --channel "$LEMONBOT_ARCHIVE_CHANNEL" --account "$LEMONBOT_ARCHIVE_ACCOUNT" \
  --conversation "$LEMONBOT_ARCHIVE_CHAT" --kind group --cuda

uv run --locked python -m lemonbot.memory.unified.retrieval_cli query \
  --config config/lemonbot.toml \
  --database "$LEMONBOT_ARCHIVE_COPY" --index "$LEMONBOT_RETRIEVAL_INDEX" \
  --channel "$LEMONBOT_ARCHIVE_CHANNEL" --account "$LEMONBOT_ARCHIVE_ACCOUNT" \
  --conversation "$LEMONBOT_ARCHIVE_CHAT" --kind group \
  --current-id "$LEMONBOT_ARCHIVE_CURRENT" --limit 20 --cuda
```

省略 `--cuda` 仅使用关键词/近期候选，报告 `semantic_disabled`。配置中的模型后端须为
`tensorrt` 才接受 `--cuda`；不通过该标志安装依赖或构建引擎。

GPU 初始化、引擎或 worker 失败时显示 `embedding_worker_unavailable` / `embedding_worker_failed`，
保留关键词/近期/精确引用能力，不自动启动 CPU 模型或重试故障 worker。
错误响应显示 `embedding_result_invalid`；队列、帧、超时和进程清理由阶段 3 worker 负责。
损坏的索引自身会明确拒绝，不把不可靠来源当作正常检索结果。

query 默认仅返回 ID、作者、时间、理由和诊断状态；只有 `--include-content` 才显示片段。
完整输出仅供本机查看，不要把私人 scope、正文、向量或数据库贴进报告。
index 输出单元变化、待嵌入数量、模型版本及降级原因；候选为空不证明整个存档都无关。

## 可重复的合成验证

无需用户聊天和微信账号：

```bash
uv run --locked python scripts/check_context_retrieval.py --config config/lemonbot.toml
uv run --locked python scripts/check_context_retrieval.py --config config/lemonbot.toml --cuda
```

第一条检查关键词/近期路线；第二条要求已准备好的真实 CUDA worker，失败不会伪装成 CUDA 通过。
脚本自动建立临时数据库、合成来源与索引，退出后清理，仅输出数量、模型版本及耗时。
它核对六个固定关联样例、跨账号隔离、原归档逻辑内容不变和重启后零向量回填。
离线测试另外覆盖阶段 1 的固定多模态样例、私聊/群/账号/channel/kind 隔离、群跨作者、
引用丢失/优先、图片换题/新图、观察失效、长文尾部、候选预算、GPU 故障和 UNKNOWN 排除。

本机 2026-10-15 使用阶段 3 同一 TensorRT FP16 模型，六个关联样例全部召回且首位正确；
真实混合检索中位数约 73.6 ms、最大 82.0 ms，重开 worker/索引后 0 单元重建、0 向量回填。
这里包括临时小范围归档刷新、FTS、GPU 查询及来源复核，不代表大存档或聊天端到端延迟。
完整脱敏数据见 [阶段 4 CUDA 证据](../../evidence/context-memory-stage4-retrieval-20261015.json)。
阶段 3 的后端推理基准见 [worker 手册](embedding-worker.md)。
