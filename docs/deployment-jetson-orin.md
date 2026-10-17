# Jetson AGX Orin：ARM64 原生部署与验收

交付结果：**可运行**。ARM64 原生微信、账号/双会话绑定、真实 @、五次模型回复、
唯一新 self 回读、持久上下文及桥/机器人重启均通过两批六条实机验收，服务保持运行。
离线测试与下面的真实通道证据单独记录。首次长期记忆关闭，使用 DeepSeek 云端，不
部署本地模型、不启用 GPU、不替换系统、驱动或 CUDA。

先阅读 [README](../README.md)、[部署](deployment.md)、[运维](operations.md)、
[CONTRIBUTING](../CONTRIBUTING.md)。本页为当前实验路线；历史 Windows、Observe、
AT-SPI 和 PR #171 不作为执行指令。旧机器的数据、绑定、记忆和 UNKNOWN 全部保留。
`--experimental-arm64` 继续作为适配分支的明确 opt-in；原 v1.0.0 标签不变。

## 本次环境与来源

2026-10-12，Asia/Shanghai，目标设备实测：

| 项目 | 版本 / 状态 |
|---|---|
| 设备 | NVIDIA Jetson AGX Orin Developer Kit 64 GB，aarch64 |
| 系统 | Ubuntu 24.04.4 LTS；内核 6.8.12-1021-tegra |
| JetPack / L4T | 7.2.1-b49 / 39.2.1-20260806224157 |
| Docker | 29.9.0，containerd snapshotter |
| Python / uv | 宿主 3.12.3 / 0.13.0；容器 Python 3.10.12 |
| 分支 / 起点 | feat/jetson-orin-arm64 / f3f0249171f3411a44995342473d5d6560de4bfe |
| 已验证代码提交 | 63bc92e8078defbc358a464c265522b0393c1af8 |
| 镜像 / 微信 | 上游 0.7.4 ARM64 候选 / 4.1.0.16 |
| 原生库 | SQLCipher 4.6.1 community，Frida 17.7.3，zstd 1.4.8 |
| 浏览器桥 | noVNC 1.3.0-2 静态资源；websockets 15.0.1 |
| 桌面 | 用户 D-Bus 与 Secret Service 可用，密钥环已解锁 |

唯一固定 ARM64 镜像：

```text
ghcr.io/thisnick/agent-wechat@sha256:decca639793631dc174e09b83937f1f7ed566a0cf94cf2d82ec703c3867e7ae5
```

[上游镜像页面](https://github.com/thisnick/agent-wechat/pkgs/container/agent-wechat/704205072?tag=0.7.4)
与 registry manifest/config 已核验为 `linux/arm64`。压缩层合计 845,571,427 字节，
解压镜像约 2.64 GiB，下载暂存和导入另占空间。镜像内部 Ubuntu 22.04 不改变宿主系统。
拉取前至少 8 GiB，操作期间至少 3 GiB，以 Docker 实际存储盘为准。

镜像没有可验证的源码 revision 标签。密钥提取文件与上游 v0.7.4 提交
`a96d56323087a86ca0ec55266a02527a7d997e7d` 对应文件 SHA-256 相同，并包含 ARM64
客户端配置；这只证明该文件关联，完整镜像来源关联仍未知。不复制上游代码进仓库，
不将 MIT 扩展至镜像、微信或 noVNC，见 [第三方声明](../THIRD_PARTY_NOTICES.md)。

[本次证据](evidence/jetson-orin-20261012.json) 区分原生依赖、合成测试和真实通道。
最终 632 项测试、Ruff、mypy（118 源文件）、锁文件、发布一致性及原生 ARM64
Gitleaks 8.30.1 扫描均通过；锁文件亦经 CI 的 uv 0.12.5 检查。三个用户单位语法和
当前调用的启动报告读取通过。唯一测试警告是既有 Starlette/httpx 弃用提示。
继续执行时重新检查设备现状，不将 dated evidence 当作登录、授权或服务状态。

## 首装与通道门槛

缺少系统依赖由设备管理员安装 Git、Python 3.12/venv、GNOME Keyring、Docker、curl
及 dpkg-deb。uv 按 [官方方法](https://docs.astral.sh/uv/getting-started/installation/)
安装。机器人/setup/uv 以桌面用户运行，只有 Docker 子进程按需使用 `--sudo`。
本设备实际 checkout 是 `/home/ega-agx-orin/Lemonbot`；其他用户使用自己的绝对路径。

```bash
cd /home/ega-agx-orin/Lemonbot
git switch feat/jetson-orin-arm64
uv sync --locked --extra dev --python /usr/bin/python3.12
uv run --locked python -m lemonbot.research.agent_wechat_lab demo
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked mypy src
uv lock --check
uv run --locked python scripts/check_release.py
uv run --locked python scripts/wechat_setup.py environment --experimental-arm64
uv run --locked python scripts/wechat_setup.py open-vnc --experimental-arm64
```

默认不安装 vision 等可选组件，OCR 工作进程按需加载依赖。公开配置保持关闭。
ARM64 的所有 setup 命令需要显式 `--experimental-arm64`。AMD64 保持原 digest、
客户端和路径；未知架构、错误镜像 OS/架构/digest/容器 ID 均拒绝。不使用 QEMU。

`environment` 在新容器创建前运行无网络、只读原生预检。也可执行
`wechat_setup.py image-preflight --experimental-arm64`，验证微信/服务/SQLCipher 的 ELF、
Frida 原生模块、zstd 与 SQLCipher 执行、固定微信版本；结果保存在私有状态中。
预检不验证登录或收发，不读任何账号/数据库密钥。Hook、真实消息表、WAL、sender/self
和顶层 @ 必须通过后续绑定和实机消息核验。有效诊断最多两小时；发生架构 Hook 缺失、
结构不兼容或身份无法确认时停止业务，交付失败能力和唯一下一步，不更换多版本排障。

本次 Docker daemon 直连 GHCR 两次 TLS 超时，没有配置 daemon 代理。采用可用下载路径
逐层校验原始 OCI 内容后导入，导入后原 digest 仍可精确解析。一般部署优先按 digest
拉取；网络故障由管理员配置明确代理或提供完整、已校验且保留 RepoDigest 的 OCI 归档，
不可重新标记其他镜像冒充固定 digest。脚本不修改全局代理，不 prune 镜像/卷。

## 新状态、扫码与身份

Orin 默认独立状态和卷如下；不复用旧机数据，不复制绑定：

```text
/home/ega-agx-orin/Lemonbot/config/lemonbot.toml
/home/ega-agx-orin/Lemonbot/config/wechat-whitelist.local.toml
~/.local/state/lemonbot/jetson-orin/agent-wechat-reproduction/api-token
~/.local/state/lemonbot/jetson-orin/agent-wechat-lab/
~/.local/share/Lemonbot-wechat-orin/
Container: lemonbot-agent-wechat-orin
Volumes: lemonbot-agent-wechat-orin-data, lemonbot-agent-wechat-orin-home
```

同账号扫码前先在旧机器持久暂停、排空、停止机器人及微信容器，保留全部旧状态；
本次用户已确认完成。不同账号也必须重新绑定，不能复制或改写旧信任锚。

候选 0.7.4 没有 `/vnc/` HTTP 路由。setup 为 ARM64 单独安装经过 SHA-256 校验的
Ubuntu noVNC 静态资源到用户 `~/.local/share/Lemonbot-noVNC`，生成并启动
`lemonbot-wechat-novnc.service`，**仅绑定 127.0.0.1:6176**。VNC WebSocket 同时检查
令牌与同源 Origin；浏览器启动凭据使用 URL fragment，并立即从地址栏清除。访问日志
关闭，第三方资源不进仓库，服务不开机自启。微信 API 仍是 `127.0.0.1:6174`。
这解决候选缺少浏览器界面的问题，没有新增业务发送代理。

默认浏览器由 `open-vnc` 打开认证页面。远程访问：

```bash
ssh -N -L 6176:127.0.0.1:6176 ega-agx-orin@DEVICE_ADDRESS
```

然后在本机浏览器使用隧道地址与私有认证。令牌、认证 URL、账号 ID 和聊天内容不写入
报告或聊天，不直接暴露端口。容器继续只使用 SYS_PTRACE 与 seccomp=unconfined，
不 privileged；两个专用卷和只读 token 挂载均校验。

手动扫码、打开唯一 `testing` 群，从机器人账号发一条初始化文字，供 canonical self
核验。候选手动扫码后可能需其官方登录后初始化协议才能填充内部账号字段/密钥；
身份字段缺失时不猜测 ID，不以昵称、目录前缀或旧绑定替代。随后：

```bash
uv run --locked python scripts/wechat_setup.py bind --experimental-arm64
uv run --locked lemonbot secret set deepseek_api_key --profile lab
```

在新状态中，ARM64 `bind` 对“UI 已登录但内部身份字段为空”执行该候选的有界登录后
初始化（`newAccount=false`）。遇到 QR/手机确认即交回人工；已有绑定时不会修复缺失
身份、切换账号或覆盖锚。0.7.4 服务库为明文 SQLite，ARM64 profile 明确指定该只读
模式；AMD64 保持加密服务库。微信消息库始终使用加密快照与原密钥校验，plaintext
不适用于消息查询。

setup 不发消息，不自动启用机器人。私有文件 0600、目录 0700；API 密钥仅隐藏输入并
存于 Secret Service。编辑仓库内 `config/wechat-whitelist.local.toml`，首次验收仅包含
testing 与一个明确授权的精确私聊名称，先手动打开两会话同步。
将生成的 `config/lemonbot.toml` 已有字段改为 `runtime.connector="wechat_agent"`、
`wechat_agent.enabled=true`、`models.provider="deepseek"`，保持 `memory_enabled=false`。
不要追加重复 TOML 表；保留 Orin 数据路径、token/state/whitelist、价格、预算和限制。
默认每日 5 CNY、每月 100 CNY；启用时复核 [模型价格](reference/models/deepseek-pricing.md)。

## 服务与两批六条验收

testing 前台，首装编辑白名单后再 bind 同步。绑定/真实元数据/密钥/明确目标授权均通过
后再启用业务：

```bash
uv run --locked python scripts/wechat_setup.py bind --experimental-arm64
uv run --locked python scripts/wechat_setup.py install-services --experimental-arm64
systemctl --user start lemonbot-wechat-metadata.service
uv run --locked python scripts/wechat_setup.py doctor --experimental-arm64
systemctl --user start lemonbot-wechat-conversation.service
uv run --locked python scripts/wechat_conversation_control.py status
uv run --locked python scripts/wechat_conversation_control.py resume
```

用户单位使用实际 checkout/Python/私有配置与当前容器 IP；setup 不 enable 任何 boot
单位，不启动 metadata/bot 业务。所有入口按显式 `--config`、`LEMONBOT_CONFIG`、
当前 checkout 的 `config/lemonbot.toml` 顺序选择配置，不依赖终端工作目录。
主配置保留 Orin 私有数据路径；旧状态目录里的 TOML 不自动回退加载。
自定义部署明确传 `--config /absolute/path/lemonbot.toml`。桥 localhost 6175、
管理 UI localhost 8767，认证 URL 不公开。resume 核验身份/目标并建立新基线，不回放停机消息。

对端按顺序发六条，应回复的消息收到一次回复后再发下一条，不重发或人工补发机器人回复：

| 批次 / 序号 | 对端输入 | 预期 |
|---|---|---|
| 第一批 1 | testing 普通文字 | 零回复 |
| 第一批 2 | testing 成员选择真 @，要求记住群聊短词 | 确认回复一次 |
| 第一批 3 | 授权私聊要求记住另一短词 | 确认回复一次 |
| 第一批 4 | 私聊追问短词 | 正确回复一次 |
| 第二批 5 | testing 真 @，追问群聊短词 | 重启后正确回复一次 |
| 第二批 6 | 私聊追问私聊短词 | 重启后正确回复一次 |

第一批后安全排空停止机器人，重启桥和机器人，保留暂停并显式恢复：

```bash
uv run --locked python scripts/wechat_bot_restart.py --stop
uv run --locked python scripts/wechat_setup.py environment --experimental-arm64
systemctl --user restart lemonbot-wechat-metadata.service
systemctl --user start lemonbot-wechat-conversation.service
uv run --locked python scripts/wechat_conversation_control.py status
uv run --locked python scripts/wechat_conversation_control.py resume
```

不重启微信、不清除暂停、不重放历史。记录接收/模型/导航/发送确认耗时与费用；报告仅
包含阶段、计数、时间、费用、回执状态，不发布正文、账号或私聊名，不要求用户核对哈希。
不追加三十分钟观察。长期记忆以后单独 opt-in 并记录验收，首次只验证持久上下文。

### 本次实机结果

普通群消息无回复，由用户确认；其余五条均 acknowledged、attempts=1，UNKNOWN=0。
重启后观察到 ManualPause，显式恢复后没有重放旧消息；群/私聊追问各正确回复一次，
没有出现另一作用域的短词。第二批追问未带验收编号，按已记录重启边界和两个已绑定
作用域各自唯一的新事件关联；保留全部独立事件，不选 first/latest 回执或正文去重。

| 序号 | 收取 | 模型请求 | 导航 | POST | 派发至确认 | 总耗时（秒） |
|---|---:|---:|---:|---:|---:|---:|
| 2 群 | 2.786 | 0.908 | 2.997 | 3.659 | 15.592 | 20.504 |
| 3 私聊 | 2.781 | 0.660 | 3.176 | 3.605 | 15.067 | 18.871 |
| 4 私聊 | 2.275 | 0.944 | 已在前台 | 3.565 | 10.675 | 14.609 |
| 5 重启后群 | 2.370 | 1.301 | 2.956 | 3.528 | 15.034 | 19.452 |
| 6 重启后私聊 | 2.610 | 0.718 | 3.374 | 3.545 | 15.864 | 19.496 |

总耗时中位数 19.452 秒。五次模型调用的本地保守费用估算合计 **0.008886 CNY**，
不是供应商账单；无长期记忆提取请求。宿主重启、更多白名单、真实 quoted @、长期
召回和其他 ARM64 设备未在本轮验收，不能外推。合成测试继续覆盖 UNKNOWN 跨重启不重发。

## 运行、恢复与回退

设备重启后按 [Orin 设备重启启动手册](restart-jetson-orin.md) 逐步恢复；其中包含本机
实际路径、正确的 noVNC 入口、密钥环解锁、服务配置刷新及 doctor/显式恢复顺序。

常用：`wechat_conversation_control.py status/pause/resume`；安全重启：
`wechat_bot_restart.py`；安全停止：`wechat_bot_restart.py --stop`。详见 [运维](operations.md)。
改白名单后安全重启再显式 resume。只在本机查看 journal，不发布含名称的原始日志。
本机 user journal 查询为空，但系统 journal 包含用户单位；可按精确单位和当前 UID
查看，安全重启脚本还限定当前 InvocationID，避免读取旧启动报告：

```bash
journalctl _UID=1000 _SYSTEMD_USER_UNIT=lemonbot-wechat-conversation.service -n 60 --no-pager
```

宿主/微信容器重启后人工恢复：排空停止旧业务，`environment --experimental-arm64`、
`open-vnc --experimental-arm64`，登录原账号、解锁密钥环、`install-services` 刷新 IP、
启动/重启桥、doctor，然后启动 bot、显式 resume。无运行机器人和 DB 时跳过首次排空。

| 失败能力 | 处理入口 |
|---|---|
| 架构、镜像、native preflight、微信版本不匹配 | 核对固定 profile；不换 latest 或仿真 |
| Docker 权限/daemon 拉取 | 本机管理员授权/处理网络；保留镜像及卷 |
| 未登录/缺身份/self 缺失 | 原客户端人工登录及初始化；不推断身份 |
| 账号变化、绑定变化、消息表/快照歧义 | 保持暂停，查固定原因码；不改锚、不猜表 |
| 错目标、重复发送、UNKNOWN | 持久暂停并排空，保留 UNKNOWN，绝不自动补发 |

回旧机器前排空停止 Orin bot，再停止 Orin 微信，由用户恢复旧端登录。保留新旧卷及
全部状态，不迁移未确认任务；回退代码不改 schema、身份、暂停或 UNKNOWN。
后续部署结果只能归为：**可运行**（全部实机门槛通过并保持运行）；**代码通过、设备验收未完成**；
**明确阻塞**（失败能力与唯一下一步，业务关闭）。本次 README 已列入上述实际验证环境。
