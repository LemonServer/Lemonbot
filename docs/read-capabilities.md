# 图片、网页、搜索和时间

四项能力沿用 DeepSeek Flash 密钥，分别由主配置开关控制；公共示例默认关闭。
群聊仍需真正 @，主动发送关闭。当前验收结论统一见[交接页](context-memory-implementation.md#实机验收边界)。

## 如何使用

| 内容 | 使用方式与边界 |
|---|---|
| 私聊图片 | 发送普通照片/截图，然后文字追问；每次模型请求最多一张原图 |
| 群聊图片 | 引用准确图片并通过成员选择真 @；不猜群里最近一张图 |
| 网页 | URL 单独一行，下一行写问题；支持公开 HTTPS 与网页分享卡片 |
| 公众号 | 遇登录/验证页会说明正文不可读；标题可读不等于正文可读 |
| 搜索 | 需要最新信息或核实时按需调用，本机 SearXNG 汇总结果再交给 DeepSeek |
| 时间 | 每轮附当前时间与时区；其他时区使用 `clock.now`，默认 `Asia/Shanghai` |

图片支持静态 JPEG/PNG/WebP，最多 10 MiB、2000 万像素，默认保留 7 天。
过期后只能使用明确标注的历史描述，不重新传过期原图；不支持语音、视频和动态表情。

启用统一上下文时，图片与文字、链接均归档后按相关性选择，**不自动带最近图片**；
私聊隔离，同群共享但保留作者。旧流程仍按群＋发送者/私聊隔离，详见[技术参考](reference/context/context-flow.md)。
成功附图后不再展示接收时的“未加载”标记，原始存档仍保留。

网页没有个人登录状态；只允许当前用户明确给出的 URL 或本轮搜索登记的结果地址。
重定向重新核验、固定 DNS，内网和验证页拒绝。URL 与提问粘连可能无法安全确定地址，
请用换行分开。网页或图片里的文字不能新增工具权限。
网页追问用有来源的正文片段，完整正文保留；截取结果不是整篇文章，也不是重新生成的摘要。

模型若把 DSML 工具标记作为正文返回，程序发送失败说明，不把标记当工具执行，不自动追加付费请求。

## 开关和额度

在 `config/lemonbot.toml` 修改已有表，不重复追加 TOML 表：

```toml
[conversation]
images_enabled = false
web_read_enabled = false
search_enabled = false
clock_enabled = false
image_retention_days = 7
search_url = "http://127.0.0.1:6177/search"
search_daily_limit = 20
search_monthly_limit = 600
```

通过验收后按需改为 true，安全重启、doctor、显式恢复，步骤见[运维](operations.md)。
本机四项已启用；模型预算 5 元/日、100 元/月，每阶段输入上限 8192、输出 512。
每图预留最高 1024 tokens，实际用量结算；报价记录见[模型预算](reference/models/deepseek-pricing.md)。

每轮最多搜索 2 次、读网页 2 次，工具合计 5 次；每日/月搜索配额跨重启保留。
本地搜索无需 Tavily 密钥或搜索 API 费用，但查询仍会发送到外部搜索引擎，DeepSeek 处理结果按 token 计费。
工具空间不够时，新流程保留必需证据并关闭本轮可选工具，不放宽预算。

## 安装与维护

主程序及浏览器使用锁文件依赖，Chromium 由 `uv run --locked playwright install chromium` 安装。
搜索使用已核验架构的固定镜像；本机记录见[早期安装与验收](history/read-capabilities-20261017.md)。
需要初始化/更新搜索时，先安全停止机器人，再执行：

```bash
uv run --locked python scripts/search_setup.py environment --sudo
uv run --locked python scripts/search_setup.py status --sudo
```

复用已有镜像和 `config/searxng/settings.local.yml`，重复安装不覆盖本机 YAML。
服务 `lemonbot-search` 仅监听 `127.0.0.1:6177`，随 Docker 恢复；宿主代理及外部引擎仍需可用。
本机代理地址是已有环境配置，不应照抄到其他设备；失败不绕验证码或改全局 Docker 设置。

图片桥更新使用停止后的 `wechat_setup.py environment`，再 `install-services`、doctor，
流程见[重启手册](restart-jetson-orin.md)。恢复既有功能不重跑付费预检或重新绑定。
关闭能力也需安全重启，保留数据、原图记录、费用、暂停及 UNKNOWN。

## 证据边界

私聊图片与追问、群图片引用、搜索/时间及部分公开网页有两会话实机证据；
网页原文追问在修订后由用户确认通过。公众号仅验证了验证页的如实反馈，正文访问未通过。
Hugging Face 曾直连超时，不能把单个网站成功等同于所有网页可用。
逐次测量留在[历史](history/read-capabilities-20261017.md)和[脱敏证据](evidence/)，不作为每日启动步骤。
