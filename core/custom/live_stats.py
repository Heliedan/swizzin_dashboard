"""Live statistics of the dashboard: CPU, RAM and disk, sent every second over the panel's websocket.

They feed the CPU Info, RAM Info and Disk Info cards, updated every second. No process is read: the load
average and the top programs are not shown.
This file lives in core/custom/ so that the panel's update keeps it. See the README (Live cards).
"Disk almost full" alert: the level of each disk is computed here, the banner is shown by
static/js/live_stats.js. Thresholds DISK_WARN_PERCENT and DISK_CRIT_PERCENT in swizzin.cfg.
Mounts without a /dev/ device (ZFS, NFS...) are followed live too, read apart (class Probe),
as are the disks mounted inside one of them (function mounts_read_apart)."""
import itertools
import os
import time
from eventlet.patcher import original
import core.util as util  # module import only: core.util and swizzin.py import each other at startup


DISK_RESCAN_INTERVAL = 30  # seconds between two reads of the list of disks
DEFAULT_THRESHOLDS = (90, 95)  # % used: warning (orange) then critical (red), can be set in swizzin.cfg
NO_ANSWER_TIMEOUT = 5  # seconds: a mount without a /dev/ device that does not answer within this delay is unavailable
_Thread = original('threading').Thread  # a real system thread: swizzin.py replaces threading with eventlet


def read_thresholds(config, logger=None):
    """(warning, critical) in % from swizzin.cfg (DISK_WARN_PERCENT, DISK_CRIT_PERCENT).

    0 turns that alert level off. An invalid value (text, negative, more than 100...) falls back to the default,
    with a warning in the log: a badly written setting must not stop the panel from starting.
    A warning greater than or equal to the critical level would never show: orange is turned off, red stays,
    with a warning in the log."""
    out = []
    for key, default in zip(('DISK_WARN_PERCENT', 'DISK_CRIT_PERCENT'), DEFAULT_THRESHOLDS):
        value = config.get(key, default)
        try:
            if isinstance(value, bool):  # True would mean 1 %
                raise ValueError(value)
            threshold = float(value)
            if not 0 <= threshold <= 100:  # also rules out nan and inf
                raise ValueError(value)
        except (TypeError, ValueError, OverflowError):  # OverflowError: an integer too large for float()
            if logger is not None:
                logger.warning("swizzin.cfg: %s = %r is invalid (expected: 0 to 100), %s used instead",
                               key, value, default)
            threshold = default
        out.append(threshold)
    warning, critical = out
    if warning and critical and warning >= critical:
        if logger is not None:
            logger.warning("swizzin.cfg: DISK_WARN_PERCENT (%g) is not below DISK_CRIT_PERCENT (%g): "
                           "orange alert turned off, red from %g %%", warning, critical, critical)
        warning = 0
    return warning, critical


def alert_level(pct, thresholds):
    """'critical', 'warning' or None for a disk that is `pct` % used.

    A threshold of 0 is turned off. When both thresholds are reached, critical wins."""
    warning, critical = thresholds
    if critical and pct >= critical:
        return "critical"
    if warning and pct >= warning:
        return "warning"
    return None


def _cpu():
    """Counters of /proc/stat: {'cpu': [user, nice, system, idle, iowait, irq, softirq, steal], 'cpu0': [...], ...}."""
    out = {}
    with open('/proc/stat') as f:
        for line in f:
            if not line.startswith('cpu'):
                break
            fields = line.split()
            values = [int(x) for x in fields[1:9]]
            out[fields[0]] = values + [0] * (8 - len(values))
    return out


def _breakdown(before, after):
    """Usage percentages between two readings of the same line of /proc/stat."""
    user, nice, system, idle, iowait, irq, softirq, steal = (b - a for a, b in zip(before, after))
    total = (user + nice + system + idle + iowait + irq + softirq + steal) or 1
    return {
        "used": (total - idle - iowait) / total * 100,
        "user": (user + nice) / total * 100,
        "system": (system + irq + softirq) / total * 100,
        "iowait": iowait / total * 100,
        "steal": steal / total * 100,
    }, total


def mount_devices(mounts=None):
    """{mount point: name of its device in /proc/diskstats, or None without a /dev/ device (ZFS, NFS...)}.

    mounts: the ones to sort, by default those the panel shows. Only the last mount of a path in /proc/mounts
    counts, the one that is seen. A path missing from /proc/mounts has no device."""
    out = dict.fromkeys(util.get_mounts() if mounts is None else mounts)
    with open('/proc/mounts') as f:
        for line in f:
            dev, mnt = line.split()[:2]
            mnt = util.unescape_mount(mnt)
            if mnt in out:
                out[mnt] = os.path.basename(os.path.realpath(dev)) if dev.startswith('/dev/') else None
    return out


def mounts_read_apart(devs):
    """The mounts of `devs` (mount_devices()) to read apart, by the Probe: those without a /dev/ device, and those
    under a mount without a /dev/ device or on the same path (a USB disk mounted in an NFS share: /mnt/nas/usb),
    according to the whole of /proc/mounts, including the mounts the panel does not show (sshfs...).

    To reach /mnt/nas/usb, the system crosses /mnt/nas: if the share stops answering, the path lookup waits
    forever, before the disk is even read. These disks keep their device in `devs`, for their read and write
    speeds. When in doubt (the order of /proc/mounts), a disk is read apart: this is safe, it is read the same
    way, in a thread."""
    no_dev = set()
    with open('/proc/mounts') as f:
        for line in f:
            dev, mnt = line.split()[:2]
            if not dev.startswith('/dev/'):
                no_dev.add(util.unescape_mount(mnt))
    return [m for m, dev in devs.items()
            if dev is None or any(m == p or m.startswith(p.rstrip('/') + '/') for p in no_dev)]


class Probe:
    """Usage of the mounts without a /dev/ device (ZFS datasets, NFS network shares...) and of the disks mounted
    inside one of them (mounts_read_apart), read apart.

    On a network share that stopped answering, statvfs() waits forever. Under eventlet (swizzin.py), that wait
    would freeze the whole panel, live cards loop included. So each reading runs in a real system thread, only one
    at a time per mount. With no answer for `timeout` seconds, the mount is unavailable.
    Nothing can interrupt a blocked thread: it stays blocked until the share answers, but no other one is
    started for that mount meanwhile."""

    def __init__(self, timeout=NO_ANSWER_TIMEOUT):
        self.timeout = timeout
        self._numbers = itertools.count(1)
        self._started = {}  # mount: (number, start time) of its last reading started
        self._finished = {}  # mount: (number, result) of its last reading finished, written by its thread

    def _read(self, mnt, number):
        try:
            if not os.path.ismount(mnt):  # unmounted: the empty folder would give the numbers of the parent disk
                raise OSError("no longer mounted")
            result = util.disk_usage(mnt)
        except Exception:  # unreadable: the thread still gives its answer
            result = None
        self._finished[mnt] = (number, result)  # a single write, without a lock: atomic in Python

    def _running(self, mnt):
        started = self._started.get(mnt)
        return started is not None and self._finished.get(mnt, (None,))[0] != started[0]

    def _stuck(self, mnt):
        return self._running(mnt) and time.monotonic() - self._started[mnt][1] >= self.timeout

    def read(self, mounts, wait=False):
        """{mount: (size, used, free, %), or None: unreadable, unmounted, no answer for `timeout` s, or
        no reading possible (a thread could not be started: process limit reached)}.

        Starts a reading for each mount that has none running. Without waiting (live cards loop), gives the
        last answer received. When waiting (route /stats/disk): the one running, until the call + `timeout` seconds
        at most, even if the live cards loop starts a new reading of a slow share meanwhile."""
        start, no_thread = time.monotonic(), set()
        for mnt in mounts:
            if not self._running(mnt):
                number = next(self._numbers)
                try:
                    _Thread(target=self._read, args=(mnt, number), daemon=True).start()
                except RuntimeError:  # process limit (pids.max...): no answer, without silencing CPU and RAM
                    no_thread.add(mnt)
                    continue
                self._started[mnt] = (number, start)  # after start(): a thread that did not start is started again
        while wait and time.monotonic() < start + self.timeout and any(
                self._running(m) and not self._stuck(m) for m in mounts):
            time.sleep(0.01)  # green sleep under eventlet: the panel goes on during the wait
        return {m: None if m in no_thread or self._stuck(m) else self._finished.get(m, (None, None))[1]
                for m in mounts}


PROBE = Probe()  # one for both the live cards loop and the /stats/disk route: at most one blocked thread per mount


def _disks():
    """{device: (sectors read, sectors written)}; a sector of /proc/diskstats is always 512 bytes."""
    out = {}
    with open('/proc/diskstats') as f:
        for line in f:
            c = line.split()
            if len(c) > 9:
                out[c[2]] = (int(c[5]), int(c[9]))
    return out


def _ram():
    """Same fields and same calculations as the panel's /stats/ram route."""
    m = {}
    with open('/proc/meminfo') as f:
        for line in f:
            key, value = line.split(':', 1)
            m[key] = int(value.split()[0])
    total, available = m['MemTotal'], m['MemAvailable']
    return {"ramtotal": util.GetHumanReadableKiB(total), "ramfree": util.GetHumanReadableKiB(available),
            "ramused": util.GetHumanReadableKiB(total - available),
            "perutil": '{0:.2f}'.format((total - available) / total * 100)}


def _model():
    """Name and frequency of the processor (read only once)."""
    name, mhz = "", 0.0
    with open('/proc/cpuinfo') as f:
        for line in f:
            if line.startswith('model name') and not name:
                name = line.split(':', 1)[1].strip()
            elif line.startswith('cpu MHz') and not mhz:
                mhz = float(line.split(':', 1)[1])
    return name, ('{0:.2f} GHz'.format(mhz / 1000) if mhz else "")


def measure(before, after, duration, model, freq, devs, thresholds=DEFAULT_THRESHOLDS, probed=None):
    """Content of the "live" event from two readings (cpu, disks) taken `duration` seconds apart.

    Each readable disk carries its alert level ("alert") according to `thresholds`,
    for the "disk almost full" banner.
    The mounts read apart (mounts_read_apart) are not read here: `probed` gives their usage (Probe.read). Those
    without a /dev/ device (None in `devs`) have no read and write speeds (nothing in /proc/diskstats).
    A disk mounted in a network share keeps its own."""
    (cpu_a, disks_a), (cpu_b, disks_b) = before, after
    total, _ = _breakdown(cpu_a['cpu'], cpu_b['cpu'])
    cores = sorted((k for k in cpu_b if k != 'cpu'), key=lambda k: int(k[3:]))
    ncpu = len(cores) or os.cpu_count() or 1
    f = '{0:.1f}'.format
    disks = {}
    probed = probed or {}
    for mnt, dev in devs.items():
        if dev is None or mnt in probed:  # ZFS, NFS, a disk in a share: read apart, a dead share freezes nothing
            usage = probed.get(mnt)
        else:
            try:
                if not os.path.ismount(mnt):  # unmounted: the empty folder would give the numbers of the parent disk
                    raise OSError("no longer mounted")
                usage = util.disk_usage(mnt)
            except OSError:
                usage = None
        if usage is None:  # an unreadable mount must not freeze the CPU and RAM cards
            disks[mnt] = {"unavailable": True}  # the card shows "—" instead of stale numbers
            continue
        size, used, free, pct = usage
        disks[mnt] = {"disktotal": size, "diskused": used, "diskfree": free, "perutil": pct,
                      "read": None, "write": None,  # without a /dev/ device: the card shows "—"
                      "alert": alert_level(float(pct), thresholds)}
        if dev is not None:
            sectors_read, sectors_written = (b - a for a, b in zip(disks_a.get(dev, (0, 0)), disks_b.get(dev, (0, 0))))
            disks[mnt]["read"] = util.GetHumanReadableBi(sectors_read * 512 / duration) + "/s"
            disks[mnt]["write"] = util.GetHumanReadableBi(sectors_written * 512 / duration) + "/s"
    return {
        "cpu": {"used": f(total["used"]), "idle": f(100 - total["used"]), "cores": ncpu, "model": model, "freq": freq,
                "user": f(total["user"]), "system": f(total["system"]), "iowait": f(total["iowait"]),
                "steal": f(total["steal"]),
                "percore": [f(_breakdown(cpu_a[k], cpu_b[k])[0]["used"]) for k in cores if k in cpu_a]},
        "ram": _ram(),
        "disks": disks,
    }


def live_stats(app, socketio):
    """Background task started at the first connection to the websocket: one reading per second, sent to every browser."""
    with app.app_context():
        model, freq = _model()
        thresholds = read_thresholds(app.config, app.logger)  # swizzin.cfg is only read when the panel starts
        devs, rescan_at = mount_devices(), time.monotonic() + DISK_RESCAN_INTERVAL
        apart = mounts_read_apart(devs)
        before, t_before = None, None
        while True:
            try:
                if time.monotonic() >= rescan_at:  # a disk plugged in or removed while the panel runs
                    devs = mount_devices()
                    apart, rescan_at = mounts_read_apart(devs), time.monotonic() + DISK_RESCAN_INTERVAL
                after, t_after = (_cpu(), _disks()), time.monotonic()
                probed = PROBE.read(apart)  # never waits
                if before is not None:
                    socketio.emit('live', measure(before, after, max(t_after - t_before, 0.001), model, freq, devs,
                                                  thresholds, probed), namespace='/websocket')
                before, t_before = after, t_after
            except Exception:
                app.logger.exception("live_stats: reading failed")
                before = None
            socketio.sleep(1)
