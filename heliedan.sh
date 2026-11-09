#!/bin/bash
# Try Heliedan's edition of the Swizzin dashboard, and go back to the official one whenever you like.
# Read it before running it, then, as root on a Swizzin server with the panel installed:
#   sudo bash heliedan.sh install [branch or tag]   switch the panel to this edition (default: the heliedan branch);
#                                                   run it again later to update
#   sudo bash heliedan.sh revert                    put the panel back exactly as it was before the first install
#   sudo bash heliedan.sh status                    show which version runs, and what this edition set up
#   sudo bash heliedan.sh enable-store              turn the App Store on: install its root service (off by default)
#   sudo bash heliedan.sh remove-store              turn the App Store off and remove its root service (before box
#                                                   remove panel; after it, this removes what the edition left behind)
# What it changes: the panel's code in /opt/swizzin, after a full backup in /var/backups/heliedan, the small journal
# reader of the Logs buttons (swizzin-journal, not root, see journal-helper/), and the panel's Python packages when
# requirements.txt asks for newer ones (revert does not downgrade them). The App Store and its root service
# (swizzin-store, see store-helper/) only with enable-store, or when an older version installed them. Nothing else: no
# nginx, sudo or Swizzin setting. Your custom tiles (core/custom/profiles.py) and swizzin.cfg are kept (enable-store
# and remove-store write APP_STORE in it). If anything goes wrong, the panel is put back as it was.
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
# The journal reader of the Logs buttons, as journal-helper/install.sh puts it in place: a socket, a service started for
# each connection, and its own system user.
JOURNAL_LIB=/usr/local/lib/swizzin-journal
JOURNAL_FILES=(/etc/systemd/system/swizzin-journal.socket /etc/systemd/system/swizzin-journal@.service
               /etc/systemd/system/sockets.target.wants/swizzin-journal.socket)
JOURNAL_USER=swizzin-journal

say() { echo "==> $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
git_panel() { sudo -H -u "$PANEL_USER" git -C "$PANEL" "$@"; }  # the panel's files belong to the swizzin user
py_panel() { sudo -H -u "$PANEL_USER" "$PYTHON" -I "$@"; }       # and so does its venv
need_root() { [[ $EUID -eq 0 ]] || die "run it as root: sudo bash $0 ${1:-}"; }
restart_panel() { systemctl restart panel; sleep 5; systemctl is-active --quiet panel; }
not_box() {
    [[ ! -f $ALTDASH_LOCK ]] || die "the edition was installed with box install altdash: Swizzin manages it." \
        "Use box upgrade altdash to update it, box remove altdash then box install panel for the official panel"
}

checks() {
    need_root "${1:-}"
    not_box
    if [[ ! -f $PANEL_LOCK || ! -d $PANEL/.git ]]; then
        if store_installed || journal_installed; then
            die "no Swizzin panel in $PANEL. If box remove panel removed it, remove what this edition left behind:" \
                "sudo bash $0 remove-store"
        fi
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

journal_installed() {  # any file of the journal reader, or its socket still listening
    local f
    for f in "${JOURNAL_FILES[@]}" "$JOURNAL_LIB"; do
        [[ -e $f || -L $f ]] && return 0
    done
    systemctl is-active --quiet swizzin-journal.socket 2>/dev/null
}

journal_left() {  # what is left of the journal reader, in words (nothing: empty)
    local f left=()
    for f in "${JOURNAL_FILES[@]}" "$JOURNAL_LIB"; do
        [[ -e $f || -L $f ]] && left+=("$f")
    done
    systemctl is-active --quiet swizzin-journal.socket 2>/dev/null && left+=("the swizzin-journal socket, still listening")
    echo "${left[*]}"
}

remove_journal() {  # never stops on an error, as remove_helper; a read in progress ends within 5 seconds anyway
    journal_installed || return 0
    say "Removing the journal reader of the Logs buttons"
    systemctl disable --now swizzin-journal.socket >/dev/null 2>&1 || systemctl stop swizzin-journal.socket >/dev/null 2>&1 || true
    systemctl stop 'swizzin-journal@*.service' >/dev/null 2>&1 || true
    rm -f "${JOURNAL_FILES[@]}" || true
    rm -rf "$JOURNAL_LIB" || true
    systemctl daemon-reload || true
    systemctl reset-failed swizzin-journal.socket >/dev/null 2>&1 || true
    if id "$JOURNAL_USER" >/dev/null 2>&1; then
        userdel "$JOURNAL_USER" >/dev/null 2>&1 || say "Warning: the user $JOURNAL_USER could not be deleted: userdel $JOURNAL_USER"
    fi
    if getent group "$JOURNAL_USER" >/dev/null 2>&1; then  # where userdel keeps the group of the same name
        groupdel "$JOURNAL_USER" >/dev/null 2>&1 || true
    fi
}

run_installer() {  # $1: store-helper or journal-helper. Its install.sh refuses folders another user can write, as
    local tmp status=0  # /opt/swizzin: it runs from a copy that belongs to root
    tmp=$(mktemp -d "/root/swizzin-$1.XXXXXX")
    cp -r "$PANEL/$1/." "$tmp/"
    chown -R root:root "$tmp"
    chmod -R go-w "$tmp"
    bash "$tmp/install.sh" || status=$?
    rm -rf "$tmp"
    return "$status"
}

install_journal() {  # the Logs buttons say so when it is missing: a failure is a warning, and leaves nothing behind
    say "Installing the journal reader of the Logs buttons (swizzin-journal, not root)"
    run_installer journal-helper && return 0
    remove_journal
    say "Warning: the journal reader could not be installed (see above): the panel runs, and its Logs buttons say" \
        "that the reader is missing. Run this again later"
}

store_on() {  # what swizzin.cfg says of the App Store: the last APP_STORE line, as the panel reads it
    local line
    line=$(grep -E '^APP_STORE[[:space:]]*=' "$PANEL/swizzin.cfg" 2>/dev/null | tail -n 1 || true)
    [[ $line =~ ^APP_STORE[[:space:]]*=[[:space:]]*True([[:space:]#]|$) ]]
}

store_setting() {  # APP_STORE = True or False in swizzin.cfg, which the panel reads when it starts
    # The panel's folder belongs to the panel's user: never read or write through a link there. The new file is made
    # in the folder above (root's), then renamed over the old one, with its owner and mode.
    /usr/bin/python3 -I - "$PANEL" "$1" <<'PYTHON'
import os, re, stat, sys, tempfile
panel, value = sys.argv[1], sys.argv[2]
path = os.path.join(panel, "swizzin.cfg")
try:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
except FileNotFoundError:
    lines, mode, owner = [], 0o644, None
except OSError as e:
    sys.exit("ERROR: %s could not be read (%s): APP_STORE was not changed" % (path, e.strerror))
else:
    with os.fdopen(fd, encoding="utf-8", errors="surrogateescape") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            sys.exit("ERROR: %s is not a plain file: APP_STORE was not changed" % path)
        lines = f.read().splitlines(True)
    mode, owner = stat.S_IMODE(st.st_mode), (st.st_uid, st.st_gid)
kept = [line for line in lines if not re.match(r"APP_STORE\s*=", line)]
if kept and not kept[-1].endswith("\n"):
    kept[-1] += "\n"
kept.append("APP_STORE = %s  # the App Store: heliedan.sh enable-store and remove-store change it\n" % value)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(panel), prefix=".swizzin.cfg.")
try:
    with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.writelines(kept)
    if owner is not None:
        os.chown(tmp, *owner)
    os.chmod(tmp, mode)
    os.rename(tmp, path)
except BaseException:
    os.unlink(tmp)
    raise
PYTHON
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

packages_ok() { py_panel "$PANEL/tests/test_requirements.py" >/dev/null 2>&1; }  # what requirements.txt asks for

update_packages() {  # as box upgrade panel does for the official panel: pip runs only when requirements.txt asks for more
    packages_ok && return 0
    say "Updating the panel's Python packages to the versions requirements.txt asks for"
    if py_panel -m pip install --quiet --no-cache-dir --disable-pip-version-check --upgrade pip wheel &&
        py_panel -m pip install --quiet --no-cache-dir --disable-pip-version-check -r "$PANEL/requirements.txt" &&
        packages_ok; then
        return 0
    fi
    say "Warning: the panel's Python packages could not all be updated (see above). The panel keeps the ones it has," \
        "which may have known security flaws: run this again later"
}

install_edition() {
    local ref=${1:-heliedan} changed had_store=no
    checks install
    ! store_busy || die "an installation is running in the App Store (or its service does not answer): try again later"
    # Files of the panel changed locally would be lost: only the custom tiles may be, as they are the same in both versions.
    changed=$(git_panel status --porcelain --untracked-files=no | awk '{print $NF}' | grep -vx 'core/custom/profiles.py' || true)
    [[ -z $changed ]] || die "these panel files were changed locally, keep a copy and undo the changes first: $(echo $changed)"
    backup_panel
    # The App Store is optional and off by default: it stays only where it was installed (by enable-store, or by a
    # version from before it was optional), and is then updated with the panel.
    store_installed && had_store=yes
    say "Fetching '$ref' from $REPO"
    git_panel fetch --quiet "$REPO" "$ref" || die "could not fetch '$ref' (nothing was changed)"
    git_panel checkout --quiet -B heliedan FETCH_HEAD || die "git could not switch the panel's files (see above): nothing was changed"
    update_packages  # before the check below, which the new code passes only with the packages it needs
    if ! sudo -u "$PANEL_USER" bash -c "cd $PANEL && $PYTHON -c 'import core.util, core.custom.live_stats, core.custom.store'" >/dev/null 2>&1; then
        drop_helper  # an update: the services of the previous version would be left without their panel
        remove_journal
        restore_panel
        die "the new code does not load with this server's Python: the panel was put back as it was"
    fi
    if [[ -f $PANEL/journal-helper/install.sh ]]; then
        install_journal
    else  # a version from before the reader: nothing would read the journal through it
        remove_journal
    fi
    if [[ $had_store == yes ]]; then
        say "Updating the App Store service (installed on this server; sudo bash $0 remove-store removes it)"
        if ! run_installer store-helper; then
            drop_helper
            remove_journal
            restore_panel
            die "the App Store service could not be installed: the panel was put back as it was"
        fi
        if ! grep -qsE '^APP_STORE[[:space:]]*=' "$PANEL/swizzin.cfg"; then  # installed before it was optional
            say "The App Store stays on: APP_STORE = True in swizzin.cfg"
            store_setting True || say "Warning: swizzin.cfg could not be changed (see above): add APP_STORE = True to it" \
                "by hand, then systemctl restart panel, to see the App Store"
        fi
    fi
    say "Restarting the panel"
    if ! restart_panel; then
        drop_helper
        remove_journal
        restore_panel
        die "the panel did not start with the new code: it was put back as it was (journalctl -u panel says why)"
    fi
    if journalctl -u panel --since "-1 min" --no-pager -o cat | grep -q "App Store disabled"; then
        say "Warning: the panel runs, but the App Store page could not load (journalctl -u panel says why)"
    fi
    say "Done: Heliedan's edition runs (commit $(git_panel rev-parse --short HEAD))."
    if store_on && store_installed; then
        say "The App Store is in the sidebar. To turn it off and remove its root service: sudo bash $0 remove-store"
    else
        say "The App Store is off (it needs a root service): sudo bash $0 enable-store turns it on"
    fi
    say "To go back to the official panel: sudo bash $0 revert"
}

enable_store() {
    local changed
    checks enable-store
    edition_panel || die "the panel is not Heliedan's edition: switch it first, sudo bash $0 install"
    ! store_busy || die "an installation is running in the App Store (or its service does not answer): try again later"
    # root installs the service from the panel's files: they must be the ones of the edition's git, as for install
    changed=$(git_panel status --porcelain --untracked-files=all -- store-helper || true)
    [[ -z $changed ]] || die "the files of store-helper/ in the panel were changed: sudo bash $0 install puts them back"
    say "Installing the App Store service (swizzin-store, root)"
    if ! run_installer store-helper; then
        drop_helper
        die "the App Store service could not be installed (see above): the App Store stays off"
    fi
    store_setting True || die "swizzin.cfg could not be changed (see above): the App Store service is installed, add" \
        "APP_STORE = True to swizzin.cfg by hand, then systemctl restart panel"
    say "Restarting the panel"
    restart_panel || die "the panel did not restart: journalctl -u panel says why. The App Store is on in swizzin.cfg" \
        "(APP_STORE = True); sudo bash $0 remove-store turns it off"
    say "Done: the App Store is in the sidebar, for the admin. To turn it off and remove its root service:" \
        "sudo bash $0 remove-store"
}

revert_edition() {
    local left
    checks revert
    [[ -d $BACKUP/original ]] || die "no backup in $BACKUP/original: the panel was not switched by this script"
    ! store_busy || die "an installation is running in the App Store (or its service does not answer): try again later"
    remove_helper
    remove_journal
    left="$(store_left) $(journal_left)"
    left=${left# }
    left=${left% }
    [[ -z $left ]] || die "the services of the edition could not be removed completely ($left): the panel was not" \
        "changed, run this again"
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
    else
        ! store_busy || die "an installation or an uninstallation is running in the App Store (or its service does" \
            "not answer): try again later. If you are sure that none runs: systemctl stop swizzin-store, then run this again"
        remove_helper
        left=$(store_left)
        [[ -z $left ]] || die "the App Store service could not be removed completely: $left"
        say "Done: the App Store service is removed. Its logs (/var/log/swizzin-store) and the copies of the settings" \
            "of the apps it uninstalled (/var/backups/swizzin-store) are kept: delete them by hand if you do not need them"
    fi
    if edition_panel; then
        if store_on; then
            store_setting False || die "swizzin.cfg could not be changed (see above): set APP_STORE = False in it by hand," \
                "then systemctl restart panel"
            say "The App Store is off in swizzin.cfg (APP_STORE = False): restarting the panel, whose page and sidebar" \
                "entry go"
            restart_panel || die "the panel did not restart: journalctl -u panel says why"
        fi
        say "The panel is still Heliedan's edition, with its Logs buttons. sudo bash $0 enable-store turns the App" \
            "Store on again, sudo bash $0 revert puts the official panel back"
    else
        # box remove panel or box upgrade panel: the journal reader has no panel left either
        remove_journal
        left=$(journal_left)
        [[ -z $left ]] || die "the journal reader could not be removed completely: $left"
        if [[ -d $BACKUP/original && ! -f $PANEL_LOCK ]]; then
            say "The backup of the official panel stays in $BACKUP: delete it by hand if you do not need it"
        fi
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
    if ! journal_installed; then
        say "Journal reader (Logs buttons): not installed"
    elif edition_panel; then
        say "Journal reader (Logs buttons): $(systemctl is-active swizzin-journal.socket 2>/dev/null || true)"
    else
        say "Journal reader (Logs buttons): LEFT BEHIND. The panel it belongs to is gone or is no longer Heliedan's" \
            "edition: it does not start again. Remove it: sudo bash $0 remove-store"
    fi
    if edition_panel; then
        if store_on; then
            say "App Store: on (APP_STORE = True in swizzin.cfg)"
        else
            say "App Store: off (sudo bash $0 enable-store turns it on)"
        fi
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
        enable-store) enable_store ;;
        remove-store) remove_store ;;
        *) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
    esac
}

# Sourced (tests/test_heliedan.py): the functions only.
[[ ${BASH_SOURCE[0]:-$0} != "$0" ]] || main "$@"
