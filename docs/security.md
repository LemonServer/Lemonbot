# Security and privacy — Lemonbot 1.0

Public configuration defaults to disabled. Live conversation mode is restricted to
`profile="lab"`, explicitly bound exact-name conversations and the original account.
Public examples disable optional media/tools; explicit local configuration can enable bounded read-only capabilities. Proactive tasks remain off. Chat/model output cannot
grant permissions or change bindings, budgets or core rules.

## Identity, delivery and privacy

- Resolve a unique internal conversation ID; display names are confirmation only.
- Genuine top-level @ metadata triggers a group reply; quoted/typed @ does not.
- Persist intent before one POST. Only a unique new self-message readback confirms.
  Timeout, interruption or ambiguity is UNKNOWN with no automatic retry.
- Serialize navigation, sending and read work; pause persists across program restart.
- Legacy context is private-target or group+sender scoped. The opt-in unified flow
  shares within a group with author attribution; private chats and accounts remain isolated.
  Only confirmed replies enter delivered context or memory extraction.
- Runtime inbox, conversation and memory data are private local SQLite records;
  they **do contain authorized chat text**. Public logs/reports omit text and raw IDs.
  Startup console/journal intentionally show configured names; keep them private.

## Environment and credentials

The fixed third-party container uses Hook/ptrace and database-key access. Required
`SYS_PTRACE` / `seccomp=unconfined` enlarge its privileges; no privileged mode,
host WeChat mount, Docker socket mount or unrelated personal directory mount.
WeChat may sync other chats internally. Neither Docker nor the application whitelist
creates group-level isolation of account data inside the client.

API, noVNC, metadata forwarder and admin bind localhost only. Bearer API token and
HMAC key are private files; cloud key is stored in unlocked Linux Secret Service.
No keys, QR images, authenticated URLs, raw account identifiers or databases belong
in Git/release reports. Do not forward these ports to a public interface.

Daily/monthly model budgets bound local conservative estimates, including optional
memory extraction. Provider balance is account-wide, not a per-bot ledger.
Send limits count rolling windows and uncertain intents; disabling short windows
never disables daily limits, pause or budget checks.

Manual login/recovery is part of the supported deployment. Do not keep competing
clients logging in, erase data for diagnostics or silently trust a changed account.
See [operations](operations.md), [agent invariants](../AGENTS.md) and
[third-party licensing](../THIRD_PARTY_NOTICES.md).

[Historical native/Observe security research](history/security-before-1.0.md)
has narrower, obsolete experiment permissions; it does not govern this channel.
