# Linux 微信代码现状

> 复核日期：2026-09-05（Asia/Shanghai）；代码基线：`66de344`
> 适用环境：Ubuntu 24.04 Desktop、GNOME Wayland、官方微信 4.1.1.8  
> 当前结论：只读研究可继续；自动发送尚未被独立证明；生产 connector、enrollment、模型和 outbox
> 仍保持硬关闭。

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
- 保持操作者当前焦点、不再调用任何聚焦接口的提交分支。

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

## 仍然关闭的安全门禁

- AT-SPI 无法区分 self/peer 消息方向，也无法证明群 sender 身份。
- 显示标签、左右位置、颜色和消息顺序都不能作为授权身份。
- 输入框真实键盘焦点没有机器可验证状态。
- 自动发送尚未完成一轮与人工操作隔离的精确回读证明。
- Portal 视觉校准尚未完成两轮、重启和锁屏/解锁覆盖。
- 因此不得 enrollment，不得运行 connector，不得接模型、自动回复、主动消息或联系人白名单。

## 下一步与停止条件

优先完成现有只读视觉校准，再单独评估账号、目标会话、私聊对端和群 sender 的身份依据。
具体交付、顺序和验收见 [`PLAN_linux.md`](../PLAN_linux.md)。发送研究降为独立、有限实验，
不作为解锁 Observe 的捷径。`Alt press → S press/release → Alt release` 仍是未执行候选，
不能写成成功机制；`LOCKMODIFIERS` 的失败记录不应被重复解释为成功证据。

若当前边界内没有新的可靠身份来源，应交付“当前约束下无法进入 Observe”的明确结论并停止
connector 扩展。改变平台、产品范围或信任边界需要新的决策，不能通过改门禁常量解决。
