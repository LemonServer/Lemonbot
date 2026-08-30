# Portal 视觉校准研究设计

## 状态与目标

本设计是独立、默认关闭的只读研究，不是 connector。目标仅是判断：经用户明确授权的本地视觉
处理能否在微信 4.1.1.8 中稳定建立 self/peer 的“方向线索”，并识别群聊段首显示标签与连续
消息的局部归属。它不证明微信身份，不允许 enrollment、读取 connector、模型调用或回复生成。

当前代码只有 `lemonbot.research.visual_calibration` 的脱敏证据模型和判定器，没有截图、OCR、
Portal 客户端、CLI、systemd 服务或 runtime 接线。

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
