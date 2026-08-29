# Lemonbot 2026

Lemonbot 是一个运行在 Ubuntu 24.04 GNOME Wayland 虚拟机中的、能力受限的个人微信代理框架。
当前里程碑只实现官方 Linux 微信的 AT-SPI `observe`：读取并持久化经过登记的私聊和测试群
入站文本，不调用模型、不生成草稿、不写输入框、不点击、不导航，也不发送任何消息。

Windows UIA、pywechat、托盘、Job Object、Credential Manager 和企业微信 connector 已从当前
主线移除。历史调查、失败路线和实机证据保留在
[研发沿革与工程交接](docs/research-handoff.md)，当前实施门禁见 [Linux 计划](PLAN_linux.md)。

## 安全边界

- 只允许 Ubuntu 24.04 x86_64、GNOME Wayland、独立低权限用户和微信测试号。
- 不包含 Hook、注入、ptrace、协议逆向、微信数据库读取、截图点击、坐标点击、键盘模拟或
  剪贴板控制。
- AT-SPI worker 没有动作协议；`deliver()` 永远返回 `observe_only`。
- Observe runtime 不启动模型、浏览器、视觉、MCP、主动任务或 outbox dispatcher，配置必须为
  `models.provider = "disabled"`，也不要求 DeepSeek 密钥。
- worker 通过 `systemd-run + bubblewrap + xdg-dbus-proxy` 隔离，只能访问过滤后的 AT-SPI bus，
  不能联网、读取 Home、`lab.db`、微信数据目录或 Secret Service。
- 初次看到会话时只建立 transcript baseline，不把屏幕历史当成新消息；尾部不能唯一对齐时
  暂停，不猜测、不补抓。
- 原始白名单入站正文保存在本机 `lab.db`，不会进入日志、探针报告、云 API 或模型。

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

## 语义门控与登记

先验证只读结构：

```bash
uv run lemonbot channel linux-atspi-probe
```

私聊和专用测试群各运行两次语义探针。命令会隐藏读取账号登记短语和当前聊天标题，显示两个
一次性 canary；按提示分别由本账号和对端账号手动发送。真实 UI 文本和 canary 不写入报告。

```bash
uv run lemonbot channel linux-atspi-semantic-probe \
  --kind private --output /home/lemon/.local/share/Lemonbot/private-1.json
uv run lemonbot channel linux-atspi-semantic-probe \
  --kind group --output /home/lemon/.local/share/Lemonbot/group-1.json
```

第二轮必须覆盖微信重启；四轮中还要完成一次锁屏/解锁后重新测试。只有四份脱敏报告结构一致
时才能生成 enrollment：

```bash
uv run lemonbot channel linux-atspi-enroll \
  --private-report /absolute/private-1.json \
  --private-report /absolute/private-2.json \
  --group-report /absolute/group-1.json \
  --group-report /absolute/group-2.json \
  --output /home/lemon/.config/Lemonbot/atspi-enrollment.json \
  --confirm-restart --confirm-lock-cycle
```

命令输出需写入配置的 `account_fingerprint`、`ui_signature`、
`enrollment_bundle_sha256` 和 `allow_target_refs`。enrollment 文件权限为 `0600`，默认生成随机
target ref，且不包含联系人、
群名、消息正文或 canary。

## 部署与运行

完成配置后：

```bash
uv run lemonbot doctor --config ~/.config/Lemonbot/config.toml
uv run lemonbot install-service --config ~/.config/Lemonbot/config.toml
systemctl --user status lemonbot.service
```

`install-service` 会创建基于 `/usr/bin/python3 --system-site-packages` 的最小 AT-SPI worker venv，
安装固定版本的 Pydantic 和当前 Lemonbot wheel，并安装两个 user systemd unit。正式 AT-SPI
connector 只允许从 `lemonbot.service` 内启动。

管理台只监听 `127.0.0.1`。没有 enrollment 时继续使用 `runtime.connector = "fake"` 做离线测试：

```bash
uv run lemonbot smoke
uv run lemonbot doctor --config ~/.config/Lemonbot/config.toml
```

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

Observe 连续运行 24 小时并通过验收后，才会另行实施 Linux Draft：通过 Secret Service 启用
DeepSeek API，只生成本地草稿，仍不操作微信。Reply、主动聊天、图片和文件读取不属于当前
版本。

## 开发验证

```bash
uv sync --all-extras --locked
uv run pytest -q
uv run ruff check .
uv run mypy src
```

GitHub CI 只使用 Ubuntu 24.04 / Python 3.12，并执行锁文件检查、pytest、Ruff、严格 mypy 和
当前树 Gitleaks。旧 Git 历史按约定不改写；历史中出现过的凭据必须保持撤销状态。
