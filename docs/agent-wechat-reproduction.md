# agent-wechat 限时复现记录与操作手册

> 当前状态（2026-10-08）：**新受控单条、连续与 self 实机通过，重启/前台/观察待验证**。
> 全仓 467 项测试、Ruff、mypy（111 个源文件）及锁检查通过；自动化检查不代替实机证据。
> 历史固定 digest 保留，PR #171 冻结。原卷和微信数据未清理，本轮收发仅限 testing 合成标记。
> 当前版本/恢复见 [环境基线](environment-baseline.md)，执行顺序见 [Linux 计划](../PLAN_linux.md)。

## 当前结论

| 检查 | 结果 |
|---|---|
| 当前账号登录、聊天列表与实际 VNC | 已通过，2026-10-06 |
| 新标记单条自动收发、持久化与对端确认 | 已通过，ACKNOWLEDGED，尝试一次 |
| 连续指令 | 新的两条各回复一次，sender 一致；旧 UNKNOWN 保留 |
| self 忽略 | 新 self 标记已被接口读到，无入站/回复，用户确认 |
| 保留卷重启、切走恢复、三十分钟 | 本轮尚未通过 |
| 正式 runtime 集成 | 代码与模拟检查通过；完整通道通过前不实机启用 |

以下章节按各自注明日期保留过程，早期“未开始”“下载中”等表述是历史快照，不覆盖本节。

## 调研结论

agent-wechat 是有源码、Docker 镜像、CLI/API、确定性 UI 状态机和 CI 工作流的真实项目，不只是 OpenClaw 演示。它通过 Docker 中的 Linux 微信客户端接收/发送 UI 动作，并用 Rust 服务提供 REST/WS API；群消息与发送者字段来自本地微信数据库读取。其 README 列出 Docker 与 ptrace 要求；上游 Compose 还配置 `seccomp=unconfined`。透明代理用的 `NET_ADMIN` 是可选项，本实验不启用。[架构与命令](https://github.com/thisnick/agent-wechat#architecture)、[容器配置](https://github.com/thisnick/agent-wechat/blob/main/docker-compose.yml)、[数据库读取实现](https://github.com/thisnick/agent-wechat/blob/main/packages/agent-server-rust/src/tools/wechat_db.rs)

上游有可报告的端到端用户运行案例，但也有登录后聊天列表为空、列表滚出视口后发送计划找不到动作等未解决报告。源码和 CI 不能替代当前 Linux/微信账号上的复现。[登录后无聊天数据报告](https://github.com/thisnick/agent-wechat/issues/153)、[滚出视口发送失败及复现步骤](https://github.com/thisnick/agent-wechat/issues/173)

**证据边界：**项目源码说明发送者值通过 `real_sender_id` 和 `Name2Id` 关联读取；这与我们先前 AT-SPI 消息行无 sender 关系的情况不同。数据库访问包含 SQLCipher 密钥提取能力，README 明确需 ptrace。容器隔离宿主文件系统，但微信登录本身可能同步账号内多个会话；`/api/chats` 也返回会话列表。本实验只保存唯一精确匹配的 `testing` 群引用，只从该群消息接口读取；这不构成底层账号数据的群级隔离。

### 本机准备检查

2026-09-29 本机为 x86_64；Ubuntu home 文件系统初始空闲约 11 GB。用户明确授权后，已更新 apt 索引、安装 Ubuntu `docker.io` 29.1.3 并启动 Docker 守护进程；没有把账号加入 `docker` 组，也没有清理现有文件。预检通过。`git ls-remote` 解析到的上游 `main` 为 `96c2e90c497d999bf5189a134a9b3b293731f42e`。官方 README 与 Compose 明确发布/引用 `ghcr.io/thisnick/agent-wechat`，故项目仓库来源路径已确认；但镜像缺 `org.opencontainers.image.source` 与 `org.opencontainers.image.revision` 两个 OCI 标签，不能证明该镜像构建自记录的 main commit。[上游 README](https://github.com/thisnick/agent-wechat#docker-setup)、[上游 Compose](https://github.com/thisnick/agent-wechat/blob/main/docker-compose.yml)。AMD64 manifest 共 17 层、合计约 1.00 GB。镜像 digest 为 `sha256:97d5a99f933592c15897a7579b4e2b2cbbb5f4dda856228f8271f91dd7cafb43`，锁文件权限 `0600`，Docker server 为 29.1.3；镜像创建时间为 `2026-09-26T22:42:06Z`、大小约 1.00 GB、17 层，image version label 为 `22.04`，未发现 WeChat 版本环境变量。GHCR 下载期间仅为 Docker systemd unit 临时设置主机已有 localhost 代理，成功后已移除 `/run/systemd/system/docker.service.d/http-proxy.conf` 并重启 Docker。第一次容器退出码为 1、`OOMKilled=false`，日志匹配两条 `Operation not permitted`；配置复核发现多余的 `--cap-drop=ALL`。修复为保留 Docker 默认 capabilities、额外加 `CAP_SYS_PTRACE` 后容器运行正常。仅移除了经标签/挂载验证的已退出容器（没有 `-v`），两份带研究标签的卷均确认保留。最终容器隔离配置复核通过：无 capability drop，只加 `CAP_SYS_PTRACE`；`6174/tcp` 仅映射到 `127.0.0.1`，挂载只有两个 named volumes 和只读 token 文件。`/api/status/auth` 为 `logged_in`，登录用户对象存在；20 个会话可由 API 列出。已读取唯一 canary marker 的一条消息，不读取其他会话消息正文。WeChat 包为 `wechat=4.1.13.23`（amd64），直接 Depends 为 `fonts-noto-cjk | google-noto-cjk-fonts`；image version label `22.04`，不代表 WeChat 版本。尚未读取会话消息或发送消息。13 项聚焦测试、全仓 377 项单测通过；有一条 Starlette/httpx 弃用警告。

## 固定版本与本地容器

单一入口是仓库内 `scripts/agent_wechat_repro.py`（Python 标准库，无需 Node/npm/curl）：

```bash
python3 scripts/agent_wechat_repro.py preflight
python3 scripts/agent_wechat_repro.py pin
python3 scripts/agent_wechat_repro.py start
python3 scripts/agent_wechat_repro.py open-vnc
python3 scripts/agent_wechat_repro.py status
# 扫码后补跑登录 FSM；读取到登录用户与会话列表后才继续
python3 scripts/agent_wechat_repro.py login-flow
```

离线安全用例见 `tests/unit/test_agent_wechat_repro.py`：覆盖登录 WebSocket 握手与事件脱敏、唯一 testing 群选择、读取前复核、失败发送不重试、缺少显式 testing 确认时不调用 API，以及输出脱敏；这些用例不代替实机验收。

当前主机普通账号没有 Docker socket 权限；研究脚本本身不提权。运行前在同一个终端交互验证 sudo，并建立只存在于 `/tmp` 的 Docker 命令包装器；它不保存凭据，也不把账号加入 `docker` 组：

```bash
sudo -v
mkdir -m 700 -p /tmp/lemonbot-docker-sudo
printf '%s\n' '#!/bin/sh' 'exec sudo -n /usr/bin/docker "$@"' > /tmp/lemonbot-docker-sudo/docker
chmod 700 /tmp/lemonbot-docker-sudo/docker
PATH="/tmp/lemonbot-docker-sudo:$PATH" python3 scripts/agent_wechat_repro.py preflight
```

后续命令也用同样的 `PATH=...` 前缀。为本次 GHCR 下载临时添加的 Docker 服务代理配置位于 `/run`，不在仓库、不传入容器，并在固定完成或失败时删除、重启 Docker。

`pin` 只拉取官方 `ghcr.io/thisnick/agent-wechat:latest`，随即将 `RepoDigests` 固定为 SHA-256，并在拉取前后确认 GitHub `main` commit 未变化。锁文件记录镜像 digest、源码 commit、OCI 来源/修订标签（如果镜像提供）、CPU 架构、解析时间与 Docker 版本，保存在 `~/.local/state/lemonbot/agent-wechat-reproduction/artifact-lock.json`，权限为 `0600`。

若 OCI revision 标签缺失，锁文件明确记 `source_revision_verified=false`：此时能复用同一镜像 digest，但不能声称该镜像由记录的源码 commit 构建。标签存在且不匹配则停止。上游 `latest` 只用于首次解析，之后容器以锁定 digest 启动。

容器使用独立命名数据卷与微信 home 卷，仅额外挂载权限为 `0600` 的 API token；不挂宿主微信目录、Docker socket 或其他宿主目录。只绑定 `127.0.0.1:6174`，VNC 经同一 API 端口并使用 token。容器保留 Docker 默认 capabilities 并仅额外增加 `SYS_PTRACE`，不启用可选 `NET_ADMIN`；按上游要求将 seccomp 设为 unconfined。脚本启动前后检查磁盘空间至少剩 3 GiB。首次拉镜像或构建建议有 8 GiB 余量；低于建议值时先核算各阶段下载与缓存峰值，并在全程保留 3 GiB 底线后再继续。若余量将接近 3 GiB，应停止并复核，不清理已有用户数据。

脚本不会删除容器或卷。`stop` 仅停本工具标记的容器并保留数据。`start` 拒绝使用镜像不符、标签不符、capabilities、端口映射或挂载不符的既有容器；卷也必须有本工具标签。

## testing 群实验步骤

```bash
python3 scripts/agent_wechat_repro.py login-flow
python3 scripts/agent_wechat_repro.py select-testing
python3 scripts/agent_wechat_repro.py new-marker
```

首次容器登录在本机打开的 VNC 浏览器界面扫码；扫码后运行 `login-flow`，由 token 认证的登录 WebSocket 补跑完整 FSM，完成登录用户发现与 DB 凭据初始化。命令使用 `newAccount=false`，把扫码数据与账号标识留在内存中，最多等待 120 秒，只输出事件类型和后置条件；登录用户已存在且聊天列表非空时直接跳过。若同一微信账号登录导致宿主客户端退出，停止登录切换并集中处理，不反复争抢会话。`select-testing` 在 API 聊天列表中只接受**唯一**名称精确为 `testing` 且 ID 形如群聊的对象；群 ID 写入权限为 `0600` 的本机状态文件，不打印。若名称不存在或重名，停止并由操作者整理会话，不允许传任意 chat ID 绕过。每次读取或发送前都重新确认唯一 `testing` 群及已选 ID 仍一致。

1. 生成一个标记。用本账号在容器微信的 testing 群手工发送，随后执行 `observe --marker <标记>`；记录程序报告的 `is_self` 与脱敏 sender 引用。
2. 生成新标记，由另一个账号在 testing 群发送；重新观察并确认 `is_self=false`、sender 引用和本账号不同。
3. 生成两个标记，让对端连续发送，再观察两者；检查发送者归属和唯一匹配。
4. 生成新标记，以以下命令让程序通过上游 `/api/messages/send` 发送到已保存的 testing 群，然后检查 API 回读及对端实际收到的消息：

```bash
python3 scripts/agent_wechat_repro.py send --confirm-testing --marker <新标记>
python3 scripts/agent_wechat_repro.py observe --marker <新标记>
```

发送前会将 marker 写入本地一次性账本；同一个 marker 即使结果超时或不明也会被拒绝再次发送。`success=true` 只表示 API 计划返回成功，需另有回读/对端证据，才算消息已送达。对每次尝试使用新 marker。

5. `stop` 后再次 `start`，检查登录恢复、读回/身份结果和新的 testing canary；容器不会重建、不会清空数据库。

Python 入口默认只输出 marker、条数、`is_self` 和以本机私钥计算的 sender/message 引用，不输出正文、昵称、微信 ID 或群 ID。私有比较键可跨实验比较 sender 引用，需作为敏感研究材料限制访问；不复制到报告。原始 API 回复只在内存中处理。仍需注意上游客户端/服务持有整个登录账号的数据。

## 验收分类与预算

总额八小时有效投入：环境/固定镜像最多两小时；单次登录收发与 sender 识别最多三小时；重启、连续消息、滚出视口和最多一个有证据支持的小修复最多两小时；记录结果与停机最多一小时。扫码等待单独记账。

- **完整通过：**testing 收发、self/对端身份区分、连续消息 sender 归属、重启后再次成功全部有脱敏报告与对端确认。
- **部分通过：**基础通道可用但任一重复性或 UI 状态失败；列出一个定位明确的修复，不直接集成。
- **不通过：**登录、消息读取/身份或发送核心链路在预算内不可完成；冻结该复现分支并回到资料卡路线。
- **待验证：**Docker、扫码或对端配合缺失；不计为技术失败或通过。

### 当前验收快照

| 验收项 | 状态 | 证据 |
|---|---|---|
| 登录和唯一绑定 `testing` 群 | 通过 | `login-flow` 满足登录、登录用户及聊天列表后置条件；群名唯一且为群聊 |
| self/对端与连续消息归属 | 通过 | self 为 `is_self=true`；对端单条与连续两条均为 `false`，连续标记相邻、顺序正确且 sender 引用一致 |
| API 自动发送 | 通过 | 新 canary `LB26_121F252A92B52027A8715CCA` 经 API 单次发送，数秒后恰好回读一条且 `is_self=true`；对端确认已看到 |
| 容器重启与重复行 | 通过 | 保留原卷重启后重新完成登录 FSM；三条历史 canary 各仅一条，self sender 引用稳定 |
| UI 会话切换与离屏列表 | 部分失败 | 目标在前台时 `open` 返回 `skipped=true`；Escape 后从无会话视图打开 testing 返回 `no_action_selected`。离屏滚动未继续测试 |

阶段判断：核心收发、发送者区分、重启后去重和 API 送达均已验证；UI 自动打开目标会话失败，因此分类为部分通过，不直接集成。

### 实机进展（2026-09-29）

手工扫码后，认证 API 显示 `logged_in`，但登录用户为空、`/api/chats` 为空，通用 `/api/status` 报 `logged_out`。AT-SPI 找到唯一名称精确为 `testing` 的可访问群行，并通过容器内点击工具打开；单独点击没有填充聊天数据库。该初始状态与上游仍开放的 [issue #153](https://github.com/thisnick/agent-wechat/issues/153) 症状一致。

随后调用 token 认证的 `/api/ws/login?timeoutMs=45000&newAccount=false`。服务端在数秒内关闭连接，客户端没有解析到文档列出的事件；事件原文未记录。连接后的状态检查显示登录用户对象已出现，聊天列表恢复为 20 个会话。推断手工扫码后还需运行完整登录 FSM 完成用户发现和数据库初始化；这是根据本次前后状态变化得出的推断。通用 `/api/status` 仍报 `logged_out`，因此不能单独用它判定登录失败。现已把脱敏、限时 WebSocket 流程加入 `login-flow` 子命令。

`select-testing` 成功唯一绑定目标群。API 发送 marker `LB26_D50672EA9844D1303D099404` 后，首次立即回读为 0 条；等待数秒再读到恰好一条，`is_self=true`。容器随后停止并以原卷重启；第一次状态检查时 `/api/status/auth` 为 `logged_out`，但登录用户记录和会话仍在。`login-flow` 没有跳过，而是再次执行 FSM，收到 `login_success`，之后认证状态恢复为 `logged_in`。重启后发送 marker `LB26_B66F12CD24206F733A1AC65C`，数秒后回读恰好一条，`is_self=true`，sender 脱敏引用与重启前一致。两次发送均没有重试同一 marker，其他聊天正文未读取。

对端身份 canary `LB26_2C236EF26C1983A0C5247499` 在对端确认发送后恰好匹配一条，`is_self=false`，sender 脱敏引用与两次 self canary 不同；发送前的零匹配只反映同步延迟。未读取其他聊天正文。连续对端验证 canary `LB26_B20700A2196CBCE18CD2A08A`、`LB26_62A8DF993DACB97C450E72AF` 各唯一匹配一条；API 返回顺序与发送顺序一致，两条在列表中相邻，`is_self=false` 且 sender 脱敏引用相同。本次按用户要求另发全新 canary `LB26_121F252A92B52027A8715CCA`：API 首次立即回读为 0 条，数秒后恰好一条且 `is_self=true`；未重用旧 marker。对端已确认看到该新 canary。

调用 testing 群的 `/api/chats/{id}/open` 首次返回 `skipped=true`，因为目标当时已在前台。按 Escape 回到无会话视图后重试同一目标，API 返回 `no_action_selected`；只检查了过滤后的无障碍结构，没有访问其他聊天。`/api/ui/open-chat` dry-run 返回 HTTP 404，说明当前固定镜像没有该无障碍回退端点。上游开放的 [PR #171](https://github.com/thisnick/agent-wechat/pull/171) 提出针对 `No action selected` 的无障碍搜索回退；这与本次现象相符，但由于镜像 OCI revision 标签缺失，不能断言该 PR 的代码与镜像构建版本一一对应，也不能把它当作已验证修复。离屏列表场景未继续测试。检查容器工具时，`chat-select --help` 意外触发会话枚举并在工具输出中暴露一个原始会话标识；该调用枚举了会话元数据，并在工具输出中显示一个原始标识；没有执行会话选择或读取消息正文，也未将标识写入仓库。发现后即停止进一步使用该工具。


### PR #171 候选登录界面恢复检查（2026-09-30）

候选容器扫码页过期后，重新打开本机认证 noVNC 页面并完整重启候选容器；仍无可扫二维码。随后通过候选 `/api/sessions/default/stop` 与 `start` 重启图形会话（Xvfb、窗口管理器、AT-SPI、VNC、WeChat），保留 data/home 卷；重新打开 noVNC 后用户仍看不到二维码。noVNC HTML 返回 200，带认证的 VNC WebSocket 握手返回 101，用户能看到微信桌面。

脱敏 `/api/status/login` 检查的 `qrDataUrl` 为空；`/api/ws/login` 只收到初始 status，45 秒内没有 QR 事件。无障碍树只表明窗口中存在扫码区域，不能证明二维码像素已绘出；一次扫码区域点击未恢复二维码。白名单弹窗关闭接口没有检测到版本更新窗。WeChat 及其 UI 子进程仍运行，API 与 AT-SPI 响应；最近 500 行容器日志没有 segfault、panic、fatal、core dump 或超时记录。容器内 DNS 可解析 `login.weixin.qq.com`，HTTPS 探测返回 200。

当前证据不支持 WeChat 进程整体冻结，但不能排除客户端内部登录流程卡住。容器级与图形会话级重启均未解决 QR 生成；暂不再重复重启或让用户扫码。下一步应检查 WeChat 4.1.13.23 的登录初始化日志与二维码请求路径，并继续限制在候选容器和测试账号内；保留全部卷。用户准备重启虚拟机；候选容器 restart policy 已设为 `unless-stopped`，尚待验证宿主重启后的恢复。

### 上游 a11y 打开回退候选审查（2026-09-29）

固定审查 PR #171 HEAD：`b8e3ef74f2cb89ddff96182cb8640bf978e1f838`。PR 当前仍开放，且页面未显示 review；它是上游 fork 的 14 个提交，合并基线相对当前记录的上游 main 已明显分叉。PR 包含登录账号目录恢复、`/api/sync/rescan`、弹窗关闭、a11y 搜索打开以及发送安全修复，整体候选不等于一个最小 UI 补丁。[PR #171](https://github.com/thisnick/agent-wechat/pull/171)

静态源码审查发现，最终提交不再按搜索结果第一行盲点：它按规范化名称找唯一精确目标，拒绝系统会话与不唯一匹配，之后点击并确认聊天 composer 已出现；无法确认时不继续。发送路径使用 click-only 的 send-safe 打开，先 Escape 离开当前会话，不发 Return，并在目标未确认时 fail closed；发送计划也有单次发送动作保护。单元测试源码覆盖精确行选择、歧义失败、系统会话拒绝和键盘保护。PR HEAD 的 Docker release build 成功；`cargo test --release --locked` 起初因 `wechat_db.rs` 测试模块重复导入 `Duration` 而编译失败。只在临时研究副本删去重复导入后，以 builder stage 镜像重跑，55 项通过、0 失败。产品镜像仍由未修改的 PR HEAD 构建；该行仅属于 `#[cfg(test)]` 模块，不影响 runtime 二进制。

GitHub 包页面展示已发布版本镜像，没有找到与 PR HEAD 对应的固定镜像 digest。[官方 GHCR 包页面](https://github.com/thisnick/agent-wechat/pkgs/container/agent-wechat)。候选 `Dockerfile` 需要 `noVNC 1.5.0`、`SQLCipher 4.6.1` 和 WeChat 安装包；三者现已下载至 `/tmp/agent-wechat-pr171-review-20260929/docker/`，并按文件字节数与 SHA-256 固定：noVNC 为 706,843 字节、`6a73e41f98388a5348b7902f54b02d177cb73b7e5eb0a7a0dcf688cc2c79b42a`；SQLCipher 为 19,115,004 字节、`d8f9afcbc2f4b55e316ca4ada4425daf3d0b4aab25f45e11a802ae422b9f53a`；WeChat 为 231,359,624 字节、`b7d0f8d53e9f648bc2c77a6096a04100d008f2d9f0d3988a2a4859b5992aca0a`，`dpkg-deb` 核实版本为 `4.1.13.23 amd64`。清理并下载这些制品后、开始 Docker 构建前，主机有 11,574,837,248 字节（约 10.8 GiB）可用。候选以本地代理拉取并固定基础镜像：`rust:1.93-bookworm@sha256:7c4ae649a84014c467d79319bbf17ce2632ae8b8be123ac2fb2ea5be46823f31`，`ubuntu:22.04@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02`。Docker build 使用 `DOCKER_BUILDKIT=0`、`--network=host` 和仅指向本机代理的 HTTP(S)_PROXY build args；临时 `/run` daemon proxy 配置在基础镜像拉取与构建结束后已移除。PR HEAD 的 release 镜像构建成功，候选 image ID 为 `sha256:6bd39acdce7165a95f89744b61121b6ca7a1f844af9d1a940a042dda98462cca`。构建监控采样最低空闲 3,832,983,552 字节（约 3.57 GiB），完成后为 4,346,937,344 字节。镜像只作了无网络、无卷的静态检查，WeChat `4.1.13.23`、SQLCipher `4.6.1`、noVNC 和 server 文件检查均通过；WeChat post-install 输出缺少 `update-desktop-database` 的警告，但 dpkg 安装成功。构建与测试结束后只清理了无标签中间镜像和 Rust builder 基础镜像；保留候选 runtime 镜像以及全部数据卷。当前空闲 7,618,310,144 字节（约 7.09 GiB）；两个原研究卷仍在，候选容器挂载的是另两个新卷。

noVNC 中点击 Connect 用于建立浏览器 viewer 连接；它本身不保证指针和键盘输入会被接受，x11vnc 的 `viewonly` 必须为 `0`。viewer 是否连接也不影响容器内通过 AT-SPI/xdotool 操作。Docker 构建与 Rust 测试已记录；PR #171 候选仍在独立的 data/home 卷上运行，token 文件权限为 0600。2026-09-30 的容器级和图形会话级重启均未恢复可解码二维码，候选登录验证当前受阻；历史固定版的 testing 收发结果与候选版状态分开记录。任何阶段都保留至少 3 GiB；不得用 `latest` 代替候选，也不得清理保留卷来满足空间要求。

### 虚拟机重启后的恢复（2026-09-30）

虚拟机重启清理了 `/tmp/agent-wechat-pr171-review-20260929`，候选容器虽配置 `unless-stopped`，仍因 `/data/auth-token` 的宿主绑定源消失而启动失败（exit 127）。ext4 上的 `/tmp` 也可能在启动时被清理，不能作为凭据持久化保证。

已保留旧容器为 `lemonbot-agent-wechat-pr171-pre-reboot` 并关闭其自动启动；用原 image ID 和原 data/home 两个卷创建同名候选容器，维持 localhost 端口、SYS_PTRACE 和 seccomp 配置。API token 已轮换并迁至 `/home/lemon/.local/state/lemonbot/agent-wechat-pr171/api-token`，目录 0700、文件 0600；新容器已启动。没有删除卷或复制宿主微信数据。当前认证仍为 `logged_out`；扫码前需继续确认界面。二维码解码失败仅代表检测器未识别到二维码，不单独证明图像缺失；进程存活/睡眠状态也不能排除内部卡住。

持久恢复入口：`python3 /home/lemon/.local/state/lemonbot/agent-wechat-pr171/resume.py status`；`open-vnc` 打开新 token 认证的页面，旧浏览器链接应弃用。`login-flow` 仅输出限时登录事件类别与认证摘要。临时上游源码与构建输入也随 `/tmp` 清理，已构建镜像和文档中的版本摘要仍保留。

### 新卷对照恢复（2026-09-30）

虚拟机再次启动后，原 PR #171 候选容器仍停止；先前启动的新卷对照也因主机关闭而停止（退出码 137，Docker 未标记 OOMKilled）。原候选 `lemonbot-agent-wechat-pr171` 保持停止。确认持久 token 文件仍为权限 `0600`、文件非空后，只启动既有对照 `lemonbot-agent-wechat-pr171-cleanprobe-20260930`；未创建新卷。对照使用与候选相同的镜像 `sha256:6bd39acdce7165a95f89744b61121b6ca7a1f844af9d1a940a042dda98462cca`，挂载专属的 cleanprobe data/home 卷和只读 token 文件，端口仍只绑定 `127.0.0.1:6174`。本次启动时间为 `2026-09-30T15:07:45Z`。启动前空闲约 6.1 GiB，启动后约 6.0 GiB，始终高于 3 GiB。

认证 API 已响应，状态为 `logged_out`、登录用户记录不存在；登录状态接口返回 `qr_pending`，但 `qrDataUrl` 为空。它只说明该接口未提供可解码二维码，不能单独证明画面中没有二维码。noVNC 的 x11vnc 初始为 `viewonly=1`；本次会话使用文档支持的远程命令临时切为 `viewonly=0`，随后查询确认。通过 xdotool 点击登录窗口设置按钮后，无障碍节点数由 22 变为 24；点击确认后回到 22，表明该页面能响应实际输入。没有更改代理选项、登录或发送消息。用户看到的仍是模糊二维码和静态转圈，故新卷未恢复清晰扫码界面。


### 历史固定版扫码页对照未完成（2026-10-01）

新卷对照仍为模糊二维码和静态转圈后，尝试从官方 GHCR 重新拉取历史固定镜像 ghcr.io/thisnick/agent-wechat@sha256:97d5a99f933592c15897a7579b4e2b2cbbb5f4dda856228f8271f91dd7cafb43。AMD64 manifest 为 17 层，压缩层合计 1,004,060,631 字节。直连时拉取 token 请求被重置；通过主机已有的 localhost 代理验证 registry 可达后，临时配置 Docker daemon 代理并重试。

首次拉取期间，可用空间从约 6.4 GiB 快速降至 3.67 GiB 时手动发 SIGINT 中止，Docker 客户端退出码 130；中止后空间回升至约 4.63 GiB。只读 Docker 用量显示 PR #171 候选镜像约 3.94 GB、Ubuntu 22.04 镜像约 119 MB，BuildKit cache 为 0。为继续同一对照，清理了可重新下载的 APT archives/indexes，以及三个已禁用的旧 Snap 修订：gnome-42-2204 revision 202、snap-store revision 1390、snapd-desktop-integration revision 315。当前 Snap 修订、候选镜像、所有容器和数据卷保留。APT lists 已清除，下次使用 apt 前需先运行 apt-get update。

第二次仍按同一 digest 拉取，并设置 3.5 GiB 自动停止阈值；阈值触发后再次以退出码 130 停止。未创建历史版容器或新卷，未登录、扫码或发送消息。移除 /run/systemd/system/docker.service.d/http-proxy.conf 后重启 Docker；daemon active、无该临时 drop-in，候选、cleanprobe 和 pre-reboot 容器均停止。Docker restart 与核对后当前可用空间为 3,910,365,184 字节（约 3.64 GiB），高于 3 GiB 底线。Docker 中仍保留已到达的镜像层，没有执行 prune。

因此，历史版在当前宿主环境下能否生成清晰二维码仍未知；现有结果不能区分 PR #171 候选问题与共同运行环境问题。当前空间不足以安全完成该固定镜像对照；再次尝试前以至少 8 GiB 空闲作为保守目标（固定镜像压缩层约 0.935 GiB，候选镜像本地体积约 3.67 GiB，历史镜像解包峰值尚未测出）。优先扩展虚拟磁盘，再按同一 digest 继续，并全程保留 3 GiB 底线。不得通过删除保留镜像或任何研究数据卷腾空间。

## 阶段结论

**历史固定版部分通过；PR #171 候选待验证。** 固定镜像上的 testing 收发、self/对端区分、连续 sender 一致、容器重启后重复行检查及对端送达确认均成功。PR #171 候选 Docker release build 成功；只做一行测试专用修正后 55 项 Rust 测试通过。候选 noVNC 与 WeChat 桌面可运行，但容器和图形会话级重启后仍未生成可解码二维码，因此候选 UI 打开、离屏列表和安全发送尚未验证。完成候选验证前不进入 Lemonbot 集成。

## testing 单群闭环交付（2026-10-01）

本轮新增独立实验模块，实现既有 Connector 协议，并复用 CoreRepository 和原 schema。
未注册到正式 runtime；模型、工具及 AT-SPI 门禁未启用。实验入口不导入上游代码。

### 离线运行和已验证结果

```bash
uv run --locked python -m lemonbot.research.agent_wechat_lab demo
# 可选：保留模拟数据库，目标必须是空目录
uv run --locked python -m lemonbot.research.agent_wechat_lab demo --state-dir /tmp/lemonbot-agent-demo
```

默认 demo 在自身临时目录内运行并清理合成数据，不触及研究卷或真实账号。
输出 `mode=simulated_http_not_live`，本轮结果为 ok=true：首次入站一条、回复一条；重启新增零条；
重复入站插入被拒绝；第二次模拟发送在消息出现后超时，重启后保留 UNKNOWN，POST 总次数两次。
没有模型或工具执行。这些是模拟 transport 和实际 SQLite 的证据，不是微信送达证据。

自动化检查：`tests/unit/test_agent_wechat_lab.py`、既有复现工具测试及 `tests/integration/test_pipeline.py`
共 63 项通过。覆盖账号/群/前台变化、缺失身份、self 和无关正文过滤、分页边界与锚点丢失、分页时
头部变化、未回读/对端伪回读/重复回读、HTTP 重定向拒绝，以及 record/create/reserve/dispatch/send
阶段终止后恢复。不确定发送均不重试；已持久化但未 dispatch 的工作可以安全恢复。

### 运行前必须完成的环境步骤

1. 用户在虚拟化软件中关机，将虚拟磁盘扩至至少 60 GiB，重新启动。当前只读检查仍为 30 GiB；
   guest sudo 不能增加虚拟化软件提供的容量。布局仍为 /dev/sda2 ext4 时，确认新增尾部空间后使用
   growpart 与 resize2fs 扩展；工具缺失时先 apt-get update（旧 APT 索引在清理时移除）。
2. 空闲至少 8 GiB 后只拉上述固定 digest，并全程监控 3 GiB 底线。历史锁文件和 api-token 已确认
   存在于 `~/.local/state/lemonbot/agent-wechat-reproduction/`，不重新执行 pin，也不拉 latest。
3. 沿用本报告的 sudo Docker 包装器运行 start，复核固定镜像、原历史 data/home 卷和持久 token。
   PR #171 与 cleanprobe 保持停止；API 只绑定 localhost。若出现容器配置不符则停止，不绕过校验。
4. 对历史容器设置并查询 `x11vnc -sync -R noviewonly` / `x11vnc -Q viewonly`，确认 viewonly=0；
   用户手动扫码、运行 login-flow 初始化，再手动打开 testing。固定版启动后三十分钟有效诊断仍
   无法登录即结束本轮实机恢复，不创建新卷、不更换客户端。下载及用户等待另计。

### 实机启动命令（2026-10-06 已启动独立实验）

```bash
uv run --locked python -m lemonbot.research.agent_wechat_lab enroll \
  --token-file /home/lemon/.local/state/lemonbot/agent-wechat-reproduction/api-token
uv run --locked python -m lemonbot.research.agent_wechat_lab run --confirm-testing \
  --token-file /home/lemon/.local/state/lemonbot/agent-wechat-reproduction/api-token
# 在独立终端生成指令，由对端手动发进 testing
uv run --locked python -m lemonbot.research.agent_wechat_lab marker
# 暂停后，手动恢复 testing 前台，再显式重新启动
uv run --locked python -m lemonbot.research.agent_wechat_lab run --confirm-testing --resume \
  --token-file /home/lemon/.local/state/lemonbot/agent-wechat-reproduction/api-token
```

默认私有实验 state：`~/.local/state/lemonbot/agent-wechat-lab/`，目录 0700；binding、比较键及
lab.db 使用受限权限。与旧研究卷、产品 lab.db 分离。比较键不得删除，否则身份引用会变化。
run 默认三十分钟、最大九十分钟；同一 state 只允许一个进程。暂停会持久化代码并退出，普通
run 拒绝自动恢复，需 --resume。恢复先核验账号/群/前台并建立新基线，不补抓停机消息。

命令只接受 LB26_E2E_ 加 24 位大写十六进制随机值；self 不处理；回复 LB26_ACK_ 加相同值。
每个 marker 只使用一次。每两秒读最多十页、每页一百条；offset 查询在连续写入时不提供原子快照，
检测到分页头部变化或无法找到锚点会暂停。首版只验证低速合成文本流，不宣称任意吞吐无损。

前台标题门禁独立实现，要求与唯一 Chats 列表属于同一个 frame，标题位于该窗口顶部七十像素内、Chats 右侧。
还排除列表消息子树、拒绝弹窗和隐藏状态，并登记路径和角色链。Qt 父子标题分别带/不带成员数时，仅在
几何包含关系及既有 testing 标题模式均成立时折叠为一个叶节点；并列候选仍暂停。没有复用上游坐标 fallback。
该选择器已于 2026-10-06 在历史镜像上核验；窗口平移不误判，无法唯一匹配就暂停，不自动点击。
镜像与记录源码之间仍缺 OCI revision 绑定，不能把源码检查当成镜像实测。

### 新闭环验收表

| 项目 | 当前结果 |
|---|---|
| 模拟 HTTP → 实际持久化 → 固定回复 | 通过，模拟 |
| 模拟重启、重复处理及不确定发送隔离 | 通过，模拟 |
| 固定镜像恢复登录并通过新前台守卫 | 实机通过；testing 已登记，实验程序基线就绪 |
| 单条与连续对端指令自动回复及对端确认 | 单条通过；连续第二条 UNKNOWN，回执歧义暂停 |
| self 不回复、重启新指令无重复 | 未执行 |
| 切走暂停、手动切回显式恢复 | 未执行 |
| 三十分钟持续运行 | 未执行 |

新里程碑最多六小时有效工作，旧二维码诊断已冻结。当前代码和登录恢复已通过，新增模块真实闭环待验收；
新标记单条实机闭环已通过；首轮 UNKNOWN 保留且不重发，未清理研究数据。下一步做连续消息、self 忽略和重启验收。

## 固定版恢复执行（2026-10-05，进行中）

- 磁盘和 /dev/sda2 ext4 已扩至 50 GiB；初始可用 26431049728 字节（约 24.6 GiB），满足下载余量。没有继续改分区或清理数据。
- Docker active，原历史 data/home 卷与 0600 持久 token 保留。三个 PR #171 相关容器停止；历史固定镜像当前尚不在本地，6174 端口空闲。
- artifact-lock.json 仍固定历史 digest。离线模拟演示复核 ok=true，真实实验 state 尚未创建。
- 首次直连拉取有镜像层写入，但速率较低。用户随后明确指定 127.0.0.1:7897 代理；直连拉取已中止，确认没有运行容器后，临时设置 Docker daemon HTTP_PROXY/HTTPS_PROXY 并继续拉取同一 digest。没有拉 latest 或构建新镜像。
- 临时配置为 /run/systemd/system/docker.service.d/http-proxy.conf，下载成功或失败后必须移除并重启 Docker；应先完成移除再启动实验容器，避免影响运行状态。
- 当前仅恢复制品，尚未扫码、登记实验群或发送真实消息。下载等待单独计时；三十分钟登录诊断预算从固定容器启动后开始。

### 聊天中断后的后台续传

等待被中断后，原下载与恢复 PTY 句柄消失，ps 核验无遗留下载/恢复进程，固定镜像仍未完成，三个候选容器仍停止。没有把中断计作登录或通道失败。

已新增 scripts/agent_wechat_recovery_job.py 及 scripts/agent_wechat_fixed_runtime.py，并启动独立 transient systemd unit：lemonbot-agent-wechat-fixed-recovery.service。监督程序以固定 digest 续传缓存，不自动更换镜像、不自行循环重试；下载等待无三十分钟总超时。磁盘接近 3 GiB 停止拉取。成功后移除本次 Docker 临时代理、重启无运行容器的 Docker，再复用原历史卷启动固定版，核验 viewonly=0 与认证 API；仍不自动登录或发送消息。

状态记录位于仓库外私有 state 的 recovery-progress.json，权限 0600，只含阶段、PID、时间、下载大小、磁盘余量及布尔结果；原始下载错误与签名 URL 不保存。恢复与监督安全测试共十项通过，Ruff 通过。

关闭交互 root shell 后，systemctl 核验后台 unit active/running、MainPID=8181，私有进度记录为 downloading；临时层数据约 213 MB、可用空间约 23.1 GiB。此数字仅是该时点快照，不是完整镜像或登录成功证据。

查询：systemctl show --no-pager lemonbot-agent-wechat-fixed-recovery.service -p ActiveState -p SubState -p MainPID；读取私有 recovery-progress.json。需要停止该后台任务时，sudo systemctl stop lemonbot-agent-wechat-fixed-recovery.service；监督程序会终止本次拉取并清理匹配的临时代理，保留镜像缓存与全部数据卷。

登录和收发验收仍待制品恢复后执行；不得把 runtime_ready 阶段单独认作 logged_in 或端到端通过。

## 下载进展核验（2026-10-06）

后台服务仍为 active/running，私有进度记录持续更新；本轮未重启拉取、未切换镜像或代理节点。核对本地 OCI index 的 AMD64 manifest：17 个数据层已完成 16 个，剩余层压缩大小 461989943 字节，核验时已写入约 302 MB，仍持续增长。刚完成的 351408838 字节数据层解释了临时下载总量下降；partial_layer_bytes 只表示尚未提交的临时文件，不能当作累计下载量或判断下载回退。

本地完整镜像尚未登记，实验容器未启动，6174 尚未监听。空闲约 22.5 GiB，原卷仍保留；此进展不属于登录或闭环通过。复核实验、复现、持久化管线和恢复脚本测试，共 73 项通过。下载成功后继续扫码、testing 前台登记和实机验收；不执行新的环境排列组合。

### 下载完成后的一次性登录衔接

新增 scripts/agent_wechat_login_ready.py，以普通桌面用户运行独立 transient unit：lemonbot-agent-wechat-login-viewer.service。最多等待现有恢复结果八小时；只有私有进度记录出现 runtime_ready，才使用历史 token 打开认证 noVNC 页面。认证、登录用户和非空聊天列表未全部满足时，只调用一次既有登录 WebSocket 流程（最多 120 秒），仅输出事件种类及布尔结果，不重复初始化、不发送消息。下载失败、中断或恢复失败就退出，不另建环境。

专项测试五项通过：恢复失败不触碰 API、成功后仅初始化一次、已登录不重复初始化、凭据/账号不输出、进度文件权限门禁。任务实测 active/running，MainPID=10308，仍等待下载，不能记作扫码或登录成功。查询 systemctl --user show --no-pager lemonbot-agent-wechat-login-viewer.service -p ActiveState -p SubState -p MainPID；终止命令 systemctl --user stop lemonbot-agent-wechat-login-viewer.service。该一次性任务不设置自动重启；若页面打开失败，保留失败状态由下一次人工检查处理。

## 跨日拉取失败与单次修复（2026-10-06）

上一下载的所有临时数据提交后，Docker pull 退出码为 1，镜像未登记。后台任务明确进入 download_failed，登录衔接任务随之退出；没有启动微信，也没有重新创建卷。故不能把此前“17 层接近完成”的下载快照当作镜像或登录恢复结果。

脱敏 daemon 日志同时报告解压 snapshot 不存在、对应 lease 不存在；lease 到期时间为 2026-10-05 09:17，而实际解压在 2026-10-06 13:28（Asia/Shanghai）。失败后固定 index 的本地 blob 消失，可用空间回升至约 22.6 GiB，仍有约 294 万可用 inode。这些证据指向跨日临时 lease 过期、资源被 GC 的主要推断，并不支持磁盘容量不足。

[containerd 官方 GC 文档](https://github.com/containerd/containerd/blob/main/docs/garbage-collection.md)确认，未使用且没有 lease 保护的临时资源可被回收，带 gc.expire 的 lease 到期会被删除。通用客户端文档举例默认 24 小时；本机 Docker 本次新 lease 实测剩余约 8 小时，不能直接套用文档的默认值。

只进行一次针对该证据的同 digest 新任务：旧任务终止已确认，无其他拉取或运行容器；恢复同一 localhost 代理，启动新恢复服务 MainPID=12288，核验 Docker 的 HttpProxy/HttpsProxy 都指向用户指定地址，并确认新 lease 尚未过期。原镜像/卷/账号环境未切换，候选容器仍停止；不循环重试。原下载受 GC 清理的临时内容需要重新获取，不能再声称只剩上一轮约 120 MB。

监督程序现在持久化 TLS、snapshot/lease 缺失、空间不足、连接重置和 EOF 等布尔错误类别，仍不记录 signed URL 或原始 stderr。新增错误分类脱敏测试通过，相关检查累计 79 项。登录衔接已重新启动，等待窗口调整为八小时以覆盖本轮慢下载；尚未打开页面、发起新登录或发送消息。保持 VM 在线完成本次拉取；完成后继续实际 VNC/RFB、viewonly=0、清晰扫码及认证/聊天列表核验。

## noVNC 实际连接检查（2026-10-06，待固定版实测）

新增独立只读脚本：

```bash
python3 scripts/agent_wechat_vnc_probe.py
```

仅在已核验固定版容器、原 token 挂载和 localhost 端口后运行。固定提交的 [VNC 路由](https://github.com/thisnick/agent-wechat/blob/96c2e90c497d999bf5189a134a9b3b293731f42e/packages/agent-server-rust/src/router/vnc.rs)使用 `/vnc/websockify`，选择 binary 子协议，并经 websockify 连接 x11vnc；[认证中间件](https://github.com/thisnick/agent-wechat/blob/96c2e90c497d999bf5189a134a9b3b293731f42e/packages/agent-server-rust/src/router/auth.rs)保护该 WebSocket，静态页面的 HTTP 200 不能证明认证或图形连接已成立。此源码与镜像缺 OCI revision 绑定，须用实机探针核验，不能当作已验证镜像行为。

探针模拟浏览器的 query token 认证，校验 WebSocket 101/Accept/binary 后读取 RFB ProtocolVersion 回应。依据 [RFC 6455](https://www.rfc-editor.org/rfc/rfc6455.html)及 [RFC 6143](https://www.rfc-editor.org/rfc/rfc6143.html)独立实现，只处理协议前导、ping/pong 和断开，不请求 framebuffer、不发送输入。结果仅含固定错误代码及 RFB 版本，始终保留 login_verified=false；阳性只能证明桥接到 RFB 服务，不能证明像素更新、输入响应或微信已登录。

真实 loopback socket 专项测试九项通过，覆盖认证拒绝、错误 Accept、缺 binary、关闭桥接、私有异常正文不输出、分片/ping、未完成分片不误判以及非法 token 不连网。Ruff 通过。这是模拟协议验证；当前恢复任务仍 downloading，端口未监听，尚无真实 VNC 检测或扫码结果。

## 当前账号 noVNC 登录恢复（2026-10-06，实机）

同一固定 digest 的新拉取成功，恢复 unit 终止码为 0、私有进度为 runtime_ready。历史容器启动于 2026-10-06 06:04:54.600894372 UTC。复核 validate_container 通过，原 data/home 卷和持久 token 挂载保持，6174 仅绑定 localhost；临时 daemon 代理已清理，x11vnc 查询 viewonly=0。未清空微信数据，PR #171 保持停止。

用户确认“微信登陆已经成功”。在该确认后重新读取当前状态：认证 logged_in，loggedInUser 为非空字符串，聊天列表可读且非空。真实 VNC 探针收到有效 WebSocket Accept、binary 子协议和 RFB 003.008 前导；不是模拟或仅 HTTP 200 的证据。本次 probe 仍标记 login_verified=false，是协议层探针能力边界；登录结论来自当前认证、聊天列表与用户确认的组合。

一次性登录初始化早先报告 phone_confirm/login_timeout，并在该时点显示 logged_in=false；这是用户完成登录前的快照，不能覆盖之后的实际成功。事件记录未含 qr，因此本轮没有单独证明全新账号的清晰二维码流程，也没有复测宿主重启。当前账号登录问题已恢复。容器与实验程序重启验证仍按下一阶段 testing 闭环步骤执行，本轮没有为此额外重启已登录容器。

本轮未发送消息。下一步：确认 testing 前台、登记独立实验绑定、启动新历史基线，再由对端发送合成命令完成自动回复验收。新模块收发、重启、切走暂停及三十分钟持续运行均未因此自动通过。

## testing 实验程序已启动（2026-10-06）

用户确认 testing 已前台。只读校准发现原选择器错误地使用屏幕绝对 y<70：实际微信 frame 在 (156,68)，标题文字在 (473,116)。另外 Qt 将同一标题暴露成父标签 testing 加成员数后缀、子标签 testing；不能将其当作两个独立会话。

本地实验选择器作一次针对性的标题兼容修复：验证 Chats 列表和标题同属一个 frame，使用窗口相对顶部区域；父子候选仅在标题模式和几何包含关系都成立时保留叶节点。并列候选、跨窗标题、无效/非有限几何、弹窗和大写 HIDDEN 状态均拒绝。发送者仍来自 API sender，标题只是前台门禁。44 项实验模块测试、Ruff 和两个模块的严格 mypy 通过；相关检查累计 88 项，另九项 VNC 专项测试保留原结果。

真实 enroll 已成功，使用独立 agent-wechat-lab state，账号/群引用和比较键保存在私有目录。一次性用户 service 为 lemonbot-agent-wechat-lab-20261006.service，最长九十分钟，不自动重启。实测 MainPID=23572 active/running，ready=true，历史基线已建立，恢复计数均为零。先前只读登记失败产生的暂停标记，已在用户手动恢复前台后通过显式 --resume 解除。

本轮运行编号 AGW_LAB_20261006_5860E533，唯一合成指令记录在私有试验文件。不记录原始账号或群 ID；不启用模型、工具、正式 runtime 或自动导航。程序就绪不代表闭环已经通过。

### 首条实机闭环证据与确认修复（2026-10-06）

对端指令入站恰好一条并完成持久化，发送者摘要与接口读取一致；出站意图一条、尝试一次。用户确认收到回复，随后只读精确回读到唯一 self ACK，self 与对端发送者不同。然而发送后实验程序以 APIUnavailableOrInvalid 暂停，出站记为 UNKNOWN，未自动重发。原始记录保留，[脱敏证据](evidence/agent-wechat-first-e2e-20261006.json)不计作完整自动确认通过。

原适配器 HTTP 等待为 10 秒，历史复现脚本为 25 秒；固定提交的 [发送路由](https://github.com/thisnick/agent-wechat/blob/96c2e90c497d999bf5189a134a9b3b293731f42e/packages/agent-server-rust/src/router/messages.rs)等待界面执行计划结束才响应。较短等待是当前怀疑项，原错误未保留阶段，因此不能断言已证明超时原因。只将实验 POST 等待调整至有界 25 秒（连接仍 10 秒），补充固定的 Send/Read、Timeout/HTTP 状态/Transport/JSON 诊断，不输出 URL、身份或响应正文。UNKNOWN 仍不重发；使用新标记复测。适配器 46 项测试、Ruff 和两个实验模块 mypy 通过，真实自动确认仍待复测。

查询 systemctl --user show --no-pager lemonbot-agent-wechat-lab-20261006.service -p ActiveState -p SubState -p MainPID；停止用 systemctl --user stop lemonbot-agent-wechat-lab-20261006.service。停止或中断时已经 dispatch 的发送意图保留 UNKNOWN，不自动重发；数据卷和实验数据库保留。

### 单条实机闭环通过（2026-10-06）

运行 AGW_LAB_20261006_RETEST_2FB4DECF：新指令入站恰好一条并完成持久化，出站意图一条、尝试一次，精确回读到新 self ACK 后自动记录 ACKNOWLEDGED，外部 receipt 摘要及确认时间存在，回复关联原入站事件。用户确认对端收到。适配程序持续 active/running，无暂停标记；原首轮 UNKNOWN 的 attempts 仍为 1。详见 [单条实机脱敏证据](evidence/agent-wechat-single-e2e-20261006.json)。

这证明当前固定环境的单条自动收发通道可用，不代表完整里程碑通过。连续消息归属、self 命令不回复、容器及实验程序重启、切走暂停/显式恢复和三十分钟持续运行仍须实测。扩大等待后成功与原超时假设一致，但原首轮没有错误阶段证据，仍不将其断言为已证明的唯一原因。

## 磁盘与版本归档复核（2026-10-08）

本轮只读复核连续试验 AGW_LAB_20261006_CONTINUOUS_9438A152：两条对端指令各有一条入站
完成记录，第一条回复 ACKNOWLEDGED，第二条 UNKNOWN，attempts 均为 1。API 中两条标记各
命中两条对端消息、各两条 self ACK。self 标记命中一条 self 和一条对端消息，实验库无该标记
入站/出站；因程序已暂停且标记混入对端，不能计为 self 忽略通过。多条 API 记录的来源尚未
区分实际重复发送与上游读取行为，不将其归因于用户或自动重发。本轮无发送或程序恢复。
详见 [连续测试脱敏复核](evidence/agent-wechat-continuous-followup-20261008.json)。

环境固定为 [制品与版本清单](../research/agent-wechat/README.md)。保持单条实机成功的证据，
完整里程碑仍未通过；下一项仅定位重复记录来源，再恢复连续验收。缓存清理释放约 3.35 GiB，
镜像、卷、凭据、实验状态均保留。测试按全仓范围完成 448 项，静态检查和锁文件检查通过。


## 可靠收发与 runtime 接入阶段（2026-10-08，本轮进行中）

按六小时有效工作预算执行，2026-10-08 05:24:02 UTC 开始；扫码和用户操作等待另计。
旧 UNKNOWN 和原试验库保留，不重发，不按正文合并不同 ID 的消息。

旧重复关系已归档为 [时间与身份摘要](evidence/agent-wechat-repeat-relations-20261008.json)。
两组旧 ACK 的 ID/时间不同，不能断言是同一消息被重复读取，也未证明重复来源。
新增发送诊断记录运行编号、阶段、累计 POST 次数、耗时、匹配数与消息标识 HMAC 摘要，
不记录正文、token 或原始账号/群 ID。

新标记受控单条：一次 POST，唯一新 self ACK，入站与 outbox 各一条，attempts=1；
用户确认只收到一条。见 [受控单条](evidence/agent-wechat-controlled-single-20261008.json)。
随后两个全新对端标记各一次 ACKNOWLEDGED，sender 摘要一致；本机 self 标记确实被
API 读到，没有入站或回复，用户也确认无 ACK。见
[连续与 self](evidence/agent-wechat-continuous-self-20261008.json)。未放宽唯一回执规则。

05:39:55 UTC 按验收计划只重启一次固定版历史容器，原卷、持久 token、localhost 绑定和
镜像校验通过，随后查询 viewonly=0。API 仍返回 logged_in，账号摘要和 testing ID 未变化；
但前台守卫报告 ChatListStructureMissing，因此尚未恢复实验程序，也不将 API 快照计作
完整重启验收成功。已打开认证 noVNC，等待用户手动恢复 testing 前台。未重新展开二维码
排障，也未创建卷、更换版本或发送新测试消息。

HTTP 传输、前台守卫和消息转换已移到连接器层，独立入口复用同一实现；正式连接器
默认关闭，固定 lab、模型/工具关闭。固定回复在现有 pipeline 的策略检查后执行，复用
持久化 outbox、白名单、限流、静默、审计和急停。通道故障持久化暂停并保留管理台；
显式恢复核验账号/目标群/前台，重建基线。实验与 runtime 共享持久互斥锁。

固定源码未发现许可证或包许可声明，镜像与源码提交绑定仍未证明；
[许可核对](evidence/agent-wechat-license-check-20261008.json)记录了缺失项。用户要求继续
本机独立 HTTP 适配开发；这不等于作者授权。没有复制或分发上游代码或镜像。

467 项全仓测试、Ruff、mypy（111 个源文件）、锁文件检查通过；模拟测试不计作实机通过。
重启新指令、前台切走恢复、独立入口三十分钟观察及正式入口实机启用仍待完成。
正式运行说明见 [runtime 手册](wechat-agent-runtime.md)。


本轮代码已分阶段提交，共用传输/诊断为 78c3bdb，默认关闭的 runtime 为 c02d9e1。
[阶段摘要](evidence/agent-wechat-runtime-stage-20261008.json)记录了实际剩余验收，
结果为“代码通过、实机待验证”，不计作正式接入通过。旧两个 UNKNOWN 的 attempts 仍为
1，实验收发服务已停止；未超出工作预算。磁盘剩余约 20.71 GiB，数据卷与私有状态保留。
