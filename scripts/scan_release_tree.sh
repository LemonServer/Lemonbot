#!/usr/bin/env bash
# Scan exactly HEAD's public files; independent of push ranges and shallow history.
set -euo pipefail
umask 077
scan_workspace=$(mktemp -d "${TMPDIR:-/tmp}/lemonbot-secret-scan.XXXXXX")
trap 'rm -rf "$scan_workspace"' EXIT

if [[ -n "${LEMONBOT_GITLEAKS_BIN:-}" ]]; then
  scanner="$LEMONBOT_GITLEAKS_BIN"
else
  archive="$scan_workspace/gitleaks.tar.gz"
  curl --fail --silent --show-error --location --retry 3 --proto '=https' \
    https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz \
    --output "$archive"
  printf '%s  %s\n' \
    551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb "$archive" \
    | sha256sum --check --status
  tar -xzf "$archive" -C "$scan_workspace" gitleaks
  scanner="$scan_workspace/gitleaks"
fi
mkdir "$scan_workspace/tree"
git archive HEAD | tar -x -C "$scan_workspace/tree"
"$scanner" dir "$scan_workspace/tree" --redact --no-banner
