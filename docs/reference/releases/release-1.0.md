# Lemonbot 1.0.0 — release scope and evidence

Stable means the pinned, documented Linux desktop text runtime has an established
real-account operating baseline. It does not guarantee every account, client or host.
Ubuntu 24.04 desktop / AMD64 / Python 3.12; recommended 4 vCPU, 8 GiB.
Image/client remain frozen; manual login and host-restart recovery are required.

## Capability evidence

| Capability | Established evidence | Limits |
|---|---|---|
| Send/receive and identity | [controlled single](../../evidence/agent-wechat-controlled-single-20261008.json), [continuous+self](../../evidence/agent-wechat-continuous-self-20261008.json) | Authorized original testing scope |
| Runtime restart and pause | [restart](../../evidence/agent-wechat-runtime-restart-e2e-20261009.json), [persistent pause](../../evidence/agent-wechat-runtime-pause-restart-20261009.json) | Program/container tests don't prove unattended host recovery |
| Group @, private chat, persistent context | Operator completed two batches, including context questions after program restart | Original group/private pair; no fresh account acceptance |
| Four-target whitelist | [startup/binding check](../../evidence/wechat-whitelist-20261010.json) and local startup checks | All resolved/readable, not full send/context E2E for each target |
| Quotes | [real source parsing](../../evidence/wechat-quote-reply-20261010.json) and synthetic pipeline tests | Quoted text labeling established; full new quoted-model-reply E2E remains incomplete |
| Long-term memory | Scoped extraction implemented; real local audit stored memory | Beyond-recent-window recall not real-account verified; repository default off |
| Latency | [six real trials](../../evidence/wechat-latency-trials-20261010.json) | Expanded-host pair ~13 s; small sample, not a latency SLA or isolated causal claim |
| Stability | Continued operator-reported normal operation; [runtime observation](../../evidence/agent-wechat-runtime-soak-20261009.json) | Planned 30 minutes ended early (~12 minutes); don't claim the full run |
| First installation | Isolated config/demo and synthetic Docker/API tests | Fresh machine + fresh account QR/send not exercised |

Public release data excludes names, internal account/chat IDs, tokens, QR data,
plaintext keys and databases. Local startup logs intentionally contain configured
names and must remain private. Historical evidence may describe previous faults;
read their date and phase rather than treating them as current failures.

## Verification

The final machine-readable release checks are recorded in
[evidence/release-1.0-checks.json](../../evidence/release-1.0-checks.json).
The [local service migration](../../evidence/release-1.0-local-service.json) records only
counts and state; four targets readable, restored after safe drain, no extra test messages.

Checks cover repository tests, lint/types, lockfile, docs links/versions, synthetic setup
and isolated checkout demonstration. They do not convert simulated checks into live E2E.

Source and docs only: no upstream image/client redistribution, private configuration,
account DB or PyPI upload. [License scope](../../../THIRD_PARTY_NOTICES.md).
