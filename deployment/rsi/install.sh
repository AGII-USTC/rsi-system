#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
repo="$(pwd -P)"
if [[ "$repo" != "$HOME/rsi_system" ]]; then
  echo "Install from $HOME/rsi_system; review unit paths for a different deployment." >&2
  exit 2
fi
python="$HOME/rsi_system/.venv/bin/python"
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/rsi" <<'WRAPPER'
#!/bin/sh
cd "$HOME/rsi_system" || exit 1
exec "$HOME/rsi_system/.venv/bin/python" -m rsi_system.core "$@"
WRAPPER
chmod 755 "$HOME/.local/bin/rsi"
"$python" -m rsi_system.core init
"$python" -m rsi_system.core lint
bash deployment/rsi/install-secret-service.sh
mkdir -p "$HOME/.config/systemd/user"
install -m 644 deployment/rsi/embodied-rsi-worker.service deployment/rsi/embodied-rsi-worker.timer deployment/rsi/embodied-rsi-research.service deployment/rsi/embodied-rsi-research.timer "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now embodied-rsi-worker.timer embodied-rsi-research.timer
systemctl --user status embodied-rsi-worker.timer --no-pager
loginctl show-user "$USER" -p Linger
