# Whitelist management — 1.0

First bind via [deployment](deployment.md); do not write IDs by hand.
Default private file: `~/.local/state/lemonbot/agent-wechat-lab/whitelist.toml`.
`[wechat_agent].whitelist_file` may instead point to another private TOML file.

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

Missing/ambiguous message tables pause only affected targets; healthy targets continue.
Account, identity and snapshot failures pause the channel. All targets unavailable
also pause globally. Restoration requires explicit resume and a new baseline; old
messages aren't replayed. [Reason codes and recovery](operations.md).
