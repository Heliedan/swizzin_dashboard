"""The journal reader of the Logs buttons (journal-helper/swizzin_journal.py): which units it reads, the exact
journalctl it runs, the limits it keeps, who it answers, and its systemd units (not root, hardened, socket for the
panel's group only).

The reader runs as systemd starts it (swizzin-journal.socket, Accept=yes): one process per connection, the connection
as its standard input and output. Reader() below does the same without root, on a temporary socket, with a fake
journalctl (--journalctl) that notes how it was started and answers as each test says. The list of units it reads is
checked against the panel's profiles here too.
Run from the repository root: python tests/test_journal_helper.py"""
import importlib
import inspect
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READER = os.path.join(ROOT, "journal-helper", "swizzin_journal.py")
UNITS = os.path.join(ROOT, "journal-helper")
sys.path.insert(0, os.path.dirname(READER))
import swizzin_journal  # noqa: E402

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
ENTRIES = ("2026-11-08T02:20:01+0100 sonarr[812]: [Info] Bootstrap: Starting Sonarr\n"
           "2026-11-08T02:20:04+0100 systemd[1]: Started Sonarr Daemon.\n")
TIMEOUT = 3  # seconds the reader gives journalctl in these tests (--journal-timeout)
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


class Reader:
    """The reader as systemd runs it (Accept=yes): a socket, and for each connection a process with the connection as
    its standard input and output. Its log (standard error) goes to a file."""

    def __init__(self, base, allow_uid=None, extra=()):
        self.path = os.path.join(base, "journal.sock")
        self.log_path = os.path.join(base, "reader.log")
        uid = os.getuid() if allow_uid is None else allow_uid
        self.argv = [sys.executable, "-I", "-B", READER, "--allow-uid", str(uid), *extra]
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        self.listener.listen(8)
        self.procs = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:  # stop() closed it
                return
            with conn, open(self.log_path, "ab") as log:
                self.procs.append(subprocess.Popen(self.argv, stdin=conn.fileno(), stdout=conn.fileno(), stderr=log,
                                                   close_fds=True))

    def ask(self, raw, timeout=10):
        """The answer to these bytes (a JSON object), or None when the reader closed the connection without one."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(self.path)
            s.sendall(raw)
            s.shutdown(socket.SHUT_WR)  # all of it: the reader does not wait for more
            data = b""
            while True:
                try:
                    chunk = s.recv(65536)
                except ConnectionResetError:  # closed without reading the request
                    break
                if not chunk:
                    break
                data += chunk
        return json.loads(data) if data else None

    def journal(self, unit="sonarr.service", timeout=10, **msg):
        return self.ask(json.dumps(dict(op="journal", unit=unit, **msg)).encode() + b"\n", timeout)

    def log(self):
        for proc in self.procs:
            proc.wait(10)
        try:
            with open(self.log_path) as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def stop(self):
        self.listener.close()
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


class ReaderTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base)
        self.fake_dir = os.path.join(self.base, "fake")
        os.mkdir(self.fake_dir)
        self.journalctl = os.path.join(self.fake_dir, "journalctl")
        with open(self.journalctl, "w") as f:
            f.write(FAKE.format(python=sys.executable))
        os.chmod(self.journalctl, 0o755)
        self.behave(output=ENTRIES)
        self.reader = self.start()

    def start(self, **kwargs):
        kwargs.setdefault("extra", ("--journalctl", self.journalctl, "--journal-timeout", str(TIMEOUT)))
        self.readers = getattr(self, "readers", 0) + 1
        folder = os.path.join(self.base, "reader%d" % self.readers)
        os.mkdir(folder)
        reader = Reader(folder, **kwargs)
        self.addCleanup(reader.stop)
        return reader

    def behave(self, **behaviour):
        with open(os.path.join(self.fake_dir, "behaviour.json"), "w") as f:
            json.dump(behaviour, f)

    def calls(self):
        try:
            with open(os.path.join(self.fake_dir, "calls.log")) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def journal(self, unit="sonarr.service", **msg):
        return self.reader.journal(unit, **msg)


class Journal(ReaderTest):
    # --- what it reads -------------------------------------------------------------------------

    def test_the_last_lines_of_a_service(self):
        reply = self.journal(lines=50, requester="tester from 203.0.113.7")
        self.assertEqual(reply, {"ok": True, "lines": ENTRIES.splitlines(), "cut": False, "status": 0})
        (call,) = self.calls()
        # the unit is part of its option, never an argument of its own; nothing else comes from the request
        self.assertEqual(call["argv"], ["--unit=sonarr.service", "--lines=50"] + ARGS)
        self.assertEqual({k: v for k, v in call["env"].items() if k != "LC_CTYPE"}, swizzin_journal.JOURNAL_ENV)
        self.assertEqual(call["cwd"], "/")
        self.assertTrue(call["session"])  # its own session: killed with what it starts
        self.assertIn("journal of sonarr.service (last 50 lines) read for tester from 203.0.113.7", self.reader.log())

    def test_hello_says_what_it_does(self):
        """install.sh's check: an answer, and no line in the log."""
        self.assertEqual(self.reader.ask(b'{"op": "hello"}\n'), {"ok": True, "features": ["journal"]})
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.reader.log(), "")

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
                       "swizzin-store.service", "swizzin-journal@x.service", "panel.service", "sonarrx.service",
                       "rtorrent.service", "rtorrent@.service", "rtorrent@a@b.service", "ssh@x.service",
                       "bazarr@@.service"]
        for unit, error in [(u, "invalid") for u in invalid] + [(u, "not-allowed") for u in not_allowed]:
            with self.subTest(unit=unit):
                self.assertEqual(self.journal(unit), {"ok": False, "error": error})
        self.assertEqual(self.calls(), [])  # journalctl never ran
        self.assertTrue(self.journal()["ok"])
        log = self.reader.log()
        self.assertIn("journal of '../x' refused (invalid) for ?", log)  # the panel never asks for these: noted
        self.assertIn("journal of 'ssh.service' refused (not-allowed) for ?", log)
        self.assertIn("journal of 'sonarr.service\\n' refused (invalid)", log)  # one line in the log, whatever it is

    def test_requests_that_are_not_one(self):
        for raw in (b"", b"\n", b"not json\n", b"[1, 2]\n", b'"journal"\n', b'{"op": "catalogue"}\n',
                    b'{"op": "install", "app": "sonarr"}\n', b'{"unit": "sonarr.service"}\n',
                    b'{"op": "journal", "unit": "sonarr.service"', b"x" * (70 * 1024) + b"\n"):
            with self.subTest(raw=raw[:40]):
                self.assertEqual(self.reader.ask(raw), {"ok": False, "error": "bad-request"})
        self.assertEqual(self.calls(), [])

    def test_one_request_per_connection(self):
        """Only the first line is read; the connection ends with its answer."""
        raw = b'{"op": "journal", "unit": "sonarr.service"}\n{"op": "journal", "unit": "radarr.service"}\n'
        self.assertTrue(self.reader.ask(raw)["ok"])
        self.assertEqual([c["argv"][0] for c in self.calls()], ["--unit=sonarr.service"])

    def test_the_number_of_lines_is_bounded(self):
        cases = [(0, 1), (-1, 1), (1, 1), (200, 200), (201, 200), (10000, 200), (2 ** 70, 200), ("50", 100),
                 (True, 100), (False, 100), (50.0, 100), (None, 100), ([50], 100)]
        for value, sent in cases:
            with self.subTest(value=value):
                self.assertTrue(self.journal(lines=value)["ok"])
                self.assertEqual(self.calls()[-1]["argv"][1], "--lines=%d" % sent)
        self.assertTrue(self.reader.ask(b'{"op": "journal", "unit": "sonarr.service", "lines": 1e400}\n')["ok"])
        self.assertEqual(self.calls()[-1]["argv"][1], "--lines=100")  # infinity
        self.journal()  # nothing said: 100
        self.assertEqual(self.calls()[-1]["argv"][1], "--lines=100")

    # --- who it answers --------------------------------------------------------------------------

    def test_only_the_panel_and_root(self):
        """Another account (the socket's group lets only the panel's in; the reader checks again): no answer, nothing
        read. Checked on serve() itself: run as root (as in the automatic tests), every connection is root's."""
        class Conn:
            sent = []
            def sendall(self, data):  # noqa: E301
                self.sent.append(data)
            def settimeout(self, t):  # noqa: E301
                pass
            def shutdown(self, how):  # noqa: E301
                pass
        opts = swizzin_journal.options(["--allow-uid", "1000", "--journalctl", self.journalctl])
        for uid, answered in ((1000, True), (0, True), (1001, False), (65534, False)):
            with self.subTest(uid=uid):
                conn, Conn.sent = Conn(), []
                with mock.patch.object(swizzin_journal, "peer_uid", return_value=uid), \
                        mock.patch.object(swizzin_journal, "read_request",
                                          return_value={"op": "journal", "unit": "sonarr.service"}) as read, \
                        self.assertLogs("swizzin-journal", "INFO") as logs:
                    swizzin_journal.serve(opts, conn)
                self.assertEqual((bool(Conn.sent), read.called), (answered, answered))
                if not answered:
                    self.assertIn("connection refused: uid %d is not the panel's" % uid, logs.output[0])
        self.assertEqual(len(self.calls()), 2)
        if os.getuid() != 0:  # and through a real socket
            other = self.start(allow_uid=os.getuid() + 12345)
            self.assertIsNone(other.journal())
            self.assertIn("connection refused: uid %d is not the panel's" % os.getuid(), other.log())
            self.assertEqual(len(self.calls()), 2)

    def test_the_panel_user_by_its_name(self):
        """Without --allow-uid: root and the panel's account, found by its name (swizzin on a real server)."""
        import pwd
        me = pwd.getpwuid(os.getuid()).pw_name
        opts = swizzin_journal.options(["--panel-user", me])
        self.assertEqual(swizzin_journal.allowed_uids(opts), {0, os.getuid()})
        opts = swizzin_journal.options(["--panel-user", "no-such-user-here"])
        self.assertEqual(swizzin_journal.allowed_uids(opts), {0})  # no panel: root only
        self.assertEqual(swizzin_journal.options([]).panel_user, "swizzin")

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
        kept, whole lines only."""
        self.behave(output="".join("%08d %s\n" % (i, "x" * 90) for i in range(100000)) + "the last line\n")
        self.reader = self.start(extra=("--journalctl", self.journalctl, "--journal-timeout", "25"))  # 10 MB: a busy CI
        reply = self.journal(timeout=30)
        self.assertTrue(reply["ok"] and reply["cut"])
        self.assertEqual(reply["lines"][-1], "the last line")
        self.assertRegex(reply["lines"][0], r"^\d{8} x{90}$")  # a whole line, not the end of one
        self.assertEqual(len(reply["lines"]), swizzin_journal.MAX_JOURNAL_ROWS)

    def test_long_lines_are_cut(self):
        """Lines of 999 characters that each take 6 bytes in JSON (\\ufffd), 256 KiB of them: the answer (about 1.5 MB)
        still reaches the panel, which reads up to 4 MiB."""
        self.behave(output="\x01" * 999 + "\n", repeat=600)
        self.reader = self.start(extra=("--journalctl", self.journalctl, "--journal-timeout", "25"))  # a busy CI
        reply = self.journal(timeout=30)
        self.assertTrue(reply["ok"] and reply["cut"])
        self.assertEqual(len(reply["lines"]), swizzin_journal.MAX_JOURNAL_OUTPUT // 1000)  # whole ones
        self.assertEqual(set(reply["lines"]), {"\ufffd" * 999})
        self.behave(output="y" * 5000 + "\n")
        self.assertEqual(self.journal()["lines"], ["y" * 1000 + "\u2026"])
        self.behave(output="z" * (3 * swizzin_journal.MAX_JOURNAL_OUTPUT))  # one huge line alone: its end is kept
        self.assertEqual(self.journal(timeout=30), {"ok": True, "lines": ["z" * 1000 + "\u2026"], "cut": True,
                                                    "status": 0})

    def test_no_entries_and_errors_of_journalctl(self):
        self.behave(output="")
        self.assertEqual(self.journal(), {"ok": True, "lines": [], "cut": False, "status": 0})
        self.behave(output="No journal files were found.\n", status=1)  # its error output comes with the lines
        self.assertEqual(self.journal(), {"ok": True, "lines": ["No journal files were found."], "cut": False,
                                          "status": 1})

    def test_without_journalctl(self):
        missing = self.start(extra=("--journalctl", os.path.join(self.base, "missing")))
        self.assertEqual(missing.journal(), {"ok": False, "error": "no-journal"})
        os.chmod(self.journalctl, 0o644)  # there, but not a program
        self.assertEqual(self.journal(), {"ok": False, "error": "no-journal"})
        self.assertEqual(swizzin_journal.JOURNALCTL, ("/usr/bin/journalctl", "/bin/journalctl"))

    # --- it never lingers ---------------------------------------------------------------------

    def test_a_stuck_journalctl_is_stopped(self):
        self.behave(output=ENTRIES, sleep=60)
        start = time.monotonic()
        self.assertEqual(self.journal(), {"ok": False, "error": "timeout"})
        self.assertLess(time.monotonic() - start, TIMEOUT + 1.5)
        self.assertTrue(gone(self.calls()[0]["pid"]))
        self.assertIn("journal of sonarr.service: no end within 3 seconds", self.reader.log())
        self.behave(output=ENTRIES)
        self.assertTrue(self.journal()["ok"])  # the next read works

    def test_what_journalctl_started_goes_with_it(self):
        """A process that keeps its output open after journalctl exited: the read ends at the time limit, and that
        process is killed with it (same session)."""
        self.behave(output=ENTRIES, child=True)
        self.assertEqual(self.journal(), {"ok": False, "error": "timeout"})
        with open(os.path.join(self.fake_dir, "child.pid")) as f:
            self.assertTrue(gone(int(f.read())))

    def test_a_reader_whose_panel_left_ends(self):
        """The panel gave up (its own time limit): the reader still ends, journalctl with it."""
        self.behave(output=ENTRIES, sleep=60)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(self.reader.path)
            s.sendall(b'{"op": "journal", "unit": "sonarr.service"}\n')
            for _ in range(50):
                if self.calls():
                    break
                time.sleep(0.05)
        for proc in self.reader.procs:
            proc.wait(TIMEOUT + 5)
        self.assertTrue(gone(self.calls()[0]["pid"]))


class Units(unittest.TestCase):
    """JOURNAL_UNITS, the reader's copy of the services of the panel's profiles."""

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
        self.assertEqual(sorted(swizzin_journal.JOURNAL_UNITS), sorted(self.profiles()))

    def test_each_entry_makes_a_valid_name(self):
        for entry in swizzin_journal.JOURNAL_UNITS:
            with self.subTest(entry):
                unit = entry + ("tester" if entry.endswith("@") else "") + ".service"
                self.assertIsNone(swizzin_journal.journal_unit(unit))
                if entry.endswith("@") and entry[:-1] not in swizzin_journal.JOURNAL_UNITS:  # the template alone
                    self.assertEqual(swizzin_journal.journal_unit(entry[:-1] + ".service"), "not-allowed")

    def test_the_panel_asks_with_the_same_rule(self):
        with open(os.path.join(ROOT, "core", "service_logs.py")) as f:
            self.assertIn('\nUNIT_NAME = re.compile(r"%s")\n' % swizzin_journal.UNIT_NAME.pattern, f.read())


def unit_lines(name):
    """The settings of a unit file: [(section, key, value)], comments left out."""
    section, found = None, []
    with open(os.path.join(UNITS, name)) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("["):
                section = line.strip("[]")
                continue
            key, _, value = line.partition("=")
            found.append((section, key, value))
    return found


class SystemdUnits(unittest.TestCase):
    """What the units promise: a socket for root and the panel's group, a reader that is not root, hardened, that
    writes nothing, and runs only with this edition's panel, like the App Store service."""

    MARKER = "/opt/swizzin/journal-helper/swizzin_journal.py"

    def settings(self, name):
        found = {}
        for section, key, value in unit_lines(name):
            found.setdefault((section, key), []).append(value)
        return found

    def test_the_socket(self):
        s = self.settings("swizzin-journal.socket")
        self.assertEqual(s[("Socket", "ListenStream")], ["/run/swizzin-journal/journal.sock"])
        self.assertEqual(s[("Socket", "Accept")], ["yes"])
        self.assertEqual(s[("Socket", "RemoveOnStop")], ["yes"])  # nothing left in /run once it is removed
        self.assertEqual((s[("Socket", "SocketUser")], s[("Socket", "SocketGroup")], s[("Socket", "SocketMode")]),
                         (["root"], ["swizzin"], ["0660"]))
        self.assertEqual(s[("Install", "WantedBy")], ["sockets.target"])
        with open(os.path.join(ROOT, "core", "service_logs.py")) as f:  # where the panel connects
            self.assertIn('\nSOCKET = "%s"' % s[("Socket", "ListenStream")][0], f.read())

    def test_the_reader_is_not_root_and_is_hardened(self):
        s = self.settings("swizzin-journal@.service")
        self.assertEqual(s[("Service", "User")], ["swizzin-journal"])
        self.assertEqual(s[("Service", "SupplementaryGroups")], ["systemd-journal"])
        self.assertEqual(s[("Service", "ExecStart")],
                         ["/usr/bin/python3 -I -B /usr/local/lib/swizzin-journal/swizzin_journal.py"])
        self.assertEqual((s[("Service", "StandardInput")], s[("Service", "StandardOutput")]), (["socket"], ["socket"]))
        for key, value in (("NoNewPrivileges", "yes"), ("ProtectSystem", "strict"), ("ProtectHome", "yes"),
                           ("PrivateTmp", "yes"), ("PrivateDevices", "yes"), ("PrivateNetwork", "yes"),
                           ("RestrictAddressFamilies", "AF_UNIX"), ("CapabilityBoundingSet", ""),
                           ("SystemCallFilter", "@system-service"), ("RestrictNamespaces", "yes"),
                           ("MemoryDenyWriteExecute", "yes"), ("IPAddressDeny", "any")):
            with self.subTest(key):
                self.assertIn(value, s[("Service", key)])
        self.assertEqual(s[("Service", "ReadOnlyPaths")], ["/run -/dev/shm -/dev/mqueue"])
        for key in ("ReadWritePaths", "StateDirectory", "LogsDirectory", "CacheDirectory", "RuntimeDirectory",
                    "AmbientCapabilities", "DynamicUser", "PermissionsStartOnly"):
            self.assertNotIn(("Service", key), {k for k in s if s[k] != [""]})  # nothing to write, no capability

    def test_only_with_this_edition(self):
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", self.MARKER[len("/opt/swizzin/"):]], cwd=ROOT,
                                 capture_output=True)
        self.assertEqual(tracked.returncode, 0, "not tracked by git: box upgrade panel would not delete it")
        for name in ("swizzin-journal.socket", "swizzin-journal@.service"):
            s = self.settings(name)
            self.assertEqual(s[("Unit", "ConditionPathIsSymbolicLink")], ["!" + self.MARKER])
            self.assertEqual(s[("Unit", "ConditionFileNotEmpty")], [self.MARKER])
        with open(os.path.join(UNITS, "install.sh")) as f:
            self.assertIn("\nMARKER=%s\n" % self.MARKER, f.read())

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is not installed")
    def test_systemd_reads_every_line(self):
        """A misspelt setting would be ignored with a mere warning. ProtectProc= and ProcSubset= come with systemd 247:
        older ones (Ubuntu 20.04) say they ignore them, nothing else."""
        r = subprocess.run(["systemd-analyze", "verify", os.path.join(UNITS, "swizzin-journal.socket")],
                           capture_output=True, text=True, timeout=60)
        about_it = [line for line in (r.stdout + r.stderr).splitlines() if "swizzin-journal" in line
                    and not re.search(r"Unknown (lvalue|key name) '(ProtectProc|ProcSubset)'", line)]
        self.assertEqual(about_it, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
