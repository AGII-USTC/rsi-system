#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "$0")/../.." && pwd -P)"
if [[ "$repo" != "$HOME/rsi_system" ]]; then
  echo "Install from $HOME/rsi_system" >&2
  exit 2
fi
cd "$repo"

umask 077
secret_dir="$HOME/.config/rsi/secret-service"
mkdir -p "$secret_dir" "$HOME/.config/systemd/user"
chmod 700 "$secret_dir"
password_file="$secret_dir/password"
if [[ ! -e "$password_file" ]]; then
  /usr/bin/openssl rand -base64 48 > "$password_file"
fi
chmod 600 "$password_file"
chmod 700 deployment/rsi/rsi-secret-service.sh
install -m 644 deployment/rsi/embodied-rsi-secret-service.service \
  "$HOME/.config/systemd/user/embodied-rsi-secret-service.service"
systemctl --user daemon-reload
systemctl --user enable --now embodied-rsi-secret-service.service
systemctl --user is-active embodied-rsi-secret-service.service
printf 'RSI D-Bus address file: %s\n' "${XDG_RUNTIME_DIR:?}/rsi-secret-service/address"
