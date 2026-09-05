# Lemonbot 2026

Lemonbot 是一个运行在 Ubuntu 24.04 GNOME Wayland 虚拟机中的、能力受限的个人微信代理框架。
当前里程碑仅允许官方 Linux 微信的只读研究。实机已经证明 AT-SPI 可读取控件树，但微信
4.1.1.8 把 self 与 peer 消息暴露为同构、无属性的 `list item`，无法安全证明方向或群发送者。
因此 enrollment 与 `wechat_atspi` runtime 均由代码硬关闭，不通过 connector 读取或持久化聊天，
不调用模型，也不执行微信动作。独立研究探针可在内存中匹配 canary；testing 发送实验另需当次
明确授权，尚未证明自动发送成功，不属于 runtime 能力。

Windows UIA、pywechat、托盘、Job Object、Credential Manager 和企业微信 connector 已从当前
主线移除。历史调查、失败路线和实机证据保留在
[研发沿革与工程交接](docs/research-handoff.md)。接手先读
[当前进度与阻塞](docs/linux-wechat-current-status.md)，再读 [下一阶段计划](PLAN_linux.md)、
[安全边界](docs/security.md) 和 [运行手册](docs/operations.md)。当前优先完成只读视觉校准与
身份可行性结论；发送快捷键成功也不能单独解锁 Observe。

## 安全边界

- 只允许 Ubuntu 24.04 x86_64、GNOME Wayland、独立低权限用户和微信测试号。
- 不包含 Hook、注入、ptrace、协议逆向、微信数据库读取、截图点击、坐标点击或剪贴板控制。
  connector 与只读探针禁止键盘动作；独立 testing canary 实验边界见安全文档和运行手册。
- AT-SPI 只能证明结构可见，不能证明消息方向；不得人工修改报告或配置绕过关闭门禁。
- AT-SPI worker 没有动作协议；`deliver()` 永远返回 `observe_only`。
- Observe runtime 不启动模型、浏览器、视觉、MCP、主动任务或 outbox dispatcher，配置必须为
  `models.provider = "disabled"`，也不要求 DeepSeek 密钥。
- worker 通过 `systemd-run + bubblewrap + xdg-dbus-proxy` 隔离，只能访问过滤后的 AT-SPI bus，
  不能联网、读取 Home、`lab.db`、微信数据目录或 Secret Service。
- 未来 Observe 初次看到会话时只建立 transcript baseline，不把屏幕历史当成新消息；尾部不能唯一对齐时
  暂停，不猜测、不补抓。
- 当前不允许微信入站持久化；未来通过 Observe 门禁后，白名单正文才可保存到本机 `lab.db`，
  不进入日志、探针报告、云 API 或模型。

个人微信自动化仍存在非零账号风控风险。当前版本是实验系统，不宣称腾讯授权或生产可用。

## 环境准备

```bash
sudo apt install python3-gi gir1.2-atspi-2.0 at-spi2-core \
  bubblewrap xdg-dbus-proxy libglib2.0-bin
gsettings set org.gnome.desktop.interface toolkit-accessibility true

cd /home/lemon/Lemonbot
uv sync --all-extras --locked
cp config/lemonbot.example.toml ~/.config/Lemonbot/config.toml
```

不要启用 systemd linger，不要通过 SSH、RDP、VNC、Xvfb 或锁屏状态运行微信观察。微信必须由
图形会话中的 `lemonbot-wechat-accessible.service` 启动，以便只为微信设置
`QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1`。

## 语义研究与关闭门禁

先验证只读结构：

```bash
uv run lemonbot channel linux-atspi-probe
```

语义探针只用于重复验证已知限制。命令会隐藏读取账号登记短语和当前聊天标题，显示两个
一次性 canary；真实 UI 文本和 canary 不写入报告。不要为了取得 `passed=true` 改写报告。

```bash
uv run lemonbot channel linux-atspi-semantic-probe \
  --kind private --output /home/lemon/.local/share/Lemonbot/private-1.json
uv run lemonbot channel linux-atspi-semantic-probe \
  --kind group --output /home/lemon/.local/share/Lemonbot/group-1.json
```

现有实机报告应为 `passed=false`。即使提供伪造的 `passed=true` 报告，下列命令也会安全拒绝，
不会生成 enrollment：

```bash
uv run lemonbot channel linux-atspi-enroll \
  --private-report /absolute/private-1.json \
  --private-report /absolute/private-2.json \
  --group-report /absolute/group-1.json \
  --group-report /absolute/group-2.json \
  --output /home/lemon/.config/Lemonbot/atspi-enrollment.json \
  --confirm-restart --confirm-lock-cycle
```

门禁只有在新的、独立审核的方向证明方案通过后才能由代码变更重新打开，不能靠运行参数打开。
视觉研究设计见 [Portal 视觉校准](docs/visual-calibration.md)。

## 部署与运行

当前不得把 runtime 配置为 `wechat_atspi`，也不得安装或启动正式读取 connector。离线开发只用：

```bash
uv run lemonbot smoke
uv run lemonbot doctor --config ~/.config/Lemonbot/config.toml
```

管理台只监听 `127.0.0.1`。配置必须保持 `runtime.connector = "fake"`、
`wechat_atspi.enabled = false`、`models.provider = "disabled"` 和 `vision.enabled = false`。

紧急停止会写入持久 sentinel；服务重启后仍拒绝运行。恢复不会补抓停机消息：

```bash
uv run lemonbot emergency-stop --config ~/.config/Lemonbot/config.toml
uv run lemonbot resume --config ~/.config/Lemonbot/config.toml --confirm
systemctl --user start lemonbot.service
```

## 数据与后续阶段

```bash
uv run lemonbot backup --config <path>
uv run lemonbot restore <archive.zip> --config <path> --confirm
uv run lemonbot data export --config <path> --output <archive.zip>
uv run lemonbot data delete-conversation wechat_personal_lab <target-ref> \
  --config <path> --confirm
```

只有未来只读路线取得充分安全证明并完成连续验收后，才会另行规划 Draft。Reply、主动聊天、
图片和文件读取不属于当前版本。

## 开发验证

```bash
uv sync --all-extras --locked
uv run pytest -q
uv run ruff check .
uv run mypy src
```

GitHub CI 只使用 Ubuntu 24.04 / Python 3.12，并执行锁文件检查、pytest、Ruff、严格 mypy 和
当前树 Gitleaks。旧 Git 历史按约定不改写；历史中出现过的凭据必须保持撤销状态。
