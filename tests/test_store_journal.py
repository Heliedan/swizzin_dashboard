"""The journal of a service through the App Store helper (operation "journal" of store-helper/swizzin_store.py), for
the Logs buttons of the panel: which units it reads, the exact journalctl it runs, the limits it keeps, and that it
never holds up the other requests.

The helper runs as a subprocess without root, with a fake journalctl (--journalctl) that notes how it was started and
answers as each test says. The list of units it reads is checked against the panel's profiles here too.
Run from the repository root: python tests/test_store_journal.py"""
import importlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from test_store_helper import HELPER, Helper, ask, fake_swizzin  # noqa: E402
from test_store_install import Conn  # noqa: E402

sys.path.insert(0, os.path.dirname(HELPER))
import swizzin_store  # noqa: E402

# The fake journalctl. behaviour.json, next to it, says what it does: "output" (text, written as UTF-8 with
# surrogateescape, so that "\udcff" is the byte 0xff), "repeat" (times), "status" (its exit code), "sleep" (seconds,
# after the output), "child" (a process that keeps its output open, then it exits).
FAKE = r"""#!{python} -I
import json, os, subprocess, sys, time
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "behaviour.json")) as f:
    b = json.load(f)
with open(os.path.join(here, "calls.log"), "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(), "pid": os.getpid(),
                        "session": os.getsid(0) == os.getpid()}}) + "\n")
out = sys.stdout.buffer
data = b.get("output", "").encode("utf-8", "surrogateescape")
for _ in range(b.get("repeat", 1)):
    out.write(data)
out.flush()
if b.get("child"):
    child = subprocess.Popen(["sleep", "60"], stdout=sys.stdout)
    with open(os.path.join(here, "child.pid"), "w") as f:
        f.write(str(child.pid))
    os._exit(0)
time.sleep(b.get("sleep", 0))
sys.exit(b.get("status", 0))
"""
# Starts the helper with Popen.__del__ made to send SIGTERM to the helper itself once the file argv[1] exists, and to
# give Python the time to run the handler right there, inside the finalizer, which ignores the exit the handler raises.
STOP_IN_A_FINALIZER = r"""
import os, runpy, signal, subprocess, sys
flag, original = sys.argv[1], subprocess.Popen.__del__
def finalizer(self, *args, **kwargs):
    if os.path.exists(flag):
        os.unlink(flag)
        os.kill(os.getpid(), signal.SIGTERM)
        for _ in range(100000):  # Python runs the handler between two of these steps
            pass
    original(self, *args, **kwargs)
subprocess.Popen.__del__ = finalizer
sys.argv = sys.argv[2:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
ENTRIES = ("2026-11-08T02:20:01+0100 sonarr[812]: [Info] Bootstrap: Starting Sonarr\n"
           "2026-11-08T02:20:04+0100 systemd[1]: Started Sonarr Daemon.\n")
TIMEOUT = 3  # seconds the helper gives journalctl in these tests (--journal-timeout)
ARGS = ["--output=short-iso", "--no-hostname", "--all", "--quiet", "--no-pager"]


def gone(pid, wait=2):
    """The process ended within `wait` seconds (a zombie not collected yet counts as ended)."""
    end = time.monotonic() + wait
    while True:
        try:
            with open("/proc/%d/stat" % pid) as f:
                if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                    return True
        except (FileNotFoundError, ProcessLookupError):  # gone before the open, or between the open and the read
            return True
        if time.monotonic() > end:
            return False
        time.sleep(0.05)


class Journal(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        fake_swizzin(self.base)
        self.fake_dir = os.path.join(self.base, "fake")
        os.mkdir(self.fake_dir)
        self.journalctl = os.path.join(self.fake_dir, "journalctl")
        with open(self.journalctl, "w") as f:
            f.write(FAKE.format(python=sys.executable))
        os.chmod(self.journalctl, 0o755)
        self.behave(output=ENTRIES)
        self.conns = []
        self.helper = Helper(self.base, extra=("--journalctl", self.journalctl, "--journal-timeout", str(TIMEOUT)))

    def tearDown(self):
        for c in self.conns:
            c.close()
        self.helper.stop()
        shutil.rmtree(self.base)

    def behave(self, **behaviour):
        with open(os.path.join(self.fake_dir, "behaviour.json"), "w") as f:
            json.dump(behaviour, f)

    def calls(self):
        try:
            with open(os.path.join(self.fake_dir, "calls.log")) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def conn(self):
        c = Conn(self.helper.path)
        self.conns.append(c)
        return c

    def journal(self, unit="sonarr.service", timeout=10, **msg):
        """One read, on the test's own connection (the helper takes 8 clients at most)."""
        if getattr(self, "main", None) is None:
            self.main = self.conn()
        self.main.send(op="journal", unit=unit, **msg)
        return self.main.recv(timeout)

    # --- what it reads -------------------------------------------------------------------------

    def test_the_last_lines_of_a_service(self):
        reply = self.journal(lines=50, requester="tester from 203.0.113.7")
        self.assertEqual(reply, {"ok": True, "lines": ENTRIES.splitlines(), "cut": False, "status": 0})
        (call,) = self.calls()
        # the unit is part of its option, never an argument of its own; nothing else comes from the request
        self.assertEqual(call["argv"], ["--unit=sonarr.service", "--lines=50"] + ARGS)
        self.assertEqual({k: v for k, v in call["env"].items() if k != "LC_CTYPE"}, swizzin_store.JOURNAL_ENV)
        self.assertEqual(call["cwd"], "/")
        self.assertTrue(call["session"])  # its own session: killed with what it starts, never this service
        log = self.helper.stop()
        self.assertIn("journal of sonarr.service (last 50 lines) read for tester from 203.0.113.7", log)
        self.helper = Helper(self.base)  # for tearDown

    def test_the_operation_is_announced(self):
        with self.helper.connect() as s:
            self.assertIn("journal", ask(s, b'{"op": "catalogue"}\n')["features"])

    def test_an_instance_of_a_template(self):
        for unit in ("rtorrent@tester.service", "wg-quick@wg1000.service", "sonarr@puck.service"):
            with self.subTest(unit):
                self.assertTrue(self.journal(unit)["ok"])
                self.assertEqual(self.calls()[-1]["argv"][0], "--unit=" + unit)

    def test_names_that_are_not_a_service_of_the_panel_are_refused(self):
        invalid = ["../x", "../../etc/passwd.service", "x/../sonarr.service", "-x.service", "--all.service",
                   "--unit=ssh.service", "x", "sonarr", "sonarr.socket", "sonarr.service ", " sonarr.service",
                   "sonarr.service\n", "sonarr service.service", "s\u00f6narr.service", "sonarr\u0130.service",
                   "a" * 300 + ".service", "*.service", "s*.service", "sonarr?.service", "[s]onarr.service",
                   "sonarr..service", "rtorrent@...service", "rtorrent@..service", ".service", "", None, 1, True,
                   ["sonarr.service"], {"unit": "sonarr.service"}]
        not_allowed = ["ssh.service", "sshd.service", "cron.service", "docker.service", "systemd-journald.service",
                       "swizzin-store.service", "panel.service", "sonarrx.service", "rtorrent.service",
                       "rtorrent@.service", "rtorrent@a@b.service", "ssh@x.service", "bazarr@@.service"]
        for unit, error in [(u, "invalid") for u in invalid] + [(u, "not-allowed") for u in not_allowed]:
            with self.subTest(unit=unit):
                self.assertEqual(self.journal(unit), {"ok": False, "error": error})
        self.assertEqual(self.calls(), [])  # journalctl never ran
        self.assertTrue(self.journal()["ok"])
        log = self.helper.stop()
        self.assertIn("journal of '../x' refused (invalid) for ?", log)  # the panel never asks for these: noted
        self.assertIn("journal of 'ssh.service' refused (not-allowed) for ?", log)
        self.assertIn("journal of 'sonarr.service\\n' refused (invalid)", log)  # one line in the log, whatever it is
        self.helper = Helper(self.base)  # for tearDown

    def test_the_number_of_lines_is_bounded(self):
        cases = [(0, 1), (-1, 1), (1, 1), (200, 200), (201, 200), (10000, 200), (2 ** 70, 200), ("50", 100),
                 (True, 100), (False, 100), (50.0, 100), (None, 100), ([50], 100)]
        for value, sent in cases:
            with self.subTest(value=value):
                self.assertTrue(self.journal(lines=value)["ok"])
                self.assertEqual(self.calls()[-1]["argv"][1], "--lines=%d" % sent)
        self.journal()
        self.main.s.sendall(b'{"op": "journal", "unit": "sonarr.service", "lines": 1e400}\n')  # infinity
        self.assertTrue(self.main.recv()["ok"])
        self.assertEqual(self.calls()[-1]["argv"][1], "--lines=100")
        self.journal()  # nothing said: 100
        self.assertEqual(self.calls()[-1]["argv"][1], "--lines=100")

    # --- what it sends back ------------------------------------------------------------------

    def test_escape_sequences_and_control_characters_never_reach_the_page(self):
        self.behave(output="".join(line + "\n" for line in (
            "\x1b[31;1mred\x1b[0m and \x1b[38;5;196mmore\x1b[m",  # colours
            "\x1b]0;a new title\x07title",  # the terminal's title
            "\x1b]8;;https://example.invalid/\x1b\\a link\x1b]8;;\x1b\\",  # a hyperlink
            "\x1bPq#0;2;0;0;0\x1b\\sixel",  # a device control string
            "\x1b7saved\x1b8 \x1b(B\x1bc reset",  # short sequences
            "bell\x07 nul\x00 back\x08space cr\rhere tab\tkept del\x7f",
            "c1 \u009b31m csi \u0085 nel",  # the 8-bit forms of CSI and of a new line
            "bidi \u202eevil\u202c \u2066x\u2069 \u200f \u061c",  # characters that reorder text
            "lines \u2028 and \u2029 separators",
            "invalid utf-8 \udcff\udcfe here",
            "a lone escape at the end \x1b",
            "<img src=x onerror=alert(1)> & stays text")))
        reply = self.journal()
        self.assertEqual(reply["lines"], [
            "red and more",
            "title",
            "a link",
            "sixel",
            "saved  reset",
            "bell\ufffd nul\ufffd back\ufffdspace cr\ufffdhere tab\tkept del\ufffd",
            "c1 \ufffd31m csi \ufffd nel",
            "bidi \ufffdevil\ufffd \ufffdx\ufffd \ufffd \ufffd",
            "lines \ufffd and \ufffd separators",
            "invalid utf-8 \ufffd\ufffd here",
            "a lone escape at the end ",
            "<img src=x onerror=alert(1)> & stays text"])
        for line in reply["lines"]:
            self.assertFalse(any(ord(ch) < 32 and ch != "\t" or 0x7f <= ord(ch) <= 0x9f for ch in line), line)

    def test_a_huge_output_keeps_its_newest_lines(self):
        """journalctl may print a lot (a message of several lines, very long ones): only its newest 256 KiB are
        kept, whole lines only, and the answer stays well under what the helper lets a client wait for."""
        self.behave(output="".join("%08d %s\n" % (i, "x" * 90) for i in range(100000)) + "the last line\n")
        reply = self.journal(timeout=30)
        self.assertTrue(reply["ok"] and reply["cut"])
        self.assertEqual(reply["lines"][-1], "the last line")
        self.assertRegex(reply["lines"][0], r"^\d{8} x{90}$")  # a whole line, not the end of one
        self.assertEqual(len(reply["lines"]), swizzin_store.MAX_JOURNAL_ROWS)
        self.assertTrue(self.journal()["ok"])  # and it answers the next one

    def test_long_lines_are_cut_and_the_answer_fits(self):
        """Lines of 999 characters that each take 6 bytes in JSON (\\ufffd), 256 KiB of them: the answer (about 1.5 MB)
        still reaches the panel, under what the helper lets a client wait for (MAX_PENDING_OUTPUT)."""
        self.behave(output="\x01" * 999 + "\n", repeat=600)
        reply = self.journal(timeout=30)
        self.assertTrue(reply["ok"] and reply["cut"])
        self.assertEqual(len(reply["lines"]), swizzin_store.MAX_JOURNAL_OUTPUT // 1000)  # whole ones
        self.assertEqual(set(reply["lines"]), {"\ufffd" * 999})
        self.behave(output="y" * 5000 + "\n")
        self.assertEqual(self.journal()["lines"], ["y" * 1000 + "\u2026"])
        self.behave(output="z" * (3 * swizzin_store.MAX_JOURNAL_OUTPUT))  # one huge line alone: its end is kept
        self.assertEqual(self.journal(timeout=30), {"ok": True, "lines": ["z" * 1000 + "\u2026"], "cut": True,
                                                    "status": 0})

    def test_no_entries_and_errors_of_journalctl(self):
        self.behave(output="")
        self.assertEqual(self.journal(), {"ok": True, "lines": [], "cut": False, "status": 0})
        self.behave(output="No journal files were found.\n", status=1)  # its error output comes with the lines
        self.assertEqual(self.journal(), {"ok": True, "lines": ["No journal files were found."], "cut": False,
                                          "status": 1})

    def test_without_journalctl(self):
        self.helper.stop()
        self.helper, self.main = Helper(self.base, extra=("--journalctl", os.path.join(self.base, "missing"))), None
        self.assertEqual(self.journal(), {"ok": False, "error": "no-journal"})
        os.chmod(self.journalctl, 0o644)  # there, but not a program
        self.helper.stop()
        self.helper, self.main = Helper(self.base, extra=("--journalctl", self.journalctl)), None
        self.assertEqual(self.journal(), {"ok": False, "error": "no-journal"})

    # --- it never holds anything up --------------------------------------------------------------

    def test_a_stuck_journalctl_is_stopped_and_the_helper_answers_meanwhile(self):
        self.behave(output=ENTRIES, sleep=60)
        c = self.conn()
        start = time.monotonic()
        c.send(op="journal", unit="sonarr.service")
        time.sleep(0.3)
        with self.helper.connect() as s:  # meanwhile, the App Store page reads the catalogue
            asked = time.monotonic()
            self.assertTrue(ask(s, b'{"op": "catalogue"}\n')["ok"])
            self.assertLess(time.monotonic() - asked, 1)  # not the 3 s of the read
            self.assertEqual(ask(s, b'{"op": "journal", "unit": "sonarr.service"}\n'),
                             {"ok": False, "error": "busy"})  # one read at a time
        self.assertEqual(c.recv(), {"ok": False, "error": "timeout"})
        self.assertLess(time.monotonic() - start, TIMEOUT + 1.5)
        self.assertTrue(gone(self.calls()[0]["pid"]))
        self.behave(output=ENTRIES)
        self.assertTrue(self.journal()["ok"])  # the next read works

    def test_what_journalctl_started_goes_with_it(self):
        """A process that keeps its output open after journalctl exited: the read ends at the time limit, and that
        process is killed with it (same session)."""
        self.behave(output=ENTRIES, child=True)
        self.assertEqual(self.journal(), {"ok": False, "error": "timeout"})
        with open(os.path.join(self.fake_dir, "child.pid")) as f:
            self.assertTrue(gone(int(f.read())))

    def test_an_installation_goes_on_during_a_read(self):
        """The installer's output keeps coming while journalctl runs (a read that waited for journalctl would hold
        it up to the time limit)."""
        box = os.path.join(self.base, "box")
        with open(box, "w") as f:
            f.write("#!/bin/bash\nfor i in $(seq 1 40); do echo \"step $i\"; sleep 0.05; done; sleep 30\n")
        os.chmod(box, 0o755)
        self.helper.stop()
        self.helper = Helper(self.base, extra=("--journalctl", self.journalctl, "--journal-timeout", str(TIMEOUT),
                                               "--box", box, "--home", self.base))
        self.behave(output=ENTRIES, sleep=60)
        installer = self.conn()
        installer.send(op="install", app="sonarr", rows=30, cols=100)
        self.assertTrue(installer.recv()["ok"])
        installer.wait_output("step 1")
        reader = self.conn()
        reader.send(op="journal", unit="sonarr.service")
        after = max(int(n) for n in re.findall(r"step (\d+)", installer.output)) + 5  # 5 more lines from now on
        installer.wait_output("step %d\r" % after, timeout=2 * TIMEOUT)
        reader.s.setblocking(False)
        with self.assertRaises(BlockingIOError):  # the read is not over: the output came during it
            reader.s.recv(1)
        reader.s.setblocking(True)
        self.assertEqual(reader.recv(), {"ok": False, "error": "timeout"})
        installer.wait_output("step 40")

    def test_a_reader_that_leaves_stops_journalctl(self):
        self.behave(output=ENTRIES, sleep=60)
        c = Conn(self.helper.path)
        c.send(op="journal", unit="sonarr.service")
        for _ in range(50):
            if self.calls():
                break
            time.sleep(0.05)
        c.close()
        self.assertTrue(gone(self.calls()[0]["pid"], wait=1))  # at once, not at the time limit
        self.behave(output=ENTRIES)
        self.assertTrue(self.journal()["ok"])

    def test_stopping_the_helper_stops_journalctl(self):
        self.behave(output=ENTRIES, sleep=60)
        c = self.conn()
        c.send(op="journal", unit="sonarr.service")
        for _ in range(50):
            if self.calls():
                break
            time.sleep(0.05)
        self.helper.stop()
        self.assertTrue(gone(self.calls()[0]["pid"]))
        self.helper = Helper(self.base)  # for tearDown

    def test_sigterm_in_a_finalizer_still_stops_the_helper(self):
        """Python may run the SIGTERM handler inside an object's finalizer (each read drops a Popen), which ignores the
        exit it raises: the helper must stop all the same, as it does elsewhere."""
        flag = os.path.join(self.base, "stop-now")
        self.helper.stop()
        self.helper, self.main = Helper(self.base, extra=("--journalctl", self.journalctl),
                                        launcher=("-c", STOP_IN_A_FINALIZER, flag)), None
        self.assertTrue(self.journal()["ok"])
        open(flag, "w").close()  # the read's Popen is dropped by now, or at the next request: SIGTERM comes then
        self.main.send(op="catalogue")
        try:
            err = self.helper.proc.communicate(timeout=5)[1].decode()
        except subprocess.TimeoutExpired:
            self.helper.proc.kill()
            self.helper.proc.communicate()
            self.fail("SIGTERM in a finalizer did not stop the helper")
        finally:
            self.conns.remove(self.main)
            self.main.close()
        self.assertFalse(os.path.exists(flag))  # the finalizer did send it
        self.assertIn("Exception ignored", err)  # and Python ignored the exit there
        self.assertEqual(self.helper.proc.returncode, 0)
        self.assertFalse(os.path.exists(self.helper.path))  # stopped as usual: its socket removed
        self.helper = Helper(self.base)  # for tearDown


class Units(unittest.TestCase):
    """JOURNAL_UNITS, the helper's copy of the services of the panel's profiles."""

    def profiles(self):
        """{unit or template: [profiles]} of core/profiles.py and core/profiles_shared.py, read as the panel reads
        them (core/util.py): systemd, else the name; False: no service."""
        sys.path.insert(0, ROOT)
        units = {}
        for module in ("core.profiles", "core.profiles_shared"):
            for name, cls in vars(importlib.import_module(module)).items():
                if not (inspect.isclass(cls) and name.endswith("_meta")):
                    continue
                unit = getattr(cls, "systemd", None)
                init = vars(cls).get("__init__")
                if unit is None and init is not None and "self.systemd" in inspect.getsource(init):
                    unit = {"wireguard_meta": "wg-quick@"}[name]  # set for each account: wg-quick@wg<uid>
                elif unit is None:
                    unit = cls.name
                if unit is not False:
                    units.setdefault(unit.partition("@")[0] + "@" if "@" in unit else unit, []).append(name)
        return units

    def test_the_list_follows_the_profiles(self):
        """A profile added or changed: its service must be added to JOURNAL_UNITS (and nothing else is there)."""
        self.assertEqual(sorted(swizzin_store.JOURNAL_UNITS), sorted(self.profiles()))

    def test_each_entry_makes_a_valid_name(self):
        for entry in swizzin_store.JOURNAL_UNITS:
            with self.subTest(entry):
                unit = entry + ("tester" if entry.endswith("@") else "") + ".service"
                self.assertIsNone(swizzin_store.journal_unit(unit))
                if entry.endswith("@") and entry[:-1] not in swizzin_store.JOURNAL_UNITS:  # the template alone
                    self.assertEqual(swizzin_store.journal_unit(entry[:-1] + ".service"), "not-allowed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
