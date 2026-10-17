# 独立 CUDA 嵌入 worker

固定模型、隔离环境、引擎构建和设备验证参考。本机已接入新上下文检索；部署范围和验收边界见[交接页](../../context-memory-implementation.md)。

## 本机版本和固定制品

2026-10-15 验证环境：AGX Orin 64GB、aarch64、Ubuntu 24.04、JetPack 7.2.1 /
L4T R39.2.1、Python 3.12.3、CUDA Toolkit 13.2.2（runtime 包 13.2.86）、
TensorRT 10.16.2.10、NumPy 2.5.2、tokenizers 0.22.2。
GPU 为 Orin、compute capability 8.7，CUDA runtime/driver API 均报告 13020。
保留原系统软件栈，主程序 `.venv` 不继承系统包、不安装 GPU 依赖。

模型是 `bge-small-zh-v1.5`，使用
[Xenova 的 ONNX 制品](https://huggingface.co/Xenova/bge-small-zh-v1.5/tree/75c43b069aac4d136ba6bc1122f995fedcfd2781)
提交 `75c43b069aac4d136ba6bc1122f995fedcfd2781`，不跟随 main 下载。
该转换注明 BAAI 基础模型，但不能核实它对应的原始 BAAI checkpoint 提交。
下载校验代码在 [artifacts.py](../../../src/lemonbot/embeddings/artifacts.py)，许可边界见
[第三方说明](../../../THIRD_PARTY_NOTICES.md)。

| 文件 | SHA-256 |
|---|---|
| model.onnx | `69a0b846f4f116b5e6aabf9546ea6754d02264f3211a13a1bd69b31b8040749a` |
| tokenizer.json | `48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26` |
| config.json | `d4193ead3a810fd694fa8a31d7fc72fbaebc0668b603e398734bf2f6538ff42f` |
| README.md | `cb22054df1a98f7ee9d5b281d023e67c27cc316e1733baf03fc994d95c3048ab` |

文档和查询均用 CLS 池化、L2 归一化，返回 512 维向量。查询添加固定指令
`为这个句子生成表示以用于检索相关文章：`，文档不添加。原模型输出的 token 向量在
GPU 中选择 CLS 后再传回；分词和最终归一化使用 CPU，不在 CPU 上运行神经网络。

## 配置和本机安装入口

可编辑设置只在仓库 `config/lemonbot.toml`。公共示例保持关闭；现有配置缺少新增字段
也能继续运行。以下为字段说明，不要求此阶段改写实际运行配置：

```toml
[context_memory]
enabled = false
archive_enabled = false
embedding_backend = "tensorrt"
embedding_precision = "fp16"
embedding_worker_python = ""  # 空值使用 data_root/embedding-worker/bin/python
embedding_assets_root = ""    # 空值使用 data_root/embeddings/<profile>
embedding_timeout_seconds = 30.0
embedding_cache_entries = 4096
```

显式路径必须绝对；CPU 后端只能人工指定 `cpu`＋`fp32`，需要另装 ONNX Runtime，
本机未安装、未验收 CPU 后端。CUDA 不可用时绝不静默改用 CPU。
阶段 3 的 verify 总是验证 CUDA FP32/FP16，不以 CPU 结果替代。

本机 profile 为 `lab`，当前路径：

- 独立环境：`/home/ega-agx-orin/.local/share/Lemonbot-wechat-orin/embedding-worker`。
- 制品、引擎、缓存：`/home/ega-agx-orin/.local/share/Lemonbot-wechat-orin/embeddings/lab`。
- 模型子目录：`model/`；引擎：`engine-fp16.plan`、`engine-fp32.plan` 及对应 JSON。
- 缓存：`vector-cache.sqlite3`；最新本地设备报告：`verification.json`。

这些目录与敏感运行状态分开，目录 0700，生成文件 0600，不放入 Git。
缓存存版本化哈希和向量，不存输入原文或账号/会话 ID；仍按私有数据保护。
只安装独立环境，主程序和 worker 都作为桌面用户运行：

```bash
cd /home/ega-agx-orin/Lemonbot
uv run --locked python scripts/embedding_setup.py install --config config/lemonbot.toml
uv run --locked python scripts/embedding_setup.py assets --config config/lemonbot.toml
uv run --locked python scripts/embedding_setup.py build --precision fp32 --config config/lemonbot.toml
uv run --locked python scripts/embedding_setup.py build --precision fp16 --config config/lemonbot.toml
uv run --locked python scripts/embedding_setup.py verify --config config/lemonbot.toml
```

若临时目录是本机 uv 缓存，可给每条命令加 `UV_CACHE_DIR=/tmp/lemonbot-uv`。
install 创建仅 worker 使用的 system-site-packages venv，以复用系统 TensorRT；按
[哈希锁定 requirements](../../../deploy/embedding-worker-requirements.txt) 安装依赖，再安装本 checkout
的 editable 包且不展开项目依赖。拒绝把核心环境当作 worker；重复执行不重建已有 venv。
锁文件更新后须同步导出：

```bash
uv export --locked --only-group embedding-worker --no-emit-project \
  --format requirements-txt --output-file deploy/embedding-worker-requirements.txt
```

assets 校验固定大小与哈希，拒绝覆盖冲突文件。build 遇到相同环境引擎只校验并复用；
GPU/驱动/runtime/TensorRT/模型版本、形状或精度不匹配时报错。运行 worker 不下载或构建。
需要重建时，先停止独立 worker，保留旧引擎及 JSON 为备份，再显式 build 和 verify。
不要手改身份记录绕过核验。脚本不控制微信或机器人服务。

verify 对固定合成样例比较 CUDA FP32 与 FP16；余弦相似度最低必须 ≥0.999，
且六个检索样例均选中预期文档。未通过就选择 CUDA FP32，并在报告写明 FP16 结果。
报告不会自动改变主配置；后续启用阶段应按该设备报告显式选择精度。
FP32 参考关闭 TF32；FP16 允许 TensorRT 按层保留更高精度。

## 进程接口与边界

主进程通过 [IsolatedEmbeddingWorker](../../../src/lemonbot/embeddings/proxy.py) 创建 worker，
使用现有 supervisor 的最小环境变量集合和 `python -I -m lemonbot.embeddings.worker`。
协议仅含 init、embed、chunk、stats 和 shutdown，沿用 1 MiB 帧上限。
主进程导入 proxy 不加载 NumPy、TensorRT、ONNX Runtime 或 torch。
没有新增接收循环、模型 API、工具或发送入口；进程隔离不等于操作系统网络沙箱。

- 一批 1–16 段，每段最多 4000 字符，分词后含 special tokens/查询指令最多 512 tokens。
  超限 embed 明确拒绝；先用 `chunks(text, purpose)` 分块，最多 100000 字符、256 段，
  保留完整原文与精确字符范围，不丢弃空白或静默截断。调用方按最多 16 段再组批。
- 队列最多 16 个等待请求，另有一个执行中的请求。查询优先于排队文档任务，
  不中断已执行批次；同优先级 FIFO。队满返回 busy，由上层决定处理，不自动重试。
- 默认 RPC 30 秒，最多配置 300 秒。超时、退出、取消活动请求、帧或结果异常会终止
  当前 worker；不自动重启、不回退 CPU。已取消的排队任务直接跳过。
- 输入错误可明确拒绝而保持 worker；GPU、引擎和未知内部错误终止 worker。
  错误返回原因码，不输出输入正文。初始化校验 backend/precision，响应校验请求 ID、
  数量、版本、512 维有限单位向量；错误结果不会进入调用者的索引。
- 向量缓存按模型/设备推理身份、分块版本、用途和原文哈希区分，默认 LRU 4096 条。
  重启可复用；输入仍先校验 token 上限。缓存损坏行会删除后重新推理。
  语义向量缓存不代替会话授权，阶段 4 必须先隔离账号和会话再检索。
- TensorRT workspace 上限 2 GiB，仅限制 builder 的 workspace，不能当作 GPU/进程总内存上限。
  最大输入 profile 是 16×512，运行时拒绝超出该范围，不扩大 workspace 或 profile 自动补救。

常见原因码：`embedding_tensorrt_unavailable`、`embedding_cuda_unavailable`、
`embedding_cuda_error_<数字>`、`embedding_engine_missing`、`embedding_engine_incompatible`、
`embedding_engine_hash_mismatch`、`embedding_text_requires_chunking`、`embedding_worker_busy`、
`embedding_worker_unavailable`。缺模型、依赖或版本不匹配时先运行显式 setup 命令，
不要通过改动机器人绑定、暂停状态或数据库解决 worker 环境问题。

## 设备验收记录

完整脱敏报告见 [2026-10-15 CUDA 证据](../../evidence/context-memory-stage3-cuda-20261015.json)。
本报告没有聊天内容、账号标识、凭据或 GPU UUID。固定文档、查询、空白、混合中英文/emoji、
长文和最大 16×512 profile 均参与验证；六个样例两种精度均排名正确。
FP16/FP32 最低余弦相似度 **0.999991845**，选择原生 TensorRT FP16。

| 指标 | CUDA FP32 | CUDA FP16 |
|---|---:|---:|
| 后端冷加载 | 576.44 ms | 197.82 ms |
| 短文 batch=1 中位数 / p95 | 1.849 / 3.592 ms | 0.854 / 1.482 ms |
| 短文 batch=16 中位数 / p95 | 6.292 / 8.332 ms | 1.547 / 3.179 ms |
| 短文 batch=16 吞吐 | 2435 段/秒 | 9148 段/秒 |

上述后端测量包含内存分配、CPU/GPU 拷贝和同步，不包含分词、归一化、IPC 或 SQLite。
热身 3 次后，单条采样 30 次、批量采样 10 次；短文吞吐不代表满长序列或聊天端到端速度，
没有锁设备功耗/频率，结果受同机负载影响。

完整 worker 冷启动约 997 ms；首次未命中 RPC 56.60 ms，命中 6.55 ms。
18 段长文重组与原文完全相同；关闭重开后命中缓存、0 次推理。
worker 峰值 RSS 约 810 MiB，验收父进程约 765 MiB。
CUDA `mem_get_info` 记录了设备全局总量/空闲量，Jetson 使用共享内存，不能据此认定
该进程独占显存或隔离出精确 GPU 占用。

本机引擎 SHA-256（不同设备或重建可能变化）：

- FP32：`4767715dfbe07ffebd1a7bd74c70dab004cd59b271254a528dd3a1eccd527878`。
- FP16：`2dde4ec81fbf7896add7439977261c3ca24c0655069fbb00bd54724964e49e87`。

自动化测试不要求 GPU 或权重，使用临时 SQLite 与合成 worker 验证故障与进程行为。
真实 CUDA 验收独立执行。以上不代表混合检索、分类/回答、微信新上下文或图片关联问题已解决；
这些仍按后续阶段实施与验收。
