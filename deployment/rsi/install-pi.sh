#!/usr/bin/env bash
set -euo pipefail
rsi_node_version=v22.23.3
rsi_pi_version=0.87.1
rsi_tools="$HOME/.local/share/rsi"
mkdir -p "$rsi_tools/node" "$rsi_tools/pi" "$HOME/.local/bin"
rsi_archive="$rsi_tools/node/node-$rsi_node_version-linux-x64.tar.xz"
if [[ ! -x "$rsi_tools/node/node-$rsi_node_version-linux-x64/bin/node" ]]; then
  curl -fsSL --retry 2 --max-time 180 "https://nodejs.org/dist/$rsi_node_version/node-$rsi_node_version-linux-x64.tar.xz" -o "$rsi_archive"
  printf '%s  %s\n' df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de "$rsi_archive" | sha256sum -c -
  tar -xf "$rsi_archive" -C "$rsi_tools/node"
fi
rsi_node_bin="$rsi_tools/node/node-$rsi_node_version-linux-x64/bin"
PATH="$rsi_node_bin:$PATH" "$rsi_node_bin/npm" install --prefix "$rsi_tools/pi" --ignore-scripts --include=optional --no-audit --no-fund --registry=https://registry.npmjs.org "@earendil-works/pi-coding-agent@$rsi_pi_version" pi-mcp-adapter@2.32.1 pi-web-access@0.28.0 @earendil-works/pi-ai@0.84.1 @earendil-works/pi-tui@0.87.1 typebox@1.3.27 jiti@2.7.0 zod@4.1.12
if [[ ! -e "$HOME/.pi/web-search.json" ]]; then
  install -m 600 "$(dirname "$0")/pi-web-search.json" "$HOME/.pi/web-search.json"
fi
cat > "$HOME/.local/bin/pi" <<'WRAPPER'
#!/bin/sh
PATH="$HOME/.local/share/rsi/node/node-v22.23.3-linux-x64/bin:$PATH"
export PATH
if [ -f "$HOME/.config/rsi/agent.env" ]; then
  set -a
  . "$HOME/.config/rsi/agent.env"
  set +a
fi
if [ -r "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/rsi-secret-service/address" ]; then
  DBUS_SESSION_BUS_ADDRESS="$(cat "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/rsi-secret-service/address")"
  export DBUS_SESSION_BUS_ADDRESS
fi
exec "$HOME/.local/share/rsi/node/node-v22.23.3-linux-x64/bin/node" --use-env-proxy "$HOME/.local/share/rsi/pi/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js" "$@"
WRAPPER
chmod 755 "$HOME/.local/bin/pi"
"$HOME/.local/bin/pi" --version
