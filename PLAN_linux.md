# Lemonbot 1.0 roadmap

The active release is **1.0.0**: pinned Linux desktop WeChat text runtime.
Deploy via [first installation](docs/deployment.md), operate via
[the current handbook](docs/operations.md), develop via [CONTRIBUTING](CONTRIBUTING.md).

## Delivered scope

- Genuine group @ and allowlisted private text; automatic serialized navigation.
- Persistent scoped context, program-restart continuity, quotes with explicit labeling.
- Optional scoped memory extraction after confirmed delivery and SQLite FTS retrieval.
- Exact-name whitelist synchronization and per-target startup status (up to 20).
- Persistent pause, precise readback, UNKNOWN no-retry, model budgets and send limits.
- Fixed image/client, deployment helper and persistent user service installation.

Four locally configured conversations have passed matching/readability checks.
Full restart/context E2E is established for the original group/private pair; quote,
long-term recall and other targets have narrower evidence. See
[release verification](docs/release-1.0.md). Do not broaden that conclusion.

Repository memory default is off; short limits default to 3/10, daily 30/global 50.
Local optional configuration can enable memory or disable short limits with 0;
positive rolling-24-hour and money limits remain. Tools/proactive/media remain off.
Manual login and host-restart recovery are required. PR #171 remains frozen.

## Next milestones

The `feat/jetson-orin-arm64` branch adds an explicitly experimental native ARM64
environment. Native dependencies, binding, two-target replies and restart context
passed on JetPack 7.2.1 Orin in the limited six-message acceptance. Other targets,
devices and host restart recovery remain unverified. Follow [the Orin guide](docs/deployment-jetson-orin.md). Preserve the
original AMD64 digest, 1.0.0 tag and send/identity/memory invariants.

1. Verify a fresh machine and account using the documented setup, without existing state.
2. Complete real-account quoted-reply and long-term recall outside the recent window.
3. Extend acceptance coverage of added whitelist conversations without conflating readability with sends.
4. Only after these, separately design unattended host recovery or further capabilities.

No automatic client/image upgrades, offline-message backfill, multimodal support or
proactive messaging are planned as implicit 1.0 behavior.
[Earlier Linux work](docs/history/linux-plan-before-1.0.md) preserves chronology;
[Windows/Observe/native identity research](docs/history/README.md) is inactive.
