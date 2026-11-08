"""Top processes: the programs that use the most CPU and memory, a section of the Details of Overview
(templates/overview/processes.html, static/js/processes.js, route /stats/processes; see the README).

Read from /proc, without root and without running anything (no ps, no top). For each process: /proc/<pid>/stat only
(its name, CPU time, memory and flags) and the owner of that file, which is the account the process runs as. A
process that changed its account without starting a new program (the workers of nginx or php-fpm) shows root as that
owner: for those only, the "Uid:" line of /proc/<pid>/status says which account it runs as. Never its command line
nor its environment: they can hold passwords or tokens. The name is the one the kernel keeps (15 characters at most),
as top shows it; control and formatting characters are replaced.

CPU: the share of the whole processor a program used between two readings, counted as the CPU tile counts it (the
time of the "cpu" line of /proc/stat): a program that keeps one core busy out of 8 shows 12.5 %, where top would say
100 %. Memory: its resident memory (RSS), which counts what it shares with others (libraries...), and its share of
the total memory. Kernel threads (PF_KTHREAD) and zombies are left out; a process that ends during a reading is
skipped.

Cost: one read of each /proc/<pid>/stat, a few milliseconds for a few hundred processes, and only when a browser asks:
the page asks every 5 seconds while the section is open, never when it is closed or hidden, and nothing runs in the
background. One reading at a time, shared by every browser and account, and kept 3 seconds. The panel is one process
under eventlet: the reading pauses every 200 processes (a green sleep), so that the live cards and the other requests
go on meanwhile. The first time, or when the last reading is more than 30 seconds old, two readings 1 second apart
(a green sleep too): CPU time is a difference.

Who sees what: the admin, every program; any other account, shared server or not, the programs of the Linux account
of the same name only, and none without one. When /proc is mounted with hidepid (or the panel runs with
ProtectProc=), the panel's own account sees its own processes only: the answer says so. In a container (LXC...),
the processes of the container."""
import os
import pwd
import re
import threading
import time
import unicodedata
from collections import namedtuple

import flask

PROC = "/proc"
KEEP = 3  # seconds a reading is shared before a new one is made
GAP = 1  # seconds between the two readings of a first look
FRESH = 30  # seconds: an older reading is not compared with (two new ones instead)
PAUSE_EVERY = 200  # processes read between two pauses, which let the other green threads run
TOP = 5  # programs in each list
PF_KTHREAD = 0x00200000  # the flag of a kernel thread (include/linux/sched.h)
GONE = (b"Z", b"X")  # zombie, dead: a process that ended and was not yet reaped
NAME_RENEW = 60  # seconds the names of the accounts are kept before being asked again
try:
    PAGE = os.sysconf("SC_PAGE_SIZE")
    TICKS = os.sysconf("SC_CLK_TCK")
except (ValueError, OSError):  # not Linux: never in use there
    PAGE, TICKS = 4096, 100
UID = re.compile(rb"^Uid:\s+\d+\s+(\d+)", re.M)  # /proc/<pid>/status: real, effective, saved, file system
ESCAPE = re.compile(r"\\([0-7]{3})")  # /proc/self/mountinfo writes a space as \040

# One process, as read: its name, its account (effective uid), the CPU time it used since it started (in ticks of
# 1/TICKS second, all its threads) and its resident memory (in bytes).
Process = namedtuple("Process", "name uid ticks rss")
# One look at every process: when (time.monotonic()), {(pid, start): Process}, the CPU time of the whole processor
# (ticks, or None), the number of cores, the total memory (bytes, or None), and whether other accounts' are hidden.
Reading = namedtuple("Reading", "at processes total cores memory hidden")
# What the lists are made of: each process with its share of the processor between two readings, over `seconds`.
Shown = namedtuple("Shown", "at seconds rows cores hidden")
Row = namedtuple("Row", "name uid cpu rss memory")


def setting(config, logger=None):
    """TOP_PROCESSES from swizzin.cfg: True (the default) or False. Any other value counts as True, with a warning in
    the log: a badly written setting must not stop the panel from starting."""
    value = config.get("TOP_PROCESSES", True)
    if isinstance(value, bool):
        return value
    if logger is not None:
        logger.warning("swizzin.cfg: TOP_PROCESSES = %r is invalid (expected True or False), True used instead", value)
    return True


def amount(size):
    """A size in bytes as the panel writes it (GetHumanReadableBi of core/util.py), with one decimal: "585.9 MiB".
    Not imported from there: core/util.py imports the whole panel."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size <= 1024:
            break
        size /= 1024.0
    else:
        unit = "PiB"
    return "%.1f %s" % (size, unit)


def clean(raw):
    """The name of a program as text for the page: invalid UTF-8, control characters and formatting characters (which
    could turn the text around, or hide part of it) replaced by U+FFFD."""
    name = raw.decode("utf-8", "replace")
    if name.isprintable():  # nearly always: nothing to replace
        return name
    return "".join("\ufffd" if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") else c for c in name)


def parse_stat(data):
    """(name, flags, state, ticks, start, rss pages) from the text of /proc/<pid>/stat, or None when it does not read
    as one. The name is between the first "(" and the LAST ")": it may hold parentheses and spaces itself."""
    first, last = data.find(b"("), data.rfind(b")")
    if first < 0 or last < first:
        return None
    fields = data[last + 2:].split()
    # fields[0] is field 3 of proc(5): state, then ... 9 flags, 14 utime, 15 stime, 22 starttime, 24 rss
    try:
        return (data[first + 1:last], int(fields[6]), fields[0], int(fields[11]) + int(fields[12]), int(fields[19]),
                int(fields[21]))
    except (IndexError, ValueError):
        return None


def file_owner(pid, fd):
    """The account a process runs as, from the owner of its open /proc/<pid>/stat."""
    return os.fstat(fd).st_uid


def hidepid(root):
    """Whether this /proc hides other accounts' processes from the panel: mounted with hidepid (1 or noaccess, 2 or
    invisible, 4 or ptraceable), and the panel neither root nor in the group named by gid=. Read in
    /proc/self/mountinfo, the last mount on that folder; False when it cannot tell."""
    options = None
    try:
        with open(os.path.join(root, "self", "mountinfo"), "rb") as f:
            for line in f.read().decode("utf-8", "replace").splitlines():
                mount, _, rest = line.partition(" - ")
                fields, kind = mount.split(), rest.split()
                if len(fields) > 4 and len(kind) > 2 and kind[0] == "proc" and \
                        ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), fields[4]) == os.path.realpath(root):
                    options = dict(o.partition("=")[::2] for o in kind[2].split(","))
    except OSError:
        return False
    if not options or options.get("hidepid", "0") in ("0", "off"):
        return False
    if os.geteuid() == 0:
        return False
    gid = options.get("gid", "")
    return not (gid.isdigit() and (int(gid) == os.getegid() or int(gid) in os.getgroups()))


def read(root=PROC, owner=file_owner):
    """One look at every process of `root`: a Reading, or None when the folder cannot be listed. Pauses every
    PAUSE_EVERY processes (time.sleep(0): a green one under eventlet, which lets the rest of the panel run)."""
    try:
        names = os.listdir(root)
    except OSError:
        return None
    processes, denied, seen = {}, False, 0
    for pid in names:
        if not pid.isdigit():
            continue
        seen += 1
        if seen % PAUSE_EVERY == 0:
            time.sleep(0)
        try:
            with open(os.path.join(root, pid, "stat"), "rb", buffering=0) as f:
                uid = owner(pid, f.fileno())
                data = f.read()
        except PermissionError:  # hidepid=1 (noaccess): listed, but not readable
            denied = True
            continue
        except OSError:  # it ended meanwhile (ENOENT, ESRCH), or something else: skipped
            continue
        parsed = parse_stat(data)
        if parsed is None:
            continue
        raw, flags, state, ticks, start, pages = parsed
        if flags & PF_KTHREAD or state in GONE:
            continue
        if uid == 0:  # root, or a process that changed its account without a new program: its status says
            uid = status_uid(root, pid)
        processes[(pid, start)] = Process(clean(raw), uid, ticks, pages * PAGE)
    total, cores = cpu_time(root)
    return Reading(time.monotonic(), processes, total, cores, memory_total(root), denied or hidepid(root))


def status_uid(root, pid):
    """The effective account of a process from the Uid: line of /proc/<pid>/status; 0 when it cannot be read."""
    try:
        with open(os.path.join(root, pid, "status"), "rb", buffering=0) as f:
            m = UID.search(f.read())
    except OSError:
        return 0
    return int(m.group(1)) if m else 0


def cpu_time(root):
    """(time of the whole processor in ticks, number of cores) from /proc/stat: the sum of the first eight fields of
    its "cpu" line, as the CPU tile counts it (core/custom/live_stats.py), and its "cpuN" lines. (None, cores from
    the system) when it cannot be read (/proc mounted with subset=pid...)."""
    total, cores = None, 0
    try:
        with open(os.path.join(root, "stat"), "rb") as f:
            for line in f:
                if not line.startswith(b"cpu"):
                    break
                fields = line.split()
                if fields[0] == b"cpu":
                    total = sum(int(x) for x in fields[1:9])
                else:
                    cores += 1
    except (OSError, ValueError):
        total = None
    return total, cores or os.cpu_count() or 1


def memory_total(root):
    """MemTotal of /proc/meminfo in bytes, or None when it cannot be read."""
    try:
        with open(os.path.join(root, "meminfo"), "rb") as f:
            for line in f:
                if line.startswith(b"MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def compare(before, after):
    """Shown: each process of `after` with the share of the whole processor it used since `before`, in %. A process
    that `before` does not have (started meanwhile, or its pid given to a new one) counts all its time."""
    seconds = max(after.at - before.at, 0.001)
    span = after.total - before.total if after.total is not None and before.total is not None else 0
    if span <= 0:  # /proc/stat not readable: the time that passed, on every core
        span = seconds * TICKS * after.cores
    rows = []
    for key, p in after.processes.items():
        old = before.processes.get(key)
        used = p.ticks - old.ticks if old is not None else p.ticks
        cpu = min(max(used, 0) / span * 100, 100.0)
        memory = p.rss / after.memory * 100 if after.memory else None
        rows.append(Row(p.name, p.uid, cpu, p.rss, memory))
    return Shown(after.at, seconds, rows, after.cores, after.hidden)


class Processes:
    """The readings of /proc, shared by every request: one at a time, kept `keep` seconds."""

    def __init__(self, root=PROC, owner=file_owner, keep=KEEP, gap=GAP, fresh=FRESH, logger=None):
        self.root, self.owner, self.keep, self.gap, self.fresh, self.logger = root, owner, keep, gap, fresh, logger
        self.lock = threading.Lock()  # green under eventlet: waiting here holds up nothing else
        self.last = None  # the latest Reading
        self.shown = None  # the latest Shown, given to every request for `keep` seconds
        self.readings = 0  # how many readings were made (for the tests)
        self.failed = False  # /proc could not be listed last time (logged once)
        self.names = {}  # uid: (name of the account, when it was asked)

    def _read(self):
        self.readings += 1
        reading = read(self.root, self.owner)
        if reading is None and not self.failed and self.logger is not None:
            self.logger.warning("Top processes: %s cannot be read", self.root)
        self.failed = reading is None
        return reading

    def look(self):
        """The latest Shown, made again when it is `keep` seconds old; None when /proc cannot be read."""
        with self.lock:
            now = time.monotonic()
            if self.shown is not None and now - self.shown.at < self.keep:
                return self.shown
            before = self.last
            if before is None or now - before.at > self.fresh:
                before = self._read()
                if before is None:
                    self.last = self.shown = None
                    return None
                time.sleep(self.gap)
            after = self._read()
            if after is None:
                self.last = self.shown = None
                return None
            self.last, self.shown = after, compare(before, after)
            return self.shown

    def account(self, uid):
        """The name of the account `uid`, or its number when it has none (a container's own accounts...)."""
        now = time.monotonic()
        known = self.names.get(uid)
        if known is None or now - known[1] > NAME_RENEW:
            try:
                name = pwd.getpwuid(uid).pw_name
            except (KeyError, OverflowError):
                name = str(uid)
            known = self.names[uid] = (name, now)
        return known[0]

    def top(self, uid=None, count=TOP):
        """What /stats/processes answers: every program (uid None, the admin) or those of the account `uid`, the
        `count` that used the most CPU (those that used some) and the most memory."""
        shown = self.look()
        if shown is None:
            return {"state": "unreadable", "own": uid is not None}
        mine = [r for r in shown.rows if uid is None or r.uid == uid]
        by_cpu = sorted((r for r in mine if r.cpu > 0), key=lambda r: (-r.cpu, -r.rss, r.name))[:count]
        by_memory = sorted((r for r in mine if r.rss > 0), key=lambda r: (-r.rss, -r.cpu, r.name))[:count]

        def row(r):  # the shares in %, rounded by the page: "<0.1%" for a small one, never "0.0%"
            return {"name": r.name, "user": self.account(r.uid), "cpu": round(r.cpu, 3), "memory": amount(r.rss),
                    "memory_share": round(r.memory, 3) if r.memory is not None else None}
        return {"state": "ok", "own": uid is not None, "hidden": shown.hidden, "cores": shown.cores,
                "seconds": round(shown.seconds, 1), "count": len(mine), "cpu": [row(r) for r in by_cpu],
                "memory": [row(r) for r in by_memory]}


def register(app, htpasswd, reader=None):
    """Add GET /stats/processes, unless swizzin.cfg says TOP_PROCESSES = False; app.config["TOP_PROCESSES"] then says
    whether the section shows (templates/overview/processes.html). Runs once per import of swizzin.py, which is
    imported twice at startup: nothing is read here."""
    on = setting(app.config, app.logger)
    app.config["TOP_PROCESSES"] = on
    if not on:
        return
    app.extensions["processes"] = reader or Processes(logger=app.logger)
    bp = flask.Blueprint("processes", __name__)

    @bp.route("/stats/processes")
    @htpasswd.required
    def processes(user):
        # The admin sees every program; another account those of its Linux account only, and none without one:
        # nothing is read for them then.
        if user == app.config["ADMIN_USER"]:
            data = app.extensions["processes"].top()
        else:
            try:
                uid = pwd.getpwnam(user).pw_uid
            except (KeyError, ValueError):
                data = {"state": "no-account", "own": True}
            else:
                data = app.extensions["processes"].top(uid)
        response = flask.jsonify(data)
        response.headers["Cache-Control"] = "no-store"
        return response

    app.register_blueprint(bp)
