#!/usr/bin/env bash
# Native invocation of UltimateAssistant on a configured Linux node.
set -euo pipefail
script=$(readlink -f -- "${BASH_SOURCE[0]}")
root=$(cd -- "$(dirname -- "$script")/../.." && pwd)
cd "$root/apps/assistant"
exec "$root/.venvs/assistant/bin/python" -m ultimate_assistant.desktop "$@"
