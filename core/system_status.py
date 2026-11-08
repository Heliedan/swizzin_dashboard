"""Reboot required and pending updates: the two pills of the admin's Overview, next to the title of At a glance
(templates/overview/system_status.html, static/js/system_status.js, route /stats/system-status; see the README).

Nothing here needs root, and nothing is written.

Pending updates: what `apt-get -s dist-upgrade` would install, from the package lists apt already has. It is a
simulation, run by the panel's own account: it takes no lock and changes nothing, even while apt or dpkg are at work.
The panel never runs `apt update` (it could not): apt-daily, or the admin, keeps the lists fresh. A run costs 2 to 4
seconds of CPU, at the lowest priority (nice 19). So it never runs during a request: one run at a time, in the
background, started when the admin's Overview asks, and again only once dpkg or apt changed something (the dates of
/var/lib/dpkg/status, /var/lib/apt/lists and /var/cache/apt/pkgcache.bin), at most once a minute. A failed run keeps the
previous count; the next try waits for such a change, or 10 minutes. Without apt-get (another system), no pill.

Reboot required: /run/reboot-required, which update-notifier (Ubuntu) and unattended-upgrades (Ubuntu and Debian)
write when an update needs one, with the packages that asked in /run/reboot-required.pkgs. Where neither is installed
(Debian without unattended-upgrades), a kernel in /boot newer than the one running, of the same flavour (-generic,
-amd64...), says so too; never in a container, nor with a kernel apt does not manage (no /boot/vmlinuz-<running>)."""
import os
import re
import shutil
import subprocess
import threading
import time

from eventlet.patcher import original

# Where apt-get and nice are looked for, and the PATH they get: never the panel's own environment.
PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
TIMEOUT = 60  # seconds: a run takes 2 to 4 of them, up to 10 on a busy server
MIN_INTERVAL = 60  # seconds between two runs, when dpkg or apt keep changing something
RETRY = 600  # seconds before the next try after a failed run, when nothing changed
OLD_LISTS = 7  # days: package lists older than that are said to be old
NAMES = 10  # package names sent to the page, at most, for each pill
# Under eventlet (swizzin.py), a timeout raises the TimeoutExpired of eventlet's own copy of the subprocess module,
# which is not subprocess.TimeoutExpired (as for vnstat in core/util.py): catch both.
_TIMEOUT_EXPIRED = (subprocess.TimeoutExpired, original("subprocess").TimeoutExpired)

# Inst libc-bin [2.31-0ubuntu9.17] (2.31-0ubuntu9.18 Ubuntu:20.04/focal-updates, Ubuntu:20.04/focal-security [amd64])
# Inst openssl (3.5.7-1~deb13u3 Debian-Security:13/stable-security [amd64])   <- newly installed: no [old version]
INST = re.compile(r"^Inst (\S+) (?:\[[^\]]*\] )?\(\S+ ([^)]*)\)")
# 3 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.   (apt 2.0 to 3.0, with LC_ALL=C)
SUMMARY = re.compile(r"^(\d+) upgraded, (\d+) newly installed, .*\bnot upgraded\.$")
PACKAGE = re.compile(r"^[a-z0-9][a-z0-9+.:~-]{0,99}$")  # a Debian package name, an architecture may follow ':'
HOOKS = ("etc/kernel/postinst.d/update-notifier", "etc/kernel/postinst.d/unattended-upgrades")


class Failure(Exception):
    """A run of apt-get that gave no count: the reason, for the panel's log and the details of the pill."""


def updates_check(config, logger):
    """UPDATES_CHECK from swizzin.cfg: True (the default) or False. Any other value counts as True, with a warning in
    the log: a badly written setting must not stop the panel from starting."""
    value = config.get("UPDATES_CHECK", True)
    if isinstance(value, bool):
        return value
    logger.warning("swizzin.cfg: UPDATES_CHECK = %r is invalid (expected True or False), True used instead", value)
    return True


def _security(origins):
    """Whether one of the origins of a version is a security archive: Ubuntu:24.04/noble-security,
    Debian-Security:12/oldstable-security... The archive is what follows the last "/" (or ":", when there is no
    version), as apt writes it."""
    origins = re.sub(r"\s*\[[^\]]*\]\s*$", "", origins)  # the architecture, at the end
    for origin in origins.split(", "):
        archive = re.split(r"[/:]", origin.strip())[-1]
        if archive.endswith("-security"):
            return True
    return False


def parse(output):
    """(number of packages to install or upgrade, names of those that come from a security archive), read from the
    output of `apt-get -s dist-upgrade` in English. Failure when it does not read as one: no summary line, or fewer
    "Inst" lines than the summary counts (an apt that would write them differently must not look up to date)."""
    total, security, counted = 0, [], None
    for line in output.splitlines():
        m = SUMMARY.match(line)
        if m:
            counted = int(m.group(1)) + int(m.group(2))
            continue
        m = INST.match(line)
        if m:
            total += 1
            if _security(m.group(2)):
                security.append(m.group(1))
        elif line.startswith("Inst "):
            raise Failure("unexpected output from apt-get: %r" % line[:120])
    if counted is None:
        raise Failure("unexpected output from apt-get: no summary line")
    if total < counted:
        raise Failure("unexpected output from apt-get: %d packages counted, %d listed" % (counted, total))
    return total, security


def _names(lines):
    """The package names among `lines`, each once, in their order."""
    out = []
    for line in lines:
        name = line.strip()
        if PACKAGE.match(name) and name not in out:
            out.append(name)
    return out


def _kernel(release):
    """(flavour, version numbers) of a kernel release: 6.8.0-149-generic -> ("generic", (6, 8, 0, 149)),
    6.1.0-26-cloud-amd64 -> ("cloud-amd64", (6, 1, 0, 26)), 6.12.9+bpo-amd64 -> ("amd64", (6, 12, 9)). The flavour is
    the parts at the end that start with a letter. None when it does not read so."""
    parts = release.split("-")
    i = len(parts)
    while i > 1 and parts[i - 1][:1].isalpha():
        i -= 1
    version, flavour = "-".join(parts[:i]), "-".join(parts[i:])
    if not flavour or not version[:1].isdigit():
        return None
    return flavour, tuple(int(n) for n in re.findall(r"\d+", version))


def newer_kernel(root, release):
    """The release of a kernel installed in /boot that is newer than `release`, the one running, of the same flavour;
    None when there is none, or when this cannot be told safely: in a container, with a kernel apt does not manage
    (no /boot/vmlinuz-<release>: LXC, the host's own kernel...), or when update-notifier or unattended-upgrades are
    installed (they write /run/reboot-required for a new kernel themselves)."""
    if os.path.exists(os.path.join(root, "run/systemd/container")):
        return None
    if any(os.path.exists(os.path.join(root, hook)) for hook in HOOKS):
        return None
    boot = os.path.join(root, "boot")
    running = _kernel(release)
    if running is None or not os.path.isfile(os.path.join(boot, "vmlinuz-" + release)):
        return None
    try:
        names = os.listdir(boot)
    except OSError:
        return None
    newest = None
    for name in names:
        other = _kernel(name[len("vmlinuz-"):]) if name.startswith("vmlinuz-") else None
        if other and other[0] == running[0] and other[1] > running[1] and (newest is None or other[1] > newest[0]):
            newest = (other[1], name[len("vmlinuz-"):])
    return newest[1] if newest else None


def reboot_status(root="/", release=None):
    """{"required": bool, "packages": [names], "more": int, "kernel": newer kernel or None, "running": kernel or
    None}: whether the system asks for a reboot, and why. Never raises."""
    flag = os.path.join(root, "run/reboot-required")
    try:
        if os.path.exists(flag):
            try:
                with open(flag + ".pkgs", encoding="utf-8", errors="replace") as f:
                    names = _names(f.read(65536).splitlines())
            except OSError:  # no .pkgs: notify-reboot-required was called without a package
                names = []
            return {"required": True, "packages": names[:NAMES], "more": max(0, len(names) - NAMES), "kernel": None,
                    "running": None}
        release = release or os.uname().release
        kernel = newer_kernel(root, release)
    except (OSError, ValueError):
        kernel = None
    return {"required": bool(kernel), "packages": [], "more": 0, "kernel": kernel,
            "running": release if kernel else None}


class PendingUpdates:
    """The count of pending updates, worked out in the background (see above). status() never waits for apt-get."""

    def __init__(self, logger, enabled=True, root="/", clock=time.monotonic):
        self.logger = logger
        self.enabled = enabled
        self.root = root
        self.clock = clock  # for the intervals between runs: the system's clock may jump
        self.lock = threading.Lock()  # green under eventlet (swizzin.py patches threading)
        self.result = None  # {"lists": bool, "total": int, "security": [names]} of the last run that gave one
        self.error = None  # why the last run failed; None when it worked
        self.key = None  # the dates of dpkg and apt files when the last run started
        self.started = None  # clock() when the last run started
        self.running = False

    def _path(self, path):
        return os.path.join(self.root, path)

    def _dates(self):
        """What a count depends on: dpkg's list of installed packages, apt's package lists and its binary cache."""
        out = []
        for path in ("var/lib/dpkg/status", "var/lib/apt/lists", "var/cache/apt/pkgcache.bin"):
            try:
                st = os.stat(self._path(path))
                out.append((st.st_mtime_ns, st.st_ino, st.st_size))
            except OSError:
                out.append(None)
        return tuple(out)

    def _lists_date(self):
        """When apt last refreshed its package lists, as far as the panel's account can tell: the latest date of the
        lists folder, of its partial folder (every apt update touches it, even when nothing changed), and of the
        stamps of apt-daily and update-notifier. None when none of them is there."""
        dates = []
        for path in ("var/lib/apt/lists", "var/lib/apt/lists/partial", "var/lib/apt/periodic/update-success-stamp",
                     "var/lib/apt/periodic/update-stamp"):
            try:
                dates.append(os.stat(self._path(path)).st_mtime)
            except OSError:
                pass
        return max(dates) if dates else None

    def status(self):
        """What the page shows, at once: {"state": ..., "checking": bool, ...}. state is "off" (UPDATES_CHECK =
        False), "no-apt", "checking" (the first run is not over), "error" (no run gave a count yet: "reason" says
        why), "no-lists" (apt has no package lists) or "ok", with "total", "security" (a number), "names" (those of
        the security updates, NAMES at most), "more" (the others), "lists" (when they were refreshed, in seconds since
        1970, or None), "lists_days" and "lists_old". Starts a run in the background when one is due."""
        if not self.enabled:
            return {"state": "off", "checking": False}
        apt = shutil.which("apt-get", path=PATH)
        if apt is None:
            return {"state": "no-apt", "checking": False}
        key = self._dates()
        now = self.clock()
        with self.lock:
            elapsed = None if self.started is None else now - self.started
            if not self.running and (elapsed is None or (key != self.key and elapsed >= MIN_INTERVAL) or
                                     (self.error is not None and elapsed >= RETRY)):
                self.running, self.started = True, now
                try:
                    threading.Thread(target=self._run, args=(apt, key), name="pending-updates", daemon=True).start()
                except Exception as e:  # no thread could be started: try again later
                    self.running, self.key, self.error = False, key, "the check could not start: %s" % e
                    self.logger.warning("Pending updates: %s", self.error)
            result, error, running = self.result, self.error, self.running
        if result is None:
            if running:
                return {"state": "checking", "checking": True}
            return {"state": "error", "reason": error, "checking": False}
        if not result["lists"]:
            return {"state": "no-lists", "checking": running}
        security = result["security"]
        reply = {"state": "ok", "checking": running, "total": result["total"], "security": len(security),
                 "names": security[:NAMES], "more": max(0, len(security) - NAMES)}
        lists = self._lists_date()
        days = None if lists is None else max(0, int((time.time() - lists) // 86400))
        reply.update(lists=lists, lists_days=days, lists_old=days is not None and days >= OLD_LISTS)
        return reply

    def _run(self, apt, key):
        """One run, in the background: never raises (a background thread that fails would say nothing)."""
        try:
            result, error = self._check(apt), None
        except Failure as e:
            result, error = None, str(e)
        except Exception as e:
            result, error = None, "the check failed: %r" % e
        with self.lock:
            # the warning goes out before the check is seen as done: whoever waits for the end finds it logged
            if error is not None and error != self.error:  # once, not at every try
                self.logger.warning("Pending updates: %s", error)
            self.key, self.running, self.error = key, False, error
            if result is not None:
                self.result = result

    def _check(self, apt):
        """{"lists": bool, "total": int, "security": [names]}: what apt-get -s dist-upgrade would install."""
        try:
            lists = any("_Packages" in name for name in os.listdir(self._path("var/lib/apt/lists")))
        except OSError:
            lists = False
        if not lists:  # never downloaded (or removed): apt would find nothing to upgrade, which says nothing
            return {"lists": False, "total": 0, "security": []}
        nice = shutil.which("nice", path=PATH)
        argv = ([nice, "-n", "19"] if nice else []) + [apt, "-s", "-q", "dist-upgrade"]
        try:
            done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=TIMEOUT, env={"PATH": PATH, "LC_ALL": "C"})
        except _TIMEOUT_EXPIRED:
            raise Failure("apt-get gave no answer within %d seconds" % TIMEOUT)
        except OSError as e:
            raise Failure("apt-get could not be run: %s" % e)
        if done.returncode != 0:
            said = [line.strip() for line in done.stderr.decode("utf-8", "replace").splitlines() if line.strip()]
            errors = [line for line in said if line.startswith("E:")]
            raise Failure("apt-get failed: %s" % ((errors or said or ["exit code %d" % done.returncode])[0][:200]))
        total, security = parse(done.stdout.decode("utf-8", "replace"))
        return {"lists": True, "total": total, "security": security}
