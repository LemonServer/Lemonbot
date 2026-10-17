# 白名单：修改与确认生效

本机只编辑 `/home/ega-agx-orin/Lemonbot/config/wechat-whitelist.local.toml`。
实际加载位置由 `config/lemonbot.toml` 的 `[wechat_agent].whitelist_file` 决定，
只读取这一份，不自动合并其他同名文件。配置选择优先级见[运维手册](operations.md#修改哪个文件)。

```toml
version = 1
groups = ["testing"]
contacts = ["精确联系人备注"]
```

总计 1–20 个唯一名称，须与微信列表完全相同（含备注、空格）。显示名用于定位检查，
最终按已核验的账号和内部会话 ID 绑定；不猜前缀、不选重复名中的第一个。
本机新上下文仍仅对已授权两会话验收，不因文档示例自动授权新目标。

## 修改后重新加载

1. 编辑白名单，先在微信里打开新增且已授权的会话，等待历史同步。
2. 在仓库中安全重启：

   ```bash
   uv run --locked python scripts/wechat_bot_restart.py
   ```

3. 查看本次报告：`resolved` 为名称/身份解析，`readable` 为本地消息可读；
   若失败保持暂停。刚修改名单导致 doctor 的 `whitelist_binding` 不一致时，先重启同步，不重绑。
4. 检查环境后显式恢复：

   ```bash
   uv run --locked python scripts/wechat_setup.py doctor --experimental-arm64 --sudo
   uv run --locked python scripts/wechat_conversation_control.py resume
   uv run --locked python scripts/wechat_conversation_control.py status
   ```

`resume` 本身不加载新配置。AMD64 省略 ARM64 开关，当前用户有 Docker 权限可省略 `--sudo`。
状态应无通道/目标暂停；可读不等于已验证成功回复。启动报告含名称，仅本机查看。

运行服务应显式记录主配置绝对路径；需要换主配置时重新 `install-services --config ...`，
之后所有控制、重启和 doctor 都使用同一路径。账号变化不能借重新 bind 绕过。

仅某目标表缺失或歧义时暂停该目标；账号、密钥和快照失败会暂停通道。
修复后显式恢复并建立新基线，不重放旧消息。原因码见[故障处理](operations.md#故障处理)。
