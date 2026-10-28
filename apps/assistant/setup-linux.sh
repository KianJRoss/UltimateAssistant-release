#!/usr/bin/env bash
# Linux counterpart of setup.ps1: the assistant's Python environment, plus either the whole
# assistant on this computer (--standalone: its own local Herald router and app, started at login
# by a systemd user service) or this device as a node of a Herald router elsewhere (--node).
#
#   apps/assistant/setup-linux.sh --standalone          # the assistant on this computer
#   apps/assistant/setup-linux.sh --node http://ROUTER:8790
#   apps/assistant/setup-linux.sh                       # environment only (also --env-only)
#
# ULTIMATE_ASSISTANT_SKIP_PACKAGES=1 skips the system packages (and their sudo), as updates do.
#
# Herald: a checkout in HERALD_SOURCE_ROOT installs editable; otherwise the release pinned in
# herald.json, checked against its SHA-256. The router key is read from ~/.herald/.env
# (HERALD_API_KEY=...); when it's missing you are asked for it. Desktop features on Hyprland
# (activity signals, the canvas overlay, screen vision, hyprland-ui) need the system packages
# below; on Arch they are installed with pacman, elsewhere their names are printed.
set -euo pipefail

app_root=$(cd "$(dirname "$0")" && pwd)
workspace_root=$(cd "$app_root/../.." && pwd)
venv="$workspace_root/.venvs/assistant"
router=""
mode=env
case ${1:-} in
  --node) mode=node; router=${2:?--node needs the router URL, e.g. http://100.64.0.1:8790} ;;
  --standalone) mode=standalone ;;
  --env-only|"") mode=env ;;
  *) echo "Usage: setup-linux.sh [--standalone | --node ROUTER_URL | --env-only]" >&2; exit 2 ;;
esac

# Desktop packages: grim (screen capture), wtype (keys), swayidle (idle), and the system Qt with
# layer-shell-qt for the canvas overlay (pip's PySide6 can't load the system layer-shell plugin).
packages=(grim wtype swayidle pipewire pyside6 qt6-webengine layer-shell-qt python-httpx)
if [[ ${ULTIMATE_ASSISTANT_SKIP_PACKAGES:-} == 1 ]]; then
  :
elif command -v pacman >/dev/null; then
  missing=$(pacman -T "${packages[@]}" || true)
  [[ -n $missing ]] && sudo pacman -S --needed --noconfirm $missing
else
  echo "Install these with your package manager for the desktop features: ${packages[*]}"
fi

python=""
# a version manager's shim can exist without a version behind it: only take one that runs
if command -v python3.13 >/dev/null && python3.13 -c "" 2>/dev/null; then
  python=$(command -v python3.13)
elif command -v mise >/dev/null; then
  mise install python@3.13 >/dev/null && python="$(mise where python@3.13)/bin/python3.13"
fi
[[ -n $python ]] || { echo "Python 3.13 is required for the assistant runtime (mise install python@3.13)." >&2; exit 1; }

[[ -x $venv/bin/python ]] || "$python" -m venv "$venv"
"$venv/bin/python" -m pip install -q --upgrade pip
if [[ -n ${HERALD_SOURCE_ROOT:-} && -f $HERALD_SOURCE_ROOT/pyproject.toml ]]; then
  "$venv/bin/python" -m pip install -q -e "$HERALD_SOURCE_ROOT"
else
  temp=$(mktemp -d)
  trap 'rm -rf "$temp"' EXIT
  wheels=()
  while IFS=$'\t' read -r name file url sha; do
    if [[ -n ${HERALD_WHEEL_DIR:-} && -f $HERALD_WHEEL_DIR/$file ]]; then  # release testing: the exact files, still checksummed
      cp "$HERALD_WHEEL_DIR/$file" "$temp/$file"
    else
      curl -fsSL "$url" -o "$temp/$file"
    fi
    echo "$sha  $temp/$file" | sha256sum -c --quiet - || { echo "The downloaded $name does not match its published checksum." >&2; exit 1; }
    wheels+=("$temp/$file")
  done < <("$venv/bin/python" -c 'import json,sys; [print(p["name"], p["file"], p["url"], p["sha256"], sep="\t") for p in json.load(open(sys.argv[1]))["parts"]]' "$app_root/herald.json")
  "$venv/bin/python" -m pip install -q "${wheels[@]}"
fi
"$venv/bin/python" -m pip install -q -r "$app_root/requirements.txt"
(cd "$app_root" && "$venv/bin/python" -c "import matplotlib, numpy, sympy; import herald.math_tools, ultimate_assistant.life_tools; print('Math and life features are ready.')")

# Local speech (setup-kokoro.ps1): Kokoro ONNX with the quantized model, checked by SHA-256.
kokoro="$workspace_root/.venvs/kokoro"
models="$app_root/models/kokoro"
[[ -x $kokoro/bin/python ]] || "$python" -m venv "$kokoro"
"$kokoro/bin/python" -m pip install -q --upgrade pip
"$kokoro/bin/python" -m pip install -q "kokoro-onnx>=0.6.1" "soundfile>=0.13"
mkdir -p "$models"
release=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1
[[ -f $models/kokoro-v1.0.int8.onnx ]] || curl -fsSL "$release/kokoro-v1.0.int8.onnx" -o "$models/kokoro-v1.0.int8.onnx"
[[ -f $models/voices-v1.0.bin ]] || curl -fsSL "$release/voices-v1.0.bin" -o "$models/voices-v1.0.bin"
[[ -f $models/LICENSE-Apache-2.0.txt ]] || curl -fsSL https://www.apache.org/licenses/LICENSE-2.0.txt -o "$models/LICENSE-Apache-2.0.txt"
sha256sum -c --quiet - <<EOF || { echo "Kokoro model files failed SHA-256 verification." >&2; exit 1; }
ae315a79b623f244700e4afb9246c46a26066782e049ba174bf3ba433970ee9c  $models/kokoro-v1.0.int8.onnx
bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d  $models/voices-v1.0.bin
EOF
echo "Kokoro speech ready."

# Browser tools (setup-browser-tools.ps1): Kapture and Playwright MCP, with Playwright's Chromium.
if command -v npm >/dev/null; then
  runtime="$workspace_root/.runtime"
  npm install -s --prefix "$runtime/browser-mcp" --no-audit --no-fund --no-update-notifier kapture-mcp@2.6.1 @playwright/mcp@0.0.83
  cli="$runtime/browser-mcp/node_modules/playwright/cli.js"
  [[ -f $cli ]] || cli="$runtime/browser-mcp/node_modules/playwright-core/cli.js"
  PLAYWRIGHT_BROWSERS_PATH="$runtime/browsers" node "$cli" install chromium >/dev/null
  echo "Kapture and Playwright MCP installed. Kapture needs the browser extension and a connected tab."
else
  echo "Node.js (npm) is not installed: skipping the browser tools (install nodejs and npm, then run again)."
fi
echo "Environment ready: $venv/bin/python"

[[ $mode == env ]] && exit 0

if [[ $mode == standalone ]]; then
  # -- the whole assistant on this computer -----------------------------------------------
  # local_runtime starts the assistant's own Herald router on 127.0.0.1:18790 with per-user state
  # and the app on 127.0.0.1:8765; updates install beside this copy and are selected at launch.
  mkdir -p ~/.config/systemd/user ~/.local/share/applications
  cat > ~/.config/systemd/user/ultimate-assistant.service <<UNIT
[Unit]
Description=Ultimate Assistant (local Herald router and app)
After=network-online.target

[Service]
WorkingDirectory=$app_root
Environment=PYTHONUNBUFFERED=1
ExecStart=$venv/bin/python -m ultimate_assistant.local_runtime
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
UNIT
  desktop=~/.local/share/applications/ultimate-assistant.desktop
  if [[ ! -e $desktop ]] || grep -q 'xdg-open http://127.0.0.1:8765' "$desktop"; then
    cat > "$desktop" <<ENTRY
[Desktop Entry]
Type=Application
Name=Ultimate Assistant
Comment=Open your assistant
Exec=xdg-open http://127.0.0.1:8765
Icon=audio-input-microphone
Terminal=false
Categories=Utility;
ENTRY
  else
    echo "Keeping your existing $desktop; open http://127.0.0.1:8765 for the app."
  fi
  systemctl --user daemon-reload
  systemctl --user enable ultimate-assistant.service
  systemctl --user restart ultimate-assistant.service
  for _ in $(seq 1 120); do
    if curl -fs -o /dev/null http://127.0.0.1:8765/; then
      echo "Ultimate Assistant is running: open http://127.0.0.1:8765 (or 'Ultimate Assistant' in your app launcher)."
      exit 0
    fi
    sleep 1
  done
  echo "The assistant service did not answer yet; check: journalctl --user -u ultimate-assistant" >&2
  exit 1
fi

# -- this device as a node ------------------------------------------------------------
mkdir -p -m 700 ~/.herald
if ! grep -q '^HERALD_API_KEY=' ~/.herald/.env 2>/dev/null; then
  read -rsp "Herald router key (HERALD_API_KEY of the router): " key; echo
  (umask 077; echo "HERALD_API_KEY=$key" >> ~/.herald/.env)
fi
status=$(set -a; . ~/.herald/.env; curl -s -o /dev/null -w '%{http_code}' -H "x-api-key: $HERALD_API_KEY" "${router%/}/health")
[[ $status == 200 ]] || { echo "The router at $router did not accept the key (HTTP $status)." >&2; exit 1; }

mkdir -p ~/.local/bin ~/.local/share/applications
launcher="$HOME/.local/bin/ultimate-assistant"
if [[ ! -e $launcher || ( -L $launcher && $(readlink -f "$launcher") == "$app_root/desktop.sh" ) ]]; then
  ln -sfn "$app_root/desktop.sh" "$launcher"
  cat > ~/.local/share/applications/ultimate-assistant.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Ultimate Assistant
Comment=Talk to your assistant or continue in text
Exec="$launcher" text
Icon=audio-input-microphone
Terminal=false
Categories=Utility;
Actions=Voice;

[Desktop Action Voice]
Name=Talk to Ultimate Assistant
Exec="$launcher" toggle
EOF
else
  echo "Keeping your existing $launcher; use $app_root/desktop.sh directly."
fi

mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/ultimate-assistant-node.service <<EOF
[Unit]
Description=Ultimate Assistant node linked to the Herald router at $router
After=network-online.target graphical-session.target

[Service]
WorkingDirectory=$app_root
Environment=HERALD_URL=$router
Environment=ULTIMATE_ASSISTANT_MAIN_NODE=0
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=%h/.herald/.env
ExecStart=$venv/bin/python -m ultimate_assistant.node_link
Restart=on-failure
RestartSec=10

[Install]
WantedBy=graphical-session.target
EOF
systemctl --user daemon-reload
systemctl --user import-environment WAYLAND_DISPLAY HYPRLAND_INSTANCE_SIGNATURE XDG_CURRENT_DESKTOP XDG_SESSION_TYPE
systemctl --user enable ultimate-assistant-node.service
systemctl --user restart ultimate-assistant-node.service
echo "Node $(hostname) linked to $router; turn on desktop_vision / desktop_control in"
echo "~/.ultimate-assistant/UltimateAssistant/settings.json to offer screen vision and desktop control."
echo "Invoke voice with ultimate-assistant (again to send), or ultimate-assistant text for the optional text interface."
