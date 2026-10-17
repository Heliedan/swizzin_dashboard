#!/usr/bin/python3
"""Uninstalls one app for the App Store. swizzin_store.py runs it as root in the job's terminal.

Steps, each checked before the next one, with the state saved after each (--state) and the outcome written
at the end (--result) for the service:
  check   nothing is touched yet: Swizzin's files are the audited ones, apt and dpkg are idle, apt would remove
          nothing but the app's own packages, every path is safe to copy and delete, there is room for the copy
  stop    the app's units are stopped and disabled (their state is noted, to put them back if needed)
  copy    the app's settings are saved in an archive, then read back and checked
  remove  the app's own Swizzin remove script runs (a private copy of the audited bytes), in Swizzin's
          environment, as box would run it, but without box's system-wide apt autoremove
  fix     what Swizzin's scripts forget: units left behind, nginx not reloaded
  verify  each point is checked on the real system: the result says exactly what is done and what is not

Until the remove script starts, a problem (or the user's Stop) puts everything back as it was: nothing is removed.
Once it started, nothing can stop it: Ctrl+C and a lost terminal are ignored.
Standard library only. The table of apps is removable.py, next to this file.
"""
import argparse
import codecs
import fcntl
import fnmatch
import hashlib
import io
import json
import os
import pty
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I leaves this folder out; it belongs to root
import removable  # noqa: E402

USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
ANSI = re.compile(r"\x1b(\[[0-9;?]*[ -/]*[@-~]|\][^\x07]*\x07|[()][A-Z0-9])")
APT_LOCKS = ("/var/lib/dpkg/lock-frontend", "/var/lib/dpkg/lock", "/var/cache/apt/archives/lock",
             "/var/lib/apt/lists/lock")
APT_UNITS = ("apt-daily.service", "apt-daily-upgrade.service")
APT_PROCESSES = {"apt", "apt-get", "aptitude", "dpkg", "box"}
MARGIN = 256 * 1024 * 1024  # free space kept on top of the copy


class Refused(Exception):
    """The removal cannot go on: nothing was removed (what was stopped is started again)."""


def options(argv=None):
    p = argparse.ArgumentParser(description="Uninstall one app for the App Store (root).")
    p.add_argument("--app", required=True)
    p.add_argument("--state", required=True, help="JSON file updated after each step")
    p.add_argument("--result", required=True, help="JSON file written at the end")
    p.add_argument("--swizzin", default="/etc/swizzin")
    p.add_argument("--locks", default="/install")
    p.add_argument("--archives", default="/var/backups/swizzin-store")
    p.add_argument("--root", default="/", help="tests: every path of the table is taken under this folder")
    p.add_argument("--table", help="tests: a JSON table instead of removable.py")
    p.add_argument("--work", default="/root", help="where the private copy of the script is written")
    p.add_argument("--timeout", type=int, default=2700, help="seconds the remove script may run")
    p.add_argument("--quiet-timeout", type=int, default=900, help="seconds the remove script may print nothing")
    p.add_argument("--retry", action="store_true", help="try again after an incomplete removal (the lock may be gone)")
    return p.parse_args(argv)


def say(text):
    try:
        print("[App Store] " + text, flush=True)
    except OSError:  # the terminal is gone (the service stopped): the removal goes on regardless
        pass


def say_newline():
    try:
        print(flush=True)
    except OSError:
        pass


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def command(*argv, timeout=120):
    """(exit code, output) of a command; a missing command or a timeout counts as a failure."""
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


class Removal:
    def __init__(self, opts):
        self.opts = opts
        self.app = opts.app
        self.common, self.pins, self.apps = removable.load(opts.table)
        self.entry = self.apps.get(self.app)
        self.instances = {}  # unit -> {"active": bool, "enabled": bool}
        self.archive = None
        self.script_started = False
        self.stop_asked = False
        self.checks = []
        self.skipped = []  # (path, reason) left out of the copy
        self.ports = set()
        self.stopped = False  # something was stopped: put it back if the removal does not go on
        self.phase = "check"

    # --- paths -------------------------------------------------------------------------------

    def real(self, path):
        """Where a path of the table is on this system (under --root in the tests)."""
        return path if self.opts.root == "/" else os.path.join(self.opts.root, path.lstrip("/"))

    def users(self):
        """Swizzin's users (/root/*.info, the master), htpasswd's and those of the app's unit instances."""
        names = set()
        for name in os.listdir(self.real("/root")):
            if name.endswith(".info") and name != ".master.info":
                names.add(self._first_field(os.path.join(self.real("/root"), name)))
        names.add(self.master)
        try:
            with open(self.real("/etc/htpasswd")) as f:
                names.update(line.split(":", 1)[0].strip() for line in f)
        except OSError:
            pass
        names.update(unit.split("@", 1)[1][:-len(".service")] for unit in self.instances if "@" in unit)
        return sorted(n for n in names if USER.fullmatch(n or "") and os.path.isdir(self.real("/home/" + n)))

    @staticmethod
    def _first_field(path):
        try:
            with open(path) as f:
                return f.read().split(":", 1)[0].strip()
        except OSError:
            return ""

    def expand(self, patterns):
        out = []
        for pattern in patterns:
            if ("{master}" in pattern or "{owner}" in pattern) and not self.master:
                raise Refused("Swizzin's master user is unknown (/root/.master.info)")
            paths = [pattern.replace("{user}", u) for u in self.user_list] if "{user}" in pattern else [pattern]
            for p in paths:
                p = p.replace("{master}", self.master).replace("{owner}", self.owner)
                folder, name = os.path.split(p)
                if "*" in name:
                    try:
                        names = sorted(n for n in os.listdir(self.real(folder)) if fnmatch.fnmatchcase(n, name))
                    except OSError:
                        names = []
                    out.extend(os.path.join(folder, n) for n in names)
                else:
                    out.append(p)
        return out

    def safe_path(self, path):
        """None, or why root must not copy or delete this path: a link that does not belong to root on the way."""
        current = self.opts.root if self.opts.root != "/" else "/"
        for part in path.strip("/").split("/")[:-1]:
            current = os.path.join(current, part)
            try:
                st = os.lstat(current)
            except FileNotFoundError:
                return None
            if stat.S_ISLNK(st.st_mode) and st.st_uid not in (0, os.geteuid()):
                return "%s is a link that does not belong to root" % current
        return None

    # --- systemd -----------------------------------------------------------------------------

    def find_instances(self):
        found = []
        for unit in self.entry.units:
            if unit.endswith("@"):
                code, out = command("systemctl", "list-units", "--all", "--plain", "--no-legend", "--type=service",
                                    unit + "*.service")
                names = {line.split()[0] for line in out.splitlines() if line.strip() and code == 0}
                wants = self.real("/etc/systemd/system")
                for folder in (os.listdir(wants) if os.path.isdir(wants) else []):
                    if folder.endswith(".wants") and os.path.isdir(os.path.join(wants, folder)):
                        names.update(n for n in os.listdir(os.path.join(wants, folder))
                                     if n.startswith(unit) and n.endswith(".service"))
                found.extend(sorted(n for n in names if n.startswith(unit) and len(n) > len(unit + ".service")))
            else:
                name = unit + ".service"
                if command("systemctl", "show", "-p", "LoadState", "--value", name)[1].strip() not in ("", "not-found"):
                    found.append(name)
        return found

    @staticmethod
    def unit_state(unit):
        active = command("systemctl", "is-active", unit)[1].strip()
        enabled = command("systemctl", "is-enabled", unit)[1].strip()
        return {"active": active in ("active", "activating", "reloading"), "enabled": enabled == "enabled"}

    # --- state -------------------------------------------------------------------------------

    def save_state(self):
        data = {"app": self.app, "phase": self.phase, "instances": self.instances, "archive": self.archive,
                "script_started": self.script_started}
        tmp = self.opts.state + ".tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.opts.state)

    def step(self, phase, title):
        self.phase = phase
        self.save_state()
        say(title)
        pause = os.environ.get("STORE_TEST_PAUSE", "")  # tests only: time to press Stop or change things mid-way
        if pause.startswith(phase + ":"):
            time.sleep(float(pause.split(":", 1)[1]))

    def check(self, key, label, ok, detail=""):
        """One point of the result: a stable id (for programs), and what the user reads."""
        self.checks.append({"id": key, "label": label, "ok": bool(ok), "detail": detail})
        say(("✓ " if ok else "✗ ") + label + (": " + detail if detail else ""))
        return ok

    def on_sigint(self, *_):
        if self.script_started:
            say("Stopping is not possible now: Swizzin's remove script is running.")
        else:
            self.stop_asked = True
            say("Stop asked: everything will be put back as it was.")

    def stop_point(self):
        if self.stop_asked:
            raise Refused("stopped at your request")

    # --- 1. check ----------------------------------------------------------------------------

    def pinned_files(self):
        return self.common + self.entry.sources + (removable.script(self.app),)

    def check_pins(self):
        """Each file of Swizzin the removal loads: the audited bytes, root's only, in folders only root writes."""
        contents = {}
        top = os.path.normpath(self.opts.swizzin)
        for rel in self.pinned_files():
            path = os.path.join(top, rel)
            folder = os.path.dirname(path)
            while True:  # up to Swizzin's folder (/etc/swizzin), which only root can replace
                st = os.lstat(folder)
                if st.st_uid != os.geteuid() or st.st_mode & 0o022 or stat.S_ISLNK(st.st_mode):
                    raise Refused("%s can be changed by another user than root" % folder)
                if folder == top or folder == os.path.dirname(folder):
                    break
                folder = os.path.dirname(folder)
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022:
                raise Refused("%s can be changed by another user than root" % path)
            with open(path, "rb") as f:
                data = f.read()
            if hashlib.sha256(data).hexdigest() not in self.pins.get(rel, ()):
                raise Refused("Swizzin's %s is not the version the App Store checked (a Swizzin update changed it)"
                              % rel)
            contents[rel] = data
        return contents

    def apt_busy(self):
        for lock in APT_LOCKS:
            try:
                fd = os.open(self.real(lock), os.O_RDWR | os.O_NOFOLLOW)
            except OSError:
                continue
            try:
                fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.lockf(fd, fcntl.LOCK_UN)
            except OSError:
                return "apt or dpkg is working (%s is locked)" % lock
            finally:
                os.close(fd)
        for unit in APT_UNITS:
            if command("systemctl", "is-active", unit)[1].strip() in ("active", "activating"):
                return "automatic system updates are running (%s)" % unit
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                try:
                    with open("/proc/%s/comm" % pid) as f:
                        name = f.read().strip()
                except OSError:
                    continue
                if name in APT_PROCESSES:
                    return "%s is running" % name
        return None

    def installed_packages(self):
        code, out = command("dpkg-query", "-W", "-f=${Package}\t${db:Status-Abbrev}\n")
        if code != 0:
            raise Refused("the list of installed packages cannot be read")
        packages = {}
        for line in out.splitlines():
            name, _, status = line.partition("\t")
            packages[name.split(":")[0]] = status.strip()
        return packages

    @staticmethod
    def is_installed(status):
        return len(status) >= 2 and status[1] in "iUFWt"

    def check_packages(self):
        """apt would remove nothing but the app's own packages (it takes the packages that depend on them along)."""
        names = sorted({name for spec in self.entry.packages for name, status in self.packages_before.items()
                        if fnmatch.fnmatchcase(name, spec) and self.is_installed(status)})
        if not names:
            return
        code, out = command("apt-get", "-s", "-o", "APT::Get::AutomaticRemove=false", "remove", "--purge", *names)
        if code != 0:
            raise Refused("apt cannot plan the removal of %s" % " ".join(names))
        taken = set()
        for line in out.splitlines():
            word = line.split(" ", 1)[0]
            if word in ("Inst", "Conf"):
                raise Refused("apt would install or configure packages while removing the app")
            if word in ("Purg", "Remv"):
                taken.add(line.split()[1].split(":")[0])
        others = sorted(p for p in taken if not any(re.fullmatch(r, p) for r in self.entry.allowed))
        if others:
            raise Refused("removing it would also remove: " + ", ".join(others))

    def estimate(self, paths):
        total = 0
        for path in paths:
            real = self.real(path)
            try:
                st = os.lstat(real)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
            elif stat.S_ISDIR(st.st_mode):
                for folder, dirs, files in os.walk(real):
                    dirs[:] = [d for d in dirs if not self.skipped_name(d) and not os.path.islink(os.path.join(folder, d))]
                    for name in files:
                        if not self.skipped_name(name):
                            try:
                                st = os.lstat(os.path.join(folder, name))
                            except OSError:
                                continue
                            if stat.S_ISREG(st.st_mode):
                                total += st.st_size
        return total

    def skipped_name(self, name):
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in self.entry.skip)

    def nginx_installed(self):
        return os.path.exists(os.path.join(self.opts.locks, ".nginx.lock"))

    def resolve(self):
        """Users, owner, unit instances and the paths of the table, as they are on this system now."""
        self.master = self._first_field(self.real("/root/.master.info"))
        self.instances = {u: self.unit_state(u) for u in self.find_instances()}
        self.owner = ""
        if self.entry.owner_unit:
            name = command("systemctl", "show", "-p", "User", "--value", self.entry.owner_unit + ".service")[1].strip()
            self.owner = name if USER.fullmatch(name) and os.path.isdir(self.real("/home/" + name)) else ""
        self.owner = self.owner or self.master
        self.user_list = self.users()
        self.copy_paths = self.expand(self.entry.copy)
        self.gone_paths = self.expand(self.entry.gone)
        self.kept_paths = [p for p in self.expand(self.entry.kept) if os.path.lexists(self.real(p))]
        self.nginx_paths = self.expand(self.entry.nginx)

    def describe(self):
        """For the confirmation, before anything is done: who is concerned and how big the copy will be."""
        if self.entry is None:
            raise Refused("the App Store cannot uninstall %s" % self.app)
        self.resolve()
        return {"users": self.user_list, "services": sorted(self.instances), "partial": self.entry.partial,
                "estimate": self.estimate(self.copy_paths), "archives": self.opts.archives}

    def phase_check(self):
        self.step("check", "1/5 Checking that the removal is safe...")
        if self.entry is None:
            raise Refused("the App Store cannot uninstall %s" % self.app)
        if not self.opts.retry and not os.path.exists(os.path.join(self.opts.locks, "." + self.app + ".lock")):
            raise Refused("%s is not installed" % self.app)
        for other in self.entry.refuse_if:
            if os.path.exists(os.path.join(self.opts.locks, "." + other + ".lock")):
                raise Refused("%s is installed: removing %s this way would harm it" % (other, self.app))
        self.contents = self.check_pins()
        busy = self.apt_busy()
        if busy:
            raise Refused(busy + ": try again in a few minutes")
        if command("apt-get", "check")[0] != 0:
            raise Refused("apt reports a problem (apt-get check): fix it over SSH first")
        audit = command("dpkg", "--audit")
        if audit[0] != 0 or audit[1].strip():
            raise Refused("dpkg reports half-installed packages (dpkg --audit): fix it over SSH first")
        self.packages_before = self.installed_packages()
        self.check_packages()
        self.resolve()
        for path in self.copy_paths + self.gone_paths + self.kept_paths + self.nginx_paths + list(self.entry.leftovers):
            problem = self.safe_path(path)
            if problem:
                raise Refused(problem)
        size = self.estimate(self.copy_paths)
        os.makedirs(self.opts.archives, mode=0o700, exist_ok=True)
        st = os.lstat(self.opts.archives)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
            raise Refused("%s must be a folder that only root can open" % self.opts.archives)
        free = shutil.disk_usage(self.opts.archives).free
        if size + MARGIN > free:
            raise Refused("not enough free space for the copy of its settings (%d MB needed, %d MB free in %s)"
                          % ((size + MARGIN) // 2**20, free // 2**20, self.opts.archives))
        self.nginx_before = self.nginx_installed() and command("nginx", "-t")[0] == 0
        self.ports = self.listening_ports()
        say("Users: %s. Settings to copy: about %s." % (", ".join(self.user_list) or "none", human(size)))

    # --- 2. stop -----------------------------------------------------------------------------

    def listening_ports(self):
        """Ports the app's units listen on now (their processes, found by cgroup)."""
        code, out = command("ss", "-ltnpH")
        if code != 0:
            return set()
        ports = set()
        for line in out.splitlines():
            fields = line.split()
            if len(fields) < 4:
                continue
            port = fields[3].rsplit(":", 1)[-1]
            for pid in re.findall(r"pid=(\d+)", line):
                try:
                    with open("/proc/%s/cgroup" % pid) as f:
                        cgroup = f.read()
                except OSError:
                    continue
                if any("/" + unit in cgroup for unit in self.instances):
                    ports.add(port)
        return ports

    def phase_stop(self):
        self.step("stop", "2/5 Stopping %s..." % self.app)
        if not self.instances:
            say("No service to stop.")
            return
        units = sorted(self.instances)
        self.stopped = True
        command("systemctl", "stop", *units)
        enabled = [u for u in units if self.instances[u]["enabled"]]
        if enabled:
            command("systemctl", "disable", *enabled)
        for unit in units:  # a unit with KillMode=process can leave its children behind
            group = command("systemctl", "show", "-p", "ControlGroup", "--value", unit)[1].strip()
            procs = os.path.join("/sys/fs/cgroup", group.lstrip("/"), "cgroup.procs") if group else ""
            if procs and os.path.isfile(procs) and open(procs).read().strip():
                command("systemctl", "kill", "--signal=SIGKILL", unit)
        still = [u for u in units if command("systemctl", "is-active", u)[1].strip() in ("active", "activating")]
        if still:
            raise Refused("%s did not stop" % ", ".join(still))
        say("Stopped: " + ", ".join(units))

    def put_back(self):
        """Before the remove script: the app as it was."""
        if not self.stopped:
            return
        enabled = [u for u, s in self.instances.items() if s["enabled"]]
        active = [u for u, s in self.instances.items() if s["active"]]
        if enabled:
            command("systemctl", "enable", *enabled)
        if active:
            command("systemctl", "start", *active)
        if enabled or active:
            say("%s was put back as it was." % self.app)

    # --- 3. copy -----------------------------------------------------------------------------

    def phase_copy(self):
        self.step("copy", "3/5 Saving a copy of its settings...")
        present = [p for p in self.copy_paths if os.path.lexists(self.real(p))]
        if not present:
            say("Nothing to copy: %s keeps no settings of yours that its removal deletes." % self.app)
            return
        name = "%s-%s.tar.gz" % (self.app, time.strftime("%Y%m%d-%H%M%S"))
        path = os.path.join(self.opts.archives, name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.archive = path
        self.save_state()
        manifest = []
        try:
            with os.fdopen(fd, "wb") as raw:
                with tarfile.open(fileobj=raw, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
                    for p in present:
                        self.add(tar, p, manifest)
                        self.stop_point()
                    self.add_text(tar, "swizzin-store/manifest.json", json.dumps(manifest, indent=1))
                    self.add_text(tar, "swizzin-store/RESTORE.txt", self.restore_text(name))
                raw.flush()
                os.fsync(raw.fileno())
            folder = os.open(self.opts.archives, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(folder)
            finally:
                os.close(folder)
            self.verify_copy(path, manifest)
        except (Refused, OSError, tarfile.TarError, ValueError) as e:
            try:  # an unfinished or unchecked copy is no copy: it does not stay
                os.unlink(path)
            except OSError:
                pass
            self.archive = None
            self.save_state()
            raise e if isinstance(e, Refused) else Refused("the copy of its settings failed: %s" % e)
        files = sum(1 for m in manifest if m["type"] == "file")
        say("Copy saved: %s (%d files, %s)." % (path, files, human(os.path.getsize(path))))
        for where, why in self.skipped:
            say("Not copied: %s (%s)" % (where, why))

    def add(self, tar, path, manifest):
        real = self.real(path)
        st = os.lstat(real)
        if stat.S_ISLNK(st.st_mode):
            self.skipped.append((path, "a link"))
        elif stat.S_ISREG(st.st_mode):
            parent = os.open(os.path.dirname(real), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                self.add_file(tar, parent, os.path.basename(real), path, st.st_uid, manifest)
            finally:
                os.close(parent)
        elif stat.S_ISDIR(st.st_mode):
            owner = st.st_uid
            for folder, dirs, files, dirfd in os.fwalk(real, follow_symlinks=False):
                shown = path + folder[len(real):]
                self.add_dir(tar, dirfd, shown, manifest)
                for d in list(dirs):
                    dst = os.lstat(d, dir_fd=dirfd)
                    if self.skipped_name(d):
                        dirs.remove(d)
                    elif stat.S_ISLNK(dst.st_mode):
                        dirs.remove(d)
                        self.skipped.append((shown + "/" + d, "a link"))
                for name in sorted(files):
                    if not self.skipped_name(name):
                        self.add_file(tar, dirfd, name, shown + "/" + name, owner, manifest)
        else:
            self.skipped.append((path, "not a regular file"))

    def add_dir(self, tar, dirfd, shown, manifest):
        st = os.fstat(dirfd)
        info = tarfile.TarInfo(shown.lstrip("/"))
        info.type, info.mode, info.uid, info.gid, info.mtime = tarfile.DIRTYPE, stat.S_IMODE(st.st_mode), \
            st.st_uid, st.st_gid, int(st.st_mtime)
        tar.addfile(info)
        manifest.append({"path": shown, "type": "dir", "uid": st.st_uid, "gid": st.st_gid,
                         "mode": oct(stat.S_IMODE(st.st_mode))})

    def add_file(self, tar, dirfd, name, shown, owner, manifest):
        """A regular file opened without following a link, read from that very file (it cannot be swapped)."""
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dirfd)
        except OSError as e:
            self.skipped.append((shown, "a link" if e.errno == 40 else os.strerror(e.errno)))
            return
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                self.skipped.append((shown, "not a regular file"))
                return
            if st.st_uid not in (0, owner):
                self.skipped.append((shown, "belongs to another user"))
                return
            info = tarfile.TarInfo(shown.lstrip("/"))
            info.size, info.mode, info.uid, info.gid, info.mtime = st.st_size, stat.S_IMODE(st.st_mode), \
                st.st_uid, st.st_gid, int(st.st_mtime)
            reader = Hashing(f)
            tar.addfile(info, reader)
            manifest.append({"path": shown, "type": "file", "uid": st.st_uid, "gid": st.st_gid,
                             "mode": oct(stat.S_IMODE(st.st_mode)), "size": st.st_size, "sha256": reader.hexdigest()})

    @staticmethod
    def add_text(tar, name, text):
        data = text.encode()
        info = tarfile.TarInfo(name)
        info.size, info.mode, info.mtime = len(data), 0o600, int(time.time())
        tar.addfile(info, io.BytesIO(data))

    def verify_copy(self, path, manifest):
        expected = {m["path"].lstrip("/"): m["sha256"] for m in manifest if m["type"] == "file"}
        seen = set()
        with tarfile.open(path, "r:gz") as tar:  # reading to the end also checks the archive's CRC
            for member in tar:
                if member.isfile() and member.name in expected:
                    h = hashlib.sha256()
                    with tar.extractfile(member) as f:
                        for block in iter(lambda: f.read(1 << 20), b""):
                            h.update(block)
                    if h.hexdigest() != expected[member.name]:
                        raise Refused("the copy of %s does not read back the same" % member.name)
                    seen.add(member.name)
        if seen != set(expected):
            raise Refused("the copy is incomplete when read back")

    def restore_text(self, name):
        units = " ".join(sorted(self.instances)) or "(no service)"
        lines = [
            "Copy of the settings of %s, made by the App Store on %s, just before uninstalling it."
            % (self.app, time.strftime("%Y-%m-%d %H:%M")),
            "",
            "Each file is stored with its full path (home/..., opt/...). To put the settings back, as root:",
            "  1. Reinstall the app: from the App Store, or: box install %s" % self.app,
            "  2. Stop it:  systemctl stop %s" % units,
            "  3. Put the files back:  tar -xzpf %s -C / --exclude=swizzin-store" % os.path.join(self.opts.archives, name),
            "  4. Start it again:  systemctl start %s" % units,
            "",
            "swizzin-store/manifest.json lists every file with its owner, mode, size and sha256.",
        ]
        if self.entry.partial:
            lines += ["", "This copy holds the settings and the databases only: caches, posters and other metadata",
                      "were left out, and the app rebuilds them (a library scan)."]
        if any("/etc/nginx/conf.d/" in p for p in self.copy_paths):
            lines += ["", "The copy includes the app's nginx file(s) in /etc/nginx/conf.d: they hold the port the",
                      "app listens on. After step 3, check `nginx -t`, then: systemctl reload nginx"]
        return "\n".join(lines) + "\n"

    # --- 4. remove ---------------------------------------------------------------------------

    def recheck(self):
        """Just before the script: nothing changed since the checks (the app did not start again, Swizzin's files)."""
        running = [u for u in self.instances if command("systemctl", "is-active", u)[1].strip() in ("active", "activating")]
        if running:
            raise Refused("%s started again in the meantime" % ", ".join(running))
        if self.check_pins() != self.contents:
            raise Refused("Swizzin's files changed in the meantime")
        busy = self.apt_busy()
        if busy:
            raise Refused(busy + ": try again in a few minutes")

    def phase_remove(self):
        self.step("remove", "4/5 Removing %s (Swizzin's remove script)..." % self.app)
        self.recheck()
        self.stop_point()
        work = tempfile.mkdtemp(prefix="swizzin-store-", dir=self.opts.work)
        try:
            script = os.path.join(work, self.app + ".sh")
            with open(os.open(script, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700), "wb") as f:
                f.write(self.contents[removable.script(self.app)])  # the very bytes that were checked
            empty = os.path.join(work, "cwd")  # Swizzin's unquoted globs must match nothing
            os.mkdir(empty, 0o700)
            apt_conf = os.path.join(work, "apt.conf")
            with open(apt_conf, "w") as f:
                f.write('DPkg::Lock::Timeout "300";\n')
            env = {k: v for k, v in os.environ.items() if k in (
                "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "PATH", "GIT_TERMINAL_PROMPT", "NEEDRESTART_SUSPEND",
                "PAGER", "SYSTEMD_PAGER", "LESSSECURE", "STORE_TEST_ROOT")}
            env.update(DEBIAN_FRONTEND="noninteractive", APT_CONFIG=apt_conf)
            globals_sh = os.path.join(self.opts.swizzin, "sources", "globals.sh")
            argv = ["/bin/bash", "-c", '. "$1" && exec bash "$2"', "swizzin-remove", globals_sh, script]
            self.script_started = True
            self.save_state()
            status, timed_out = self.run_script(argv, env, empty)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        say("Swizzin's remove script ended%s." % (" (stopped: it ran too long or waited for an answer)"
                                                   if timed_out else " with code %s" % status))
        return timed_out

    def run_script(self, argv, env, cwd):
        """The script in a terminal of its own: its output is shown, nobody types in it but the known answers."""
        pid, fd = pty.fork()
        if pid == 0:
            try:
                for sig in (signal.SIGINT, signal.SIGQUIT, signal.SIGPIPE, signal.SIGHUP, signal.SIGTERM):
                    signal.signal(sig, signal.SIG_DFL)
                os.chdir(cwd)
                os.execve(argv[0], argv, env)
            finally:
                os._exit(127)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        tail, answered = "", set()
        started = last_output = time.monotonic()
        status, timed_out = None, False
        while True:
            ready, _, _ = select.select([fd], [], [], 1)
            if ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    data = b""
                if data:
                    last_output = time.monotonic()
                    try:
                        sys.stdout.buffer.write(data)
                        sys.stdout.flush()
                    except OSError:
                        pass
                    tail = (tail + ANSI.sub("", decoder.decode(data)))[-4000:]
                    for i, (question, answer) in enumerate(self.entry.answers):
                        if i not in answered and question in tail:
                            answered.add(i)
                            os.write(fd, (answer + "\r").encode())
                            say("Answered '%s' for you (Swizzin's default; the copy keeps the settings)." % answer)
            done, code = os.waitpid(pid, os.WNOHANG)
            if done:
                status = os.waitstatus_to_exitcode(code)
                break
            now = time.monotonic()
            if now - started > self.opts.timeout or now - last_output > self.opts.quiet_timeout:
                timed_out = True
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(pid, sig)
                    except OSError:
                        pass
                    time.sleep(5)
                    if os.waitpid(pid, os.WNOHANG)[0]:
                        break
                break
        try:
            while True:  # what it wrote just before ending
                ready, _, _ = select.select([fd], [], [], 0.2)
                if not ready:
                    break
                data = os.read(fd, 65536)
                if not data:
                    break
                sys.stdout.buffer.write(data)
            sys.stdout.flush()
        except OSError:
            pass
        os.close(fd)
        if not tail.endswith("\n"):  # what follows starts on a line of its own
            say_newline()
        return status, timed_out

    # --- 5. fix and verify -------------------------------------------------------------------

    def phase_fix(self):
        self.phase = "fix"
        self.save_state()
        left = [u for u in self.instances
                if command("systemctl", "show", "-p", "LoadState", "--value", u)[1].strip() not in ("", "not-found")]
        if left:
            command("systemctl", "stop", *left)
            command("systemctl", "disable", *left)
        for path in self.entry.leftovers:
            real = self.real(path)
            try:
                st = os.lstat(real)
            except FileNotFoundError:
                continue
            if (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)) and st.st_uid in (0, os.geteuid()):
                os.unlink(real)
                say("Deleted what Swizzin's script forgot: " + path)
        command("systemctl", "daemon-reload")
        if self.instances:
            command("systemctl", "reset-failed", *self.instances)
        if self.nginx_installed():
            if command("nginx", "-t")[0] == 0:
                command("systemctl", "reload", "nginx")
            # never restarted: the panel itself goes through nginx

    def phase_verify(self, timed_out):
        self.step("verify", "5/5 Checking the result...")
        self.check("script", "Swizzin's remove script finished", not timed_out,
                   "" if not timed_out else "stopped: it ran too long or waited for an answer")
        self.check("lock", "Swizzin no longer lists it as installed",
                   not os.path.exists(os.path.join(self.opts.locks, "." + self.app + ".lock")))
        loaded = [u for u in self.instances
                  if command("systemctl", "show", "-p", "LoadState", "--value", u)[1].strip() not in ("", "not-found")]
        self.check("units", "Its services are gone", not loaded, ", ".join(loaded))
        running = self.processes_left()
        self.check("processes", "None of its programs still runs", not running, ", ".join(running))
        after = self.installed_packages()
        still = sorted(name for name, status in self.packages_before.items()
                       if self.is_installed(status) and self.is_installed(after.get(name, ""))
                       and any(fnmatch.fnmatchcase(name, spec) for spec in self.entry.packages))
        self.check("packages", "Its packages are removed", not still, ", ".join(still))
        others = sorted(name for name, status in self.packages_before.items()
                        if self.is_installed(status) and not self.is_installed(after.get(name, ""))
                        and not any(re.fullmatch(r, name) for r in self.entry.allowed))
        self.check("other-packages", "No other package was removed", not others, ", ".join(others))
        left = [p for p in self.gone_paths if os.path.lexists(self.real(p))]
        self.check("files", "Its files are deleted", not left, ", ".join(left))
        missing = [p for p in self.kept_paths if not os.path.lexists(self.real(p))]
        self.check("kept", "What it keeps is still there", not missing, ", ".join(missing))
        nginx_left = [p for p in self.expand(self.entry.nginx) if os.path.lexists(self.real(p))]
        self.check("web", "Its web page is no longer served", not nginx_left, ", ".join(nginx_left))
        if self.nginx_installed():
            ok = command("nginx", "-t")[0] == 0
            self.check("nginx", "The web server's configuration is valid", ok or not self.nginx_before,
                       "" if ok else ("broken before the removal already" if not self.nginx_before else "nginx -t fails"))
        if self.ports:
            busy = self.listening_ports_any() & self.ports
            self.check("ports", "Its ports are free again", not busy, ", ".join(sorted(busy)))

    def processes_left(self):
        programs = [self.real(p) for p in self.gone_paths]
        found = []
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                exe = os.readlink("/proc/%s/exe" % pid)
            except OSError:
                continue
            exe = exe[:-len(" (deleted)")] if exe.endswith(" (deleted)") else exe
            if any(exe == p or exe.startswith(p.rstrip("/") + "/") for p in programs):
                found.append("%s (pid %s)" % (os.path.basename(exe), pid))
        return found

    @staticmethod
    def listening_ports_any():
        code, out = command("ss", "-ltnH")
        return {line.split()[3].rsplit(":", 1)[-1] for line in out.splitlines() if code == 0 and len(line.split()) >= 4}

    # --- the whole ---------------------------------------------------------------------------

    def run(self):
        signal.signal(signal.SIGINT, self.on_sigint)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)  # a lost terminal must not cut a removal off
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # the service waits for the end of a removal
        result = {"app": self.app}
        try:
            self.phase_check()
            self.stop_point()
            self.phase_stop()
            self.stop_point()
            self.phase_copy()
            self.stop_point()
            timed_out = self.phase_remove()
        except Refused as e:
            if self.script_started:  # cannot happen: once started, nothing raises Refused
                raise
            self.put_back()
            say("Not uninstalled: %s. Nothing was removed." % e)
            result.update(result="failed", message=str(e))
            return self.finish(result)
        except Exception as e:  # a bug: the same as a refusal before the script, an incomplete removal after
            if not self.script_started:
                self.put_back()
                say("Not uninstalled: unexpected error (%s). Nothing was removed." % e)
                result.update(result="failed", message="unexpected error: %s" % e)
                return self.finish(result)
            say("Unexpected error while removing (%s): checking what was done..." % e)
            timed_out = False
        try:
            self.phase_fix()
        except Exception as e:
            say("Could not finish the cleanup: %s" % e)
        self.phase_verify(timed_out)
        ok = all(c["ok"] for c in self.checks)
        result.update(result="removed" if ok else "incomplete")
        say("%s uninstalled: every check passed." % self.app if ok else
            "%s is only partly uninstalled: see the checks above." % self.app)
        return self.finish(result)

    def finish(self, result):
        result.update(checks=self.checks, archive=self.archive, skipped=[list(s) for s in self.skipped])
        self.phase = "done"
        self.save_state()
        tmp = self.opts.result + ".tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump(result, f)
        os.replace(tmp, self.opts.result)
        return 0


class Hashing:
    """A file read through, hashed on the way."""

    def __init__(self, f):
        self.f = f
        self.h = hashlib.sha256()

    def read(self, size=-1):
        data = self.f.read(size)
        self.h.update(data)
        return data

    def hexdigest(self):
        return self.h.hexdigest()


def human(size):
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return ("%d %s" % (size, unit)) if unit == "bytes" else ("%.1f %s" % (size, unit))
        size /= 1024


def main(argv=None):
    opts = options(argv)
    if not re.fullmatch(r"[a-z0-9]{1,32}", opts.app):
        print("invalid app name", file=sys.stderr)
        return 2
    return Removal(opts).run()


if __name__ == "__main__":
    sys.exit(main())
