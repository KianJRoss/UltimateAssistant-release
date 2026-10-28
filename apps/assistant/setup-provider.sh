#!/usr/bin/env bash
# Linux counterpart of setup-provider.ps1: install one coding CLI the onboarding connects.
#   setup-provider.sh antigravity|codex|claude
set -euo pipefail
provider=${1:?usage: setup-provider.sh antigravity|codex|claude}
download=$(mktemp)
trap 'rm -f "$download"' EXIT
case "$provider" in
  antigravity)
    curl -fsSL https://antigravity.google/cli/install.sh -o "$download"
    bash "$download" --skip-aliases || { echo "Antigravity installer failed." >&2; exit 1; } ;;
  claude)
    curl -fsSL https://claude.ai/install.sh -o "$download"
    bash "$download" || { echo "Claude Code installer failed." >&2; exit 1; } ;;
  codex)
    command -v npm >/dev/null || { echo "Node.js (npm) is required: install nodejs and npm, then retry." >&2; exit 1; }
    # a user prefix: no root needed; ~/.local/bin is where the other CLIs land too
    npm install -g --prefix "$HOME/.local" --no-update-notifier @openai/codex || { echo "Codex installation failed." >&2; exit 1; } ;;
  *) echo "unknown provider: $provider" >&2; exit 2 ;;
esac
