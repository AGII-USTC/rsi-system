#!/usr/bin/env bash
set -euo pipefail

runtime="${XDG_RUNTIME_DIR:?}/rsi-secret-service"
data="$HOME/.local/share/rsi/secret-service"
password_file="$HOME/.config/rsi/secret-service/password"
bus_address="unix:path=$runtime/bus"

if [[ ! -f "$password_file" || ! -r "$password_file" ]]; then
  echo "RSI keyring password file is missing" >&2
  exit 1
fi
if [[ "$(stat -c %a "$password_file")" != "600" ]]; then
  echo "RSI keyring password file must have mode 0600" >&2
  exit 1
fi

umask 077
mkdir -p "$runtime" "$data"
chmod 700 "$runtime" "$data"
rm -f "$runtime/bus"
export XDG_DATA_HOME="$data"
export DBUS_SESSION_BUS_ADDRESS="$bus_address"
export GNOME_KEYRING_CONTROL="$runtime/control"

/usr/bin/dbus-daemon --session --address="$bus_address" --nofork --nopidfile &
bus_pid=$!
keyring_pid=""
cleanup() {
  [[ -z "$keyring_pid" ]] || kill "$keyring_pid" 2>/dev/null || true
  kill "$bus_pid" 2>/dev/null || true
  wait "$bus_pid" 2>/dev/null || true
}
trap cleanup EXIT TERM INT

for _ in {1..100}; do
  [[ -S "$runtime/bus" ]] && break
  kill -0 "$bus_pid" 2>/dev/null || exit 1
  sleep 0.1
done
[[ -S "$runtime/bus" ]] || exit 1

/usr/bin/gnome-keyring-daemon --foreground --components=secrets \
  --control-directory="$runtime/control" --unlock < "$password_file" &
keyring_pid=$!
for _ in {1..100}; do
  if /usr/bin/busctl --address="$bus_address" --user list --no-pager 2>/dev/null |
      grep -q 'org.freedesktop.secrets'; then
    printf '%s\n' "$bus_address" > "$runtime/address"
    wait "$keyring_pid"
    exit $?
  fi
  kill -0 "$keyring_pid" 2>/dev/null || exit 1
  sleep 0.1
done
echo "RSI Secret Service did not appear on its private bus" >&2
exit 1
