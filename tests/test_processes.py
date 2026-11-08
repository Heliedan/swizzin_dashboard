"""Top processes (core/processes.py), without the panel: the reading of /proc, on a fake /proc in a throwaway folder
(the files of the kernel, as it writes them), and on the real one of the machine for the test's own process.

What is checked: the name, the account and the times read from /proc/<pid>/stat (a name with parentheses, spaces or
characters that are not text), kernel threads and zombies left out, a process that ends during the reading, the
share of the processor between two readings, the memory, hidepid, the shared reading kept 3 seconds, the lists of
an account, and that nothing but stat and status is ever opened in the folder of a process: never its command line
nor its environment.

Run from the repository root: python tests/test_processes.py (nothing on the machine is touched)."""
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import processes  # noqa: E402
from core.processes import Process, Processes, Reading, compare, parse_stat  # noqa: E402

PLANTED = "-".join(("hunter2", "do", "not", "show"))  # in the command line and environment of every fake process
ADMIN, FRIEND, WWW = 1000, 1001, 33
NAMES = {0: "root", ADMIN: "tester", FRIEND: "friend", WWW: "www-data"}


def stat_line(pid, name, ticks=0, rss=0, state="S", flags=0x400100, start=5000):
    """/proc/<pid>/stat as the kernel writes it (proc(5)): `ticks` of CPU (utime + stime), `rss` pages."""
    if isinstance(name, str):
        name = name.encode()
    utime = ticks // 2
    rest = [state, 1, pid, pid, 0, -1, flags, 120, 0, 3, 0, utime, ticks - utime, 0, 0, 20, 0, 4, 0, start,
            987654321, rss] + [0] * 28
    return b"%d (%s) %s\n" % (pid, name, " ".join(str(x) for x in rest).encode())


def getpwuid(uid):
    """pwd.getpwuid() for the accounts of these tests."""
    if uid not in NAMES:
        raise KeyError(uid)
    return mock.Mock(pw_name=NAMES[uid])


class FakeProc:
    """A /proc in a throwaway folder: /proc/stat, /proc/meminfo, /proc/self/mountinfo and one folder per process.
    The account of a process is given by owner(), as the owner of its files would be."""

    def __init__(self, cores=4, memory_kib=8 * 1024 * 1024):
        self.root = tempfile.mkdtemp()
        self.uids = {}
        self.cores = cores
        os.makedirs(os.path.join(self.root, "self"))
        self.cpu(0)
        with open(os.path.join(self.root, "meminfo"), "w") as f:
            f.write("MemTotal:       %d kB\nMemFree:          123456 kB\nMemAvailable:    234567 kB\n" % memory_kib)
        self.mountinfo("rw")

    def cpu(self, total):
        """/proc/stat with `total` ticks of the whole processor, spread on its fields and its cores."""
        part = total // 10
        line = lambda name, t: "%s %d 0 %d %d 0 0 0 0 0 0\n" % (name, t // 10 * 3, t // 10, t - t // 10 * 4)
        with open(os.path.join(self.root, "stat"), "w") as f:
            f.write("cpu  %d %d %d %d %d %d %d %d 0 0\n" % (part * 3, part, part, total - part * 9, part, part, part,
                                                            part))
            for n in range(self.cores):
                f.write(line("cpu%d" % n, total // self.cores))
            f.write("intr 12345 0 0\nctxt 6789\nbtime 1794000000\nprocesses 4321\n")

    def mountinfo(self, options, point=None):
        with open(os.path.join(self.root, "self", "mountinfo"), "w") as f:
            f.write("24 29 0:22 / /sys rw,nosuid,nodev,noexec,relatime shared:7 - sysfs sysfs rw\n"
                    "25 29 0:23 / %s rw,nosuid,nodev,noexec,relatime shared:12 - proc proc %s\n"
                    "29 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n" % (
                        point or os.path.realpath(self.root), options))

    def add(self, pid, name, uid=ADMIN, ticks=0, rss=0, status_uid=None, **kw):
        folder = os.path.join(self.root, str(pid))
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "stat"), "wb") as f:
            f.write(stat_line(pid, name, ticks, rss, **kw))
        if status_uid is not None:
            with open(os.path.join(folder, "status"), "w") as f:
                f.write("Name:\t%s\nUmask:\t0022\nState:\tS (sleeping)\nTgid:\t%d\nPid:\t%d\nPPid:\t1\n"
                        "Uid:\t%d\t%d\t%d\t%d\nGid:\t0\t0\t0\t0\n" % (name, pid, pid, 0, status_uid, status_uid,
                                                                      status_uid))
        for secret in ("cmdline", "environ"):
            with open(os.path.join(folder, secret), "wb") as f:
                f.write(("%s\0--password=%s\0TOKEN=%s\0" % (name, PLANTED, PLANTED)).encode())
        self.uids[str(pid)] = uid

    def remove(self, pid):
        shutil.rmtree(os.path.join(self.root, str(pid)))
        self.uids.pop(str(pid), None)

    def owner(self, pid, fd):
        return self.uids[pid]

    def reader(self, **kw):
        kw.setdefault("gap", 0)
        return Processes(self.root, owner=self.owner, **kw)

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


def server(fake):
    """A small seedbox: the admin's apps, a friend's, root's, a worker of nginx and two kernel threads."""
    fake.add(1, "systemd", 0, ticks=900, rss=3000, status_uid=0)
    fake.add(2, "kthreadd", 0, flags=0x208040)
    fake.add(40, "kworker/0:1-events", 0, ticks=5000, flags=0x4208060)
    fake.add(612, "nginx", 0, ticks=40, rss=900, status_uid=WWW)  # a worker: setuid() without exec
    fake.add(700, "rtorrent main", ADMIN, ticks=20000, rss=150000)
    fake.add(701, "autobrr", ADMIN, ticks=3000, rss=12000)
    fake.add(702, "python3", ADMIN, ticks=800, rss=9000)
    fake.add(800, "rtorrent main", FRIEND, ticks=9000, rss=60000)
    fake.add(801, "bash", FRIEND, ticks=2, rss=1200)
    fake.add(802, "defunct", FRIEND, ticks=50, state="Z")


class ProcTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeProc()
        self.addCleanup(self.fake.close)
        accounts = mock.patch.object(processes, "pwd")  # the accounts of these tests, not the machine's
        self.accounts = accounts.start()
        self.addCleanup(accounts.stop)
        self.accounts.getpwuid.side_effect = getpwuid


class ParseStat(unittest.TestCase):
    def test_a_line_of_the_kernel(self):
        self.assertEqual(parse_stat(stat_line(700, "rtorrent main", 2001, 150000, start=777)),
                         (b"rtorrent main", 0x400100, b"S", 2001, 777, 150000))

    def test_a_name_with_parentheses_and_spaces(self):
        """The name ends at the LAST ")": a program can name itself "a) b (c" (prctl, 15 characters)."""
        for name in (b"a) b (c", b") S 1 2 3", b"((x))", b"x)", b"<script>"):
            with self.subTest(name):
                parsed = parse_stat(stat_line(9, name, 10, 20))
                self.assertEqual((parsed[0], parsed[3], parsed[5]), (name, 10, 20))

    def test_what_does_not_read_as_one(self):
        for data in (b"", b"12 noname S 1", b"12 (short) S 1 2 3\n", b"12 (x) S " + b"y " * 30, b"12 x) (S 1"):
            with self.subTest(data):
                self.assertIsNone(parse_stat(data))


class Clean(unittest.TestCase):
    def test_names_shown_as_they_are(self):
        for name in ("rtorrent main", "<img src=x>", "kworker/0:1", "qBittorrent", "Prowlarr.Mai", "\u00e9t\u00e9"):
            self.assertEqual(processes.clean(name.encode()), name)

    def test_what_is_not_text_is_replaced(self):
        """Invalid UTF-8, line breaks, terminal escapes and the characters that turn text around (U+202E)."""
        self.assertEqual(processes.clean(b"a\nb\x1b[31m"), "a\ufffdb\ufffd[31m")
        self.assertEqual(processes.clean("evil\u202etxt.exe".encode()), "evil\ufffdtxt.exe")
        self.assertEqual(processes.clean(b"\xff\xfe ok"), "\ufffd\ufffd ok")
        self.assertEqual(processes.clean(b"tab\there\x7f"), "tab\ufffdhere\ufffd")


class Amount(unittest.TestCase):
    def test_as_the_panel_writes_sizes(self):
        """The units and the steps of GetHumanReadableBi (core/util.py), with one decimal."""
        cases = [(0, "0.0 B"), (1024, "1024.0 B"), (1025, "1.0 KiB"), (614400000, "585.9 MiB"),
                 (3 * 1024 ** 3, "3.0 GiB"), (5 * 1024 ** 4, "5.0 TiB"), (2 * 1024 ** 5, "2.0 PiB")]
        for size, text in cases:
            self.assertEqual(processes.amount(size), text)

    def test_the_panel_is_not_imported(self):
        """core/util.py imports swizzin.py, which patches the whole process (eventlet): not for these tests."""
        self.assertNotIn("swizzin", sys.modules)
        self.assertNotIn("core.util", sys.modules)


class Read(ProcTest):
    def test_what_is_read(self):
        server(self.fake)
        reading = processes.read(self.fake.root, self.fake.owner)
        found = {pid: p for (pid, start), p in reading.processes.items()}
        self.assertEqual(sorted(found), ["1", "612", "700", "701", "702", "800", "801"])  # no kernel thread, no zombie
        self.assertEqual(found["700"], Process("rtorrent main", ADMIN, 20000, 150000 * processes.PAGE))
        self.assertEqual(found["612"].uid, WWW)  # owner root: the Uid: line of its status says
        self.assertEqual(found["1"].uid, 0)
        self.assertEqual((reading.cores, reading.memory, reading.hidden), (4, 8 * 1024 ** 3, False))
        self.assertEqual(reading.total, 0)

    def test_root_without_a_status_stays_root(self):
        self.fake.add(5, "gone-status", 0, ticks=1)  # no status file: it ended between the two reads
        reading = processes.read(self.fake.root, self.fake.owner)
        self.assertEqual([p.uid for p in reading.processes.values()], [0])

    def test_the_owner_of_the_file_by_default(self):
        """Without a function of the test: the owner of /proc/<pid>/stat, here the account that runs the test."""
        self.fake.add(5, "mine", ticks=1)
        reading = processes.read(self.fake.root)
        self.assertEqual([p.uid for p in reading.processes.values()], [os.geteuid()])  # as root: no status, so 0

    def test_a_process_that_ends_during_the_reading(self):
        """Listed, then gone: no stat file any more (ENOENT), or the kernel's "No such process" (ESRCH)."""
        server(self.fake)
        os.remove(os.path.join(self.fake.root, "701", "stat"))
        real_open = open

        def dying(path, *a, **kw):
            if path.endswith(os.path.join("702", "stat")):
                raise ProcessLookupError(3, "No such process")
            return real_open(path, *a, **kw)
        with mock.patch.object(processes, "open", dying, create=True):
            reading = processes.read(self.fake.root, self.fake.owner)
        self.assertEqual(sorted(pid for pid, _ in reading.processes), ["1", "612", "700", "800", "801"])
        self.assertFalse(reading.hidden)

    def test_other_entries_and_unreadable_lines_are_skipped(self):
        server(self.fake)
        os.makedirs(os.path.join(self.fake.root, "sys", "kernel"))
        os.makedirs(os.path.join(self.fake.root, "999"))
        with open(os.path.join(self.fake.root, "999", "stat"), "wb") as f:
            f.write(b"999 (broken")
        self.fake.uids["999"] = ADMIN
        reading = processes.read(self.fake.root, self.fake.owner)
        self.assertNotIn("999", [pid for pid, _ in reading.processes])
        self.assertEqual(len(reading.processes), 7)

    def test_no_proc(self):
        self.assertIsNone(processes.read(os.path.join(self.fake.root, "nothing")))

    def test_without_proc_stat_nor_meminfo(self):
        """/proc mounted with subset=pid: the processes only."""
        server(self.fake)
        os.remove(os.path.join(self.fake.root, "stat"))
        os.remove(os.path.join(self.fake.root, "meminfo"))
        reading = processes.read(self.fake.root, self.fake.owner)
        self.assertEqual((reading.total, reading.cores, reading.memory), (None, os.cpu_count() or 1, None))
        self.assertEqual(len(reading.processes), 7)

    def test_pauses_while_reading(self):
        """time.sleep(0) every 200 processes: a green pause under eventlet (tests/test_processes_page.py)."""
        for pid in range(100, 550):
            self.fake.add(pid, "p%d" % pid, ticks=1)
        with mock.patch.object(processes.time, "sleep") as sleep:
            reading = processes.read(self.fake.root, self.fake.owner)
        self.assertEqual(len(reading.processes), 450)
        self.assertEqual(sleep.call_args_list, [mock.call(0)] * 2)

    def test_never_the_command_line_nor_the_environment(self):
        """Every file opened under /proc while making the lists: stat, status of root's processes, and the files of
        the whole system. What the command lines and environments hold shows nowhere."""
        server(self.fake)
        opened = []
        real_open = open

        def spy(path, *a, **kw):
            opened.append(os.path.relpath(path, self.fake.root))
            return real_open(path, *a, **kw)
        reader = self.fake.reader()
        with mock.patch.object(processes, "open", spy, create=True):
            answer = json.dumps([reader.top(), reader.top(FRIEND)])
        self.assertNotIn(PLANTED, answer)
        self.assertNotIn("password", answer)
        per_process = {os.path.basename(p) for p in opened if p.split(os.sep)[0].isdigit()}
        self.assertEqual(per_process, {"stat", "status"})
        self.assertEqual({p for p in opened if not p.split(os.sep)[0].isdigit()},
                         {"stat", "meminfo", os.path.join("self", "mountinfo")})
        self.assertEqual(sorted({p.split(os.sep)[0] for p in opened if p.endswith("status")}), ["1", "612"])


class Hidepid(ProcTest):
    def hidden(self):
        return processes.read(self.fake.root, self.fake.owner).hidden

    def test_mount_options(self):
        self.fake.add(5, "mine")
        cases = [("rw", False), ("rw,hidepid=0", False), ("rw,hidepid=off", False), ("rw,hidepid=1", True),
                 ("rw,hidepid=2", True), ("rw,hidepid=invisible", True), ("rw,hidepid=noaccess", True),
                 ("rw,hidepid=ptraceable,subset=pid", True), ("rw,subset=pid", False),
                 ("rw,hidepid=2,gid=%d" % os.getegid(), False), ("rw,hidepid=2,gid=4242424", True)]
        for options, hidden in cases:
            with self.subTest(options), mock.patch.object(processes.os, "geteuid", return_value=1000):
                self.fake.mountinfo(options)
                self.assertEqual(self.hidden(), hidden)

    def test_root_sees_everything(self):
        self.fake.mountinfo("rw,hidepid=invisible")
        with mock.patch.object(processes.os, "geteuid", return_value=0):
            self.assertFalse(self.hidden())

    def test_only_the_mount_of_this_proc(self):
        self.fake.mountinfo("rw,hidepid=2", point="/somewhere/else")
        with mock.patch.object(processes.os, "geteuid", return_value=1000):
            self.assertFalse(self.hidden())
        os.remove(os.path.join(self.fake.root, "self", "mountinfo"))
        self.assertFalse(self.hidden())

    def test_a_process_that_cannot_be_read(self):
        """hidepid=1 (noaccess): the folder is listed, its stat refused. The others are still read."""
        server(self.fake)
        real_open = open

        def refused(path, *a, **kw):
            if path.endswith(os.path.join("800", "stat")):
                raise PermissionError(13, "Permission denied")
            return real_open(path, *a, **kw)
        with mock.patch.object(processes, "open", refused, create=True):
            reading = processes.read(self.fake.root, self.fake.owner)
        self.assertTrue(reading.hidden)
        self.assertEqual(len(reading.processes), 6)


def reading(at, total, cores=4, memory=1000, **procs):
    """A Reading made by hand: procs name=(uid, ticks, rss[, start])."""
    out = {}
    for name, values in procs.items():
        uid, ticks, rss = values[:3]
        start = values[3] if len(values) > 3 else 1
        out[(name, start)] = Process(name, uid, ticks, rss)
    return Reading(at, out, total, cores, memory, False)


class Compare(unittest.TestCase):
    def shares(self, before, after):
        return {r.name: round(r.cpu, 3) for r in compare(before, after).rows}

    def test_share_of_the_whole_processor(self):
        """4 cores, 5 seconds: 2000 ticks of the whole processor. One core kept busy (500 ticks) is 25 %."""
        before = reading(10, 100000, a=(1, 1000, 1), b=(1, 50, 1), c=(1, 7, 1))
        after = reading(15, 102000, a=(1, 1500, 1), b=(1, 70, 1), c=(1, 7, 1))
        self.assertEqual(self.shares(before, after), {"a": 25.0, "b": 1.0, "c": 0.0})
        self.assertEqual(compare(before, after).seconds, 5)

    def test_a_new_process_counts_all_its_time(self):
        """Started between the readings, or a pid given to another process (another start): all its time."""
        before = reading(10, 1000, a=(1, 400, 1, 50))
        after = reading(11, 1400, a=(1, 40, 1, 90), n=(1, 100, 1))
        self.assertEqual(self.shares(before, after), {"a": 10.0, "n": 25.0})

    def test_never_more_than_all_nor_less_than_nothing(self):
        before = reading(10, 1000, a=(1, 500, 1), b=(1, 0, 1))
        after = reading(11, 1100, a=(1, 400, 1), b=(1, 900, 1))
        self.assertEqual(self.shares(before, after), {"a": 0.0, "b": 100.0})

    def test_without_proc_stat(self):
        """The time that passed, on every core: 2 s on 4 cores at 100 ticks a second."""
        before = reading(10, None, a=(1, 100, 1))
        after = reading(12, None, a=(1, 300, 1))
        with mock.patch.object(processes, "TICKS", 100):
            self.assertEqual(self.shares(before, after), {"a": 25.0})
        frozen = reading(12, 1000, a=(1, 300, 1))  # a /proc/stat that did not move: the same
        with mock.patch.object(processes, "TICKS", 100):
            self.assertEqual(self.shares(reading(10, 1000, a=(1, 100, 1)), frozen), {"a": 25.0})

    def test_memory(self):
        after = reading(11, 1100, memory=4000, a=(1, 0, 1000))
        self.assertEqual(compare(reading(10, 1000), after).rows[0].memory, 25.0)
        after = reading(11, 1100, memory=None, a=(1, 0, 1000))
        self.assertIsNone(compare(reading(10, 1000), after).rows[0].memory)


class Shared(ProcTest):
    def test_the_lists(self):
        server(self.fake)
        reader = self.fake.reader()
        reader.top()
        self.fake.cpu(4000)  # 10 s on 4 cores, but the clock says otherwise: the shares come from /proc/stat
        for pid, name, uid, ticks, rss in ((700, "rtorrent main", ADMIN, 21000, 150000),
                                           (701, "autobrr", ADMIN, 3020, 12000),
                                           (800, "rtorrent main", FRIEND, 9400, 60000)):
            self.fake.add(pid, name, uid, ticks=ticks, rss=rss)
        reader.shown = None  # as when the 3 seconds are over
        data = reader.top()
        mib = processes.PAGE / 1024 / 1024
        self.assertEqual((data["state"], data["own"], data["hidden"], data["cores"], data["count"]),
                         ("ok", False, False, 4, 7))
        self.assertEqual([(r["name"], r["user"], r["cpu"]) for r in data["cpu"]],
                         [("rtorrent main", "tester", 25.0), ("rtorrent main", "friend", 10.0),
                          ("autobrr", "tester", 0.5)])  # only those that used the processor
        self.assertEqual([(r["name"], r["user"]) for r in data["memory"]],
                         [("rtorrent main", "tester"), ("rtorrent main", "friend"), ("autobrr", "tester"),
                          ("python3", "tester"), ("systemd", "root")])  # 5 at most
        self.assertEqual(data["memory"][0]["memory"], "%.1f MiB" % (150000 * mib))
        self.assertEqual(data["memory"][0]["memory_share"], round(150000 * processes.PAGE / (8 * 1024 ** 3) * 100, 3))
        self.assertEqual(reader.top(FRIEND)["memory"][1]["memory_share"], 0.057)  # bash: "<0.1%" on the page
        friend = reader.top(FRIEND)
        self.assertEqual((friend["own"], friend["count"]), (True, 2))
        self.assertEqual([(r["name"], r["user"]) for r in friend["cpu"]], [("rtorrent main", "friend")])
        self.assertEqual([r["name"] for r in friend["memory"]], ["rtorrent main", "bash"])
        nobody = reader.top(4242)
        self.assertEqual((nobody["count"], nobody["cpu"], nobody["memory"]), (0, [], []))
        self.assertEqual({row["user"] for row in reader.top(WWW)["memory"]}, {"www-data"})

    def test_an_account_without_a_name(self):
        self.fake.add(5, "in-a-container", 100999, rss=10)
        self.assertEqual(self.fake.reader().top()["memory"][0]["user"], "100999")

    def test_one_reading_shared_and_kept(self):
        """The first look: two readings, 1 second apart (gap). Then one reading at most every `keep` seconds, whoever
        asks; after `fresh` seconds without one, two again."""
        server(self.fake)
        reader = self.fake.reader(keep=0.3, gap=0.1, fresh=1)
        began = time.monotonic()
        first = reader.top()
        self.assertGreaterEqual(time.monotonic() - began, 0.1)
        self.assertEqual(reader.readings, 2)
        # the gap between the two readings, rounded to 0.1 s: 0.1 on a quiet machine, a little more under load (CI)
        self.assertGreaterEqual(first["seconds"], 0.1)
        self.assertLess(first["seconds"], 0.6)
        for uid in (None, FRIEND, ADMIN, None):
            reader.top(uid)
        self.assertEqual(reader.readings, 2)
        time.sleep(0.35)
        since = reader.top()["seconds"]  # since the last reading: 0.35 s and what the machine adds
        self.assertGreaterEqual(since, 0.3)
        self.assertLess(since, 1.0)
        self.assertEqual(reader.readings, 3)
        time.sleep(1.1)
        reader.top()
        self.assertEqual(reader.readings, 5)

    def test_many_at_the_same_time(self):
        """20 requests at once (threads): one look, two readings, the same lists for all."""
        server(self.fake)
        reader = self.fake.reader(gap=0.2)
        answers = []
        threads = [threading.Thread(target=lambda: answers.append(json.dumps(reader.top()))) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((reader.readings, len(answers), len(set(answers))), (2, 20, 1))

    def test_proc_unreadable(self):
        logger = logging.getLogger("test-processes")
        reader = Processes(os.path.join(self.fake.root, "nothing"), gap=0, logger=logger)
        with self.assertLogs(logger, "WARNING") as logs:
            self.assertEqual(reader.top(), {"state": "unreadable", "own": False})
            self.assertEqual(reader.top(FRIEND), {"state": "unreadable", "own": True})
            logger.warning("end")
        self.assertEqual(len(logs.records), 2)  # said once, then the line of this test
        self.assertIn("cannot be read", logs.output[0])

    def test_names_of_accounts_kept_a_while(self):
        server(self.fake)
        reader = self.fake.reader()
        reader.top()
        reader.shown = None
        reader.top()
        self.assertEqual(sorted(c.args[0] for c in self.accounts.getpwuid.call_args_list), [0, ADMIN, FRIEND])


class RealProc(unittest.TestCase):
    """The /proc of the machine that runs the test (in the automatic tests: their container's): this process is
    found, with its name and its account."""

    @unittest.skipUnless(os.path.isfile("/proc/self/stat"), "no /proc here")
    def test_this_process(self):
        with open("/proc/self/comm", "rb") as f:
            comm = processes.clean(f.read().rstrip(b"\n"))
        found = {pid: p for (pid, _), p in processes.read().processes.items()}
        me = found[str(os.getpid())]
        self.assertEqual((me.name, me.uid), (comm, os.geteuid()))
        self.assertGreater(me.rss, 0)
        reader = Processes(gap=0.05)
        data = reader.top(os.geteuid())
        self.assertEqual((data["state"], data["own"], reader.readings), ("ok", True, 2))
        self.assertGreaterEqual(data["count"], 1)
        self.assertGreaterEqual(data["cores"], 1)


class Setting(unittest.TestCase):
    def test_true_false_or_invalid(self):
        logger = logging.getLogger("test-processes-setting")
        self.assertIs(processes.setting({}), True)
        self.assertIs(processes.setting({"TOP_PROCESSES": False}), False)
        for value in ("False", 0, None, "no"):
            with self.subTest(value), self.assertLogs(logger, "WARNING") as logs:
                self.assertIs(processes.setting({"TOP_PROCESSES": value}, logger), True)
            self.assertIn("TOP_PROCESSES = %r is invalid" % value, logs.output[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
