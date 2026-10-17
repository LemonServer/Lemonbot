> 1.0 deployment: [current guide](../../docs/deployment.md). The observed artifact locks and scripts below are historical research, not a fresh-install prerequisite.

# agent-wechat 固定实验制品

该目录保存可入库的制品和配置版本，不包含上游源码或账号数据。

- [artifact-lock.json](artifact-lock.json)：历史镜像固定 digest、AMD64 架构及当时记录的源码提交。
- [environment-baseline.json](environment-baseline.json)：2026-10-08 固定主机/容器版本及 2026-10-09 验收状态、挂载配置、Python 依赖及实验代码 SHA-256。
- [恢复手册](../../docs/history/environment-baseline.md)：复核、启动、暂停和保留数据的方法。

镜像缺 OCI revision/source 标签，记录的源码提交与镜像构建**没有被证明绑定**。
运行制品以 digest 为准；不要调用历史 `pin` 命令重新解析 `latest`。

API token、比较键、原始群 ID、实验数据库和微信卷继续保存在本机私有路径。
该目录不是账号备份；限定实机验收的通过结论以 [复现报告](../../docs/history/agent-wechat-reproduction.md)和脱敏证据为准。
