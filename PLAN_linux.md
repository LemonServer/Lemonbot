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

The explicitly authorized first capability extension adds native DeepSeek Flash image
input, public-page reads, local SearXNG search and clock access. These stay disabled
in public examples; track live evidence separately in [read capabilities](docs/read-capabilities.md).
The local Orin acceptance now covers scoped image replies and followups, genuine
group image quotes, public link reads, search/time replies and honest verification-page
card feedback. WeChat article body access remains unverified; this is limited to
the authorized group/private pair and does not extend to other targets or accounts.
Group participation without real @ and proactive followups remain future work.
No automatic client/image upgrades, offline-message backfill or proactive messaging
are planned as implicit 1.0 behavior.

The authorized unified-context redesign is implemented in eight separate tasks:
contracts, archive, CUDA embeddings, retrieval, two-stage classification/answer,
consolidation/cache, offline readiness, then live deployment. Track current stage
and handoff in [the implementation record](docs/context-memory-implementation.md).
Stages 1–7 add default-off contracts, raw archive, backup-copy migration, CUDA
embeddings, scoped retrieval and classification/answer assembly. Native Orin FP16
embedding/retrieval and offline two-stage checks passed. Stage 8 has safely migrated
and enabled the new flow for the authorized pair; real synthetic two-stage API preflight
passed, while finite WeChat acceptance is still in progress. Confirmed-delivery consolidation/cache accounting is offline-tested;
stage 7 integrated offline readiness, lifecycle, migration/rollback and source diagnostics
also passed. See [context operations](docs/context-memory-operations.md) for the
prepared stage-8 cutover and [two-stage flow](docs/context-flow.md) for budget boundaries.
Stage 8 real-model and WeChat acceptance remains pending.
New group memory is shared with author attribution, private/account scopes stay
isolated, and local model inference defaults to CUDA/TensorRT. Do not deploy later
stages or migrate live data while completing an earlier task.

[Earlier Linux work](docs/history/linux-plan-before-1.0.md) preserves chronology;
[Windows/Observe/native identity research](docs/history/README.md) is inactive.
