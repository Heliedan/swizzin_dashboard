"""The App Store's uninstaller (store-helper/remove_runner.py), run without root on a fake system.

A fake root folder stands for / (homes, /opt, nginx, systemd units, Swizzin's locks), a fake Swizzin holds the
remove scripts, and fake systemctl, nginx, dpkg, dpkg-query, apt-get and ss (tests/fake_system.py) keep their
state in a JSON file and note every call. Nothing on the machine is touched.
Run from the repository root: python tests/test_remove_runner.py
"""
import fcntl
import hashlib
import json
import os
import select
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "store-helper", "remove_runner.py")
FAKE = os.path.join(ROOT, "tests", "fake_system.py")
sys.path.insert(0, os.path.join(ROOT, "store-helper"))
import remove_runner  # noqa: E402

GLOBALS = r"""# fake sources/globals.sh: what the remove scripts use of Swizzin's environment
export log=/dev/null
ask() {
    printf '%s (Y/n) ' "$1"
    read -r reply < /dev/tty
    case "$reply" in [Nn]*) return 1 ;; *) return 0 ;; esac
}
apt_remove() { apt-get remove -y "$@" > /dev/null; }
export -f ask apt_remove
"""

DEMO = r"""# like most Swizzin remove scripts
R=$STORE_TEST_ROOT
systemctl disable --now -q demo
rm -rf "$R/opt/demo" "$R/home/alice/.config/demo"
rm -f "$R/etc/nginx/apps/demo.conf"
systemctl reload nginx
rm "$R/install/.demo.lock"
echo "demo removed"
"""  # and it forgets its unit file, as remove/airsonic.sh does


class World:
    """A fake server with one app, demo, installed for alice (the master) and bob."""

    def __init__(self):
        self.umask = os.umask(0o022)  # files and folders writable by their owner only, as Swizzin's are
        self.base = tempfile.mkdtemp()
        self.root = os.path.join(self.base, "root")
        self.swizzin = os.path.join(self.base, "swizzin")
        self.archives = os.path.join(self.base, "archives")
        self.work = os.path.join(self.base, "work")
        self.bin = os.path.join(self.base, "bin")
        for folder in ("install", "root", "home/alice/.config", "home/bob/.config", "etc/nginx/apps", "etc/nginx/conf.d",
                       "etc/systemd/system/multi-user.target.wants", "var/lib/dpkg", "var/cache/apt/archives",
                       "var/lib/apt/lists", "opt"):
            os.makedirs(os.path.join(self.root, folder))
        for name in ("swizzin/sources", "swizzin/scripts/remove", "work", "bin"):
            os.makedirs(os.path.join(self.base, name), exist_ok=True)
        self.write("/root/.master.info", "alice:secret\n")
        self.write("/root/bob.info", "bob:secret\n")
        self.write("/etc/htpasswd", "alice:x\nbob:y\n")
        for lock in remove_runner.APT_LOCKS:
            self.write(lock, "")
        for tool in ("systemctl", "nginx", "dpkg", "dpkg-query", "apt-get", "ss"):
            os.symlink(FAKE, os.path.join(self.bin, tool))
        with open(os.path.join(self.swizzin, "sources", "globals.sh"), "w") as f:
            f.write(GLOBALS)
        self.state = {"units": {"demo.service": {"active": "active", "enabled": "enabled",
                                                 "file": "/etc/systemd/system/demo.service"}},
                      "packages": {"nginx": "ii ", "coreutils": "ii "}, "rdeps": {}, "nginx_ok": True}
        self.save()
        open(os.path.join(self.base, "fake-calls.log"), "w").close()
        # demo, installed: settings with a SQLite journal and logs, a program folder, a web page, a unit
        self.write("/install/.demo.lock", "")
        self.write("/install/.nginx.lock", "")
        self.write("/home/alice/.config/demo/config.toml", "port = 7474\n")
        self.write("/home/alice/.config/demo/demo.db", "D" * 5000)
        self.write("/home/alice/.config/demo/demo.db-wal", "W" * 100)
        self.write("/home/alice/.config/demo/logs/demo.log", "L" * 100)
        self.write("/opt/demo/demo", "#!/bin/sh\n")
        self.write("/home/alice/media/film.mkv", "F")
        self.write("/etc/nginx/apps/demo.conf", "location /demo {}\n")
        self.write("/etc/systemd/system/demo.service", "[Service]\n")
        self.app("demo", DEMO, units=["demo"], copy=["/home/{master}/.config/demo"], skip=["logs"],
                 gone=["/home/{master}/.config/demo", "/opt/demo"], kept=["/home/{master}/media"],
                 nginx=["/etc/nginx/apps/demo.conf"], leftovers=["/etc/systemd/system/demo.service"])

    def write(self, path, text):
        real = os.path.join(self.root, path.lstrip("/"))
        os.makedirs(os.path.dirname(real), exist_ok=True)
        with open(real, "w") as f:
            f.write(text)

    def path(self, path):
        return os.path.join(self.root, path.lstrip("/"))

    def save(self):
        path = os.path.join(self.base, "fake-state.json")
        with open(path + ".test", "w") as f:  # written aside, then renamed: a fake tool never reads half a file
            json.dump(self.state, f)
        os.replace(path + ".test", path)

    def load(self):
        with open(os.path.join(self.base, "fake-state.json")) as f:
            self.state = json.load(f)
        return self.state

    def app(self, name, script, **fields):
        with open(os.path.join(self.swizzin, "scripts", "remove", name + ".sh"), "w") as f:
            f.write(script)
        self.apps = getattr(self, "apps", {})
        self.apps[name] = fields
        self.write_table()

    def write_table(self, pins=None):
        def digest(rel):
            with open(os.path.join(self.swizzin, rel), "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        files = ["sources/globals.sh"] + ["scripts/remove/%s.sh" % a for a in self.apps]
        table = {"common": ["sources/globals.sh"], "apps": self.apps,
                 "pins": pins if pins is not None else {rel: [digest(rel)] for rel in files}}
        with open(os.path.join(self.base, "table.json"), "w") as f:
            json.dump(table, f)

    def argv(self, app, *extra):
        return [sys.executable, "-I", "-B", RUNNER, "--app", app, "--state", os.path.join(self.base, "state.json"),
                "--result", os.path.join(self.base, "result.json"), "--swizzin", self.swizzin, "--locks", self.path("/install"),
                "--archives", self.archives, "--root", self.root, "--table", os.path.join(self.base, "table.json"),
                "--work", self.work, *extra]

    def env(self, **extra):
        return {"PATH": self.bin + ":/usr/bin:/bin", "STORE_TEST_ROOT": self.root, "HOME": self.base,
                "TERM": "xterm-256color", "LANG": "C.UTF-8", **extra}

    def run(self, app="demo", *extra, timeout=60, **env):
        p = subprocess.run(self.argv(app, *extra), env=self.env(**env), stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        self.output = p.stdout.decode("utf-8", "replace")
        return self.result()

    def start(self, app="demo", *extra, **env):
        return subprocess.Popen(self.argv(app, *extra), env=self.env(**env), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    @staticmethod
    def wait_output(p, text, timeout=60):
        """What a started runner printed, read as it comes, up to text (p.communicate() gives the rest)."""
        out, end = b"", time.monotonic() + timeout
        while text.encode() not in out:
            ready = select.select([p.stdout], [], [], max(end - time.monotonic(), 0))[0]
            chunk = os.read(p.stdout.fileno(), 65536) if ready else b""
            if not chunk:
                raise AssertionError("%r never came; output: %s" % (text, out.decode("utf-8", "replace")))
            out += chunk
        return out

    @staticmethod
    def wait_for(p, test, timeout=60):
        """Until test() is true, while the started runner still runs."""
        end = time.monotonic() + timeout
        while not test():
            if p.poll() is not None or time.monotonic() > end:
                raise AssertionError("the runner ended or took too long (code %s)" % p.poll())
            time.sleep(0.05)

    def result(self):
        with open(os.path.join(self.base, "result.json")) as f:
            return json.load(f)

    def calls(self):
        with open(os.path.join(self.base, "fake-calls.log")) as f:
            return f.read().splitlines()

    def checks(self, result):
        return {c["id"]: c["ok"] for c in result["checks"]}

    def members(self, archive):
        with tarfile.open(archive, "r:gz") as tar:
            return {m.name: m for m in tar.getmembers()}


class Runner(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def tearDown(self):
        os.umask(self.w.umask)
        shutil.rmtree(self.w.base, ignore_errors=True)

    def test_a_removal_that_goes_well(self):
        result = self.w.run()
        self.assertEqual(result["result"], "removed", self.w.output)
        self.assertTrue(all(self.w.checks(result).values()), result["checks"])
        # the app was stopped before its script ran, by systemd unit, and not disabled by the runner: disabling
        # reloads systemd, which on Ubuntu often starts the automatic updates at once (the script disables it)
        calls = self.w.calls()
        self.assertLess(calls.index("systemctl stop demo.service"), calls.index("systemctl disable --now -q demo"))
        self.assertNotIn("systemctl disable demo.service", calls[:calls.index("systemctl disable --now -q demo")])
        self.assertIn("systemctl reload nginx", calls[calls.index("systemctl disable --now -q demo") + 1:])
        # what Swizzin's script forgot is done, what it must not touch is still there
        self.assertFalse(os.path.exists(self.w.path("/etc/systemd/system/demo.service")))
        self.assertTrue(os.path.exists(self.w.path("/home/alice/media/film.mkv")))
        self.assertIn("demo removed", self.w.output)
        # the copy: settings and SQLite journal, without the logs, readable by root only, with its manifest
        archive = result["archive"]
        self.assertEqual(os.path.dirname(archive), self.w.archives)
        self.assertEqual(stat.S_IMODE(os.stat(archive).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.w.archives).st_mode), 0o700)
        members = self.w.members(archive)
        for name in ("home/alice/.config/demo/config.toml", "home/alice/.config/demo/demo.db",
                     "home/alice/.config/demo/demo.db-wal", "swizzin-store/manifest.json", "swizzin-store/RESTORE.txt"):
            self.assertIn(name, members)
        self.assertFalse([n for n in members if "logs" in n])
        with tarfile.open(archive, "r:gz") as tar:
            manifest = json.load(tar.extractfile("swizzin-store/manifest.json"))
            restore = tar.extractfile("swizzin-store/RESTORE.txt").read().decode()
            config = tar.extractfile("home/alice/.config/demo/config.toml").read()
        self.assertEqual(config, b"port = 7474\n")
        entry = [m for m in manifest if m["path"].endswith("config.toml")][0]
        self.assertEqual(entry["sha256"], hashlib.sha256(b"port = 7474\n").hexdigest())
        self.assertIn("box install demo", restore)
        self.assertIn("1/5", self.w.output)
        self.assertIn("5/5", self.w.output)

    def test_a_copy_made_in_the_same_second_keeps_the_other_one(self):
        os.makedirs(self.w.archives, mode=0o700)
        now = time.time()
        taken = ["demo-%s.tar.gz" % time.strftime("%Y%m%d-%H%M%S", time.localtime(now + s)) for s in range(30)]
        for name in taken:  # every name the run can pick in the next 30 seconds is already a copy
            with open(os.path.join(self.w.archives, name), "w") as f:
                f.write("an older copy")
        result = self.w.run()
        self.assertEqual(result["result"], "removed", self.w.output)
        self.assertRegex(os.path.basename(result["archive"]), r"^demo-\d{8}-\d{6}-2\.tar\.gz$")
        self.assertIn(os.path.basename(result["archive"])[:-len("-2.tar.gz")] + ".tar.gz", taken)
        self.assertIn("home/alice/.config/demo/config.toml", self.w.members(result["archive"]))
        for name in taken:
            with open(os.path.join(self.w.archives, name)) as f:
                self.assertEqual(f.read(), "an older copy")

    def test_traps_in_the_copied_folders_are_not_followed(self):
        os.symlink("/etc/passwd", self.w.path("/home/alice/.config/demo/passwd"))  # planted by a user
        os.symlink("/etc", self.w.path("/home/alice/.config/demo/etc"))
        os.mkfifo(self.w.path("/home/alice/.config/demo/fifo"))  # would hang a plain read
        result = self.w.run(timeout=30)
        self.assertEqual(result["result"], "removed", self.w.output)
        members = self.w.members(result["archive"])
        self.assertFalse([n for n in members if n.endswith(("passwd", "/etc", "fifo")) or "/etc/" in n])
        reasons = dict(result["skipped"])
        self.assertEqual(reasons["/home/alice/.config/demo/passwd"], "a link")
        self.assertEqual(reasons["/home/alice/.config/demo/etc"], "a link")
        self.assertEqual(reasons["/home/alice/.config/demo/fifo"], "not a regular file")

    def test_a_changed_swizzin_file_stops_everything_before_anything_is_touched(self):
        self.w.write_table(pins={"sources/globals.sh": ["0" * 64], "scripts/remove/demo.sh": ["0" * 64]})
        result = self.w.run()
        self.assertEqual(result["result"], "failed")
        self.assertIn("not the version", result["message"])
        self.assertFalse([c for c in self.w.calls() if c.startswith("systemctl stop")])
        self.assertIsNone(result["archive"])
        self.assertTrue(os.path.exists(self.w.path("/install/.demo.lock")))

    def test_a_script_writable_by_others_is_refused(self):
        os.chmod(os.path.join(self.w.swizzin, "scripts", "remove", "demo.sh"), 0o666)
        result = self.w.run()
        self.assertEqual(result["result"], "failed")
        self.assertIn("can be changed by another user", result["message"])

    def test_apt_busy_refuses(self):
        with open(self.w.path("/var/lib/dpkg/lock-frontend"), "r+") as lock:
            fcntl.lockf(lock, fcntl.LOCK_EX)  # apt holds its lock
            result = self.w.run()
        self.assertEqual(result["result"], "failed")
        self.assertIn("try again", result["message"])
        self.w.state["apt_daily"] = "activating"
        self.w.save()
        self.assertIn("automatic system updates", self.w.run()["message"])

    def apt_daily(self, value):
        state = self.w.load()
        state["apt_daily"] = value
        self.w.save()

    def apt_daily_on_stop(self, value):
        """The automatic updates start when the app is stopped: after the first apt check, before the wait for apt
        (Ubuntu starts them on its own, at any time)."""
        self.w.state["apt_daily_on_stop"] = value
        self.w.save()

    def apt_checks(self):
        return self.w.calls().count("systemctl is-active apt-daily.service")

    def test_apt_busy_after_the_copy_is_waited_for(self):
        self.apt_daily_on_stop("activating")
        p = self.w.start("demo", "--apt-wait", "30")
        out = self.w.wait_output(p, "Waiting for apt to be free")
        checks = self.apt_checks()
        self.w.wait_for(p, lambda: self.apt_checks() > checks)  # checked again, still busy
        self.assertIsNone(p.poll())  # waiting, nothing removed yet
        self.assertTrue(os.path.exists(self.w.path("/home/alice/.config/demo/config.toml")))
        self.apt_daily("inactive")
        out = (out + p.communicate(timeout=60)[0]).decode()
        self.assertIn("Waiting for apt to be free (automatic system updates", out)
        self.assertEqual(self.w.result()["result"], "removed", out)

    def test_apt_busy_too_long_after_the_copy_puts_everything_back(self):
        self.apt_daily_on_stop("activating")
        p = self.w.start("demo", "--apt-wait", "3")
        out = p.communicate(timeout=60)[0].decode()
        result = self.w.result()
        self.assertEqual(result["result"], "failed", out)
        self.assertIn("automatic system updates", result["message"])
        state = self.w.load()["units"]["demo.service"]
        self.assertEqual((state["active"], state["enabled"]), ("active", "enabled"))
        self.assertTrue(os.path.exists(self.w.path("/home/alice/.config/demo/config.toml")))

    def test_stop_while_waiting_for_apt_puts_everything_back(self):
        self.apt_daily_on_stop("activating")
        p = self.w.start("demo", "--apt-wait", "60")
        out = self.w.wait_output(p, "Waiting for apt to be free")
        p.send_signal(signal.SIGINT)  # the page's Stop
        out = (out + p.communicate(timeout=60)[0]).decode()
        result = self.w.result()
        self.assertEqual(result["result"], "failed", out)
        self.assertIn("stopped at your request", result["message"])
        self.assertEqual(self.w.load()["units"]["demo.service"]["active"], "active")

    def test_apt_that_would_take_another_package_refuses(self):
        self.w.state["packages"].update({"demo-server": "ii ", "other-app": "ii "})
        self.w.state["rdeps"] = {"demo-server": ["other-app"]}
        self.w.save()
        self.w.apps["demo"].update(packages=["demo-server"], allowed=["demo-server"])
        self.w.write_table()
        result = self.w.run()
        self.assertEqual(result["result"], "failed")
        self.assertIn("other-app", result["message"])
        self.w.state["rdeps"], self.w.state["sim_extra"] = {}, ["Inst something-new [2.0]"]
        self.w.save()
        self.assertIn("install or configure", self.w.run()["message"])

    def test_an_app_that_another_one_needs_is_refused(self):
        self.w.apps["demo"]["refuse_if"] = ["deluge"]
        self.w.write_table()
        self.w.write("/install/.deluge.lock", "")
        result = self.w.run()
        self.assertEqual(result["result"], "failed")
        self.assertIn("deluge is installed", result["message"])

    def test_a_script_that_fails_halfway_is_reported_incomplete(self):
        self.w.app("demo", 'rm -rf "$STORE_TEST_ROOT/home/alice/.config/demo"; echo "E: something broke"; exit 1\n',
                   **self.w.apps["demo"])
        result = self.w.run()
        self.assertEqual(result["result"], "incomplete")
        checks = self.w.checks(result)
        self.assertFalse(checks["lock"])
        self.assertFalse(checks["files"])  # /opt/demo is still there
        self.assertTrue(os.path.exists(result["archive"]))  # but the settings it deleted are in the copy
        self.assertIn("home/alice/.config/demo/config.toml", self.w.members(result["archive"]))

    def test_the_known_question_is_answered_for_the_user(self):
        script = 'if ask "Would you like to purge the configuration?" Y; then echo PURGED; fi\n' + DEMO
        self.w.app("demo", script, answers=[["Would you like to purge the configuration?", "y"]],
                   **{k: v for k, v in self.w.apps["demo"].items() if k != "answers"})
        result = self.w.run()
        self.assertEqual(result["result"], "removed", self.w.output)
        self.assertIn("PURGED", self.w.output)
        self.assertIn("Answered 'y' for you", self.w.output)

    def test_a_script_that_waits_forever_is_stopped(self):
        self.w.app("demo", 'printf "Unexpected question? "; read -r x < /dev/tty\n', **self.w.apps["demo"])
        result = self.w.run("demo", "--quiet-timeout", "2", timeout=60)
        self.assertEqual(result["result"], "incomplete")
        self.assertFalse(self.w.checks(result)["script"])

    def test_stop_before_the_script_puts_everything_back(self):
        p = self.w.start(STORE_TEST_PAUSE="copy:3")
        for _ in range(100):  # wait for the copy step
            if os.path.exists(os.path.join(self.w.base, "state.json")) and \
                    json.load(open(os.path.join(self.w.base, "state.json")))["phase"] == "copy":
                break
            time.sleep(0.1)
        p.send_signal(signal.SIGINT)  # what Ctrl+C or the page's Stop does
        out = p.communicate(timeout=60)[0].decode()
        result = self.w.result()
        self.assertEqual(result["result"], "failed", out)
        self.assertIn("stopped at your request", result["message"])
        state = self.w.load()["units"]["demo.service"]
        self.assertEqual((state["active"], state["enabled"]), ("active", "enabled"))  # started and enabled again
        self.assertTrue(os.path.exists(self.w.path("/home/alice/.config/demo/config.toml")))
        self.assertEqual(os.listdir(self.w.archives), [])  # an unfinished copy does not stay

    def test_ctrl_c_during_the_script_is_ignored(self):
        script = ('echo "demo script running"\n'
                  'for i in $(seq 1200); do [ -e "$STORE_TEST_ROOT/go" ] && break; sleep 0.05; done\n')
        self.w.app("demo", script + DEMO, **self.w.apps["demo"])  # runs until the test lets it go (60 s at most)
        p = self.w.start()
        out = self.w.wait_output(p, "demo script running")
        p.send_signal(signal.SIGINT)
        out += self.w.wait_output(p, "Stopping is not possible now")
        open(self.w.path("/go"), "w").close()
        out = (out + p.communicate(timeout=60)[0]).decode()
        self.assertEqual(self.w.result()["result"], "removed", out)

    def test_an_app_started_again_meanwhile_is_put_back(self):
        # someone starts it from the panel's switch, after it was stopped, just before Swizzin's script
        self.w.state["active_in_phase"] = {"demo.service": "remove"}
        self.w.save()
        result = self.w.run()
        self.assertEqual(result["result"], "failed")
        self.assertIn("started again", result["message"])
        self.assertTrue(os.path.exists(result["archive"]))  # the checked copy stays: it can only help

    def test_a_kept_file_deleted_and_another_package_removed_are_reported(self):
        self.w.state["packages"]["libfoo"] = "ii "
        self.w.save()
        self.w.app("demo", DEMO + 'rm -rf "$STORE_TEST_ROOT/home/alice/media"\napt-get remove -y libfoo\n',
                   **self.w.apps["demo"])
        result = self.w.run()
        self.assertEqual(result["result"], "incomplete")
        checks = self.w.checks(result)
        self.assertFalse(checks["kept"])
        self.assertFalse(checks["other-packages"])

    def test_nginx_broken_before_is_not_blamed(self):
        self.w.state["nginx_ok"] = False
        self.w.save()
        result = self.w.run()
        self.assertEqual(result["result"], "removed", self.w.output)
        self.assertNotIn("systemctl reload nginx", [c for c in self.w.calls()][-5:])  # not reloaded while broken

    def test_every_user_gets_a_copy_and_template_instances_are_found(self):
        self.w.state["units"] = {"demo@alice.service": {"active": "active", "enabled": "enabled"},
                                 "demo@bob.service": {"active": "inactive", "enabled": "enabled"}}
        self.w.save()
        self.w.write("/etc/systemd/system/demo@.service", "[Service]\n")
        self.w.write("/home/bob/.config/demo/config.toml", "port = 7575\n")
        script = DEMO.replace("systemctl disable --now -q demo", "") + \
            'rm -rf "$STORE_TEST_ROOT/home/bob/.config/demo" "$STORE_TEST_ROOT/etc/systemd/system/demo@.service"\n'
        fields = dict(self.w.apps["demo"], units=["demo@"], copy=["/home/{user}/.config/demo"],
                      gone=["/home/{user}/.config/demo", "/opt/demo"], leftovers=[])
        self.w.app("demo", script, **fields)
        result = self.w.run()
        self.assertEqual(result["result"], "removed", self.w.output)
        members = self.w.members(result["archive"])
        self.assertIn("home/alice/.config/demo/config.toml", members)
        self.assertIn("home/bob/.config/demo/config.toml", members)
        self.assertIn("systemctl stop demo@alice.service demo@bob.service", self.w.calls())
        self.assertIn("Users: alice, bob.", self.w.output)


class Pieces(unittest.TestCase):
    """Parts checked in-process: what needs root on a real system is played by patching."""

    def setUp(self):
        self.w = World()
        opts = remove_runner.options(["--app", "demo", "--state", os.path.join(self.w.base, "s.json"),
                                      "--result", os.path.join(self.w.base, "r.json"), "--swizzin", self.w.swizzin,
                                      "--archives", self.w.archives, "--root", self.w.root,
                                      "--locks", self.w.path("/install"),
                                      "--table", os.path.join(self.w.base, "table.json")])
        self.r = remove_runner.Removal(opts)

    def tearDown(self):
        os.umask(self.w.umask)
        shutil.rmtree(self.w.base, ignore_errors=True)

    def test_a_link_owned_by_a_user_on_the_way_is_refused(self):
        os.rename(self.w.path("/home/bob/.config"), self.w.path("/home/bob/config-real"))
        os.symlink(self.w.path("/home/bob/config-real"), self.w.path("/home/bob/.config"))
        if os.geteuid() == 0:  # tests run as root: the link was made by root, give it to a user as on a real server
            os.lchown(self.w.path("/home/bob/.config"), 1000, 1000)
        with mock.patch("os.geteuid", return_value=0):  # as root: a link of user 1000 is not trusted
            self.assertIn("is a link that does not belong to root",
                          self.r.safe_path("/home/bob/.config/demo/settings.json"))
            self.assertIsNone(self.r.safe_path("/home/alice/.config/demo/config.toml"))

    def test_not_enough_space_for_the_copy_refuses(self):
        with mock.patch("shutil.disk_usage", return_value=shutil._ntuple_diskusage(10**9, 10**9, 1000)):
            with self.assertRaises(remove_runner.Refused) as e:
                os.environ["PATH"] = self.w.bin + ":" + os.environ.get("PATH", "")
                os.environ["STORE_TEST_ROOT"] = self.w.root
                self.r.phase_check()
        self.assertIn("not enough free space", str(e.exception))


    def test_a_disk_that_fills_up_during_the_copy_is_said_in_words(self):
        full = OSError(28, "No space left on device")
        with mock.patch.dict(os.environ, self.w.env()):
            self.r.phase_check()
            with mock.patch("os.fsync", side_effect=full), self.assertRaises(remove_runner.Refused) as e:
                self.r.phase_copy()
        self.assertEqual(str(e.exception), "the copy of its settings failed: the disk is full. Free some space, then try again")
        self.assertEqual(os.listdir(self.w.archives), [])  # an unfinished copy does not stay
        self.assertEqual(remove_runner.copy_error(PermissionError(13, "Permission denied", "/x")), "Permission denied (/x)")

    def program(self, path, seconds="60"):
        """A real process whose program lies at path in the fake root (a copy of sleep)."""
        real = self.w.path(path)
        os.makedirs(os.path.dirname(real), exist_ok=True)
        shutil.copy(shutil.which("sleep"), real)
        p = subprocess.Popen([real, seconds])
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        return p

    def in_a_container(self, p):
        """in_this_system as if p ran in a Docker container (another PID namespace)."""
        here = remove_runner.in_this_system
        return mock.patch.object(remove_runner, "in_this_system", lambda pid: pid != str(p.pid) and here(pid))

    def test_in_this_system_compares_pid_namespaces(self):
        proc = os.path.join(self.w.base, "proc")
        for pid, ns in (("self", "pid:[1]"), ("10", "pid:[1]"), ("20", "pid:[2]")):
            os.makedirs(os.path.join(proc, pid, "ns"))
            os.symlink(ns, os.path.join(proc, pid, "ns", "pid"))
        self.assertTrue(remove_runner.in_this_system("10", proc))
        self.assertFalse(remove_runner.in_this_system("20", proc))  # a container's process
        self.assertTrue(remove_runner.in_this_system("30", proc))  # cannot tell: counted
        self.assertTrue(remove_runner.in_this_system(str(os.getpid())))

    def test_the_same_program_in_a_container_is_not_left_behind(self):
        p = self.program("/opt/demo/demo-server")
        self.r.gone_paths = ["/opt/demo"]
        self.assertEqual(self.r.processes_left(), ["demo-server (pid %d)" % p.pid])
        with self.in_a_container(p):
            self.assertEqual(self.r.processes_left(), [])

    def test_apt_in_a_container_does_not_hold_this_server(self):
        p = self.program("/opt/tools/apt-get")
        with mock.patch.dict(os.environ, self.w.env()):
            self.assertEqual(self.r.apt_busy(), "apt-get is running")
            with self.in_a_container(p):
                self.assertIsNone(self.r.apt_busy())

    @staticmethod
    def state(pid):
        """The state of a process, read from /proc (R, S, Z...)."""
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rpartition(")")[2].split()[0]

    def test_an_ended_apt_not_collected_yet_does_not_hold_this_server(self):
        """A zombie (state Z): apt-get has ended, and its parent has not collected it yet. It holds no lock."""
        ended = self.program("/opt/tools/apt-get", "0")
        end = time.monotonic() + 10
        while self.state(ended.pid) != "Z" and time.monotonic() < end:  # this test is its parent: not collected
            time.sleep(0.01)
        self.assertEqual(self.state(ended.pid), "Z")
        with mock.patch.dict(os.environ, self.w.env()):
            self.assertIsNone(self.r.apt_busy())
            self.program("/usr/local/bin/apt-get")  # one that runs still counts
            self.assertEqual(self.r.apt_busy(), "apt-get is running")


if __name__ == "__main__":
    unittest.main()
