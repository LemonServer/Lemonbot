# Lemonbot 1.0

A Linux WeChat text agent with persistent conversation context and optional scoped
long-term memory. Lemonbot replies to genuine mentions in allowlisted groups and
new text in allowlisted private chats, using DeepSeek Flash.

**Start here:** [first deployment](docs/deployment.md) ·
[Jetson Orin ARM64](docs/deployment-jetson-orin.md) ·
[Orin reboot startup](docs/restart-jetson-orin.md) ·
[operations and troubleshooting](docs/operations.md) ·
[development](CONTRIBUTING.md) · [all docs](docs/README.md)

## What 1.0 supports

- 1–20 exact-name allowlisted conversations; automatic serial navigation and sending.
- Group replies only when message metadata really mentions the logged-in account.
- Separate private-chat and group-member context, retained across program restart.
- Quoted text with explicit quote context; optional long-term extraction and SQLite FTS recall.
- Persistent inbox/outbox, precise new-self-message confirmation, budgets, rate limits,
  pause controls and an authenticated localhost administration UI.
- Brief conversational replies; proactive messages remain off.
- Optional experimental DeepSeek image input, public page reading, local SearXNG search
  and time tools. Public defaults remain off; see [read capabilities](docs/read-capabilities.md)
  for configuration and the distinction between API checks and live WeChat acceptance.

This is a **desktop deployment**, requiring manual QR login. Host restart recovery
is manual. Automatic navigation depends on the pinned client's UI and must fail
closed when the target cannot be verified. The account may sync other chats inside
WeChat; the container is not a group-level privacy boundary.

Base send/receive, genuine mentions, private chat, program restart context and pause
recovery have real-account evidence. Four configured conversations were resolved
and read successfully; this is not full end-to-end acceptance for every target.
Quote parsing and memory extraction have narrower evidence; long-term recall beyond
the recent window and fresh-machine/account deployment remain unverified.
See the [capability evidence matrix](docs/release-1.0.md).

## Requirements

Ubuntu **24.04 desktop**, Python **3.12**, `uv`, Docker Engine,
a logged-in desktop session with an unlocked Secret Service keyring.
Recommended: **4 vCPU / 8 GiB RAM**, at least **8 GiB free before image pull**;
retain **3 GiB free**. The Docker image digest and WeChat version are frozen.

| Host | Status |
|---|---|
| Ubuntu 24.04 desktop / x86_64 | Existing pinned 1.0 environment and documented real-account evidence |
| Jetson AGX Orin / Ubuntu 24.04 / ARM64 | **Verified on JetPack 7.2.1**: native channel, two authorized conversations, persistent context and program/bridge restart; other devices and targets unverified |

ARM64 uses a separate pinned candidate, WeChat 4.1.0.16, fresh state and dedicated
volumes. It requires explicit `--experimental-arm64`; see the [Orin guide](docs/deployment-jetson-orin.md).
No emulation, system reflash, local model or GPU acceleration is needed by this route.
The separate, default-off [context-memory redesign](docs/context-memory-implementation.md)
includes an independently verified [CUDA embedding worker](docs/embedding-worker.md),
[scoped hybrid retrieval](docs/context-retrieval.md) and an offline-tested
[two-stage answering flow](docs/context-flow.md), plus default-off
[source-backed consolidation and caches](docs/context-consolidation.md), with
[offline readiness and operations](docs/context-memory-operations.md).
The local Orin deployment is undergoing stage-8 acceptance with the new context flow
limited to the authorized group/private pair; consolidation remains off. Public defaults
stay disabled, and full live context acceptance is still pending.
The opt-in flag remains explicit on the adaptation branch; the unchanged v1.0.0
tag still describes the original AMD64 release. ARM64 host restart recovery is manual.

## Try without WeChat

```bash
git clone https://github.com/LemonServer/Lemonbot.git
cd Lemonbot
uv sync --locked --extra dev
uv run --locked python -m lemonbot.research.agent_wechat_lab demo
```

This exercises simulated HTTP, persistence, restart and UNKNOWN no-retry without
Docker, an account or a model key. `simulated_http_not_live` is not a real-channel result.

## Deploy and operate

Follow [the complete first-install guide](docs/deployment.md); do not start the bot
from the public example before login, binding and secret setup. Public configuration
stays disabled, and setup never sends messages or enables boot startup.

Editable local configuration lives in this checkout: `config/lemonbot.toml` is the
main config and references `config/wechat-whitelist.local.toml` by absolute path.
Both are Git-ignored. All entry points select explicit `--config`, then
`LEMONBOT_CONFIG`, then this checkout's main config, independently of the shell's
working directory. Keys remain in the desktop keyring; bindings, pause and database
remain in private user directories. See [configuration and whitelist](docs/wechat-whitelist.md).

From the repository, after setup:

```bash
systemctl --user start lemonbot-wechat-conversation.service
uv run --locked python scripts/wechat_conversation_control.py status
uv run --locked python scripts/wechat_conversation_control.py resume
# Safe restart / stop: pause first and wait for in-flight work.
uv run --locked python scripts/wechat_bot_restart.py
uv run --locked python scripts/wechat_bot_restart.py --stop
```

Change exact names in the private whitelist, then safely restart and explicitly
resume. A restart preserves pause and context; UNKNOWN is never automatically resent.

## Develop

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md) for architecture,
checks and invariants. [PLAN_linux.md](PLAN_linux.md) is the current roadmap;
[historical research](docs/history/README.md) preserves earlier routes and evidence.

[MIT](LICENSE) covers Lemonbot's own code.
[Third-party notices](THIRD_PARTY_NOTICES.md) describe the separately referenced
image and unresolved upstream licensing; no third-party images or account data
are included in this release.
