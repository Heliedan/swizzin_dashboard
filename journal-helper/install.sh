#!/bin/bash
# Installs the journal reader of the panel's Logs buttons (or removes it with --uninstall). It does not run as root:
# see swizzin_journal.py and swizzin-journal@.service. heliedan.sh install runs this; by hand, as root, from a copy
# of this repository that the panel user cannot write, for example:
#   git clone -b heliedan https://github.com/Heliedan/swizzin_dashboard /root/swizzin_dashboard
#   bash /root/swizzin_dashboard/journal-helper/install.sh
set -euo pipefail
HERE=$(dirname "$(readlink -f "$0")")
DEST=/usr/local/lib/swizzin-journal
SOCKET_UNIT=/etc/systemd/system/swizzin-journal.socket
SERVICE_UNIT=/etc/systemd/system/swizzin-journal@.service
SOCKET=/run/swizzin-journal/journal.sock
USER_NAME=swizzin-journal

[[ $EUID -eq 0 ]] || { echo "Run this as root (sudo)." >&2; exit 1; }

if [[ ${1:-} == --uninstall ]]; then
    systemctl disable --now swizzin-journal.socket >/dev/null 2>&1 || systemctl stop swizzin-journal.socket >/dev/null 2>&1 || true
    systemctl stop 'swizzin-journal@*.service' >/dev/null 2>&1 || true  # a read in progress (5 seconds at most)
    rm -f "$SOCKET_UNIT" "$SERVICE_UNIT" /etc/systemd/system/sockets.target.wants/swizzin-journal.socket
    rm -rf "$DEST" "$SOCKET_UNIT.d" "$SERVICE_UNIT.d"
    systemctl daemon-reload
    systemctl reset-failed swizzin-journal.socket >/dev/null 2>&1 || true
    if id "$USER_NAME" >/dev/null 2>&1; then
        userdel "$USER_NAME" >/dev/null 2>&1 || echo "Could not delete the user $USER_NAME: userdel $USER_NAME" >&2
    fi
    if getent group "$USER_NAME" >/dev/null; then  # where userdel keeps the group of the same name
        groupdel "$USER_NAME" >/dev/null 2>&1 || true
    fi
    echo "Journal reader removed."
    exit 0
fi

# The files are run by the reader's own user and installed by root: each one, and every folder from here up to /,
# must belong to root and be writable by root only. Otherwise another user (the panel's, for one) could change them,
# or swap them in between. A file must not be a link either: install would copy what it points to.
unsafe() { [[ $(stat -c %u "$1") != 0 ]] || (( 8#$(stat -c %a "$1") & 8#022 )); }
for f in "$HERE/swizzin_journal.py" "$HERE/swizzin-journal.socket" "$HERE/swizzin-journal@.service"; do
    if [[ -L $f || ! -f $f ]]; then
        echo "Refusing to install $f: it must be a plain file." >&2
        exit 1
    fi
done
for f in "$HERE/swizzin_journal.py" "$HERE/swizzin-journal.socket" "$HERE/swizzin-journal@.service"; do
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
getent group swizzin >/dev/null || { echo "No 'swizzin' group: is the Swizzin panel installed?" >&2; exit 1; }
getent group systemd-journal >/dev/null || { echo "No 'systemd-journal' group on this system." >&2; exit 1; }
[[ -x /usr/bin/python3 ]] || { echo "/usr/bin/python3 is missing." >&2; exit 1; }
# Like the App Store service: only with Heliedan's edition of the panel (see swizzin-journal.socket).
MARKER=/opt/swizzin/journal-helper/swizzin_journal.py
if [[ -L $MARKER || ! -f $MARKER || ! -s $MARKER ]]; then
    echo "The panel in /opt/swizzin is not Heliedan's edition ($MARKER is missing): the journal reader would not" \
         "start. Switch the panel first: heliedan.sh install (see the README)." >&2
    exit 1
fi

if ! id "$USER_NAME" >/dev/null 2>&1; then
    # A system user that owns nothing and cannot log in; the group systemd-journal is given by the unit only.
    useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin \
        --comment "Swizzin journal reader" "$USER_NAME"
fi
install -d -o root -g root -m 0755 "$DEST"
install -o root -g root -m 0644 "$HERE/swizzin_journal.py" "$DEST/swizzin_journal.py"
install -o root -g root -m 0644 "$HERE/swizzin-journal.socket" "$SOCKET_UNIT"
install -o root -g root -m 0644 "$HERE/swizzin-journal@.service" "$SERVICE_UNIT"
systemctl daemon-reload
systemctl enable swizzin-journal.socket >/dev/null 2>&1
systemctl restart swizzin-journal.socket

# Check: the socket exists with the expected owner and mode, and answers the panel user.
for _ in $(seq 50); do [[ -S $SOCKET ]] && break; sleep 0.1; done
stat -c '%n %U:%G %a' "$SOCKET"
sudo -u swizzin /usr/bin/python3 -I -c '
import json, socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(10); s.connect(sys.argv[1])
s.sendall(b"{\"op\": \"hello\"}\n"); data = b""
while not data.endswith(b"\n"):
    chunk = s.recv(65536)
    if not chunk: break
    data += chunk
r = json.loads(data)
assert r.get("ok") is True and "journal" in r.get("features", ()), r
print("journal reader answers the panel user")' "$SOCKET"
echo "Journal reader installed (systemctl status swizzin-journal.socket)."
