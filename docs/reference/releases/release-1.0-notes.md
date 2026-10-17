# Lemonbot 1.0.0

First stable release of the pinned Linux desktop WeChat text runtime.

- Genuine @ replies in allowlisted groups and automatic private-chat text replies.
- Serialized navigation, persistent isolated context, quoted-text labeling and optional scoped long-term memory.
- Durable inbox/outbox, exact confirmation, UNKNOWN no retry, persistent pause, budgets and rate limits.
- Portable setup entry, persistent user services, safe restart/stop and reorganized engineer/AI onboarding.

Supported: Ubuntu 24.04 desktop, AMD64, Python 3.12; recommended 4 vCPU / 8 GiB.
Manual QR login and host restart recovery remain required. Default configuration is disabled.
The upstream Docker image and WeChat client are pinned and not redistributed.

Verification: 602 repository tests passed, Ruff/mypy/lock and local Markdown link checks passed,
independent-checkout install/demo and 17 deployment/archive checks passed, Gitleaks found no leaks.
The local existing-account deployment was safely migrated to persistent user services and resumed.
Fresh-machine/account end-to-end installation, full quoted-reply E2E and long-term recall beyond
recent context remain unverified; simulated checks do not imply live success.

[First deployment](https://github.com/LemonServer/Lemonbot/blob/v1.0.0/docs/deployment.md) ·
[Operations](https://github.com/LemonServer/Lemonbot/blob/v1.0.0/docs/operations.md) ·
[Development](https://github.com/LemonServer/Lemonbot/blob/v1.0.0/CONTRIBUTING.md) ·
[Evidence and limits](https://github.com/LemonServer/Lemonbot/blob/v1.0.0/docs/release-1.0.md)

MIT covers Lemonbot's own source. Upstream image/source licensing remains independently
unconfirmed; see [third-party notices](https://github.com/LemonServer/Lemonbot/blob/v1.0.0/THIRD_PARTY_NOTICES.md).
This release contains source/docs only, no client installers, image archives, account data or PyPI upload.
