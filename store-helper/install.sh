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

if [[ ${1:-} == --uninstall ]]; then
    systemctl disable --now swizzin-store 2>/dev/null || true
    rm -f "$UNIT" "$NEEDRESTART"
    rm -rf "$DEST"
    systemctl daemon-reload
    echo "App Store helper removed (its logs stay in /var/log/swizzin-store)."
    exit 0
fi

# Root will run these files: never take them from a place the panel user can change.
if [[ $(stat -c %U "$HERE") == swizzin || $(stat -c %U "$HERE/swizzin_store.py") == swizzin ]]; then
    echo "Refusing to install from $HERE: the panel user can write it. Use a separate copy (see the top of this script)." >&2
    exit 1
fi
id swizzin >/dev/null 2>&1 || { echo "No 'swizzin' user: is the Swizzin panel installed?" >&2; exit 1; }
[[ -x /usr/bin/python3 ]] || { echo "/usr/bin/python3 is missing." >&2; exit 1; }
[[ -x /usr/local/bin/swizzin/box ]] || { echo "Swizzin's box command is missing (/usr/local/bin/swizzin/box)." >&2; exit 1; }

# Restarting the helper would cut off an installation in progress.
if [[ -S /run/swizzin-store/store.sock ]] && /usr/bin/python3 -I -c '
import json, socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(3); s.connect("/run/swizzin-store/store.sock")
s.sendall(b"{\"op\": \"catalogue\"}\n"); data = b""
while not data.endswith(b"\n"):
    chunk = s.recv(65536)
    if not chunk: break
    data += chunk
sys.exit(0 if json.loads(data).get("job") else 1)' 2>/dev/null; then
    echo "An installation is running in the App Store: wait for it to finish, then run this again." >&2
    exit 1
fi

install -d -o root -g root -m 0755 "$DEST"
install -o root -g root -m 0644 "$HERE/swizzin_store.py" "$DEST/swizzin_store.py"
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
