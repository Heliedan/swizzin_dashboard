"""heliedan.sh: status, enable-store, remove-store and revert, on a fake Swizzin server with Heliedan's edition
installed, its journal reader and its App Store turned on.

The script's functions run against a fake root (its paths are moved there, systemctl, userdel and the installers of the
two services are fakes), with the real App Store helper when a test needs one that answers. Swizzin's own `box remove panel` and `box upgrade panel` are
replayed from a copy of Swizzin (SWIZZIN_SRC, as for tests/test_removable.py), their paths moved under the fake root:
those tests are skipped without it, except with REQUIRE_SWIZZIN_SRC=1 (the automatic tests), where they fail.

Run from the repository root: python tests/test_heliedan.py (nothing on the machine is touched, as root or not).
"""
import hashlib
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "store-helper"))
import swizzin_store  # noqa: E402
from test_store_helper import Helper, fake_swizzin  # noqa: E402
from test_store_install import Conn, fake_installers  # noqa: E402

HELIEDAN = os.path.join(ROOT, "heliedan.sh")
SRC = os.environ.get("SWIZZIN_SRC") or "/etc/swizzin"
HAVE_SRC = os.access(os.path.join(SRC, "scripts", "remove", "panel.sh"), os.R_OK)
SRC_REQUIRED = os.environ.get("REQUIRE_SWIZZIN_SRC") == "1"  # the automatic tests: never skipped in silence
# Swizzin's scripts that are replayed, as they are at the commit AUDITED of store-helper/removable.py. Another version
# is not run: the marker must be checked again against it first (does it still delete store-helper/?).
SWIZZIN_PANEL_SCRIPTS = {
    "remove/panel.sh": "17ae7855b3379f0aa307019cc835aca07fe0d551118ab3e00812f3f26dd38049",
    "upgrade/panel.sh": "fd25e01407a0c3426a22f3664c804e0c0d47750337cfd80d2ffe93156aa06586",
}

# Fake systemctl. Units are files under $FAKE_ROOT/etc/systemd/system; a unit runs while $FAKE_STATE/run/<unit>.active
# exists, and a stop does not stop it while <unit>.stuck exists too. Every call is noted in $FAKE_STATE/calls.log.
FAKE_SYSTEMCTL = r"""#!/bin/bash
echo "systemctl $*" >> "$FAKE_STATE/calls.log"
quiet=0 now=0 args=()
for a in "$@"; do
    case $a in -q|--quiet) quiet=1 ;; --now) now=1 ;; --no-pager) ;; *) args+=("$a") ;; esac
done
units=$FAKE_ROOT/etc/systemd/system run=$FAKE_STATE/run
unit=${args[1]:-}; unit=${unit%.service}
stop() { [[ -e $run/$unit.stuck ]] || rm -f "$run/$unit.active"; }
case ${args[0]:-} in
    is-active)
        if [[ -e $run/$unit.active ]]; then state=active; else state=inactive; fi
        ((quiet)) || echo "$state"
        [[ $state == active ]] ;;
    disable)
        file=$unit.service; [[ $unit == *.socket ]] && file=$unit
        [[ -e $units/$file ]] || { echo "Failed to disable unit: Unit file $file does not exist." >&2; exit 1; }
        rm -f "$units/multi-user.target.wants/$file" "$units/sockets.target.wants/$file"
        if ((now)); then stop; fi ;;
    stop) stop ;;
    start|restart) mkdir -p "$run"; touch "$run/$unit.active" ;;
    daemon-reload|reset-failed) ;;
    enable) mkdir -p "$run"; touch "$run/$unit.enabled" ;;
    *) echo "fake systemctl: $*" >&2; exit 1 ;;
esac
"""

# heliedan.sh sourced (functions only), every path of it moved under the fake root, then run like `heliedan.sh "$@"`
# (or one of its functions, TEST_CALL). Nothing of the test's arguments reaches the script while it is sourced.
PRELUDE = r"""
args=("$@")
set --
source "$HELIEDAN"
R=$FAKE_ROOT
PANEL=$R/opt/swizzin
PANEL_LOCK=$R/install/.panel.lock
ALTDASH_LOCK=$R/install/.altdash.lock
BACKUP=$R/var/backups/heliedan
PYTHON=$TEST_PYTHON
STORE_LIB=$R/usr/local/lib/swizzin-store
STORE_UNIT=$R/etc/systemd/system/swizzin-store.service
STORE_FILES=("$STORE_UNIT" "$R/etc/systemd/system/multi-user.target.wants/swizzin-store.service"
             "$R/etc/needrestart/conf.d/swizzin-store.conf")
STORE_SOCKET=$TEST_SOCKET
JOURNAL_LIB=$R/usr/local/lib/swizzin-journal
JOURNAL_FILES=("$R/etc/systemd/system/swizzin-journal.socket" "$R/etc/systemd/system/swizzin-journal@.service"
               "$R/etc/systemd/system/sockets.target.wants/swizzin-journal.socket")
JOURNAL_USER=heliedan-test-no-such-user  # never a real account of this machine
PANEL_USER=$TEST_USER
need_root() { :; }                     # run as root or not: the paths are the tests' own
git_panel() { git -C "$PANEL" "$@"; }  # no swizzin user here
py_panel() { "$PYTHON" "$@"; }
sleep() { :; }
# The installers of the two services: their pieces put in place under the fake root (as their install.sh would), and
# the service running; $FAKE_STATE/<folder>.fails makes one fail.
run_installer() {
    echo "run_installer $1" >> "$FAKE_STATE/calls.log"
    [[ ! -e $FAKE_STATE/$1.fails ]] || return 1
    if [[ $1 == store-helper ]]; then
        mkdir -p "$STORE_LIB" "$(dirname "${STORE_FILES[1]}")"; touch "$STORE_UNIT"; systemctl restart swizzin-store
    else
        mkdir -p "$JOURNAL_LIB" "$(dirname "${JOURNAL_FILES[2]}")"; touch "${JOURNAL_FILES[@]}"
        systemctl restart swizzin-journal.socket
    fi
}
"${TEST_CALL:-main}" "${args[@]}"
"""

# What Swizzin's scripts need around them (functions of its globals, sudo, packages), for a replay.
SWIZZIN_STUBS = r"""
echo_progress_start() { :; }; echo_progress_done() { :; }; echo_info() { :; }; echo_warn() { echo "warn: $*"; }
apt_install() { :; }
sudo() { [[ $1 == -u ]] && shift 2; "$@"; }  # sudo -u swizzin git ...: as this user
dpkg() { return 0; }                         # acl is installed: no setfacl on /home/*
chown() { :; }
"""

STORE_PIECES = ("etc/systemd/system/swizzin-store.service", "etc/systemd/system/multi-user.target.wants/swizzin-store.service",
                "etc/needrestart/conf.d/swizzin-store.conf", "etc/systemd/system/swizzin-store.service.d",
                "usr/local/lib/swizzin-store")
JOURNAL_PIECES = ("etc/systemd/system/swizzin-journal.socket", "etc/systemd/system/swizzin-journal@.service",
                  "etc/systemd/system/sockets.target.wants/swizzin-journal.socket", "usr/local/lib/swizzin-journal")
CFG = "# the admin's own settings\nADMIN_USER = 'tester'\nAPP_STORE = True\n"


def redirected(text):
    """Names of the variables of heliedan.sh that hold an absolute path (an array included)."""
    return set(re.findall(r"^([A-Z_]+)=\(?(?:\S* +)*?\"?/", text, re.M))


def remove_pieces(server, pieces=STORE_PIECES):
    for piece in pieces:
        path = server.path(piece)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.org", "-c", "commit.gpgsign=false",
                           *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class Server:
    """A Swizzin server where heliedan.sh installed the edition: the panel (a clone of the official one, switched to
    the edition), its backup, Swizzin's lock, the journal reader as journal-helper/install.sh puts it, and the App
    Store turned on (enable-store): its service as store-helper/install.sh puts it, APP_STORE = True in swizzin.cfg."""

    def __init__(self, base):
        self.base, self.root = base, os.path.join(base, "root")
        self.panel = self.path("/opt/swizzin")
        official = os.path.join(base, "official")  # what Swizzin clones (origin of the panel)
        write(os.path.join(official, "swizzin.py"), "print('panel')\n")
        write(os.path.join(official, "core", "custom", "profiles.py"), "# custom tiles\n")
        git(base, "-c", "init.defaultBranch=master", "init", "-q", official)  # not -b: git 2.25 (Ubuntu 20.04)
        git(official, "add", "-A")
        git(official, "commit", "-q", "-m", "official")
        git(base, "clone", "-q", official, self.panel)
        os.makedirs(self.path("/var/backups/heliedan"))
        shutil.copytree(self.panel, self.path("/var/backups/heliedan/original"), symlinks=True)
        write(self.path("/var/backups/heliedan/original.info"), "branch=master\ncommit=%s\ndate=2026-10-20\n"
              % git(self.panel, "rev-parse", "HEAD"))
        git(self.panel, "checkout", "-q", "-b", "heliedan")  # heliedan.sh: no upstream, as after checkout -B FETCH_HEAD
        write(os.path.join(self.panel, "core", "custom", "store", "__init__.py"), "# the App Store page\n")
        for folder in ("store-helper", "journal-helper"):
            shutil.copytree(os.path.join(ROOT, folder), os.path.join(self.panel, folder),
                            ignore=shutil.ignore_patterns("__pycache__"))
        git(self.panel, "add", "-A")
        git(self.panel, "commit", "-q", "-m", "edition")
        write(self.cfg, CFG)  # not tracked, as on a real server
        os.chmod(self.cfg, 0o640)
        write(self.path("/install/.panel.lock"))
        hour_ago = os.stat(self.path("/var/backups/heliedan/original.info")).st_mtime - 3600
        os.utime(self.path("/install/.panel.lock"), (hour_ago, hour_ago))  # the panel was there before the backup
        write(self.path("/opt/.venv/swizzin/bin/python"), "#!/bin/sh\nexit 0\n")  # its requirements are fine
        os.chmod(self.path("/opt/.venv/swizzin/bin/python"), 0o755)
        write(self.path("/etc/systemd/system/panel.service"), "[Service]\n")
        # the App Store service, with a drop-in an admin added (systemctl edit swizzin-store)
        shutil.copy(os.path.join(ROOT, "store-helper", "swizzin-store.service"), self.path("/etc/systemd/system/"))
        os.makedirs(self.path("/etc/systemd/system/multi-user.target.wants"))
        os.symlink("../swizzin-store.service", self.path("/etc/systemd/system/multi-user.target.wants/swizzin-store.service"))
        write(self.path("/etc/systemd/system/swizzin-store.service.d/override.conf"), "[Service]\n")
        os.makedirs(self.path("/etc/needrestart/conf.d"))
        shutil.copy(os.path.join(ROOT, "store-helper", "needrestart.conf"), self.path("/etc/needrestart/conf.d/swizzin-store.conf"))
        os.makedirs(self.path("/usr/local/lib/swizzin-store"))
        for name in ("swizzin_store.py", "removable.py", "remove_runner.py", "check_app.py"):
            shutil.copy(os.path.join(ROOT, "store-helper", name), self.path("/usr/local/lib/swizzin-store/"))
        os.makedirs(self.path("/var/log/swizzin-store"))  # kept by every removal
        # the journal reader
        for name in ("swizzin-journal.socket", "swizzin-journal@.service"):
            shutil.copy(os.path.join(ROOT, "journal-helper", name), self.path("/etc/systemd/system/"))
        os.makedirs(self.path("/etc/systemd/system/sockets.target.wants"))
        os.symlink("../swizzin-journal.socket", self.path("/etc/systemd/system/sockets.target.wants/swizzin-journal.socket"))
        os.makedirs(self.path("/usr/local/lib/swizzin-journal"))
        shutil.copy(os.path.join(ROOT, "journal-helper", "swizzin_journal.py"), self.path("/usr/local/lib/swizzin-journal/"))
        self.bin = os.path.join(base, "bin")
        write(os.path.join(self.bin, "systemctl"), FAKE_SYSTEMCTL)
        os.chmod(os.path.join(self.bin, "systemctl"), 0o755)
        os.makedirs(os.path.join(base, "run"))
        self.set_running("panel")
        self.set_running("swizzin-journal.socket")
        self.socket = os.path.join(base, "no-helper.sock")  # until a test starts a real helper

    def path(self, p):
        return os.path.join(self.root, p.lstrip("/"))

    @property
    def cfg(self):
        return os.path.join(self.panel, "swizzin.cfg")

    def read_cfg(self):
        with open(self.cfg) as f:
            return f.read()

    def set_running(self, unit, running=True, stuck=False):
        for name, on in ((unit + ".active", running), (unit + ".stuck", stuck)):
            flag = os.path.join(self.base, "run", name)
            if on:
                open(flag, "w").close()
            elif os.path.exists(flag):
                os.unlink(flag)

    def running(self, unit):
        return os.path.exists(os.path.join(self.base, "run", unit + ".active"))

    def env(self):
        return {"PATH": self.bin + ":/usr/bin:/bin", "HOME": self.base, "LANG": "C.UTF-8", "FAKE_ROOT": self.root,
                "FAKE_STATE": self.base, "HELIEDAN": HELIEDAN, "TEST_PYTHON": sys.executable, "TEST_SOCKET": self.socket,
                "TEST_USER": pwd.getpwuid(os.getuid()).pw_name}

    def heliedan(self, *args, call="main", **env):
        """heliedan.sh "$@" on this server (or one of its functions): (exit code, what it printed)."""
        r = subprocess.run(["bash", "-c", PRELUDE, "heliedan.sh", *args], env=dict(self.env(), TEST_CALL=call, **env),
                           capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout + r.stderr

    def calls(self):
        try:
            with open(os.path.join(self.base, "calls.log")) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def store_left(self):
        return [p for p in STORE_PIECES if os.path.lexists(self.path(p))]

    def journal_left(self):
        return [p for p in JOURNAL_PIECES if os.path.lexists(self.path(p))]

    def swizzin(self, script):
        """Swizzin's own script (scripts/remove/panel.sh or scripts/upgrade/panel.sh), run on this server."""
        with open(os.path.join(SRC, "scripts", script), "rb") as f:
            raw = f.read()
        assert hashlib.sha256(raw).hexdigest() == SWIZZIN_PANEL_SCRIPTS[script], \
            "%s is not the audited one: check the marker against it, then update SWIZZIN_PANEL_SCRIPTS" % script
        text = raw.decode()
        text = re.sub(r"(?<![\w.$/-])/(opt|srv|etc|install|home)(?=/)", self.root + r"/\1", text)
        outside = set(re.findall(r"(?<![\w.$/-])/[\w./*-]+", text.replace(self.root, "ROOT")))
        assert outside <= {"/dev/null", "/bin/bash"}, "a path of %s is not moved: %s" % (script, outside)
        cwd = os.path.join(self.base, "cwd")  # its relative paths (rm -rf opt/swizzin/...) land in an empty folder
        os.makedirs(cwd, exist_ok=True)
        env = dict(self.env(), log=os.path.join(self.base, "swizzin.log"))
        r = subprocess.run(["bash", "-c", SWIZZIN_STUBS + text], cwd=cwd, env=env, capture_output=True, text=True,
                           timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr
        return r


class HeliedanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Every path of heliedan.sh must be moved under the fake root, or the tests would act on this machine.
        with open(HELIEDAN) as f:
            paths = redirected(f.read())
        moved = set(re.findall(r"^([A-Z_]+)=", PRELUDE, re.M))
        assert paths and paths <= moved, "heliedan.sh has paths that the tests do not move: %s" % (paths - moved)

    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)  # even when Server() fails halfway
        self.server = Server(self.base)
        self.helpers = []

    def require_src(self):
        if not HAVE_SRC:
            raise RuntimeError("no copy of Swizzin in SWIZZIN_SRC, and REQUIRE_SWIZZIN_SRC=1: these tests must run "
                               "(see CONTRIBUTING.md)")

    def tearDown(self):
        for helper in self.helpers:
            helper.stop()
        subprocess.run(["pkill", "-f", self.base], stderr=subprocess.DEVNULL)

    def helper(self, busy=False):
        """A real App Store helper on its own socket, running for the service: idle, or busy with an installation."""
        base = os.path.join(self.base, "helper%d" % len(self.helpers))
        os.makedirs(base)
        if busy:
            helper = Helper(base, extra=fake_installers(base))
        else:
            fake_swizzin(base)
            helper = Helper(base)
        self.helpers.append(helper)
        self.server.socket = helper.path
        self.server.set_running("swizzin-store")
        if busy:
            c = Conn(helper.path)
            c.send(op="install", app="jackett")  # a long installation
            c.wait_output("working")
            c.close()
        return helper


class Status(HeliedanTest):
    def test_the_edition_with_its_service(self):
        self.server.set_running("swizzin-store")
        code, out = self.server.heliedan("status")
        self.assertEqual(code, 0, out)
        self.assertIn("Panel: Heliedan's edition", out)
        self.assertIn("App Store service: active", out)
        self.assertIn("Journal reader (Logs buttons): active", out)
        self.assertIn("App Store: on (APP_STORE = True in swizzin.cfg)", out)
        self.assertNotIn("LEFT BEHIND", out)
        self.assertIn("Backup of the panel from before the switch", out)

    def test_the_default_without_the_app_store(self):
        """What install leaves on a new server: the journal reader, no App Store."""
        remove_pieces(self.server)
        write(self.server.cfg, "# the admin's own settings\n")
        code, out = self.server.heliedan("status")
        self.assertEqual(code, 0, out)
        self.assertIn("App Store service: not installed", out)
        self.assertIn("Journal reader (Logs buttons): active", out)
        self.assertIn("App Store: off (sudo bash heliedan.sh enable-store turns it on)", out)

    def test_no_service(self):
        remove_pieces(self.server)
        code, out = self.server.heliedan("status")
        self.assertEqual(code, 0, out)
        self.assertIn("App Store service: not installed", out)

    @unittest.skipUnless(HAVE_SRC or SRC_REQUIRED, "no copy of Swizzin (set SWIZZIN_SRC)")
    def test_after_box_remove_panel(self):
        self.require_src()
        self.server.swizzin("remove/panel.sh")
        self.assertFalse(os.path.exists(self.server.panel))
        code, out = self.server.heliedan("status")
        self.assertEqual(code, 0, out)  # it used to stop at "no Swizzin panel"
        self.assertIn("Panel: not installed", out)
        self.assertIn("App Store service: LEFT BEHIND (inactive)", out)
        self.assertIn("Journal reader (Logs buttons): LEFT BEHIND", out)
        self.assertNotIn("App Store: o", out)  # no panel: no setting to show
        self.assertIn("heliedan.sh remove-store", out)

    @unittest.skipUnless(HAVE_SRC or SRC_REQUIRED, "no copy of Swizzin (set SWIZZIN_SRC)")
    def test_after_box_upgrade_panel(self):
        self.require_src()
        r = self.server.swizzin("upgrade/panel.sh")
        self.assertIn("Working around unclean git repo", r.stdout)  # git pull fails: no upstream for heliedan
        self.assertFalse(os.path.exists(os.path.join(self.server.panel, swizzin_store.PANEL_MARKER[len("/opt/swizzin/"):])))
        # Why the marker is not core/custom/store: Swizzin copies core/custom aside and back, so the App Store's page
        # comes back as a stray file in the official panel.
        self.assertTrue(os.path.exists(os.path.join(self.server.panel, "core", "custom", "store", "__init__.py")))
        code, out = self.server.heliedan("status")
        self.assertEqual(code, 0, out)
        self.assertIn("Panel: the official code", out)
        self.assertIn("App Store service: LEFT BEHIND", out)


    @unittest.skipUnless(HAVE_SRC or SRC_REQUIRED, "no copy of Swizzin (set SWIZZIN_SRC)")
    def test_an_older_service_left_behind_says_it_keeps_running(self):
        """A service installed before it knew its panel (no Condition in its unit) does not stop by itself."""
        self.require_src()
        unit = self.server.path("/etc/systemd/system/swizzin-store.service")
        with open(unit) as f:
            text = f.read()
        write(unit, re.sub(r"^Condition.*\n", "", text, flags=re.M))
        self.server.swizzin("remove/panel.sh")
        self.server.set_running("swizzin-store")
        code, out = self.server.heliedan("status")
        self.assertEqual(code, 0, out)
        self.assertIn("LEFT BEHIND (active)", out)
        self.assertIn("this older version keeps running and starts at boot. Remove it now", out)

    @unittest.skipUnless(HAVE_SRC or SRC_REQUIRED, "no copy of Swizzin (set SWIZZIN_SRC)")
    def test_left_behind_while_a_job_runs(self):
        self.require_src()
        self.helper(busy=True)
        self.server.swizzin("remove/panel.sh")
        code, out = self.server.heliedan("status")
        self.assertEqual(code, 0, out)
        self.assertIn("LEFT BEHIND (active, running: install jackett)", out)
        self.assertIn("systemctl stop swizzin-store ends it", out)
        self.server.socket = os.path.join(self.base, "nothing.sock")  # running, but it does not answer
        code, out = self.server.heliedan("status")
        self.assertIn("LEFT BEHIND (active, does not answer)", out)

    def test_a_folder_is_not_the_marker(self):
        """The marker is a file, as for the service: a folder of that name (made by the panel user) is not."""
        marker = os.path.join(self.server.panel, "store-helper", "swizzin_store.py")
        os.unlink(marker)
        write(os.path.join(marker, "x"), "x\n")
        code, out = self.server.heliedan("status")
        self.assertIn("App Store service: LEFT BEHIND", out)


class RemoveStore(HeliedanTest):
    def assert_removed(self, out):
        self.assertEqual(self.server.store_left(), [], out)
        self.assertFalse(self.server.running("swizzin-store"))
        self.assertTrue(os.path.isdir(self.server.path("/var/log/swizzin-store")))  # its logs stay
        self.assertIn("daemon-reload", " ".join(self.server.calls()))

    @unittest.skipUnless(HAVE_SRC or SRC_REQUIRED, "no copy of Swizzin (set SWIZZIN_SRC)")
    def test_after_box_remove_panel_then_again(self):
        self.require_src()
        self.server.swizzin("remove/panel.sh")
        code, out = self.server.heliedan("remove-store")
        self.assertEqual(code, 0, out)
        self.assert_removed(out)
        self.assertIn("Done: the App Store service is removed", out)
        self.assertEqual(self.server.journal_left(), [], out)  # what the edition left behind goes too
        self.assertFalse(self.server.running("swizzin-journal.socket"))
        self.assertIn("The backup of the official panel stays in", out)
        calls = len(self.server.calls())
        code, out = self.server.heliedan("remove-store")  # idempotent
        self.assertEqual(code, 0, out)
        self.assertIn("not installed: nothing to remove", out)
        self.assertEqual(set(self.server.calls()[calls:]), {"systemctl is-active --quiet swizzin-store",
                                                            "systemctl is-active --quiet swizzin-journal.socket"})
        code, out = self.server.heliedan("status")
        self.assertIn("App Store service: not installed", out)
        self.assertIn("Journal reader (Logs buttons): not installed", out)

    def test_before_box_remove_panel(self):
        """The order the README gives: remove-store first, while the panel is still there and the service runs."""
        self.helper()
        code, out = self.server.heliedan("remove-store")
        self.assertEqual(code, 0, out)
        self.assert_removed(out)
        self.assertIn("The panel is still Heliedan's edition, with its Logs buttons", out)
        self.assertTrue(os.path.exists(self.server.panel))  # the panel's code is not touched
        # the App Store turned off for the panel, which restarts without it; the journal reader stays
        self.assertEqual(self.server.read_cfg(), "# the admin's own settings\nADMIN_USER = 'tester'\nAPP_STORE = False"
                         "  # the App Store: heliedan.sh enable-store and remove-store change it\n")
        self.assertIn("systemctl restart panel", self.server.calls())
        self.assertEqual(len(self.server.journal_left()), len(JOURNAL_PIECES))
        self.assertTrue(self.server.running("swizzin-journal.socket"))
        code, out = self.server.heliedan("status")
        self.assertIn("App Store: off", out)
        calls = len(self.server.calls())
        code, out = self.server.heliedan("remove-store")  # again: nothing more to do, no restart
        self.assertEqual(code, 0, out)
        self.assertIn("not installed: nothing to remove", out)
        self.assertNotIn("systemctl restart panel", self.server.calls()[calls:])

    def test_unit_file_already_deleted(self):
        """Someone deleted the unit by hand: disable cannot work, the rest goes anyway (dangling link included)."""
        self.helper()
        os.unlink(self.server.path("/etc/systemd/system/swizzin-store.service"))
        code, out = self.server.heliedan("remove-store")
        self.assertEqual(code, 0, out)
        self.assert_removed(out)
        self.assertIn("systemctl stop swizzin-store", self.server.calls())

    def test_only_the_service_still_running(self):
        remove_pieces(self.server)
        self.helper()
        code, out = self.server.heliedan("remove-store")
        self.assertEqual(code, 0, out)
        self.assert_removed(out)

    def test_refused_while_an_installation_runs(self):
        self.helper(busy=True)
        code, out = self.server.heliedan("remove-store")
        self.assertEqual(code, 1, out)
        self.assertIn("an installation or an uninstallation is running", out)
        self.assertEqual(len(self.server.store_left()), len(STORE_PIECES))  # nothing removed
        self.assertTrue(self.server.running("swizzin-store"))
        self.assertFalse([c for c in self.server.calls() if c.split()[1] in ("disable", "stop")])

    def test_refused_when_the_service_does_not_answer(self):
        self.server.set_running("swizzin-store")  # running, but no socket answers: any doubt counts as busy
        code, out = self.server.heliedan("remove-store")
        self.assertEqual(code, 1, out)
        self.assertIn("systemctl stop swizzin-store, then run this again", out)
        self.assertEqual(len(self.server.store_left()), len(STORE_PIECES))

    def test_says_what_is_left(self):
        self.helper()
        self.server.set_running("swizzin-store", stuck=True)  # a stop that does not stop it
        code, out = self.server.heliedan("remove-store")
        self.assertEqual(code, 1, out)
        self.assertIn("could not be removed completely: the swizzin-store service, still running", out)


class Revert(HeliedanTest):
    def assert_reverted(self, code, out):
        self.assertEqual(code, 0, out)
        self.assertEqual(self.server.store_left(), [], out)
        self.assertEqual(self.server.journal_left(), [], out)
        self.assertFalse(self.server.running("swizzin-journal.socket"))
        self.assertFalse(self.server.running("swizzin-store"))
        self.assertEqual(git(self.server.panel, "rev-parse", "--abbrev-ref", "HEAD"), "master")  # the backup
        self.assertFalse(os.path.exists(os.path.join(self.server.panel, "store-helper")))
        self.assertFalse(os.path.exists(self.server.path("/var/backups/heliedan/original")))  # set aside
        self.assertTrue(self.server.running("panel"))
        self.assertIn("Done: the official panel is back", out)

    def test_puts_the_panel_back_and_leaves_no_service(self):
        self.helper()
        self.assert_reverted(*self.server.heliedan("revert"))
        code, out = self.server.heliedan("status")
        self.assertIn("App Store service: not installed", out)
        self.assertIn("Journal reader (Logs buttons): not installed", out)

    def test_without_the_app_store(self):
        """The default of a new install: only the journal reader to remove."""
        remove_pieces(self.server)
        self.assert_reverted(*self.server.heliedan("revert"))

    @unittest.skipUnless(HAVE_SRC or SRC_REQUIRED, "no copy of Swizzin (set SWIZZIN_SRC)")
    def test_after_box_upgrade_panel(self):
        self.require_src()
        self.server.swizzin("upgrade/panel.sh")
        self.assertFalse(os.path.exists(os.path.join(self.server.panel, "journal-helper")))  # its marker goes too
        self.assert_reverted(*self.server.heliedan("revert"))

    def test_the_panel_is_not_touched_if_the_service_stays(self):
        self.helper()
        self.server.set_running("swizzin-store", stuck=True)
        code, out = self.server.heliedan("revert")
        self.assertEqual(code, 1, out)
        self.assertIn("could not be removed completely", out)
        self.assertEqual(git(self.server.panel, "rev-parse", "--abbrev-ref", "HEAD"), "heliedan")
        self.assertTrue(os.path.isdir(self.server.path("/var/backups/heliedan/original")))
        self.assertNotIn("systemctl restart panel", self.server.calls())

    @unittest.skipUnless(HAVE_SRC or SRC_REQUIRED, "no copy of Swizzin (set SWIZZIN_SRC)")
    def test_after_box_remove_panel_it_points_to_remove_store(self):
        self.require_src()
        self.server.swizzin("remove/panel.sh")
        code, out = self.server.heliedan("revert")
        self.assertEqual(code, 1, out)
        self.assertIn("no Swizzin panel in", out)
        self.assertIn("heliedan.sh remove-store", out)
        self.assertEqual(len(self.server.store_left()), len(STORE_PIECES))  # revert changes nothing there


class Install(HeliedanTest):
    """Pieces of heliedan.sh install that run without a network or a real panel."""

    def test_a_failure_never_cuts_off_an_installation_started_meanwhile(self):
        self.helper(busy=True)
        code, out = self.server.heliedan(call="drop_helper")  # install failed after its first look (git fetch...)
        self.assertEqual(code, 0, out)
        self.assertIn("running a job (or does not answer), so it was left in place", out)
        self.assertEqual(len(self.server.store_left()), len(STORE_PIECES))
        self.assertFalse([c for c in self.server.calls() if c.split()[1] in ("disable", "stop")])

    def test_a_failure_removes_an_idle_service(self):
        self.helper()
        code, out = self.server.heliedan(call="drop_helper")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.server.store_left(), [], out)

    def test_the_first_backup_is_kept(self):
        info = self.server.path("/var/backups/heliedan/original.info")
        with open(info) as f:
            before = f.read()
        code, out = self.server.heliedan(call="backup_panel")
        self.assertEqual(code, 0, out)
        self.assertIn("Keeping the first backup", out)
        with open(info) as f:
            self.assertEqual(f.read(), before)

    def test_a_panel_installed_again_gets_a_new_backup(self):
        """box remove panel, box install panel, then install: the old backup is not the panel to put back."""
        lock = self.server.path("/install/.panel.lock")
        later = os.stat(self.server.path("/var/backups/heliedan/original.info")).st_mtime + 60
        os.utime(lock, (later, later))  # box install panel touches the lock at its end
        code, out = self.server.heliedan(call="backup_panel")
        self.assertEqual(code, 0, out)
        self.assertIn("that backup is set aside", out)
        backups = os.listdir(self.server.path("/var/backups/heliedan"))
        self.assertEqual(sorted(b.split("-")[0] for b in backups), ["original", "original.info", "replaced", "replaced"])
        with open(self.server.path("/var/backups/heliedan/original.info")) as f:
            self.assertIn("branch=heliedan", f.read())  # this panel, as it is now


class EnableStore(HeliedanTest):
    """The App Store is off by default: enable-store installs its root service and turns it on in swizzin.cfg."""

    def setUp(self):
        super().setUp()
        remove_pieces(self.server)  # a new install: no App Store
        write(self.server.cfg, "# the admin's own settings\n")

    def test_turns_it_on(self):
        code, out = self.server.heliedan("enable-store")
        self.assertEqual(code, 0, out)
        self.assertIn("run_installer store-helper", self.server.calls())
        self.assertTrue(self.server.running("swizzin-store"))
        self.assertEqual(self.server.read_cfg(), "# the admin's own settings\nAPP_STORE = True  # the App Store: "
                         "heliedan.sh enable-store and remove-store change it\n")
        self.assertEqual(self.server.calls()[-1], "systemctl is-active --quiet panel")  # restarted, and running
        self.assertIn("systemctl restart panel", self.server.calls())
        self.assertIn("Done: the App Store is in the sidebar", out)
        self.helper()  # the service now runs, idle (enable-store never cuts off a job)
        code, out = self.server.heliedan("enable-store")  # again: the service updated, one line in swizzin.cfg
        self.assertEqual(code, 0, out)
        self.assertEqual(self.server.read_cfg().count("APP_STORE"), 1)
        code, out = self.server.heliedan("status")
        self.assertIn("App Store: on", out)

    def test_a_failure_leaves_it_off(self):
        open(os.path.join(self.base, "store-helper.fails"), "w").close()
        code, out = self.server.heliedan("enable-store")
        self.assertEqual(code, 1, out)
        self.assertIn("the App Store stays off", out)
        self.assertEqual(self.server.read_cfg(), "# the admin's own settings\n")
        self.assertNotIn("systemctl restart panel", self.server.calls())

    def test_only_with_the_edition_and_its_own_files(self):
        changed = os.path.join(self.server.panel, "store-helper", "swizzin_store.py")
        with open(changed, "a") as f:
            f.write("# changed by the panel's user\n")
        code, out = self.server.heliedan("enable-store")
        self.assertEqual(code, 1, out)
        self.assertIn("the files of store-helper/ in the panel were changed", out)
        git(self.server.panel, "checkout", "-q", "--", "store-helper")
        write(os.path.join(self.server.panel, "store-helper", "extra.py"), "# added by the panel's user\n")
        code, out = self.server.heliedan("enable-store")
        self.assertEqual(code, 1, out)
        os.unlink(os.path.join(self.server.panel, "store-helper", "extra.py"))
        os.unlink(changed)  # box upgrade panel: no longer the edition
        code, out = self.server.heliedan("enable-store")
        self.assertEqual(code, 1, out)
        self.assertIn("the panel is not Heliedan's edition", out)
        self.assertNotIn("run_installer store-helper", self.server.calls())
        self.assertEqual(self.server.read_cfg(), "# the admin's own settings\n")

    @unittest.skipUnless(HAVE_SRC or SRC_REQUIRED, "no copy of Swizzin (set SWIZZIN_SRC)")
    def test_not_after_box_remove_panel(self):
        self.require_src()
        self.server.swizzin("remove/panel.sh")
        code, out = self.server.heliedan("enable-store")
        self.assertEqual(code, 1, out)
        self.assertIn("no Swizzin panel in", out)
        self.assertIn("heliedan.sh remove-store", out)  # the journal reader is left behind


class StoreSetting(HeliedanTest):
    """store_setting writes APP_STORE in swizzin.cfg, as root in a folder that the panel's user owns."""

    def set(self, value="True"):
        return self.server.heliedan(value, call="store_setting")

    def test_one_line_the_rest_kept(self):
        write(self.server.cfg, "ADMIN_USER = 'tester'\nAPP_STORE=False\n# APP_STORE = True, a comment\n"
                               "APP_STORE   =   True # twice\nHOST = '127.0.0.1'")  # no new line at the end
        os.chmod(self.server.cfg, 0o640)
        self.assertEqual(self.set(), (0, ""))
        self.assertEqual(self.server.read_cfg(), "ADMIN_USER = 'tester'\n# APP_STORE = True, a comment\nHOST = "
                         "'127.0.0.1'\nAPP_STORE = True  # the App Store: heliedan.sh enable-store and remove-store "
                         "change it\n")
        st = os.stat(self.server.cfg)
        self.assertEqual((st.st_mode & 0o777, st.st_uid), (0o640, os.getuid()))  # its mode and owner
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.server.panel)) if "cfg" in n], [])  # none left
        code, _ = self.server.heliedan(call="store_on")
        self.assertEqual(code, 0)
        self.assertEqual(self.set("False"), (0, ""))
        code, _ = self.server.heliedan(call="store_on")
        self.assertEqual(code, 1)

    def test_a_missing_file_is_made(self):
        os.unlink(self.server.cfg)
        self.assertEqual(self.set(), (0, ""))
        self.assertEqual(self.server.read_cfg(), "APP_STORE = True  # the App Store: heliedan.sh enable-store and "
                         "remove-store change it\n")
        self.assertEqual(os.stat(self.server.cfg).st_mode & 0o777, 0o644)

    def test_never_through_a_link(self):
        """The panel's user may replace swizzin.cfg with a link (to a file only root can read, say): root neither
        reads nor writes through it."""
        secret = os.path.join(self.base, "secret")
        write(secret, "APP_STORE = True\nroot's secret\n")
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind):
                os.unlink(self.server.cfg)
                (os.symlink if kind == "symlink" else os.link)(secret, self.server.cfg)
                code, out = self.set("False")
                self.assertEqual(code, 1, out)
                self.assertIn("APP_STORE was not changed", out)
                with open(secret) as f:
                    self.assertEqual(f.read(), "APP_STORE = True\nroot's secret\n")
                self.assertNotIn("secret", os.listdir(os.path.dirname(self.server.panel)))


# Fake panel Python for update_packages: tests/test_requirements.py passes once $FAKE_STATE/packages.ok exists, which
# pip -r makes, unless $FAKE_STATE/pip.fails exists. Every call is noted in $FAKE_STATE/calls.log.
FAKE_PYTHON = r"""#!/bin/bash
echo "python $*" >> "$FAKE_STATE/calls.log"
case $1 in
    -m) [[ ! -e $FAKE_STATE/pip.fails ]] || exit 1
        [[ " $* " != *" -r "* ]] || touch "$FAKE_STATE/packages.ok" ;;
    *) [[ -e $FAKE_STATE/packages.ok ]] ;;
esac
"""


class Packages(HeliedanTest):
    """install brings the panel's Python packages up to its requirements.txt (security floors), as box upgrade panel
    does for the official panel: pip runs only when a package is missing or too old."""

    PIP = "python -m pip install --quiet --no-cache-dir --disable-pip-version-check "

    def setUp(self):
        super().setUp()
        self.python = os.path.join(self.base, "fake-python")
        write(self.python, FAKE_PYTHON)
        os.chmod(self.python, 0o755)
        self.check = "python %s/tests/test_requirements.py" % self.server.path("/opt/swizzin")

    def update(self):
        code, out = self.server.heliedan(call="update_packages", TEST_PYTHON=self.python)
        return code, out, [c for c in self.server.calls() if c.startswith("python ")]

    def test_nothing_runs_when_they_are_recent_enough(self):
        open(os.path.join(self.base, "packages.ok"), "w").close()
        self.assertEqual(self.update(), (0, "", [self.check]))

    def test_pip_brings_them_up_to_requirements_txt(self):
        code, out, calls = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn("Updating the panel's Python packages", out)
        self.assertNotIn("Warning", out)
        self.assertEqual(calls, [self.check, self.PIP + "--upgrade pip wheel",
                                 self.PIP + "-r %s/requirements.txt" % self.server.path("/opt/swizzin"), self.check])

    def test_a_pip_failure_is_a_warning_and_install_goes_on(self):
        """The packages stay as they were and the panel runs with them: install then checks that the code loads."""
        open(os.path.join(self.base, "pip.fails"), "w").close()
        code, out, calls = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn("Warning: the panel's Python packages could not all be updated", out)
        self.assertEqual(calls, [self.check, self.PIP + "--upgrade pip wheel"])


def tree(root):
    """Every file and folder under root, with its content: to see that nothing changed."""
    found = {}
    for folder, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(folder, name)
            if os.path.islink(path) or os.path.isdir(path):
                found[path] = os.readlink(path) if os.path.islink(path) else "dir"
            else:
                with open(path, "rb") as f:
                    found[path] = f.read()
    return found


class InstalledWithBox(HeliedanTest):
    """Installed with box install altdash, the edition belongs to Swizzin's box: this script leaves it alone."""

    def setUp(self):
        super().setUp()
        os.rename(self.server.path("/install/.panel.lock"), self.server.path("/install/.altdash.lock"))

    def test_install_revert_and_remove_store_are_refused(self):
        before = tree(self.server.root)
        for command in ("install", "revert", "enable-store", "remove-store"):
            code, out = self.server.heliedan(command)
            self.assertEqual(code, 1, out)
            self.assertIn("installed with box install altdash", out)
            self.assertIn("box remove altdash", out)
        self.assertEqual(tree(self.server.root), before)  # nothing touched

    def test_status_points_to_box(self):
        code, out = self.server.heliedan("status")
        self.assertEqual(code, 0, out)
        self.assertIn("Installed with box install altdash", out)
        self.assertIn("box upgrade altdash", out)


class Marker(unittest.TestCase):
    """The helper, its unit, install.sh and heliedan.sh agree on the file that says the panel is this edition."""

    def test_one_marker_everywhere(self):
        marker = swizzin_store.PANEL_MARKER
        self.assertTrue(marker.startswith("/opt/swizzin/"))
        relative = marker[len("/opt/swizzin/"):]
        self.assertTrue(os.path.isfile(os.path.join(ROOT, relative)))  # a file of this edition...
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", relative], cwd=ROOT, capture_output=True)
        self.assertEqual(tracked.returncode, 0, "not tracked by git: box upgrade panel would not delete it")
        self.assertFalse(relative.startswith("core/custom/"))  # ...that Swizzin's upgrade does not copy back
        with open(HELIEDAN) as f:
            text = f.read()
        self.assertIn("\nPANEL=/opt/swizzin\n", text)
        self.assertIn("\nMARKER=%s\n" % relative, text)
        with open(os.path.join(ROOT, "store-helper", "install.sh")) as f:
            self.assertIn("\nMARKER=%s\n" % marker, f.read())
        with open(os.path.join(ROOT, "store-helper", "swizzin-store.service")) as f:
            unit = f.read()
        self.assertIn("\nConditionPathIsSymbolicLink=!%s\n" % marker, unit)
        self.assertIn("\nConditionFileNotEmpty=%s\n" % marker, unit)
        for tie in ("BindsTo=", "PartOf=", "Requires=", "Requisite="):  # a restart of the panel must not reach it
            self.assertNotRegex(unit, re.compile("^" + tie, re.M))
        self.assertRegex(unit, re.compile(r"^Restart=on-failure$", re.M))  # its exit 0 when the panel is gone: final

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is not installed")
    def test_systemd_reads_every_line_of_the_unit(self):
        """A misspelt Condition would be ignored with a mere warning, and the service would start anyway."""
        unit = os.path.join(ROOT, "store-helper", "swizzin-store.service")
        r = subprocess.run(["systemd-analyze", "verify", unit], capture_output=True, text=True, timeout=60)
        about_it = [line for line in (r.stdout + r.stderr).splitlines() if "swizzin-store.service" in line]
        self.assertEqual((r.returncode, about_it), (0, []))

    def test_usage_lists_remove_store_and_sourcing_runs_nothing(self):
        r = subprocess.run(["bash", HELIEDAN], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("sudo bash heliedan.sh remove-store", r.stdout)
        self.assertIn("sudo bash heliedan.sh enable-store", r.stdout)
        self.assertIn("If anything goes wrong, the panel is put back as it was.", r.stdout)  # the whole header
        self.assertNotIn("set -euo", r.stdout)
        r = subprocess.run(["bash", "-c", 'source "$1" && echo sourced', "test", HELIEDAN], capture_output=True,
                           text=True, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual((r.returncode, r.stdout), (0, "sourced\n"), r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
