# agent-wechat runtime 使用与边界

本地 HTTP 客户端由 Lemonbot 独立编写。正式入口仅允许 lab、模型和工具关闭；API 固定为
`127.0.0.1:6174`，账号和 testing 群来自已验证的私有绑定。默认配置不启用此连接器。
代码和模拟测试通过不代表通道实机验收通过；实机状态见复现报告。

## 准备

使用固定镜像、原数据卷、持久 token，并手动登录和保持 testing 前台。通过独立实验入口
登记绑定后，复制 `config/wechat-agent.example.toml` 到私有配置目录，确认独立 data_root，
再显式将 `runtime.connector` 设为 wechat_agent、`wechat_agent.enabled` 设为 true。不得使用实验目录的 lab.db 作为 runtime 数据库。
原实验数据库和 UNKNOWN 任务保留，不迁移到正式试运行库。

```bash
uv run --locked lemonbot run --config /absolute/path/wechat-agent.toml
```

实验入口与 runtime 共享绑定目录中的持久 run.lock；同时运行会拒绝启动。停止实验入口后
才能运行 runtime。不要删除正在使用的锁文件；锁文件保留不表示服务仍在运行。

## 消息与确认

仅处理新的 `LB26_E2E_<24位大写十六进制>`，回复 `LB26_ACK_<相同标记>`。self 忽略；
缺失 sender、账号变化、前台离开 testing、分页锚点丢失或回执歧义都暂停通道。
入站事件和发送者引用继续使用原 HMAC 摘要，通道标识保持 wechat_agent_lab。

固定回复通过现有 pipeline、白名单、限流、静默时间、审计和 outbox；模型、工具、记忆
派生与主动消息均不执行。默认每十分钟三条、每小时十条、每天三十条，静默时间为本地
23:00–08:00，不为验收放宽。启动和显式恢复建立新历史基线，不承诺补抓停机消息。

一条意图只执行一次 POST；只有唯一的新 self ACK 才确认。超时、回执歧义或发送后崩溃
进入 UNKNOWN，不自动重发，也不按正文合并不同消息 ID。暂停期间保留 UNKNOWN。

## 暂停与恢复

运行入口将本机管理台短期登录链接写入 data_root/runtime/lab.admin-url（0600），
不将凭据写入日志。使用以下命令在本机打开；不要复制链接到仓库或报告。

```bash
uv run --locked python scripts/open_agent_wechat_admin.py --config /absolute/path/wechat-agent.toml
```
管理台显示 wechat_agent_lab 的暂停状态和固定原因码。通道故障不退出管理台。

手动切回 testing 后，使用管理台“核验并恢复微信通道”。恢复重新核验账号、唯一群绑定、
前台结构和消息基线，通过后才解除暂停。失败时仍暂停。进程重启不解除已有暂停。
“恢复全部”同样执行通道核验；急停需先停止服务并按现有 `resume --confirm` 流程解除
持久急停，再通过管理台核验通道。恢复不重发 UNKNOWN。

## 上游使用状态

固定源码提交未发现许可证文件或包许可声明；镜像未提供与源码提交绑定的 OCI revision。
用户要求继续本机独立 HTTP 接入，这不是上游作者授权或许可证确认。仓库不复制上游源码，
不分发其镜像或微信安装包；许可未确认的状态仍须在对外分发前单独处理。
