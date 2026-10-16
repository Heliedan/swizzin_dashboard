"""Installing from the App Store helper, with a fake `box` that behaves like Swizzin's installers.

Run from the repository root: python tests/test_store_install.py
(the helper runs as a subprocess, without root, on temporary folders: nothing is installed)."""
import glob
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from test_store_helper import Helper, fake_swizzin  # noqa: E402

FAKE_BOX = r"""#!/bin/bash
# Fake Swizzin `box` for the tests: each app plays one situation of a real installer.
LOCKS="@LOCKS@"
cmd=$1; app=$2
if [ "$cmd" = test ]; then
    echo "checking $app"
    [ "$app" = flood ] && exit 1
    if [ "$app" = mylar ] && [ ! -e "$LOCKS/../mylar-checked" ]; then   # slow to start: fails only the first time
        touch "$LOCKS/../mylar-checked"; exit 1
    fi
    exit 0
fi
printf '\e[1;32mInstalling %s\e[0m\n' "$app"
echo "size $(stty size)"
case "$app" in
    sonarr)   # a question read on the terminal, like Swizzin's `ask`
        printf 'Continue? (y/n) '
        read -r reply < /dev/tty
        echo "size $(stty size)"
        [ "$reply" = y ] && touch "$LOCKS/.$app.lock" && echo "sonarr installed" ;;
    radarr)   # a whiptail dialog, answered with Enter
        whiptail --yesno "Install radarr now?" 8 40 && touch "$LOCKS/.$app.lock" ;;
    lidarr)   # an accented letter split across two writes
        printf 'caf\xc3'; sleep 0.3; printf '\xa9 ok\n'; touch "$LOCKS/.$app.lock" ;;
    bazarr)   # leaves a process holding the terminal, like a daemon started carelessly (deaf to the hangup)
        (trap '' HUP; exec sleep 6) &
        touch "$LOCKS/.$app.lock"; echo "bazarr done" ;;
    autobrr)  # shows what it was given, then fails (no lock)
        echo "PWD=$(pwd)"; env | sort | sed 's/^/ENV /'
        ls -l /proc/$$/fd > "$LOCKS/../fds.txt" ;;   # a plain redirection: no pipe of our own in the list
    jackett)  # a long installation
        echo "working"; sleep 30 ;;
    flood)    # installs, but its health check fails
        touch "$LOCKS/.$app.lock" ;;
    mylar)    # installs, but takes a while to start: its first health check fails
        touch "$LOCKS/.$app.lock" ;;
    deluge)   # a whiptail menu, like the version choice of Deluge or rTorrent: the second entry installs
        choice=$(whiptail --menu "Pick a version" 12 40 3 a Alpha b Beta c Gamma 3>&1 1>&2 2>&3)
        echo "picked $choice"; [ "$choice" = b ] && touch "$LOCKS/.$app.lock" ;;
esac
"""


class Conn:
    def __init__(self, path):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.settimeout(10)
        self.s.connect(path)
        self.buf = b""
        self.output = ""

    def send(self, **msg):
        self.s.sendall(json.dumps(msg).encode() + b"\n")

    def recv(self, timeout=10):
        self.s.settimeout(timeout)
        while b"\n" not in self.buf:
            chunk = self.s.recv(65536)
            if not chunk:
                raise ConnectionError("closed by the helper")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        msg = json.loads(line)
        if "output" in msg:
            self.output += msg["output"]
        return msg

    def until(self, test, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            msg = self.recv(max(end - time.time(), 0.1))
            if test(msg):
                return msg
        raise AssertionError("timed out; output so far: " + self.output[-500:])

    def wait_output(self, text, timeout=15):
        if text not in self.output:
            self.until(lambda m: text in self.output, timeout)

    def close(self):
        self.s.close()


def fake_installers(base):
    """fake_swizzin() plus the fake box, its apps, health checks, home and logs; the helper's extra options."""
    fake_swizzin(base)
    for app in ("sonarr", "radarr", "lidarr", "bazarr", "autobrr", "jackett", "flood", "medusa", "deluge", "mylar"):
        open(os.path.join(base, "scripts", app + ".sh"), "w").close()
    open(os.path.join(base, "locks", ".medusa.lock"), "w").close()
    os.makedirs(os.path.join(base, "tests"))
    open(os.path.join(base, "tests", "sonarr.sh"), "w").close()  # sonarr has its own health check
    os.makedirs(os.path.join(base, "home"))
    box = os.path.join(base, "box")
    with open(box, "w") as f:
        f.write(FAKE_BOX.replace("@LOCKS@", os.path.join(base, "locks")))
    os.chmod(box, 0o755)
    return ("--box", box, "--tests", os.path.join(base, "tests"), "--home", os.path.join(base, "home"),
            "--logs", os.path.join(base, "logs"), "--retest-delay", "1")


class Install(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.extra = fake_installers(self.base)
        self.home = os.path.join(self.base, "home")
        self.logs = os.path.join(self.base, "logs")
        self.helper = Helper(self.base, extra=self.extra)
        self.conns = []

    def tearDown(self):
        for c in self.conns:
            c.close()
        self.helper.stop()
        subprocess.run(["pkill", "-f", self.base], stderr=subprocess.DEVNULL)
        shutil.rmtree(self.base, ignore_errors=True)

    def conn(self):
        c = Conn(self.helper.path)
        self.conns.append(c)
        return c

    def install(self, app, **size):
        c = self.conn()
        c.send(op="install", app=app, **size)
        return c, c.until(lambda m: "ok" in m)

    def done(self, c, timeout=15):
        return c.until(lambda m: "done" in m, timeout)["done"]

    def test_install_answering_a_question(self):
        c, started = self.install("sonarr", rows=24, cols=80)
        self.assertTrue(started["ok"])
        self.assertTrue(started["control_key"])
        c.wait_output("Continue? (y/n)")
        self.assertIn("size 24 80", c.output)
        c.send(op="resize", rows=40, cols=120)
        c.send(op="input", data="y\r")
        phase = c.until(lambda m: "phase" in m)
        self.assertEqual(phase, {"phase": "test", "generic_test": False})
        done = self.done(c)
        self.assertEqual((done["result"], done["test"], done["generic_test"]), ("installed", "passed", False))
        self.assertFalse(done["retested"])  # passed the first time: checked once
        self.assertIn("size 40 120", c.output)  # the installer saw the new window size
        self.assertIn("checking sonarr", c.output)
        self.assertTrue(os.path.exists(os.path.join(self.base, "locks", ".sonarr.lock")))
        log, summary = glob.glob(os.path.join(self.logs, "*.log"))[0], glob.glob(os.path.join(self.logs, "*.json"))[0]
        self.assertEqual(stat.S_IMODE(os.stat(log).st_mode), 0o600)  # installers can print passwords
        with open(log, "rb") as f:
            self.assertIn(b"Installing sonarr", f.read())
        with open(summary) as f:
            self.assertEqual(json.load(f)["result"], "installed")

    @unittest.skipUnless(shutil.which("whiptail"), "whiptail is not installed")
    def test_whiptail_dialog(self):
        c, _ = self.install("radarr")
        c.wait_output("Install radarr now?")
        c.send(op="input", data="\r")
        done = self.done(c)
        self.assertEqual((done["result"], done["test"], done["generic_test"]), ("installed", "passed", True))

    def test_failed_install_skips_the_checks(self):
        c, _ = self.install("autobrr")
        done = self.done(c)
        self.assertEqual((done["result"], done["test"]), ("failed", None))
        self.assertNotIn("checking", c.output)

    def test_failed_checks_are_run_again_once(self):
        c, _ = self.install("flood")
        self.assertEqual(c.until(lambda m: "phase" in m), {"phase": "test", "generic_test": True})
        self.assertEqual(c.until(lambda m: "phase" in m), {"phase": "retest", "seconds": 1})
        self.assertEqual(c.until(lambda m: "phase" in m), {"phase": "test", "generic_test": True})
        done = self.done(c)
        self.assertEqual((done["result"], done["test"], done["retested"]), ("installed", "failed", True))
        self.assertEqual(c.output.count("checking flood"), 2)
        self.assertIn("checking again in 1 seconds", c.output)

    def test_slow_app_passes_the_second_checks(self):
        c, _ = self.install("mylar")
        done = self.done(c)
        self.assertEqual((done["result"], done["test"], done["retested"]), ("installed", "passed", True))
        self.assertEqual(c.output.count("checking mylar"), 2)

    def test_watcher_sees_the_second_checks(self):
        c, _ = self.install("flood")
        c.until(lambda m: m.get("phase") == "retest")
        w = self.conn()
        w.send(op="attach")
        attached = w.until(lambda m: "attached" in m)
        self.assertEqual(attached["attached"]["phase"], "test")  # still checking while it waits
        w.until(lambda m: "output" in m)
        self.assertIn("checking again", w.output)  # replayed
        self.assertEqual(self.done(w)["test"], "failed")

    def test_installer_environment(self):
        c, _ = self.install("autobrr")
        self.done(c)
        env = dict(line[4:].split("=", 1) for line in c.output.splitlines() if line.startswith("ENV "))
        self.assertEqual(set(env) - {"PWD", "SHLVL", "_"},
                         {"HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "PATH", "GIT_TERMINAL_PROMPT",
                          "NEEDRESTART_SUSPEND", "PAGER", "SYSTEMD_PAGER", "LESSSECURE"})  # nothing from the client or the helper
        self.assertEqual((env["PAGER"], env["SYSTEMD_PAGER"], env["LESSSECURE"]), ("cat", "cat", "1"))  # no pager to escape from
        self.assertEqual((env["HOME"], env["USER"], env["TERM"]), ("/root", "root", "xterm-256color"))
        self.assertIn("PWD=" + self.home, c.output)
        with open(os.path.join(self.base, "fds.txt")) as f:
            fds = f.read()
        # no socket, no log file and nothing else of the helper leaked to the installer
        self.assertNotIn("socket:", fds)
        self.assertNotIn(".log", fds)
        targets = {line.split(" -> ")[1] for line in fds.splitlines() if " -> " in line}
        self.assertTrue(all(t.startswith("/dev/pts/") or t.endswith("/box") for t in targets), targets)

    def test_names_are_checked(self):
        c = self.conn()
        for app, error in (("../x", "invalid"), ("Sonarr", "invalid"), ("a b", "invalid"), ("x" * 33, "invalid"),
                           (["sonarr"], "invalid"), ("plex", "unknown"), ("nginx", "blocked"),
                           ("letsencrypt", "blocked"), ("medusa", "installed")):
            c.send(op="install", app=app)
            self.assertEqual(c.recv(), {"ok": False, "error": error}, app)

    def test_one_installation_at_a_time(self):
        c, _ = self.install("jackett")
        c.wait_output("working")
        other = self.conn()
        other.send(op="install", app="sonarr")
        self.assertEqual(other.recv(), {"ok": False, "error": "busy"})
        other.send(op="catalogue")
        reply = other.recv()
        self.assertEqual((reply["job"]["app"], reply["job"]["phase"]), ("jackett", "install"))

    def test_watchers_cannot_type_until_they_take_control(self):
        a, started = self.install("sonarr")
        a.wait_output("Continue? (y/n)")
        b = self.conn()
        b.send(op="attach")
        attached = b.until(lambda m: "attached" in m)
        self.assertFalse(attached["controller"])
        self.assertNotIn("control_key", attached)
        b.wait_output("Continue? (y/n)")  # what was printed before it arrived is replayed
        b.send(op="input", data="y\r")
        self.assertEqual(b.until(lambda m: "error" in m), {"ok": False, "error": "not-controller"})
        b.send(op="attach", take=True)  # the panel asked for the admin password first
        self.assertTrue(b.until(lambda m: "attached" in m)["controller"])
        a.send(op="input", data="y\r")
        self.assertEqual(a.until(lambda m: "error" in m), {"ok": False, "error": "not-controller"})
        b.send(op="input", data="n\r")
        self.assertEqual(self.done(b)["result"], "failed")

    def test_control_key_after_reconnecting(self):
        a, started = self.install("sonarr")
        a.wait_output("Continue? (y/n)")
        a.close()  # the browser tab went away: the installation goes on
        b = self.conn()
        b.send(op="attach", control_key=started["control_key"])
        self.assertTrue(b.until(lambda m: "attached" in m)["controller"])
        b.send(op="input", data="y\r")
        self.assertEqual(self.done(b)["result"], "installed")

    def test_malformed_requests_do_not_stop_the_service(self):
        c = self.conn()  # an absurd window size with no installation running: the default size is used
        c.send(op="install", app="sonarr", rows=float("inf"), cols=float("nan"))
        started = c.until(lambda m: "ok" in m)
        self.assertTrue(started["ok"], started)
        c.wait_output("Continue? (y/n)")
        self.assertIn("size 30 100", c.output)
        c.s.sendall(b'{"op": "resize", "rows": 1e400, "cols": -1e400}\n')  # from the controller
        for raw in (b'{"op": ["install"]}\n', b'{"op": {"x": 1}}\n', b'{"op": "attach", "control_key": "\\u00e9t\\u00e9"}\n',
                    b"[" * 60000 + b"\n", b'{"op": "install", "app": "radarr", "rows": "x"}\n'):
            other = self.conn()
            other.s.sendall(raw)
            try:
                reply = other.recv(5)
            except (ConnectionError, OSError):
                reply = None  # dropped: acceptable, as long as the service keeps running
            self.assertIsNone(self.helper.proc.poll(), raw[:40])
            if reply is not None and "attached" not in reply:
                self.assertFalse(reply.get("ok"), raw[:40])
        c.send(op="input", data="y\r")
        self.assertEqual(self.done(c)["result"], "installed")  # the installation went on to the end
        self.assertIsNone(self.helper.proc.poll())

    def test_a_full_disk_does_not_stop_the_installation(self):
        base = tempfile.mkdtemp()
        helper = Helper(base, extra=fake_installers(base), max_file_size=200)  # its log cannot grow past 200 bytes
        try:
            c = Conn(helper.path)
            c.send(op="install", app="autobrr")  # prints a few kilobytes
            c.until(lambda m: "ok" in m)
            done = c.until(lambda m: "done" in m)["done"]
            self.assertEqual(done["result"], "failed")  # autobrr's normal outcome: the helper did not crash
            self.assertIn("ENV PATH=", c.output)  # the terminal got everything, only the log was cut
            self.assertIsNone(helper.proc.poll())
            c.close()
        finally:
            helper.stop()
            subprocess.run(["pkill", "-f", base], stderr=subprocess.DEVNULL)
            shutil.rmtree(base, ignore_errors=True)

    def test_a_new_installation_is_not_sent_to_old_tabs(self):
        a, _ = self.install("lidarr")
        self.done(a)
        b, _ = self.install("autobrr")
        self.done(b)
        with self.assertRaises(socket.timeout):  # tab A only follows its own installation
            a.recv(timeout=1)

    def test_taking_control_changes_the_key_and_tells_the_old_controller(self):
        a, started = self.install("sonarr")
        a.wait_output("Continue? (y/n)")
        w = self.conn()
        w.send(op="attach", take=True)
        taken = w.until(lambda m: "attached" in m)
        self.assertTrue(taken["controller"])
        self.assertNotEqual(taken["control_key"], started["control_key"])  # the old key no longer gives control
        self.assertEqual(a.until(lambda m: "control" in m), {"control": "lost"})
        a.send(op="input", data="y\r")
        self.assertEqual(a.until(lambda m: "error" in m)["error"], "not-controller")
        r = self.conn()
        r.send(op="attach", control_key=started["control_key"])
        self.assertFalse(r.until(lambda m: "attached" in m)["controller"])
        w.send(op="input", data="y\r")
        self.assertEqual(self.done(w)["result"], "installed")

    def test_typing_while_no_installer_runs_is_ignored(self):
        c, _ = self.install("flood")  # its checks fail: one second without a terminal before the second check
        c.until(lambda m: m.get("phase") == "retest")
        c.send(op="input", data="x")
        msg = c.until(lambda m: "done" in m or "error" in m)
        self.assertIn("done", msg)  # no "not-controller": the controller still controls the job

    def test_split_accented_letter(self):
        c, _ = self.install("lidarr")
        self.done(c)
        self.assertIn("café ok", c.output)
        self.assertNotIn("�", c.output)

    def test_a_process_keeping_the_terminal_does_not_hold_the_installation(self):
        c, _ = self.install("bazarr")
        t = time.time()
        done = self.done(c, timeout=10)
        self.assertEqual(done["result"], "installed")
        self.assertLess(time.time() - t, 5)  # the leftover `sleep 6` still holds the terminal

    def test_ctrl_c_stops_the_installer(self):
        self.helper.stop()  # even when the helper itself was started with SIGINT ignored
        self.helper = Helper(self.base, extra=self.extra, ignore_sigint=True)
        c, _ = self.install("jackett")
        c.wait_output("working")
        t = time.time()
        c.send(op="input", data="\x03")
        self.assertEqual(self.done(c, timeout=10)["result"], "failed")
        self.assertLess(time.time() - t, 5)  # the installer was sleeping for 30 seconds

    def test_typing_too_much_at_once(self):
        c, _ = self.install("sonarr")
        c.wait_output("Continue?")
        c.send(op="input", data="y" * 5000)
        self.assertEqual(c.until(lambda m: "error" in m), {"ok": False, "error": "invalid"})

    def test_last_installation_is_reported(self):
        c, _ = self.install("flood")
        self.done(c)
        c.send(op="catalogue")
        reply = c.until(lambda m: "apps" in m)
        self.assertIsNone(reply["job"])
        self.assertEqual((reply["last"]["app"], reply["last"]["result"]), ("flood", "installed"))
        self.helper.stop()  # and still after a restart
        self.helper = Helper(self.base, extra=self.extra)
        c2 = self.conn()
        c2.send(op="catalogue")
        self.assertEqual(c2.recv()["last"]["app"], "flood")

    def test_stopping_the_service_marks_the_installation_interrupted(self):
        c, _ = self.install("jackett")
        c.wait_output("working")
        self.helper.stop()
        with open(glob.glob(os.path.join(self.logs, "*.json"))[0]) as f:
            summary = json.load(f)
        self.assertEqual(summary["result"], "interrupted")
        time.sleep(0.5)
        self.assertEqual(subprocess.run(["pgrep", "-f", "box install jackett"], capture_output=True).stdout, b"")
        self.helper = Helper(self.base, extra=self.extra)

    def test_a_crash_is_marked_interrupted_at_the_next_start(self):
        self.helper.stop()
        os.makedirs(self.logs, exist_ok=True)
        with open(os.path.join(self.logs, "20261016-120000-sonarr.json"), "w") as f:
            json.dump({"id": "20261016-120000-sonarr", "app": "sonarr", "phase": "install", "result": None}, f)
        self.helper = Helper(self.base, extra=self.extra)
        c = self.conn()
        c.send(op="catalogue")
        self.assertEqual(c.recv()["last"]["result"], "interrupted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
