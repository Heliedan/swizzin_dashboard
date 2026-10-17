#!/bin/bash
# Try Heliedan's edition of the Swizzin dashboard, and go back to the official one whenever you like.
# Read it before running it, then, as root on a Swizzin server with the panel installed:
#   sudo bash heliedan.sh install [branch or tag]   switch the panel to this edition (default: the heliedan branch);
#                                                   run it again later to update
#   sudo bash heliedan.sh revert                    put the panel back exactly as it was before the first install
#   sudo bash heliedan.sh status                    show which version runs
# What it changes: the panel's code in /opt/swizzin, after a full backup in /var/backups/heliedan, and the App Store's
# small root service (swizzin-store, see store-helper/). Nothing else: no nginx, sudo or Swizzin setting. Your custom
# tiles (core/custom/profiles.py) and swizzin.cfg are kept. If anything goes wrong, the panel is put back as it was.
set -euo pipefail

REPO=https://github.com/Heliedan/swizzin_dashboard
PANEL=/opt/swizzin
BACKUP=/var/backups/heliedan
PYTHON=/opt/.venv/swizzin/bin/python
HELPER_FILES=(/etc/systemd/system/swizzin-store.service /etc/needrestart/conf.d/swizzin-store.conf)

say() { echo "==> $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
git_panel() { sudo -H -u swizzin git -C "$PANEL" "$@"; }  # the panel's files belong to the swizzin user

checks() {
    [[ $EUID -eq 0 ]] || die "run it as root: sudo bash $0 ${1:-}"
    [[ -f /install/.panel.lock && -d $PANEL/.git ]] || die "no Swizzin panel in $PANEL"
    id swizzin >/dev/null 2>&1 || die "no 'swizzin' user: is the Swizzin panel installed?"
    [[ -x $PYTHON ]] || die "the panel's Python is missing ($PYTHON)"
}

store_busy() {  # an installation started from the App Store must never be cut off; any doubt counts as busy
    systemctl is-active --quiet swizzin-store 2>/dev/null || return 1
    [[ $(/usr/bin/python3 -I -c '
import json, socket
s = socket.socket(socket.AF_UNIX); s.settimeout(5); s.connect("/run/swizzin-store/store.sock")
s.sendall(b"{\"op\": \"catalogue\"}\n"); data = b""
while not data.endswith(b"\n"):
    chunk = s.recv(65536)
    if not chunk: break
    data += chunk
print("idle" if json.loads(data).get("job") is None else "busy")' 2>/dev/null) != idle ]]
}

remove_helper() {
    if systemctl cat swizzin-store >/dev/null 2>&1 || [[ -d /usr/local/lib/swizzin-store ]]; then
        say "Removing the App Store service"
        systemctl disable --now swizzin-store >/dev/null 2>&1 || true
        rm -f "${HELPER_FILES[@]}"
        rm -rf /usr/local/lib/swizzin-store
        systemctl daemon-reload
    fi
}

restore_panel() {  # the panel exactly as in the backup (code, git state, swizzin.cfg, custom tiles)
    say "Putting the panel back as it was"
    rm -rf "$PANEL.restore" "$PANEL.replaced"
    cp -a "$BACKUP/original" "$PANEL.restore"  # a full copy first, then a swap: never a half-restored panel
    mv "$PANEL" "$PANEL.replaced"
    mv "$PANEL.restore" "$PANEL"
    rm -rf "$PANEL.replaced"
    systemctl restart panel
}

install_edition() {
    local ref=${1:-heliedan} changed tmp
    checks install
    ! store_busy || die "an installation is running in the App Store (or its service does not answer): try again later"
    # Files of the panel changed locally would be lost: only the custom tiles may be, as they are the same in both versions.
    changed=$(git_panel status --porcelain --untracked-files=no | awk '{print $NF}' | grep -vx 'core/custom/profiles.py' || true)
    [[ -z $changed ]] || die "these panel files were changed locally, keep a copy and undo the changes first: $(echo $changed)"
    if [[ ! -d $BACKUP/original ]]; then
        say "Backing up $PANEL to $BACKUP/original"
        install -d -m 700 "$BACKUP"
        cp -a "$PANEL" "$BACKUP/original"
        printf 'branch=%s\ncommit=%s\ndate=%s\n' "$(git_panel rev-parse --abbrev-ref HEAD)" "$(git_panel rev-parse HEAD)" \
            "$(date -Is)" > "$BACKUP/original.info"
    else
        say "Keeping the first backup ($BACKUP/original): revert will still bring back the official panel"
    fi
    say "Fetching '$ref' from $REPO"
    git_panel fetch --quiet "$REPO" "$ref" || die "could not fetch '$ref' (nothing was changed)"
    git_panel checkout --quiet -B heliedan FETCH_HEAD || die "git could not switch the panel's files (see above): nothing was changed"
    if ! sudo -u swizzin bash -c "cd $PANEL && $PYTHON -c 'import core.util, core.custom.live_stats, core.custom.store'" >/dev/null 2>&1; then
        restore_panel
        die "the new code does not load with this server's Python: the panel was put back as it was"
    fi
    # install.sh refuses folders another user can write, as /opt/swizzin: it runs from a copy that belongs to root
    say "Installing the App Store service"
    tmp=$(mktemp -d /root/swizzin-store.XXXXXX)
    cp -r "$PANEL/store-helper/." "$tmp/"
    chown -R root:root "$tmp"
    chmod -R go-w "$tmp"
    if ! bash "$tmp/install.sh"; then
        rm -rf "$tmp"
        remove_helper
        restore_panel
        die "the App Store service could not be installed: the panel was put back as it was"
    fi
    rm -rf "$tmp"
    say "Restarting the panel"
    systemctl restart panel
    sleep 5
    if ! systemctl is-active --quiet panel; then
        remove_helper
        restore_panel
        die "the panel did not start with the new code: it was put back as it was (journalctl -u panel says why)"
    fi
    if journalctl -u panel --since "-1 min" --no-pager -o cat | grep -q "App Store disabled"; then
        say "Warning: the panel runs, but the App Store page could not load (journalctl -u panel says why)"
    fi
    say "Done: Heliedan's edition runs (commit $(git_panel rev-parse --short HEAD)). The App Store is in the sidebar."
    say "To go back to the official panel: sudo bash $0 revert"
}

revert_edition() {
    checks revert
    [[ -d $BACKUP/original ]] || die "no backup in $BACKUP/original: the panel was not switched by this script"
    ! store_busy || die "an installation is running in the App Store (or its service does not answer): try again later"
    remove_helper
    restore_panel
    sleep 5
    systemctl is-active --quiet panel || die "the panel did not restart: journalctl -u panel says why"
    # set aside rather than deleted, so that a later install starts from a fresh backup of the official panel
    local kept="$BACKUP/reverted-$(date +%Y%m%d-%H%M%S)"
    mv "$BACKUP/original" "$kept"
    mv "$BACKUP/original.info" "$kept.info"
    say "Done: the official panel is back (commit $(git_panel rev-parse --short HEAD)). Old backup kept in $kept"
}

show_status() {
    checks status
    local branch commit
    branch=$(git_panel rev-parse --abbrev-ref HEAD)
    commit=$(git_panel rev-parse --short HEAD)
    if git_panel cat-file -e HEAD:core/custom/store/__init__.py 2>/dev/null; then  # only this edition has the App Store
        say "Panel: Heliedan's edition (commit $commit)"
    elif [[ $branch == heliedan ]]; then
        say "Panel: the official code (commit $commit), brought back by box upgrade panel; revert cleans up the rest"
    else
        say "Panel: branch $branch (commit $commit)"
    fi
    say "App Store service: $(systemctl is-active swizzin-store 2>/dev/null || true)"
    if [[ -f $BACKUP/original.info ]]; then
        say "Backup of the panel from before the switch: $BACKUP/original ($(paste -sd' ' "$BACKUP/original.info"))"
    else
        say "No backup: the panel was not switched by this script"
    fi
}

case ${1:-} in
    install) install_edition "${2:-}" ;;
    revert) revert_edition ;;
    status) show_status ;;
    *) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
