# Portal 视觉校准研究设计

## 状态与目标

本设计是独立、默认关闭的只读研究，不是 connector。目标仅是判断：经用户明确授权的本地视觉
处理能否在微信 4.1.1.8 中稳定建立 self/peer 的“方向线索”，并识别群聊段首显示标签与连续
消息的局部归属。它不证明微信身份，不允许 enrollment、读取 connector、模型调用或回复生成。

当前代码包含 `lemonbot.research.visual_calibration` 的脱敏证据模型、独立的 Portal
ScreenCast 探针和本地 OCR 最小化 worker。基础探针只允许选择一个窗口、隐藏光标、在内存中
消费有界帧数，并只输出帧数、尺寸和像素格式。校准模式只处理当前三条 canary 行：计算不含
正文/颜色的边缘布局摘要，并将气泡上方的小块内存 PNG 交给本地 RapidOCR；worker 只返回计数、
歧义位和会话内加盐的 `unverified_display_sender`。没有截图落盘、云视觉、systemd 服务、
connector 或 runtime 接线。

本地图形会话中的操作者可运行：

```bash
uv run lemonbot channel linux-portal-screen-probe --frames 2 --timeout-seconds 60
```

系统必须显示 Portal 选择器，由用户明确选择微信窗口并授权。取消、超时、多窗口或 PipeWire
异常都只输出安全错误类别。

在操作者已经手动打开 `testing` 群后，可先验证动作面仍为只读：

```bash
uv run lemonbot channel linux-atspi-testing-action-probe --confirm-testing
```

输出中的 `actions_performed` 必须为 0；该结果不授权发送。一次校准使用最近 5 分钟内、权限为
`0600` 的群语义报告。语义探针会给出三条一次性 canary：self、对端段首、同一对端紧接发送的
continuation。三条都唯一出现后，在本地图形会话运行：

```bash
uv run lemonbot channel linux-portal-group-calibration \
  --semantic-report /absolute/path/group-semantic.json \
  --output /absolute/path/visual-round-1.json
```

每一轮都会再次出现 Portal 选择器，且单轮报告固定 `passed=false`。完成两轮并确实分别覆盖微信
重启和锁屏/解锁后，才可离线合并：

```bash
uv run lemonbot channel linux-portal-group-calibration-verify \
  --report /absolute/path/visual-round-1.json \
  --report /absolute/path/visual-round-2.json \
  --output /absolute/path/visual-decision.json
```

即使合并结果 `passed=true`，identity、connector enrollment 和回复生成仍固定为 false。

Linux 微信 4.1.1.8 实测把消息 item 的矩形暴露为“同原点、累计高度”，不能直接当作单行截图
区域。语义报告必须同时记录每条 canary 的前一兄弟矩形；只有原点/宽度相同、累计高度严格增加、
三条 canary 同属一个 transcript 且索引顺序受限时，才以高度差归一化。peer 必须紧随 self；
continuation 最多允许一个客户端内部 list item。该中间项不被解释为发送者或身份，continuation
仍必须通过本地视觉证明没有新段首标签。任何矩形重叠或路径异常立即拒绝 Portal 分析。

## 获取与生命周期边界

1. 用户必须在本地图形会话中通过 xdg-desktop-portal ScreenCast 明确选择和授权窗口。
2. 不调用 GNOME Shell 私有截图接口，不从 SSH 获取画面，不模拟确认，不绕过 Wayland。
3. 原始帧只在内存中处理；如库确需临时文件，只能使用权限受控的专用临时目录，并在每帧分析
   后关闭句柄和删除。崩溃恢复必须能清理遗留文件，未证明前不得实现落盘路径。
4. 不把聊天画面交给 DeepSeek、智谱或其他云/远程模型。本地 OCR 只在内存中匹配本轮 canary
   和提取显示标签，随后立即丢弃原文。
5. 不记录聊天正文、canary、昵称、截图、窗口标题或原始 OCR 输出。

## 最小化证据

每轮只允许向判定器提交：随机 `run_ref`、Portal/本地处理声明、生命周期覆盖标记、self/peer
canary 对应的非文本布局摘要，以及“段首标签锚定”和“连续消息归属”是否无歧义。布局摘要不能
包含正文、标签或颜色值，并且只有 canary 已知方向能为它赋义。

显示标签先使用每个图形会话新生成的随机 salt 和 session ref 在本地计算
`unverified_display_sender`。原始标签立即丢弃；摘要不能跨会话关联，也不能用于联系人白名单、
管理员、权限、限额或任何安全决策。

## 通过条件

- 至少两轮独立 canary 校准，self/peer 布局在每轮内不同、跨轮稳定。
- 校准集合覆盖一次微信重启和一次锁屏/解锁。
- 每轮都证明段首标签锚定和同段连续消息归属，无弹窗、遮挡、缩放或 OCR 歧义。
- 每轮均来自显式 Portal 授权且只在本地处理；任何云处理标记立即失败。

“通过”只表示可以继续研究方向线索。判定结果固定为
`identity_authorized=false`、`connector_enrollment_allowed=false` 和
`reply_generation_allowed=false`。

## 停止条件

未知布局、布局漂移、self/peer 摘要相同、缺失发送者标签、连续段边界不明、OCR 多解、窗口尺寸
或缩放未校准、Portal 授权撤销、锁屏、微信重启、弹窗或捕获中断时，立即输出 `unknown` 并停止
处理。不猜测、不补抓、不向模型生成回复。
