#!/bin/bash
# Try Heliedan's edition of the Swizzin dashboard, and go back to the official one whenever you like.
# Read it before running it, then, as root on a Swizzin server with the panel installed:
#   sudo bash heliedan.sh install [branch or tag]   switch the panel to this edition (default: the heliedan branch);
#                                                   run it again later to update
#   sudo bash heliedan.sh revert                    put the panel back exactly as it was before the first install
#   sudo bash heliedan.sh status                    show which version runs, and an App Store service left behind
#   sudo bash heliedan.sh remove-store              remove the App Store service (before box remove panel, or after)
# What it changes: the panel's code in /opt/swizzin, after a full backup in /var/backups/heliedan, and the App Store's
# small root service (swizzin-store, see store-helper/). Nothing else: no nginx, sudo or Swizzin setting. Your custom
# tiles (core/custom/profiles.py) and swizzin.cfg are kept. If anything goes wrong, the panel is put back as it was.
set -euo pipefail

REPO=https://github.com/Heliedan/swizzin_dashboard
PANEL=/opt/swizzin
PANEL_LOCK=/install/.panel.lock
# Swizzin's lock when the edition was installed with box install altdash: box then manages the panel and the App
# Store service, and this script leaves them alone.
ALTDASH_LOCK=/install/.altdash.lock
PANEL_USER=swizzin
BACKUP=/var/backups/heliedan
PYTHON=/opt/.venv/swizzin/bin/python
# The App Store service, as store-helper/install.sh puts it in place. Its logs (/var/log/swizzin-store) and the
# copies of the settings of the apps it uninstalled (/var/backups/swizzin-store) are never deleted.
STORE_LIB=/usr/local/lib/swizzin-store
STORE_UNIT=/etc/systemd/system/swizzin-store.service
STORE_FILES=("$STORE_UNIT" /etc/systemd/system/multi-user.target.wants/swizzin-store.service
             /etc/needrestart/conf.d/swizzin-store.conf)
STORE_SOCKET=/run/swizzin-store/store.sock
# The service runs only while the panel has this file of the edition (PANEL_MARKER in store-helper/swizzin_store.py)
MARKER=store-helper/swizzin_store.py

say() { echo "==> $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
git_panel() { sudo -H -u "$PANEL_USER" git -C "$PANEL" "$@"; }  # the panel's files belong to the swizzin user
need_root() { [[ $EUID -eq 0 ]] || die "run it as root: sudo bash $0 ${1:-}"; }
not_box() {
    [[ ! -f $ALTDASH_LOCK ]] || die "the edition was installed with box install altdash: Swizzin manages it." \
        "Use box upgrade altdash to update it, box remove altdash then box install panel for the official panel"
}

checks() {
    need_root "${1:-}"
    not_box
    if [[ ! -f $PANEL_LOCK || ! -d $PANEL/.git ]]; then
        store_installed && die "no Swizzin panel in $PANEL. If box remove panel removed it, remove the App Store" \
            "service it left behind: sudo bash $0 remove-store"
        die "no Swizzin panel in $PANEL"
    fi
    id "$PANEL_USER" >/dev/null 2>&1 || die "no '$PANEL_USER' user: is the Swizzin panel installed?"
    [[ -x $PYTHON ]] || die "the panel's Python is missing ($PYTHON)"
}

edition_panel() { [[ -f $PANEL/$MARKER && -s $PANEL/$MARKER && ! -L $PANEL/$MARKER ]]; }  # as swizzin-store.service checks it

store_installed() {  # any file of the App Store service, or the service still running
    local f
    for f in "${STORE_FILES[@]}" "$STORE_UNIT.d" "$STORE_LIB"; do
        [[ -e $f || -L $f ]] && return 0
    done
    systemctl is-active --quiet swizzin-store 2>/dev/null
}

store_left() {  # what is left of the App Store service, in words (nothing: empty)
    local f left=()
    for f in "${STORE_FILES[@]}" "$STORE_UNIT.d" "$STORE_LIB"; do
        [[ -e $f || -L $f ]] && left+=("$f")
    done
    systemctl is-active --quiet swizzin-store 2>/dev/null && left+=("the swizzin-store service, still running")
    echo "${left[*]}"
}

store_job() {  # what the App Store service runs ("install sonarr"), "none", or nothing when it does not answer
    /usr/bin/python3 -I -c '
import json, socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(5); s.connect(sys.argv[1])
s.sendall(b"{\"op\": \"catalogue\"}\n"); data = b""
while not data.endswith(b"\n"):
    chunk = s.recv(65536)
    if not chunk: break
    data += chunk
job = json.loads(data).get("job")
print("none" if job is None else "%s %s" % (job.get("action", "install"), job.get("app")))' "$STORE_SOCKET" 2>/dev/null || true
}

store_busy() {  # an installation started from the App Store must never be cut off; any doubt counts as busy
    systemctl is-active --quiet swizzin-store 2>/dev/null || return 1
    [[ $(store_job) != none ]]
}

remove_helper() {  # never stops on an error: install's failures go on to put the panel back (store_left says the rest)
    store_installed || return 0
    say "Removing the App Store service"
    systemctl disable --now swizzin-store >/dev/null 2>&1 || systemctl stop swizzin-store >/dev/null 2>&1 || true
    rm -f "${STORE_FILES[@]}" || true  # the link of the boot target too: disable cannot remove it without the unit
    rm -rf "$STORE_UNIT.d" "$STORE_LIB" || true
    systemctl daemon-reload || true
    systemctl reset-failed swizzin-store >/dev/null 2>&1 || true
}

drop_helper() {  # install's failure paths: an installation started from the page meanwhile is never cut off
    if store_busy; then
        say "Warning: the App Store service is running a job (or does not answer), so it was left in place." \
            "Once the job is over, remove it: sudo bash $0 remove-store"
    else
        remove_helper
    fi
}

backup_panel() {  # the official panel, before the first switch: what revert puts back
    if [[ -d $BACKUP/original && -f $BACKUP/original.info && $PANEL_LOCK -nt $BACKUP/original.info ]]; then
        # box remove panel then box install panel since that backup: the panel to put back is this one now
        local old
        old="$BACKUP/replaced-$(date +%Y%m%d-%H%M%S)"
        say "The panel was installed again since the backup in $BACKUP/original: that backup is set aside in $old"
        mv "$BACKUP/original" "$old"
        mv "$BACKUP/original.info" "$old.info"
    fi
    if [[ ! -d $BACKUP/original ]]; then
        say "Backing up $PANEL to $BACKUP/original"
        install -d -m 700 "$BACKUP"
        cp -a "$PANEL" "$BACKUP/original"
        printf 'branch=%s\ncommit=%s\ndate=%s\n' "$(git_panel rev-parse --abbrev-ref HEAD)" "$(git_panel rev-parse HEAD)" \
            "$(date -Is)" > "$BACKUP/original.info"
    else
        say "Keeping the first backup ($BACKUP/original): revert will still bring back the official panel"
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
    backup_panel
    say "Fetching '$ref' from $REPO"
    git_panel fetch --quiet "$REPO" "$ref" || die "could not fetch '$ref' (nothing was changed)"
    git_panel checkout --quiet -B heliedan FETCH_HEAD || die "git could not switch the panel's files (see above): nothing was changed"
    if ! sudo -u "$PANEL_USER" bash -c "cd $PANEL && $PYTHON -c 'import core.util, core.custom.live_stats, core.custom.store'" >/dev/null 2>&1; then
        drop_helper  # an update: the service of the previous version would be left without its panel
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
        drop_helper
        restore_panel
        die "the App Store service could not be installed: the panel was put back as it was"
    fi
    rm -rf "$tmp"
    say "Restarting the panel"
    systemctl restart panel
    sleep 5
    if ! systemctl is-active --quiet panel; then
        drop_helper
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
    local left
    checks revert
    [[ -d $BACKUP/original ]] || die "no backup in $BACKUP/original: the panel was not switched by this script"
    ! store_busy || die "an installation is running in the App Store (or its service does not answer): try again later"
    remove_helper
    left=$(store_left)
    [[ -z $left ]] || die "the App Store service could not be removed completely ($left): the panel was not changed, run this again"
    restore_panel
    sleep 5
    systemctl is-active --quiet panel || die "the panel did not restart: journalctl -u panel says why"
    # set aside rather than deleted, so that a later install starts from a fresh backup of the official panel
    local kept="$BACKUP/reverted-$(date +%Y%m%d-%H%M%S)"
    mv "$BACKUP/original" "$kept"
    mv "$BACKUP/original.info" "$kept.info"
    say "Done: the official panel is back (commit $(git_panel rev-parse --short HEAD)). Old backup kept in $kept"
}

remove_store() {
    local left
    need_root remove-store
    not_box
    if ! store_installed; then
        say "The App Store service is not installed: nothing to remove"
        return 0
    fi
    ! store_busy || die "an installation or an uninstallation is running in the App Store (or its service does not" \
        "answer): try again later. If you are sure that none runs: systemctl stop swizzin-store, then run this again"
    remove_helper
    left=$(store_left)
    [[ -z $left ]] || die "the App Store service could not be removed completely: $left"
    say "Done: the App Store service is removed. Its logs (/var/log/swizzin-store) and the copies of the settings of" \
        "the apps it uninstalled (/var/backups/swizzin-store) are kept: delete them by hand if you do not need them"
    if edition_panel; then
        say "The panel is still Heliedan's edition: its App Store page now says the service is missing." \
            "sudo bash $0 install puts it back, sudo bash $0 revert puts the official panel back"
    elif [[ -d $BACKUP/original && ! -f $PANEL_LOCK ]]; then
        say "The backup of the official panel stays in $BACKUP: delete it by hand if you do not need it"
    fi
}

show_status() {
    local branch commit state job why
    need_root status
    if [[ -f $ALTDASH_LOCK ]]; then
        say "Installed with box install altdash: Swizzin manages the panel and the App Store service." \
            "box upgrade altdash updates them, box remove altdash removes them"
        return 0
    fi
    if [[ -f $PANEL_LOCK && -d $PANEL/.git ]]; then
        branch=$(git_panel rev-parse --abbrev-ref HEAD)
        commit=$(git_panel rev-parse --short HEAD)
        if git_panel cat-file -e HEAD:core/custom/store/__init__.py 2>/dev/null; then  # only this edition has the App Store
            say "Panel: Heliedan's edition (commit $commit)"
        elif [[ $branch == heliedan ]]; then
            say "Panel: the official code (commit $commit), brought back by box upgrade panel or box update; revert cleans up the rest"
        else
            say "Panel: branch $branch (commit $commit)"
        fi
    else
        say "Panel: not installed (no $PANEL_LOCK or no $PANEL): removed by box remove panel?"
    fi
    state=$(systemctl is-active swizzin-store 2>/dev/null || true)
    if ! store_installed; then
        say "App Store service: not installed"
    elif edition_panel; then
        say "App Store service: $state"
    elif ! grep -qs '^ConditionFileNotEmpty=' "$STORE_UNIT"; then  # installed before it knew its panel
        say "App Store service: LEFT BEHIND ($state). The panel it belongs to is gone or is no longer Heliedan's" \
            "edition, and this older version keeps running and starts at boot. Remove it now: sudo bash $0 remove-store"
    else
        why="The panel it belongs to is gone or is no longer Heliedan's edition"
        job=none
        [[ $state != active ]] || job=$(store_job)
        case $job in
            none) say "App Store service: LEFT BEHIND ($state). $why: it stops by itself and does not start again." \
                      "Remove it: sudo bash $0 remove-store" ;;
            "") say "App Store service: LEFT BEHIND ($state, does not answer). $why. If you are sure that no job" \
                    "runs: systemctl stop swizzin-store, then sudo bash $0 remove-store" ;;
            *) say "App Store service: LEFT BEHIND ($state, running: $job). $why: it stops by itself once that job" \
                   "ends. A question of the installer cannot be answered without the panel: systemctl stop" \
                   "swizzin-store ends it. Then remove it: sudo bash $0 remove-store" ;;
        esac
    fi
    if [[ -f $BACKUP/original.info ]]; then
        say "Backup of the panel from before the switch: $BACKUP/original ($(paste -sd' ' "$BACKUP/original.info"))"
    else
        say "No backup: the panel was not switched by this script"
    fi
}

main() {
    case ${1:-} in
        install) install_edition "${2:-}" ;;
        revert) revert_edition ;;
        status) show_status ;;
        remove-store) remove_store ;;
        *) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
    esac
}

# Sourced (tests/test_heliedan.py): the functions only.
[[ ${BASH_SOURCE[0]:-$0} != "$0" ]] || main "$@"
