# Operations — Lemonbot 1.0.0

Run these from the installed checkout as the desktop user. First install:
[deployment guide](deployment.md). Custom configs require `--config /absolute/path`.
For this Jetson Orin installation, use the [device reboot startup manual](restart-jetson-orin.md),
which includes the required ARM64 flag and this device's noVNC entry point.

## Everyday commands

```bash
# Start (a previous pause remains paused).
systemctl --user start lemonbot-wechat-conversation.service
uv run --locked python scripts/wechat_conversation_control.py status
uv run --locked python scripts/wechat_conversation_control.py pause
uv run --locked python scripts/wechat_conversation_control.py resume
# Safe restart prints the new startup whitelist report, then requires resume.
uv run --locked python scripts/wechat_bot_restart.py
# Safe stop pauses, drains in-flight work, leaves data and pause intact.
uv run --locked python scripts/wechat_bot_restart.py --stop
# Local names may appear in logs: do not post whole journals publicly.
journalctl --user -u lemonbot-wechat-conversation.service -n 60 --no-pager
uv run --locked python scripts/wechat_setup.py doctor --sudo
```

Restart/stop wait up to 90 seconds for processing and nonterminal outbox work.
If draining fails, inspect state; do not kill an in-flight send merely to restart.
Never delete pause files or change UNKNOWN to pending. UNKNOWN is intentionally
non-retryable even when a user reports not receiving the message.

## Change whitelist or settings

Edit `config/wechat-whitelist.local.toml` in the checkout, then safe restart and **explicitly resume**.
The main config is `config/lemonbot.toml`; all entry points honor explicit `--config`
before `LEMONBOT_CONFIG` before the checkout default. Services record an absolute
config path. After selecting a different main config, regenerate service units with
`install-services --config ...` and use the same config for control/restart.
Resume alone does not reload config or synchronize newly named conversations.
Use exact names, including spaces, and open new chats in WeChat before restarting.
The startup report distinguishes name matching, message readability and reply enabling.
[Whitelist details](wechat-whitelist.md).

## Host/container recovery

Host startup is not enabled or automatically validated. After a host restart:

```bash
# If the bot survived or restarted, drain it before changing the backend.
uv run --locked python scripts/wechat_bot_restart.py --stop
uv run --locked python scripts/wechat_setup.py environment --sudo
uv run --locked python scripts/agent_wechat_repro.py open-vnc
# Manually log in the original account, unlock the host keyring.
uv run --locked python scripts/wechat_setup.py install-services --sudo
systemctl --user restart lemonbot-wechat-metadata.service
uv run --locked python scripts/wechat_setup.py doctor --sudo
systemctl --user start lemonbot-wechat-conversation.service
uv run --locked python scripts/wechat_conversation_control.py resume
```

If no bot was running and no DB exists, skip the first stop. Do not rerun initial
bind on another account or delete old volumes. The metadata backend is recreated
from private persistent files; it does not depend on `/tmp`.

## Troubleshooting

| Reason | Action |
|---|---|
| ExplicitResumeRequired / ManualPause | Verify doctor and whitelist report, then resume |
| NotLoggedIn | Check noVNC and login the originally bound account |
| AccountChanged | Stop; verify intended account, never overwrite trust anchor to bypass |
| MessageSnapshotUnavailable | Inspect specific snapshot/bridge cause; restore backend, disk and pinned environment; do not clear WeChat data |
| MessageTableMissing | Open that target in WeChat and let local messages synchronize; resume afterward |
| MessageTableAmbiguous / AnchorLost | Keep pause; inspect source or bounded history, do not select a guessed table or replay |
| WhitelistTargetNotFound / Ambiguous | Correct exact name or duplicate remarks; safe restart |
| WhitelistBridgeSyncFailed / ConnectError | Confirm metadata user unit and container backend/IP; follow recovery |
| AmbiguousReadback / UNKNOWN | Keep evidence; never automatically resend or merge identical bodies |
| Keyring/model failures | Unlock Secret Service, inspect key status, official balance and budget; no retry loop |
| No group reply | Verify genuine member-selection @, whitelist, pause, budget and limits; typed nickname is not a trigger |

A quoted message triggers only through its current message metadata. Media is off
by default; the optional [read capabilities](read-capabilities.md) add exact image
quotes and public link cards. Same text with different IDs remains distinct.

## Data, upgrades and rollback

Keep the private state directory, runtime data directory, Secret Service entry and
both Docker volumes. Stop/drain before backup; back up SQLite including its consistent
WAL state (or use SQLite backup API). Store backups privately, never in GitHub issues.
Public reports should contain only versions, reason codes, timings and counts.

Before an upgrade record your commit/config, drain, make a private backup, install
locked dependencies and restart/resume. Roll back code without rewriting the DB,
UNKNOWN or pause state. No automatic schema downgrade is provided.
Historical recovery experiments are [archived](history/README.md), not current commands.

统一上下文公开默认关闭；本机已进入阶段 8 两目标验收。只读检查、备份/删除语义、迁移门槛与配置回退见
[统一上下文运维手册](context-memory-operations.md)。生产迁移及真实验收只在阶段 8 执行。
