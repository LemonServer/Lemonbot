# Linux-only 安全模型

## 当前信任边界

Lemonbot 当前只信任核心代码、管理员生成的 schema v2 配置、`0600` enrollment、SQLite 数据库
和固定 systemd 部署。聊天文本、AT-SPI 属性、网页、附件、OCR、视觉结果、模型结果和工具输出
一律视为不可信数据。

当前微信路径只有 Observe：

```text
官方 Linux 微信
  → 过滤后的 AT-SPI bus
  → 无网络、无 Home、无密钥的只读 worker
  → 严格长度前缀 JSON / Pydantic snapshot
  → enrollment、方向、sender、尾部唯一对齐
  → lab.db inbox/messages/audit
```

模型、视觉、浏览器、MCP、主动任务、outbox dispatcher 和微信动作接口均不在该链路中。

## 永久禁止

支付、购买、转账、订阅、红包、凭据和 MFA、OAuth 授权、账号安全设置、软件安装、提权、
安全设置修改、模型或工具发起的永久删除、任意 Shell/代码执行、批量发送、自动加好友、拉群
和 `@所有人` 永久拒绝。聊天、网页或模型不能扩大白名单、target ref、权限或阶段。

管理员离线 CLI 的显式数据删除不是模型能力，必须停止服务并带 `--confirm`。

## 微信身份与隐私

- 只允许独立测试号，个人微信自动化不代表腾讯授权，存在非零封号风险。
- 不读取微信数据库、Cookie、登录材料或附件缓存；不使用 Hook、注入、ptrace、协议逆向、
  坐标、键盘、剪贴板或截图点击。
- canary 为一次性合成值；真实账号短语、聊天标题和 UI 文本只在探针内存中比较。
- 探针报告只含路径、角色、接口、属性键和摘要；enrollment 不保存显示名称。
- Observe 正文在通过 target、方向、sender 和 cursor 验证后保存到本机 `lab.db`。日志、配置、
  报告和管理台响应不得打印正文。
- header 摘要仍可能被字典猜测，不应当作匿名化；bundle 和报告按本地私有数据保护。

## AT-SPI worker 隔离

核心通过 accessibility bus 的 D-Bus daemon 查询连接 PID，只允许 Registry 和属于已登记微信
进程的唯一 bus name。无法获得精确 PID 映射时拒绝启动，不退回完整 session bus。

worker 使用独立 system-Python venv，通过 `systemd-run` 设置 `NoNewPrivileges`、只读系统、
`ProtectHome`、`AF_UNIX`、`IPAddressDeny=any`、内存和任务上限；bubblewrap 再解除网络/PID/
IPC/UTS 命名空间，只挂载只读系统库、worker venv、私有 tmpfs 和单独代理 socket。worker 看不
到核心配置、数据目录、微信数据目录或 Secret Service。

worker 不调用 Ubuntu GI 中可能崩溃的已弃用 `Accessible.get_text()`；正文只来自已登记节点的
稳定可访问名称。worker IPC 只有 `init/ready/snapshot/health/error/shutdown`。监听注册只作为作用域验证，snapshot
以受限周期重读生成；没有 selector 修改、导航、输入、
点击、发送或任意命令消息。未知类型、超限帧、错误关联、worker 退出或 D-Bus 代理失败都会
毒化实例并停止通道。

## 入站一致性

- 当前目标必须存在于 enrollment 和配置 allowlist，chat kind/header 摘要必须精确一致。
- 第一个 snapshot 只建立 baseline。
- 新 snapshot 必须与已保存尾部存在唯一重叠；无重叠或多重重叠都暂停。
- 事件 ID 是上一链哈希与当前消息指纹形成的链，崩溃后重复观察产生相同 ID，由数据库去重。
- self 和 inbound 必须具有不同结构签名；群 inbound 必须具有非显示文本的稳定 sender 属性。
- 暂停、急停、重启或切换目标后不补抓无法证明的新旧边界。

## 密钥与未来模型

Linux 密钥只允许 Freedesktop Secret Service，锁定时 fail closed，不回退到环境变量或 TOML。
Observe 强制 `models.provider="disabled"`，启动不读取 DeepSeek/智谱密钥，也不检查预算。

未来 Draft 阶段必须另行启用预算、DeepSeek worker 和提示注入隔离，并继续保证不操作微信。
当前代码中保留的通用模型、视觉、浏览器和 MCP 组件不构成 Observe 的授权能力。

## 已知限制

- AT-SPI 不是微信官方自动化 API，客户端升级可随时改变或关闭结构。
- 当前只读取可见会话，不导航、不滚动，停机或后台会话消息可能丢失。
- display header 只用于 Observe 的当前视图复核，不能解锁 Reply；未来发送必须证明更强的稳定
  目标身份和精确回执。
- bubblewrap 和 D-Bus 代理缩小 worker 权限，但不能消除宿主内核、桌面栈或微信本身漏洞。
- 正式验收必须在隔离 VM 上完成；单元测试和 fake 树不能替代 24 小时真实 Observe。

漏洞报告中不要附带真实 API Key、聊天记录、账号短语、canary、Cookie、bundle 或 VM 凭据。
发现疑似密钥泄漏时先在服务端撤销并轮换，再保留本地证据调查。
