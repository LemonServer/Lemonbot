# Linux 微信代码现状

> 快照日期：2026-09-03（Asia/Shanghai）  
> 适用环境：Ubuntu 24.04 Desktop、GNOME Wayland、官方微信 4.1.1.8  
> 当前结论：只读研究可继续；自动发送尚未被独立证明；生产 connector、enrollment、模型和 outbox
> 仍保持硬关闭。

## 仓库状态

- 当前本地 `HEAD` 为 `5ad3c3d`（`feat: add fail-closed Linux visual and send research`）。
- `origin/main` 仍指向 `e521df8`；`5ad3c3d` 尚未 push。
- `5ad3c3d` 已提交 Portal ScreenCast、本地 OCR 最小化 worker、视觉校准模型、testing 动作面与
  初版发送研究代码。
- `5ad3c3d` 之后的输入焦点、按键合成和实机证据修订仍在工作树中，尚未提交。
- systemd user unit 另有一项未提交改动：删除当前用户管理器不支持、会导致
  `218/CAPABILITIES` 的 `ProtectSystem`、`ProtectControlGroups` 和 `ProtectKernel*` 指令。该改动
  会降低显式 sandbox 配置，提交操作已被安全审查拒绝；未经用户知情授权不得提交或安装。
- 未执行 push。

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

## 当前测试状态

当前工作树已完成：

```text
pytest: 278 passed, 1 third-party deprecation warning
ruff:   passed
mypy:   passed (104 source files)
git diff --check: passed
```

第三方 warning 来自 Starlette `TestClient` 对 `httpx` 的弃用提示，与微信研究路径无关。

## 仍然关闭的安全门禁

- AT-SPI 无法区分 self/peer 消息方向，也无法证明群 sender 身份。
- 显示标签、左右位置、颜色和消息顺序都不能作为授权身份。
- 输入框真实键盘焦点没有机器可验证状态。
- 自动发送尚未完成一轮与人工操作隔离的精确回读证明。
- Portal 视觉校准尚未完成两轮、重启和锁屏/解锁覆盖。
- 因此不得 enrollment，不得运行 connector，不得接模型、自动回复、主动消息或联系人白名单。

## 下一步最小工作

1. 不再复用 `LOCKMODIFIERS` 作为 Alt+S 成功证据。
2. 在操作者明确确认光标闪烁后，研究真实硬件序列
   `Alt press → S press/release → Alt release`；必须先只读解析当前键盘映射，Alt release 放在
   `finally`，一次实验只允许一个新 canary。
3. 将该次新 canary 与所有人工消息隔离，使用工具持有的精确值回读；只有唯一新哈希命中才记为
   `readback_unattributed`，仍不得记为 `acknowledged`。
4. 若真实按键序列仍失败，停止键盘路线；不要猜坐标点击。鼠标路线必须先通过 Portal/可靠几何
   证明目标区域，不能使用当前累计矩形直接取中心点。
5. 完成后统一收敛公共 CLI、删除或隐藏失败的内部实验开关，补充完整回归并提交小而清晰的 commit。
