# First deployment — Lemonbot 1.0.0

This is the existing AMD64 route. For native ARM64 on the separately verified Orin
environment, follow the [Jetson AGX Orin guide](deployment-jetson-orin.md). Its
adaptation branch requires an explicit opt-in and preserves the original 1.0.0 tag.

Supported: Ubuntu 24.04 **desktop**, AMD64, Python 3.12; recommended 4 vCPU / 8 GiB RAM.
Use the desktop user's terminal, unlocked GNOME Keyring and a D-Bus session.
The [release environment manifest](../research/agent-wechat/release-environment-1.0.json)
records the frozen client/image and recovery boundaries.
A headless/server install is not covered. Fresh-machine/account end-to-end deployment
has not been real-account tested; setup automation is tested with synthetic APIs.

## 1. Dependencies and source

Install Git, Python 3.12, GNOME Keyring and Docker Engine using your distribution or
[Docker's official Ubuntu instructions](https://docs.docker.com/engine/install/ubuntu/). Install `uv` using [the official uv installation instructions](https://docs.astral.sh/uv/getting-started/installation/).
Do not run `uv`, Lemonbot or this setup script as root.

```bash
sudo apt install git python3.12 python3.12-venv gnome-keyring
# Docker Engine must already be installed/running; verify sudo docker version.
git clone --branch v1.0.0 https://github.com/LemonServer/Lemonbot.git
cd Lemonbot
uv sync --locked --extra dev
uv run --locked python scripts/wechat_setup.py --help
```

Docker access may use `--sudo` (only Docker subprocesses elevate), or your explicitly
configured Docker group. Group membership grants broad host access; log out/in after
changing groups. Interactive sudo prompts appear in the terminal, never in config.
All commands below assume the default state paths; advanced deployments can pass
`--state-dir /absolute/path --token-file /absolute/path` consistently to setup.
Setup, control, restart and runtime accept `--config /absolute/path/lemonbot.toml`.
Without it, `LEMONBOT_CONFIG` takes precedence over this checkout's
`config/lemonbot.toml`. Existing config state/token paths are honored; conflicting
explicit state/token overrides are rejected.

## 2. Fixed environment and manual login

Have at least 8 GiB free on Docker's filesystem before the first pull; maintain 3 GiB.
No script prunes images, deletes volumes or copies host WeChat credentials.

```bash
uv run --locked python scripts/wechat_setup.py environment --sudo
uv run --locked python scripts/agent_wechat_repro.py open-vnc
```

The latter opens an authenticated localhost browser page without printing its token.
For custom token locations, open the localhost noVNC page and use the private token
locally; never paste authenticated URLs into issues. Connect only establishes the
VNC connection; setup separately verifies `viewonly=0` so input works.
Scan using the intended account, then manually open a **uniquely named `testing` group**.
Do not run another client that repeatedly competes for the same account.

The fixed AMD64 image is
`ghcr.io/thisnick/agent-wechat@sha256:97d5a99f933592c15897a7579b4e2b2cbbb5f4dda856228f8271f91dd7cafb43`,
with WeChat **4.1.13.23**. The container uses two dedicated named volumes and a read-only
persistent token mount. API/VNC is `127.0.0.1:6174`, never all interfaces. Required
permissions are `SYS_PTRACE`, `seccomp=unconfined`, never `--privileged`.

Proxy: Docker pull runs in the daemon, so exporting a proxy only for the script is
insufficient. Configure your daemon's proxy explicitly if needed, then rerun setup.
`--proxy http://127.0.0.1:YOUR_PORT` verifies that the daemon already uses that proxy
before an uncached pull; it never changes global Docker
configuration or assumes port 7897. Preserve 3 GiB after downloading.

## 3. Initial identity and whitelist

From the **bot account**, manually send one harmless initialization text in testing,
so the latest 100 messages contain a verified self message. No second account is
required. Setup uses actual `isSelf` metadata, not inferred nickname or account prefixes.

```bash
uv run --locked python scripts/wechat_setup.py bind --sudo
```

This authenticates twice, verifies testing, generates an account anchor, private
metadata and conversation bindings, deploys Lemonbot's own metadata backend, and
synchronizes the initial testing whitelist. It sends no messages. A repeat bind
cannot silently replace the bound account. `CanonicalSelfAmbiguous/Missing` means
send a self initialization text in testing and retry; mismatch requires investigation.

Generated private files (never commit):

```
config/lemonbot.toml
config/wechat-whitelist.local.toml
~/.local/state/lemonbot/agent-wechat-reproduction/api-token
~/.local/state/lemonbot/agent-wechat-lab/
  comparison-key, binding.json, metadata-binding.json, conversation-binding.json
  paused.json, conversation-admin-session.json (created by control)
```

Edit `config/wechat-whitelist.local.toml`, using exact chat-list names including spaces and remarks:

```toml
version = 1
groups = ["testing"]
contacts = ["Your contact's displayed remark"]
```

Remove the example contact until the actual conversation exists. Total 1–20 unique
names; no fuzzy matching. Open each requested chat in WeChat first. Duplicate names,
wrong type or changed identity fail closed. See [whitelist details](wechat-whitelist.md).
Do not edit private internal IDs. Startup synchronizes these names before processing.

## 4. Model and private runtime configuration

```bash
uv run --locked lemonbot secret set deepseek_api_key --profile lab
```

Enter the key using the hidden terminal prompt. It is stored in Secret Service,
not TOML, command arguments or the repository. Review [current official pricing and
budget accounting](deepseek-pricing.md) before enabling cloud requests.

In generated `config/lemonbot.toml`, change only these activation fields:

```toml
[runtime]
connector = "wechat_agent"
# Keep generated data_root and other fields.
[wechat_agent]
enabled = true
# Keep generated mode/token_file/state_dir/whitelist_file.
[models]
provider = "deepseek"
# Keep explicit price/budget entries and model deepseek-flash.
```

**Edit the existing sections**, do not append duplicate TOML tables. Public example
[config](../config/wechat-agent-conversation.example.toml) is disabled by default.
Long-term memory opt-in: `[wechat_agent] memory_enabled = true`; it adds paid
extraction requests after confirmed replies. Tools, proactive actions and media stay off.
Conversation mode disables nighttime quiet hours but keeps policy checks and limits.

Default reply limits: 3 / ten minutes, 10 / hour, 30 / rolling 24 hours per conversation,
50 / rolling 24 hours globally. Optional local override:

```toml
[limits.wechat_reply]
per_10_minutes = 0
per_hour = 0
per_day = 100
global_per_day = 100
```

`0` disables only short windows. Positive daily limits count pending/active intents,
confirmed deliveries and UNKNOWN, not just received replies. Money budgets remain
5 CNY/day and 100 CNY/month; usage is a conservative estimate, not the provider bill.

## 5. Persistent services, diagnose and start

```bash
# First install only: keep testing foreground and resync after editing the whitelist.
uv run --locked python scripts/wechat_setup.py bind --sudo
uv run --locked python scripts/wechat_setup.py install-services --sudo
systemctl --user start lemonbot-wechat-metadata.service
uv run --locked python scripts/wechat_setup.py doctor --sudo
systemctl --user start lemonbot-wechat-conversation.service
uv run --locked python scripts/wechat_conversation_control.py status
uv run --locked python scripts/wechat_conversation_control.py resume
```

Setup installs persistent **user** units using this checkout, Python and configuration.
It does not enable boot startup. The metadata backend runs in the container; the user
unit forwards only authenticated host-local requests on `127.0.0.1:6175` to the current
container IP. After a container/host restart rerun environment and install-services
to restore the backend and refresh that IP. This is deliberate manual recovery.

Startup prints per-target resolved/readable/enabled status. Names appear only in
local console/journal: do not publish those logs. An unavailable new target may pause
only itself. Account/key/snapshot errors pause the channel. Resume revalidates and
establishes a fresh baseline; historical and downtime messages are not replayed.

Admin UI binds `127.0.0.1:8767`; use the locally generated authenticated URL, do not
expose or share it. Use the control script for credential-free console status.
Next: [operations](operations.md), [release evidence](release-1.0.md).
