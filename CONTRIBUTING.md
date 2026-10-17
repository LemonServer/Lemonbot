# Development and contributions

Start with [README](README.md), [deployment](docs/deployment.md) and
[AGENTS.md](AGENTS.md). Runtime support targets Ubuntu 24.04 desktop / Python 3.12.
The [Orin ARM64 adaptation](docs/deployment-jetson-orin.md) has limited live evidence
on JetPack 7.2.1 and remains a separate opt-in branch. Checks need only locked runtime and dev dependencies; vision is
loaded on demand by its separate worker.

## Local checks (no real account required)

```bash
uv sync --locked --extra dev
uv run --locked python -m lemonbot.research.agent_wechat_lab demo
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked mypy src
uv lock --check
uv run --locked python scripts/check_release.py
```

Do not replace real-account acceptance with synthetic tests. Live operations need
an explicitly authorized account and conversations; public default is disabled.

## Architecture map

| Path | Responsibility |
|---|---|
| `src/lemonbot/cli.py`, `runtime.py` | Entry points, component assembly, task scheduling |
| `connectors/wechat_agent*.py` | HTTP, identity, exact binding, navigation, read-cycle, delivery, pause |
| `orchestration/` | Policy-before-model pipeline, prompts, context and budget integration |
| `storage/` | Durable inbox/outbox, audit and migrations |
| `memory/` | Scoped extraction and FTS recall |
| `security/`, `policy/` | Credentials and capability/authorization controls |
| `scripts/agent_wechat_metadata.py`, `agent_wechat_wal_snapshot.py` | Own authenticated bridge, validated encrypted snapshots and notifications |
| `scripts/wechat_setup.py` | Fresh deployment, private config and persistent user units |
| `scripts/wechat_bot_restart.py`, `wechat_conversation_control.py` | Drain/restart/stop, explicit pause/resume |
| `tests/unit`, `tests/integration` | Synthetic protocol, faults, persistence and policy checks |

The bridge coalesces filesystem notifications; reads and model/navigation/send work
are gated to avoid re-entry. Each bounded read-cycle uses one validated snapshot,
independent target anchors and account checks. Missing target tables pause locally;
identity/snapshot failures pause globally. Model replies flow through the existing
pipeline/outbox, not a second sending loop.

## Changes and reviews

Make narrowly scoped commits with relevant tests and explanations of behavior/risk.
Keep package, runtime and protocol versions consistent. Schema changes need explicit
migrations; do not change schema for deployment docs. Add fault tests when changing
confirmation, concurrency, pause or extraction. Keep current docs separate from research.
Follow [MIT](LICENSE) for own code and [third-party notices](THIRD_PARTY_NOTICES.md).
Do not vendor upstream code or redistribute third-party images without license review.

## Documentation

Daily instructions live in `docs/operations.md` and `docs/restart-jetson-orin.md`;
current acceptance and the next task live only in `docs/context-memory-implementation.md`.
Put implementation details under `docs/reference/`, dated investigations under `docs/history/`
and sanitized results under `docs/evidence/`. Update the index and relative links when moving files.
Do not append debugging transcripts to daily manuals or treat past measurements as current state.
