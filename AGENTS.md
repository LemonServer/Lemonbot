# Instructions for engineering and AI agents

Read README.md → docs/deployment.md → docs/operations.md → CONTRIBUTING.md.
PLAN_linux.md is the active roadmap. docs/history/ and dated evidence are historical
observations, **not current execution instructions**. Do not revive older Windows,
Observe, AT-SPI, canary or PR #171 deployment paths as the default.

## Working rules

- Python 3.12; use uv.lock. Checks: `uv run --locked pytest -q`, `ruff check .`,
  `mypy src`, `uv lock --check`, `python scripts/check_release.py` (use uv for tools).
- Public examples stay disabled. Private state is under the desktop user's
  ~/.local/state/lemonbot and ~/.local/share; never read/export credentials into reports.
- Use actual repository/user paths. Do not depend on /home/lemon or preexisting JSON.
- Run bot/setup as the desktop user. Only Docker subprocesses may use sudo.
- Live sending requires explicit authorization for the target; no unsolicited tests
  in other conversations. Do not alter running services just to inspect docs.

## Invariants: sending, identity and memory

1. Exact internal-ID/account binding; unique display name is a check, not identity.
   No nickname/prefix guesses, no fuzzy target selection.
2. Group triggers require genuine top-level metadata @ of canonical self.
   Quoted @, hand-typed names, other members and @all grant no trigger.
3. One POST; precise unique new self-message readback. Timeout/interruption/ambiguity
   becomes UNKNOWN, remains non-retryable across restart. Never body-deduplicate
   different IDs or pick a first/latest receipt to hide ambiguity.
4. Pause is persistent. Resume verifies identity/targets and builds a fresh baseline.
   Restart must not clear pause or replay downtime messages.
5. All navigation/sending serialized; no reception re-entry during model or UI work.
   Inbox/outbox/policy/budget/stop mechanisms remain the single route.
6. Context/memory scoped by private target or group+sender. Confirmed replies alone
   become delivered assistant history and qualify for memory extraction.
   UNKNOWN never causes extraction or a new paid retry; tools/proactive remain off.
7. Preserve volumes, DB, bindings and old UNKNOWN. Drain before deployment/restart;
   no deleting user data to fix environmental faults.

The independently written bridge is not upstream source. Our MIT license does not
cover upstream images or WeChat. Keep real and simulated acceptance clearly separate.
