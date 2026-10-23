"""heliedan.sh: status, remove-store and revert, on a fake Swizzin server with Heliedan's edition installed.

The script's functions run against a fake root (its paths are moved there, systemctl is a fake), with the real App
Store helper when a test needs one that answers. Swizzin's own `box remove panel` and `box upgrade panel` are
replayed from a copy of Swizzin (SWIZZIN_SRC, as for tests/test_removable.py), their paths moved under the fake root:
those tests are skipped without it, except with REQUIRE_SWIZZIN_SRC=1 (the automatic tests), where they fail.

Run from the repository root: python tests/test_heliedan.py (nothing on the machine is touched, as root or not).
"""
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
        [[ -e $units/$unit.service ]] || { echo "Failed to disable unit: Unit file $unit.service does not exist." >&2; exit 1; }
        rm -f "$units/multi-user.target.wants/$unit.service"
        if ((now)); then stop; fi ;;
    stop) stop ;;
    start|restart) mkdir -p "$run"; touch "$run/$unit.active" ;;
    daemon-reload|reset-failed) ;;
    *) echo "fake systemctl: $*" >&2; exit 1 ;;
esac
"""

# heliedan.sh sourced (functions only), every path of it moved under the fake root, then run like `heliedan.sh "$@"`.
PRELUDE = r"""
source "$HELIEDAN"
R=$FAKE_ROOT
PANEL=$R/opt/swizzin
PANEL_LOCK=$R/install/.panel.lock
BACKUP=$R/var/backups/heliedan
PYTHON=$TEST_PYTHON
STORE_LIB=$R/usr/local/lib/swizzin-store
STORE_UNIT=$R/etc/systemd/system/swizzin-store.service
STORE_FILES=("$STORE_UNIT" "$R/etc/systemd/system/multi-user.target.wants/swizzin-store.service"
             "$R/etc/needrestart/conf.d/swizzin-store.conf")
STORE_SOCKET=$TEST_SOCKET
PANEL_USER=$TEST_USER
need_root() { :; }                     # run as root or not: the paths are the tests' own
git_panel() { git -C "$PANEL" "$@"; }  # no swizzin user here
sleep() { :; }
main "$@"
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
    the edition), its backup, Swizzin's lock, and the App Store service as store-helper/install.sh puts it."""

    def __init__(self, base):
        self.base, self.root = base, os.path.join(base, "root")
        self.panel = self.path("/opt/swizzin")
        official = os.path.join(base, "official")  # what Swizzin clones (origin of the panel)
        write(os.path.join(official, "swizzin.py"), "print('panel')\n")
        write(os.path.join(official, "core", "custom", "profiles.py"), "# custom tiles\n")
        git(base, "init", "-q", "-b", "master", official)
        git(official, "add", "-A")
        git(official, "commit", "-q", "-m", "official")
        git(base, "clone", "-q", official, self.panel)
        os.makedirs(self.path("/var/backups/heliedan"))
        shutil.copytree(self.panel, self.path("/var/backups/heliedan/original"), symlinks=True)
        write(self.path("/var/backups/heliedan/original.info"), "branch=master\ncommit=%s\ndate=2026-10-20\n"
              % git(self.panel, "rev-parse", "HEAD"))
        git(self.panel, "checkout", "-q", "-b", "heliedan")  # heliedan.sh: no upstream, as after checkout -B FETCH_HEAD
        write(os.path.join(self.panel, "core", "custom", "store", "__init__.py"), "# the App Store page\n")
        shutil.copytree(os.path.join(ROOT, "store-helper"), os.path.join(self.panel, "store-helper"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        git(self.panel, "add", "-A")
        git(self.panel, "commit", "-q", "-m", "edition")
        write(self.path("/install/.panel.lock"))
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
        self.bin = os.path.join(base, "bin")
        write(os.path.join(self.bin, "systemctl"), FAKE_SYSTEMCTL)
        os.chmod(os.path.join(self.bin, "systemctl"), 0o755)
        os.makedirs(os.path.join(base, "run"))
        self.set_running("panel")
        self.socket = os.path.join(base, "no-helper.sock")  # until a test starts a real helper

    def path(self, p):
        return os.path.join(self.root, p.lstrip("/"))

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

    def heliedan(self, *args):
        """heliedan.sh "$@" on this server: (exit code, what it printed)."""
        r = subprocess.run(["bash", "-c", PRELUDE, "heliedan.sh", *args], env=self.env(), capture_output=True,
                           text=True, timeout=120)
        return r.returncode, r.stdout + r.stderr

    def calls(self):
        try:
            with open(os.path.join(self.base, "calls.log")) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def store_left(self):
        return [p for p in STORE_PIECES if os.path.lexists(self.path(p))]

    def swizzin(self, script):
        """Swizzin's own script (scripts/remove/panel.sh or scripts/upgrade/panel.sh), run on this server."""
        with open(os.path.join(SRC, "scripts", script)) as f:
            text = f.read()
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
        self.assertNotIn("LEFT BEHIND", out)
        self.assertIn("Backup of the panel from before the switch", out)

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
        self.assertIn("The backup of the official panel stays in", out)
        calls = len(self.server.calls())
        code, out = self.server.heliedan("remove-store")  # idempotent
        self.assertEqual(code, 0, out)
        self.assertIn("not installed: nothing to remove", out)
        self.assertEqual(self.server.calls()[calls:], ["systemctl is-active --quiet swizzin-store"])  # only a look
        code, out = self.server.heliedan("status")
        self.assertIn("App Store service: not installed", out)

    def test_before_box_remove_panel(self):
        """The order the README gives: remove-store first, while the panel is still there and the service runs."""
        self.helper()
        code, out = self.server.heliedan("remove-store")
        self.assertEqual(code, 0, out)
        self.assert_removed(out)
        self.assertIn("The panel is still Heliedan's edition", out)
        self.assertTrue(os.path.exists(self.server.panel))  # the panel itself is not touched

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

    @unittest.skipUnless(HAVE_SRC or SRC_REQUIRED, "no copy of Swizzin (set SWIZZIN_SRC)")
    def test_after_box_upgrade_panel(self):
        self.require_src()
        self.server.swizzin("upgrade/panel.sh")
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

    def test_usage_lists_remove_store_and_sourcing_runs_nothing(self):
        r = subprocess.run(["bash", HELIEDAN], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("sudo bash heliedan.sh remove-store", r.stdout)
        self.assertNotIn("set -euo", r.stdout)
        r = subprocess.run(["bash", "-c", 'source "$1" && echo sourced', "test", HELIEDAN], capture_output=True,
                           text=True, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual((r.returncode, r.stdout), (0, "sourced\n"), r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
