# Whitelist management — 1.0

First bind via [deployment](deployment.md); do not write IDs by hand.
The main config is `config/lemonbot.toml` in the installed checkout. Its
`[wechat_agent].whitelist_file` selects one file by absolute path; setup defaults
to `config/wechat-whitelist.local.toml` beside it. Both files are Git-ignored.
No other whitelist is discovered or merged automatically.

On this Orin, edit `/home/ega-agx-orin/Lemonbot/config/wechat-whitelist.local.toml`.
The runtime, setup, restart and control entry points all select explicit `--config`,
then `LEMONBOT_CONFIG`, then this checkout's `config/lemonbot.toml`. The default
does not depend on the current directory. Outside a source checkout, an installed
package retains the platform user config directory default.

```toml
version = 1
groups = ["testing", "Another exact group name"]
contacts = ["Exact displayed contact remark"]
```

Use names exactly as displayed in the chat list, including spaces; remarks override
contact nicknames. Total 1–20 names, globally unique. The configured names authorize
binding, but account changes require separate investigation. Duplicate or changed
identity never falls back to a guessed match. Open new targets in WeChat first.
Keep files private, owned by the current user and not writable by other users.

After changes:

```bash
uv run --locked python scripts/wechat_bot_restart.py
uv run --locked python scripts/wechat_conversation_control.py resume
```

Resume alone doesn't synchronize changes. Startup prints each name, group/private
kind, resolved/readable/enabled status and reason. Readable is not proof of successful
real sending. Names remain in local stdout/journal, not public evidence.

Check the startup report before resuming. For current pause status:

```bash
uv run --locked python scripts/wechat_conversation_control.py status
```

`paused=false` and an empty `target_pause_reasons` indicate no channel/target pause;
they do not replace the startup target report. Orin startup logs are also available
with `journalctl _UID=1000 _SYSTEMD_USER_UNIT=lemonbot-wechat-conversation.service
-g '^wechat_startup ' -n 30 --no-pager`; check timestamps because these are startup results.

Existing deployments must explicitly migrate the old `conversation-runtime.toml`
to the checkout's `config/lemonbot.toml`, changing only its whitelist path. Preserve
existing local whitelist contents, credentials and state; safely drain first, then
regenerate service units with `install-services`. Old configs remain available as
backups but are never an implicit fallback. `RuntimeConfigMissing` requires restoring
the selected config or passing `--config` explicitly, not rebinding the account.
For a deliberate rollback, safely stop, reinstall services with `--config` pointing
to the preserved old TOML, and use that same explicit path for restart, doctor and
control. Check the startup report and doctor before resume. Never reset runtime data.

Missing/ambiguous message tables pause only affected targets; healthy targets continue.
Account, identity and snapshot failures pause the channel. All targets unavailable
also pause globally. Restoration requires explicit resume and a new baseline; old
messages aren't replayed. [Reason codes and recovery](operations.md).
