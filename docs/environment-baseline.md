# 固定环境与恢复手册

> 更新：2026-10-09。testing 真 @ 与一个白名单私聊的模型回复、上下文隔离及程序重启延续实机通过，服务保持运行；仓库默认关闭。日常入口见 [conversation 手册](wechat-agent-conversation.md)，下文旧 canary 启动路径仍保留。宿主重启自动恢复未验收。

基线：`historical-fixed-20261008`，2026-10-08 本机读取确认。
机器可读清单见 [environment-baseline.json](../research/agent-wechat/environment-baseline.json)，
制品锁见 [artifact-lock.json](../research/agent-wechat/artifact-lock.json)。

## 实际版本

| 层 | 版本或配置 |
|---|---|
| 宿主 | Ubuntu 24.04.3 LTS，x86_64，kernel 7.0.0-30-generic，GNOME 46 |
| 磁盘 | `/dev/sda2`，ext4，约 50 GiB；本轮清理后空闲约 20.8 GiB |
| Docker / containerd | 29.1.3 / 2.2.1，Ubuntu 包；完整包版本见清单 |
| Python / uv | 3.12.3 / 0.12.5 |
| Lemonbot | 0.1.0；依赖按 `uv.lock` 安装，清单记录锁文件 SHA-256 |
| 容器系统 | Ubuntu 22.04.5 LTS，AMD64 |
| 容器微信 | `wechat=4.1.13.23` |
| 容器图形能力 | x11vnc 0.9.16-8；Xvfb 21.1.4 系列；libatspi 2.44.0-3 |
| 日常 conversation | inotify 变更通知＋60 秒补偿；认证 POST /read-cycle；每会话最多 1,000 条，锚点最多 100 条；两会话共用同库快照；UNKNOWN 不重发 |
| 历史 canary | 轮询睡眠 2 秒；GET 等待 10 秒、发送等待 25 秒；UNKNOWN 不重发 |

宿主原生微信研究记录中的 **4.1.1.8** 与容器微信 **4.1.13.23** 属于不同环境。
历史固定版指固定镜像 digest，并不表示容器微信是 4.1.1.8。
主机包版本是观测记录；没有制作整台虚拟机快照或证明在任意新机器上均可复现。

固定镜像：

```text
ghcr.io/thisnick/agent-wechat@sha256:97d5a99f933592c15897a7579b4e2b2cbbb5f4dda856228f8271f91dd7cafb43
```

记录的上游源码提交为 `96c2e90c497d999bf5189a134a9b3b293731f42e`，镜像无 OCI revision 标签，
二者未被证明绑定。镜像 digest 冻结容器文件内容；源码提交仅用于研究定位，不作为构建证明。

## 容器与私有状态

| 对象 | 固定值 |
|---|---|
| 容器 | `lemonbot-agent-wechat-repro` |
| 数据卷 | `lemonbot-agent-wechat-repro-data` → `/data` |
| 用户卷 | `lemonbot-agent-wechat-repro-home` → `/home/wechat` |
| API/VNC | `127.0.0.1:6174`；不映射公网 |
| 界面时区 | `Asia/Shanghai`（UTC+8）；API 保留带时区的 UTC 时间戳 |
| 特权 | 非 privileged，仅额外 `SYS_PTRACE`；上游需要 `seccomp=unconfined` |
| 重启策略 | `no`；本机恢复须显式启动，宿主重启恢复尚未实测 |
| VNC 输入 | x11vnc `viewonly=0`；浏览器 Connect 仅建立查看连接 |

2026-10-09 已将现有容器 `/etc/localtime` 与 `/etc/timezone` 设置为上海时区，未重启微信。
现有微信进程未设置 TZ 覆盖值；容器本地时间核验为 +0800。新诊断消息的界面时间已显示 20:10，
对应 API 的 12:10 UTC，确认新消息显示生效。部分旧时间标签仍保持原值，未强制重启刷新。
固定版启动脚本会在创建时设置 TZ，并核验现有容器的时区文件，避免重建后回到 UTC。

宿主不挂载微信目录、Docker socket 或个人目录。账号登录仍可能同步其他会话；容器不提供
群级数据隔离。实验只从绑定的 testing 消息接口处理合成命令，不输出无关聊天。

私有路径均不进入 Git：

```text
~/.local/state/lemonbot/agent-wechat-reproduction/
  artifact-lock.json       运行时制品锁
  api-token                0600，挂载至 /data/auth-token，只读
~/.local/state/lemonbot/agent-wechat-lab/
  binding.json             原始群 ID 与摘要绑定
  comparison-key           HMAC 比较键
  lab.db                   独立实验数据库
  paused.json              持久暂停原因
```

目录权限为 0700、状态文件为 0600；凭据不使用 `/tmp`。仓库制品锁不含凭据，不是私有状态备份。

## 本机恢复步骤

以下仅适用于已有 `lemon` 用户、原卷、token 和私有制品锁的本机。恢复 helper 会核验卷标签、
镜像、挂载与权限，缺失时拒绝，不创建替代卷。PR #171 及其对照容器保持停止。

先查看镜像和空间；已有镜像无需下载：

```bash
df -h /
sudo docker image inspect --format '{{.Architecture}} {{.Os}}' \
  ghcr.io/thisnick/agent-wechat@sha256:97d5a99f933592c15897a7579b4e2b2cbbb5f4dda856228f8271f91dd7cafb43
```

若确实缺镜像，空闲至少 8 GiB 才拉取同一 digest，持续保留至少 3 GiB；不拉 `latest`。
不自动安装包、创建新卷或复制微信凭据。当前镜像已保留，本轮没有拉取。

```bash
sudo python3 scripts/agent_wechat_fixed_runtime.py
python3 scripts/agent_wechat_vnc_probe.py
sudo docker exec -e DISPLAY=:99 lemonbot-agent-wechat-repro x11vnc -Q viewonly
```

helper 临时设置 `noviewonly` 并查询确认。本轮没有为了文档复核重启容器。
VNC probe 只证明 WebSocket/RFB，登录仍须用户确认及 API 的 logged_in/登录用户/聊天列表。
浏览器入口由 `scripts/agent_wechat_login_ready.py` 的本地一次性恢复流程打开，文档不打印带 token 的 URL。

在用户手动登录并打开 testing 后，**首次登记**运行：

```bash
uv run --locked python -m lemonbot.research.agent_wechat_lab enroll \
  --confirm-testing --token-file \
  "$HOME/.local/state/lemonbot/agent-wechat-reproduction/api-token"
```

已有同账号/同群绑定使用原状态；只在明确决定恢复、目标群重新核验后运行：

```bash
uv run --locked python -m lemonbot.research.agent_wechat_lab run \
  --confirm-testing --resume --seconds 1800 --token-file \
  "$HOME/.local/state/lemonbot/agent-wechat-reproduction/api-token"
```

启动建立历史基线，不补抓停机消息，不重发 UNKNOWN。使用 `marker` 生成全新指令，
仅由对端在 testing 发送一次；不要重用报告中的旧标记。恢复程序不能替代核验暂停原因。

## 当前恢复限制

独立通道与正式入口可靠收发验收均通过。原卷容器及程序重启、新指令、持久暂停、显式恢复有实机证据；独立入口完整三十分钟通过，正式入口约 12 分钟、22 次正常采样后用户提前结束并接受，完整三十分钟未完成。旧实验 UNKNOWN 保留不重发；宿主重启恢复仍未验收。

正式连接器已提供默认关闭的配置和模拟测试，使用同一传输并共享持久锁；独立 data_root，
不迁移旧实验任务。恢复步骤见 [runtime 手册](wechat-agent-runtime.md)。原 native AT-SPI
继续关闭，本阶段不调用模型或工具。

## 磁盘整理记录

2026-10-08 清理前空闲 18,692,603,904 字节，清理后 22,292,410,368 字节，释放
3,599,806,464 字节（约 3.35 GiB）。只删除未被进程引用的淘汰 VS Code 扩展、超过一天的
扩展安装缓存和缩略图缓存。仍在使用的旧 Codex 扩展保留；当前扩展、编辑器设置、会话、
下载目录、回收站、微信文件、Docker 镜像/容器/卷及实验状态均保留。
Docker 的 reclaimable 标签不作为删除许可。本轮无 prune 或磁盘分区变更。
详见 [清理证据](evidence/disk-cleanup-20261008.json)。

## 延迟优化部署（2026-10-10）

代码基线 `fbf5afc`，变更明细与验收状态见 [延迟优化报告](wechat-latency-optimization.md)。
微信未重启，固定镜像、微信版本与原卷未改变；仅替换自有元数据桥并重启桥和空闲 runtime。
部署前持久暂停，活动入站和 outbox 均为零；重启仍保留 ManualPause，显式恢复后队列为空。
旧 UNKNOWN 一条保留不重发。当前仍为 2 vCPU、约 4 GiB 内存；扩容对照尚未进行。
