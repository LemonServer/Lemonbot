# Linux 微信代码现状

> 更新：2026-10-09。正式 runtime 接入验收通过：testing 单群合成指令收发、程序重启、持久暂停及显式恢复均通过，正式入口完成约 12 分钟稳定采样，用户决定提前结束并接受结果（未完成原定三十分钟窗口）。仓库默认配置继续关闭，模型和工具关闭，仍需人工登录并保持 testing 前台。
> 475 项测试、Ruff、mypy（112 个源文件）和锁检查通过；旧 UNKNOWN 保留，PR #171 冻结。

## 原生 AT-SPI 历史状态

> 研究事实复核：2026-09-10；路线更新：2026-09-29（Asia/Shanghai）。代码历史基线：`f96d479` + 本次归档的研究修改。
> 已确认状态：Lemonbot 原生产 connector、enrollment、模型和 outbox 仍硬关闭。
> 2026-09-29 历史优先级：限时复现 agent-wechat；环境状态及逐步验收见
> [`agent-wechat-reproduction.md`](agent-wechat-reproduction.md)。阶段结论为部分通过：核心 API 收发、身份区分、连续 sender 一致、重启和送达确认成功；testing 已在前台时 `/open` 跳过，Escape 后从无会话视图打开返回 `no_action_selected`。上游 a11y 回退端点在固定镜像中为 404，离屏列表未验证。当时实验容器停止，两个专用数据卷保留；暂不集成。当时通用 `/api/status` 报告 `logged_out`；本次登录以当前 `/api/status/auth` 实测为准。

本文是当前进度、阻塞和研究结果的统一入口。下一阶段实施顺序见
[`PLAN_linux.md`](../PLAN_linux.md)，命令与操作要求见 [`operations.md`](operations.md)，
信任边界见 [`security.md`](security.md)，旧路线和早期实验见
[`research-handoff.md`](research-handoff.md)。历史文档中的“当前”“下一步”不覆盖本次复核结论。

## 仓库基线与提交沿革

- 本次文档整合前，工作树干净，`HEAD` 与本地远端跟踪引用 `origin/main` 均为 `66de344`。
  未查询远端服务器，也未执行 push；本地跟踪引用不能证明服务器实时状态。
- `188ae10`、`5699977`：建立 Linux 部署基础与 Observe runtime，主线移除 Windows 和企业微信通道。
- 2026-08-29 的后续修复处理 AT-SPI 注册、原生崩溃、轮询与消息行识别。
- `2f857e4`：实机无法证明消息方向后，硬关闭 enrollment 与 runtime。
- `e521df8`、`5ad3c3d`：提交视觉校准模型、Portal、本地 OCR、testing 动作面和发送研究代码。
- `8a57fbb`：整理研究记录；其中未提交状态已被后续提交和本次复核替代。
- `66de344`：已提交微信 user unit 兼容性修改及测试，删除目标 VM 不支持、导致
  `218/CAPABILITIES` 的部分 sandbox 指令。提交存在不代表本次已安装或验证 VM 服务。

## 进度与阻塞诊断

核心存储、策略、模型、记忆、审批和 outbox 已具备实现与测试；个人微信通道尚未完成真实入站
到回复的闭环。当前是“框架已建立，通道未通过验收”，不是等待开启配置的成品。

| 能力 | 已有证据 | 缺失的验收条件 |
|---|---|---|
| 核心事件链路 | fake connector 与集成测试 | 真实微信通道接入验收 |
| AT-SPI 读取 | 树、标题、canary 消息行可读 | self/peer 方向、稳定目标和群 sender 身份 |
| Portal/OCR | 单窗口内存抓帧与校准代码 | 两轮校准、重启和锁屏覆盖 |
| testing 发送 | setter、焦点、按键与回读研究 | 与人工操作隔离的自动提交证据 |
| Observe/runtime | 框架、配置检查和关闭门禁 | 满足语义、身份与生命周期条件后的实机验收 |

主要停滞原因：

1. 纯 AT-SPI 方案依赖客户端没有暴露的方向和 sender 属性，继续修复轮询不能弥补语义缺失。
2. 视觉校准只提供方向线索，而现有 enrollment 要求不同的 self/inbound 结构签名，群聊还要求
   稳定 sender 属性。两者之间尚无已验证的接入方案；视觉通过不会自动解锁 connector。
3. 发送返回值不能证明微信执行了动作；人工发送曾污染旧 canary 的归因。发送成功即使被证明，
   也不能解决入站身份阻塞。
4. 两轮只读校准未闭环时已扩展多种发送分支，工作量增加但未解除 Observe 的前置条件。
5. 多份文档混合了历史计划、实机事实和未执行候选，过期提交状态与动作边界增加了接手成本。

## 已实现且通过测试的研究能力

### AT-SPI 观察

- `linux-atspi-probe`：输出角色、接口、固定词表计数和无可见文本结构摘要。
- `linux-atspi-semantic-probe`：以有界只读轮询匹配 private/group canary，不注册原生事件监听器。
- 消息节点修正为实际 `list item`，不会再把外层 transcript list 误认为消息行。
- 报告和 CLI 错误输出均经过最小化，不包含正文、昵称、截图、原始 ID 或异常详情。
- enrollment 与 `wechat_atspi` runtime 仍由代码拒绝。

### Portal 本地视觉研究

- 经用户显式授权的单窗口 ScreenCast，隐藏光标并只消费有界内存帧。
- 本地布局摘要和 RapidOCR 最小化 worker；不调用云视觉，不持久化截图或 OCR 原文。
- sender 显示标签只转换为会话内加盐的 `unverified_display_sender`，不能用于身份、白名单、权限
  或跨会话关联。
- 当前只完成基础抓帧；两轮视觉校准、微信重启覆盖和锁屏/解锁覆盖尚未完成。

### testing 群动作研究

- 精确要求当前唯一可见标题为 `testing`，并绑定唯一输入框与发送控件结构。
- 动作面只读探针跳过输入框名称和消息列表正文，固定 `actions_performed=0`。
- Linux 微信发送控件唯一 AT-SPI Action 的真实名称是 `SetFocus`，不是 click/press/activate。
- 按钮 `SetFocus` 返回成功后，`FOCUSED` 状态会延迟出现；代码使用最多 2 秒的有界轮询。
- 输入框 `Component.grab_focus()` 会返回成功，但输入框、子树乃至应用树都不暴露可证明的
  `FOCUSED` 状态；实机也观察到它没有让真实输入光标进入闪烁状态。
- visually empty 的 Qt 编辑器仍暴露固定非空 accessible placeholder，因此草稿只能分类为
  `empty`、`generated_canary` 或 `unclassified`，不能把 `unclassified` 当作存在用户正文。
- `EditableText.set_text_contents()` 可返回成功并改变可见编辑区，但 accessible name/Text 快照
  可能完全不变。testing-only 代码可记录 `operator_plus_setter`，但这不是生产级草稿证明。

## 发送实验的真实结果

所有实验都限定在用户明确授权的当前 `testing` 群，使用程序生成的 `LB26_SEND_*` canary；进入
可能产生外部结果的按键后均禁止自动重试。没有使用鼠标坐标点击、剪贴板、Hook、注入或协议调用。

| 路径 | 实机结果 | 结论 |
|---|---|---|
| 直接调用发送控件 Action | 只改变焦点 | Action 是 `SetFocus`，不是发送 |
| 发送按钮焦点 + Return | canary 留在草稿区 | 普通按钮不是 default button；无发送 |
| 发送按钮焦点 + Space | canary 留在草稿区 | AT-SPI 接受事件，微信未激活发送 |
| 发送按钮焦点 + Alt+S | canary 留在草稿区 | 快捷键焦点上下文错误 |
| `Component.grab_focus()` 输入框 + Alt+S | 新 canary 精确回读为 0 | 返回成功不等于真实光标焦点 |
| 操作者确认光标闪烁 + `LOCKMODIFIERS` Alt+S | 新 canary 精确回读为 0 | modifier lock 未被微信识别为真实 Alt chord |
| 操作者手动发送 | transcript 可读到唯一精确 canary 哈希 | 证明回读可用，不证明自动提交 |

曾有一次自动实验后出现旧 canary 的唯一 transcript 哈希命中；操作者随后澄清该消息由其手动
实验发送。因此该命中不得作为自动发送成功证据。之后使用工具独占跟踪的全新 canary 复测，
transcript 命中始终为 0。当前正确结论是：**自动发送尚未被证明。**

现有内部探针已经能够表达以下 testing-only 分支，但它们尚未形成稳定的公共 CLI/API：

- 生成新 canary、记录 setter 回执、聚焦发送按钮或输入框、单次按键和精确回读。
- 对用户已确认仍在草稿区的 canary，仅持有 SHA-256 并执行一次恢复提交。
- Return keysym `0xFF0D` 和 Space keysym `0x20` 的正确 AT-SPI `SYM` 调用。
- `LOCKMODIFIERS → S → UNLOCKMODIFIERS`，且 Alt 解锁位于 `finally`。

2026-09-07 代码复核：当前 confirmed-draft 分支仍会聚焦输入框或发送按钮；历史记录中的
“保持操作者当前焦点”分支不在当前实现中，不能按已有能力使用。

上述代码不得接入 connector。公共 `linux-atspi-testing-send-canary` 的接口、脱敏器和运行手册在
发送机制稳定前还需要再次收敛，不能将内部 argparse 研究开关视为受支持命令。

## 测试证据与适用范围

2026-09-03 历史交接记录报告以下成绩，未在本次 Windows 复核中重现为 Linux 实机验收：

```text
pytest: 278 passed, 1 third-party deprecation warning
ruff:   passed
mypy:   passed (104 source files)
git diff --check: passed
```

第三方 warning 来自 Starlette `TestClient` 对 `httpx` 的弃用提示，与微信研究路径无关。

2026-09-05 在 Windows checkout、现有 `.venv312` 中复核代码基线 `66de344`：

- pytest：`260 passed, 13 failed, 5 skipped`，1 个第三方 warning。
- 失败涉及 Linux `fcntl` 文件锁及相关 CLI、Portal 图形会话要求、POSIX 文件权限断言；不能
  据此判定 Linux 回归失败，也不能把本次结果记为全绿。
- Ruff、mypy（104 个源文件）和 `git diff --check` 通过。
- 本次未重跑 Ubuntu 回归、未连接微信实机、未取得新的视觉或发送证据。

后续验收须在 Ubuntu 使用锁定依赖执行完整回归，并把代码 SHA、环境、通过/失败/跳过数与
实机报告一起记录。fake 树测试只验证程序分支，不替代客户端能力证明。

### 2026-09-07 离线推进

已从本地 `origin/main` 快进合入 `f96d479` 文档提交；未访问远端服务器、未 push。本轮修复：

- 内部探针在加载 AT-SPI 前拒绝互斥模式、孤立参数和无效确认哈希，防止“仅检查”选项被发送
  分支忽略。
- 公共发送报告保留草稿确认方式，校验本次命令模式、回读数量、草稿清空状态与结果的一致性；
  无效报告只返回未知，且不重试或输出原文。
- 新增假控件/子进程报告回归，覆盖重复回读、矛盾证据、畸形字段和参数混用。
- Portal CLI 统一校验并过滤来源、单窗口、帧数、尺寸和本地处理声明；新校准报告保存
  `capture_facts`，合并时拒绝缺失捕获证据和类型不严格的样本。历史报告须重新采集。
- 捕获尺寸变化立即停止管线；无法裁剪标签时返回 OCR 歧义，不能误作“已确认没有标签”。
- 补充账号、目标会话、私聊对端和群 sender 的身份依据复核，明确视觉通过仍不能进入 Observe。

验证环境为 Ubuntu 24.04.3 LTS / Python 3.12.3，使用现有 `.venv`：完整 pytest 为
`350 passed, 0 failed, 0 skipped`，1 个上述第三方弃用 warning；Ruff、严格 mypy（104 个
源文件）、`uv lock --check --offline` 和 `git diff --check` 均通过。沙箱内最小
`aiosqlite.connect(":memory:")` 也超时，完整 pytest 经授权在沙箱外执行。未重新同步依赖；
本机未发现 `gitleaks` 可执行文件，本轮未执行密钥扫描，仍需 CI 验证。

本轮未调用真实微信、Portal 或 OCR，未新增方向、身份、生命周期或自动发送实机证据。
当时提出的视觉校准顺序已由 2026-09-29 的 agent-wechat 限时复现计划取代。

## 仍然关闭的安全门禁

- AT-SPI 无法区分 self/peer 消息方向，也无法证明群 sender 身份。
- 显示标签、左右位置、颜色和消息顺序都不能作为授权身份。
- 输入框真实键盘焦点没有机器可验证状态。
- 自动发送尚未完成一轮与人工操作隔离的精确回读证明。
- Portal 视觉校准尚未完成两轮、重启和锁屏/解锁覆盖。
- 因此不得 enrollment，不得运行 connector，不得接模型、自动回复、主动消息或联系人白名单。

## 历史身份调研与 Observe 门禁结论（截至 2026-09-10）

> 本节记录当时的 AT-SPI/资料卡研究和 Observe 门禁判断；其中视觉校准作为下一项工作的建议已过期。
> 当前执行顺序以 [`PLAN_linux.md`](../PLAN_linux.md) 和
> [`agent-wechat-reproduction.md`](agent-wechat-reproduction.md) 为准。

2026-09-09/10 新的非 OCR 实机结论见
[`AT-SPI 身份调研`](atspi-identity-research.md)：`testing` 子树的独立 accessible ID 均为空，
消息行无关系或动作；但操作者从消息头像打开资料卡后，`Weixin ID` 标签与非空值可直接读取。
使用两个独立子进程完成的一轮同成员资料卡关闭/重开对比，字段值摘要与结构均一致。不同成员
对比因暂时缺少第二个样本而未执行；长连接比较曾挂起，独立短生命周期读取恢复正常。
这修正了“缺少任何具体身份来源”的研究前提，但尚未证明自动消息到资料卡绑定、跨重启稳定性，
不能据此开启 Observe。当时建议优先验证资料卡字段与消息关联；该建议已由 2026-09-29 的复现计划取代。

### 身份依据复核（2026-09-07，代码审阅与既有实机记录）

| 对象 | 当前证据来源 | 能证明什么 | 尚缺什么 |
|---|---|---|---|
| 登录账号 | `semantic_probe` 将操作者输入的登记短语转成 `account_fingerprint` | 本次使用了相同登记短语 | 客户端当前登录账号与该短语的机器可验证绑定 |
| 目标会话 | 精确标题匹配、header 路径与标题 SHA-256 | 当前树中找到对应标题及结构位置 | 不依赖显示名、能区分同名会话且跨重启稳定的目标身份 |
| 私聊对端 | 已知方向 canary、消息行结构；未来视觉布局线索 | 本轮合成消息位置；视觉通过后最多提供方向线索 | 对端稳定身份与入站来源的独立绑定 |
| 群 sender | `_sender_identity_candidate` 搜索非正文子节点的 ID 类属性；OCR 标签转为会话内加盐摘要 | ID 属性候选或局部显示标签相同 | 已测客户端无稳定 sender 属性；候选属性本身也需要证明其代表微信身份 |

依据见 [`linux_atspi_probe.py`](../src/lemonbot/connectors/linux_atspi_probe.py) 的
`semantic_probe` / `_sender_identity_candidate`，以及
[`visual_calibration.py`](../src/lemonbot/research/visual_calibration.py) 的显示标签摘要与方向判定。
表中身份缺口是代码审阅结合既有记录得出的结论，本轮没有新实机观测。哈希不会把用户输入、
标题或显示标签转成经过验证的微信身份。

因此当前证据不足以进入 Observe；即便两轮视觉校准通过，上述账号、目标与 sender 绑定仍须
独立解决。没有新的具体身份来源时停止 connector 扩展，继续修改轮询或发送方式不能解除该阻塞。

当时的视觉校准和身份评估建议不再是当前实施顺序；当前工作以新的
[`agent-wechat-reproduction.md`](agent-wechat-reproduction.md) 为准。发送研究仍不作为解锁 Observe 的捷径。`Alt press → S press/release → Alt release` 仍是未执行候选，
不能写成成功机制；`LOCKMODIFIERS` 的失败记录不应被重复解释为成功证据。

Observe 门禁仍保持关闭。复现 agent-wechat 的成功只作为后续适配层输入，不自动开启生产 connector；
改变产品范围或信任边界不能通过改门禁常量解决。
