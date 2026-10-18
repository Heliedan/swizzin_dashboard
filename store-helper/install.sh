#!/bin/bash
# Installs the App Store helper (or removes it with --uninstall).
# Run as root from a copy of this repository that the panel user cannot write, for example:
#   git clone -b heliedan https://github.com/Heliedan/swizzin_dashboard /root/swizzin_dashboard
#   bash /root/swizzin_dashboard/store-helper/install.sh
set -euo pipefail
HERE=$(dirname "$(readlink -f "$0")")
DEST=/usr/local/lib/swizzin-store
UNIT=/etc/systemd/system/swizzin-store.service
NEEDRESTART=/etc/needrestart/conf.d/swizzin-store.conf

[[ $EUID -eq 0 ]] || { echo "Run this as root (sudo)." >&2; exit 1; }

# Restarting or removing the helper would cut off an installation or an uninstallation in progress: go on only
# if the running helper clearly says that none is running (any doubt refuses).
if systemctl is-active --quiet swizzin-store; then
    state=$(/usr/bin/python3 -I -c '
import json, socket
s = socket.socket(socket.AF_UNIX); s.settimeout(5); s.connect("/run/swizzin-store/store.sock")
s.sendall(b"{\"op\": \"catalogue\"}\n"); data = b""
while not data.endswith(b"\n"):
    chunk = s.recv(65536)
    if not chunk: break
    data += chunk
print("idle" if json.loads(data).get("job") is None else "busy")' 2>/dev/null || true)
    if [[ $state == busy ]]; then
        echo "An installation or an uninstallation is running in the App Store: wait for it to finish, then run this again." >&2
        exit 1
    elif [[ $state != idle ]]; then
        echo "Could not ask the App Store helper whether an installation is running. Try again in a minute," \
             "or stop it first if you are sure: systemctl stop swizzin-store" >&2
        exit 1
    fi
fi

if [[ ${1:-} == --uninstall ]]; then
    systemctl disable --now swizzin-store 2>/dev/null || true
    rm -f "$UNIT" "$NEEDRESTART"
    rm -rf "$DEST"
    systemctl daemon-reload
    echo "App Store helper removed (its logs stay in /var/log/swizzin-store, the copies of settings in /var/backups/swizzin-store)."
    exit 0
fi

# Root will run these files: each one, and every folder from here up to /, must belong to root and be writable
# by root only. Otherwise another user (the panel's, for one) could change them, or swap them in between.
unsafe() { [[ $(stat -c %u "$1") != 0 ]] || (( 8#$(stat -c %a "$1") & 8#022 )); }
FILES=(swizzin_store.py removable.py remove_runner.py check_app.py)
for f in "${FILES[@]/#/$HERE/}" "$HERE/swizzin-store.service" "$HERE/needrestart.conf"; do
    if unsafe "$f"; then
        echo "Refusing to install $f: it must belong to root and be writable by root only." >&2
        exit 1
    fi
done
dir=$HERE
while :; do
    if unsafe "$dir"; then
        echo "Refusing to install from $HERE: $dir must belong to root and be writable by root only." \
             "Use a copy in /root (see the top of this script)." >&2
        exit 1
    fi
    [[ $dir == / ]] && break
    dir=$(dirname "$dir")
done
id swizzin >/dev/null 2>&1 || { echo "No 'swizzin' user: is the Swizzin panel installed?" >&2; exit 1; }
[[ -x /usr/bin/python3 ]] || { echo "/usr/bin/python3 is missing." >&2; exit 1; }
[[ -x /usr/local/bin/swizzin/box ]] || { echo "Swizzin's box command is missing (/usr/local/bin/swizzin/box)." >&2; exit 1; }

install -d -o root -g root -m 0755 "$DEST"
for f in "${FILES[@]}"; do
    install -o root -g root -m 0644 "$HERE/$f" "$DEST/$f"
done
install -o root -g root -m 0644 "$HERE/swizzin-store.service" "$UNIT"
if [[ -d /etc/needrestart/conf.d ]]; then
    install -o root -g root -m 0644 "$HERE/needrestart.conf" "$NEEDRESTART"
fi
systemctl daemon-reload
systemctl enable swizzin-store >/dev/null 2>&1
systemctl restart swizzin-store

# Checks: the socket exists with the expected owner and mode, and the panel user can read the catalogue.
for _ in $(seq 50); do [[ -S /run/swizzin-store/store.sock ]] && break; sleep 0.1; done
stat -c '%n %U:%G %a' /run/swizzin-store /run/swizzin-store/store.sock
sudo -u swizzin /usr/bin/python3 -I -c '
import json, socket
s = socket.socket(socket.AF_UNIX); s.settimeout(3); s.connect("/run/swizzin-store/store.sock")
s.sendall(b"{\"op\": \"catalogue\"}\n"); data = b""
while not data.endswith(b"\n"):
    chunk = s.recv(65536)
    if not chunk: break
    data += chunk
r = json.loads(data); print("catalogue read by the panel user:", len(r["apps"]), "apps")'
echo "App Store helper installed and running (systemctl status swizzin-store)."
