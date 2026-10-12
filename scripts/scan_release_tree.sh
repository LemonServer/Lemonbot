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
  case "$(uname -s):$(uname -m)" in
    Linux:x86_64)
      scanner_arch=x64
      scanner_checksum=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
      ;;
    Linux:aarch64)
      scanner_arch=arm64
      scanner_checksum=e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080
      ;;
    *)
      printf '%s\n' 'Unsupported scanner platform; supply a verified LEMONBOT_GITLEAKS_BIN.' >&2
      exit 1
      ;;
  esac
  curl --fail --silent --show-error --location --retry 3 --proto '=https' \
    "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_${scanner_arch}.tar.gz" \
    --output "$archive"
  printf '%s  %s\n' "$scanner_checksum" "$archive" \
    | sha256sum --check --status
  tar -xzf "$archive" -C "$scan_workspace" gitleaks
  scanner="$scan_workspace/gitleaks"
fi
[[ -f "$scanner" && -x "$scanner" ]] || { printf '%s\n' 'Scanner must be executable.' >&2; exit 1; }
[[ "$("$scanner" version)" == '8.30.1' ]] || { printf '%s\n' 'Gitleaks 8.30.1 required.' >&2; exit 1; }
mkdir "$scan_workspace/tree"
git archive HEAD | tar -x -C "$scan_workspace/tree"
"$scanner" dir "$scan_workspace/tree" --redact --no-banner
