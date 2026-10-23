#!/usr/bin/python3
"""Swizzin App Store helper: the only part of the App Store that runs as root.

The panel runs as the `swizzin` user and cannot even read the list of Swizzin
applications (/etc/swizzin is readable by root only), so it asks this service over
a unix socket. One JSON object per line, in both directions. The service answers a
few fixed operations and never runs a command, a path or an environment that comes
from the client. Only root and the panel user may connect (checked with SO_PEERCRED).

Operations:
  {"op": "catalogue"}                    -> {"ok", "apps": [{name, description, installed, blocked}], "job", "last",
                                             "features": what this version can do}
  {"op": "install", "app", "rows", "cols"} -> {"ok", "started": job, "control_key"} or {"ok": false, "error"}
  {"op": "attach", "control_key"|"take"}   -> {"ok", "attached": job, "controller"}, then {"output"}, {"phase"}, {"done"}
  {"op": "input", "data"} / {"op": "resize", "rows", "cols"}   (from the connection that controls the job only)
  {"op": "remove-preview", "app"}        -> {"ok", "preview": {users, services, partial, estimate, archives, retry},
                                             "ticket"} (what the confirmation shows; the ticket is good for 5 minutes)
  {"op": "remove", "app", "ticket", "confirm", "requester", "rows", "cols"} -> {"ok", "started": job, "control_key"}

An installation runs `box install <app>` in a pseudo-terminal, exactly as over SSH (the
installers ask their questions on /dev/tty and draw whiptail menus), then `box test <app>`
when the app's lock file appeared, and once more a minute later if it failed (some apps take
a while to open their port after their first start). One at a time. Whoever started it, or later took
control (the panel asks for the admin password first), may type in it; others only watch.

An uninstallation runs remove_runner.py in the same kind of terminal: only for the apps of removable.py, whose
Swizzin files are the audited ones; it saves the app's settings first and never goes through `box remove`
(see remove_runner.py). Once Swizzin's remove script runs, nothing cuts it off, not even this service stopping.

Installed by store-helper/install.sh into /usr/local/lib/swizzin-store/ (root-owned),
never run from /opt/swizzin, which the panel user can write. Standard library only.
It serves Heliedan's edition of the panel and nothing else: it does not start without that panel, and stops (once
no installation or uninstallation runs) when the panel is removed or replaced by the official one (PANEL_MARKER).
"""
import argparse
import codecs
import fcntl
import glob
import hashlib
import json
import logging
import os
import pty
import pwd
import re
import secrets
import selectors
import signal
import socket
import stat
import struct
import subprocess
import sys
import termios
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # -I leaves this folder out; install.sh checks that it belongs to root
import check_app  # noqa: E402
import removable  # noqa: E402
import remove_runner  # noqa: E402

NAME = re.compile(r"[a-z0-9]{1,32}")
UNIT = re.compile(r"[A-Za-z0-9@._-]+\.service")
RUNNER = os.path.join(HERE, "remove_runner.py")
APP_CHECK = os.path.join(HERE, "check_app.py")
# Not offered by the App Store, refused here: system components and tools that are set up on the
# machine itself (over SSH), some of which change how the server is reached or secured, and xmrig,
# a cryptocurrency miner that mines for the Swizzin project when it is not given a wallet.
BLOCKED = ("panel", "nginx", "letsencrypt", "quota", "wireguard", "x2go", "webmin", "shellinabox",
           "ffmpeg", "rclone", "vsftpd", "duckdns", "xmrig")
MAX_CLIENTS = 8
MAX_LINE = 64 * 1024
MAX_PENDING_OUTPUT = 2 * 1024 * 1024
MAX_INPUT = 4096
REPLAY = 512 * 1024
MAX_LOG = 50 * 1024 * 1024
KEEP_LOGS = 20
FEATURES = ["install", "remove"]  # tells the panel which operations this version of the service offers
TICKET_TTL = 300  # seconds a confirmation window may stay open before uninstalling
REMOVE_STOP_WAIT = 540  # seconds this service waits for a removal when told to stop (its TimeoutStopSec is 600)
REMOVE_PHASES = ("check", "stop", "copy", "remove", "fix", "verify")
# Heliedan's edition of the panel carries this service's source: while it is there, the panel is that edition.
# box remove panel deletes /opt/swizzin, box upgrade panel puts the official code back (git reset --hard), and
# heliedan.sh revert puts the backup back: each one takes it away, and this service then has nothing to serve.
PANEL_MARKER = "/opt/swizzin/store-helper/swizzin_store.py"
MARKER_EVERY = 15  # seconds between two looks at it; missing at two looks in a row, the panel is gone
# The installers' whole environment: nothing comes from the client or from this service's own
# environment (scripts read common names such as user, pass or log). PATH ends like root's over SSH
# (Swizzin adds /usr/local/bin/swizzin in .bashrc). TERM is required: without
# it Swizzin's messages lose their text. NEEDRESTART_SUSPEND: apt must not restart services
# (this one included) in the middle of an installation.
# PAGER, SYSTEMD_PAGER, LESSSECURE: no interactive pager (which can run commands) opens in the terminal.
JOB_ENV = {"HOME": "/root", "USER": "root", "LOGNAME": "root", "SHELL": "/bin/bash", "TERM": "xterm-256color",
           "LANG": "C.UTF-8", "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/bin/swizzin",
           "GIT_TERMINAL_PROMPT": "0", "NEEDRESTART_SUSPEND": "1", "PAGER": "cat", "SYSTEMD_PAGER": "cat",
           "LESSSECURE": "1"}

log = logging.getLogger("swizzin-store")


def options(argv=None):
    p = argparse.ArgumentParser(description="Swizzin App Store helper (root service).")
    p.add_argument("--socket", default="/run/swizzin-store/store.sock")
    p.add_argument("--scripts", default="/etc/swizzin/scripts/install", help="Swizzin install scripts")
    p.add_argument("--tests", default="/etc/swizzin/scripts/tests", help="Swizzin per-app health checks")
    p.add_argument("--lang", default="/etc/swizzin/sources/lang_en", help="Swizzin descriptions")
    p.add_argument("--locks", default="/install", help="Swizzin lock files (.<app>.lock)")
    # box lives there on every install; /usr/local/bin/box exists only on older ones
    p.add_argument("--box", default="/usr/local/bin/swizzin/box", help="the box command")
    p.add_argument("--home", default="/root", help="working directory of the installers")
    p.add_argument("--logs", default="/var/log/swizzin-store", help="one log (and one summary) per installation")
    p.add_argument("--panel-user", default="swizzin",
                   help="the only user besides root allowed to connect; the socket belongs to its group")
    p.add_argument("--allow-uid", type=int,
                   help="tests: allow this uid instead of the panel user, and leave the socket group alone")
    p.add_argument("--blocked", default=",".join(BLOCKED), help="comma-separated apps the store does not offer")
    p.add_argument("--app-check", default=APP_CHECK,
                   help="the App Store's own check, for the apps whose Swizzin check is wrong (check_app.py)")
    p.add_argument("--retest-delay", type=int, default=60,
                   help="seconds before checking an app again when its first checks failed (0: no second check)")
    p.add_argument("--swizzin", default="/etc/swizzin", help="Swizzin's folder (remove scripts and their environment)")
    p.add_argument("--archives", default="/var/backups/swizzin-store", help="copies of the settings of removed apps")
    p.add_argument("--work", default="/root", help="where the uninstaller writes its private copy of a remove script")
    p.add_argument("--no-remove", default="/etc/swizzin-store.no-remove",
                   help="uninstalling from the App Store is turned off while this file exists")
    p.add_argument("--root", default="/", help="tests: the uninstaller takes every path under this folder")
    p.add_argument("--os-release", default="/etc/os-release", help="tests: the file that says which system this is")
    p.add_argument("--table", help="tests: a JSON table of uninstallable apps instead of removable.py")
    p.add_argument("--test-path", help="tests: folder put first in the uninstaller's PATH (fake system tools)")
    p.add_argument("--test-env", action="append", default=[], help="tests: NAME=VALUE for the uninstaller")
    p.add_argument("--runner-option", action="append", default=[], help="tests: an option for remove_runner.py")
    p.add_argument("--panel-marker", default=PANEL_MARKER,
                   help="the file of Heliedan's edition of the panel without which this service does not run")
    p.add_argument("--marker-every", type=float, default=MARKER_EVERY, help="tests: seconds between two looks at it")
    return p.parse_args(argv)


def panel_present(path):
    """Whether the panel is still Heliedan's edition (PANEL_MARKER), checked like the conditions of
    swizzin-store.service: a file that is not empty, and not a symbolic link. Its metadata only (lstat): the panel
    user can write there, so it is never opened or read, and nothing of it but its presence is used."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_size > 0


def descriptions(path):
    """{'JELLYFIN': 'A TV, Film and Music ...'} from Swizzin's lang_en ($L['KEY'] = '...';)."""
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"\s*\$L\['([A-Z0-9_]+)'\]\s*=\s*'(.*)';\s*$", line)
                if m:
                    out[m.group(1)] = m.group(2).replace("\\'", "'").strip()
    except OSError as e:
        log.warning("descriptions unreadable (%s): %s", path, e)
    return out


def app_names(opts):
    """Every Swizzin install script, read afresh each time."""
    return sorted(f[:-3] for f in os.listdir(opts.scripts) if f.endswith(".sh") and NAME.fullmatch(f[:-3]))


def installed(opts, app):
    return os.path.exists(os.path.join(opts.locks, "." + app + ".lock"))


def catalogue(opts):
    locks = {f[1:-5] for f in os.listdir(opts.locks) if f.startswith(".") and f.endswith(".lock")}
    text = descriptions(opts.lang)
    blocked = set(opts.blocked.split(","))
    table = removable.load(opts.table)
    return [{"name": n, "description": text.get(n.upper(), ""), "installed": n in locks, "blocked": n in blocked,
             "remove": removal_status(opts, table, n) if n in locks else None} for n in app_names(opts)]


def os_codename(path):
    """'noble', 'bookworm'... or None."""
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("VERSION_CODENAME="):
                    return line.split("=", 1)[1].strip().strip('"') or None
    except OSError:
        pass
    return None


def removal_status(opts, table, app):
    """'ok', or why the App Store does not uninstall this app now: not-offered, untested (not through the
    campaign yet), other-system (the campaign tested it on other systems only), disabled (by root), conflict (an
    installed app that its removal would harm), script-changed (an update of Swizzin changed a file it would run)."""
    common, pins, apps = table
    entry = apps.get(app)
    if entry is None:
        return "not-offered"
    if not entry.tested:
        return "untested"
    if os_codename(opts.os_release) not in entry.tested:
        return "other-system"
    if os.path.exists(opts.no_remove):
        return "disabled"
    if any(os.path.exists(os.path.join(opts.locks, "." + other + ".lock")) for other in entry.refuse_if):
        return "conflict"
    for rel in common + entry.sources + (removable.script(app),):
        try:
            with open(os.path.join(opts.swizzin, rel), "rb") as f:
                if hashlib.sha256(f.read()).hexdigest() not in pins.get(rel, ()):
                    return "script-changed"
        except OSError:
            return "script-changed"
    return "ok"


def window(msg):
    """Terminal size asked by the browser, kept within sane bounds."""
    try:
        rows, cols = int(msg.get("rows", 30)), int(msg.get("cols", 100))
    except (TypeError, ValueError, OverflowError):  # OverflowError: infinity (JSON allows 1e400)
        rows, cols = 30, 100
    return min(max(rows, 10), 200), min(max(cols, 40), 400)


class Client:
    def __init__(self, sock, uid):
        self.sock = sock
        self.uid = uid
        self.inbuf = bytearray()
        self.out = bytearray()
        self.closed = False
        self.attached = False


class Job:
    """One installation: `box install <app>`, then `box test <app>` if the app's lock appeared."""

    def __init__(self, opts, app, size, action="install"):
        self.app = app
        self.action = action  # install or remove: the page words its messages after it
        # unique even for two jobs in the same second (Try again right after a failure), and sorted by time
        now = time.time()
        self.id = "%s-%06d-%s" % (time.strftime("%Y%m%d-%H%M%S", time.localtime(now)), now % 1 * 1000000, app)
        self.key = secrets.token_urlsafe(24)
        # install, then test, then done; or the uninstaller's steps (REMOVE_PHASES), then done
        self.phase = "install" if action == "install" else "check"
        self.result = None  # installed / failed / interrupted; or removed / incomplete / failed
        self.test = None  # passed / failed
        self.retested = False  # the checks failed once and were run again
        self.retest_at = None  # when to run them again (time.monotonic())
        self.generic_test = not os.path.isfile(os.path.join(opts.tests, app + ".sh"))
        # Swizzin's check is wrong for these apps (check_app.py says why): the App Store's own check replaces it
        self.check = "app-store" if action == "install" and app in check_app.SERVICES else "swizzin"
        self.started, self.ended = time.time(), None
        self.size = size
        self.pid = self.fd = self.exited_at = self.status = None
        self.decoder = None
        self.replay, self.replay_len, self.trimmed = [], 0, False
        self.typed = 0
        self.controller = None
        self.checks, self.archive, self.message, self.retry = [], None, None, False  # an uninstallation's outcome
        self.state_path = os.path.join(opts.logs, self.id + ".state")  # written by remove_runner.py
        self.result_path = os.path.join(opts.logs, self.id + ".result")
        try:
            self.log = os.fdopen(os.open(os.path.join(opts.logs, self.id + ".log"),
                                         os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab", buffering=0)
        except OSError as e:  # the log is a help, never a reason to refuse or stop an installation
            log.warning("no log for this installation: %s", e)
            self.log = None
        self.logged = 0

    def summary(self):
        summary = {"id": self.id, "app": self.app, "action": self.action, "phase": self.phase, "result": self.result,
                   "test": self.test, "check": self.check,
                   "generic_test": self.generic_test, "retested": self.retested, "started": int(self.started),
                   "ended": int(self.ended) if self.ended else None}
        if self.action == "remove":
            summary.update(checks=self.checks, archive=self.archive, message=self.message, retry=self.retry)
        return summary

    def replay_text(self):
        text = "".join(self.replay)
        if self.trimmed and "\n" in text:  # the oldest output was dropped: start on a whole line
            text = text[text.index("\n") + 1:]
        return text


class Helper:
    def __init__(self, opts):
        self.opts = opts
        self.sel = selectors.DefaultSelector()
        self.clients = {}
        self.job = None  # the running installation, or the last one of this run
        self.last = None  # summary of the last finished installation
        self.tickets = {}  # ticket -> (app, expiry): the confirmation window shown for an uninstallation
        self.marker_misses = 0  # looks in a row that did not find PANEL_MARKER
        if opts.allow_uid is not None:
            self.allowed, self.group = {0, opts.allow_uid}, None
        else:
            user = pwd.getpwnam(opts.panel_user)
            self.allowed, self.group = {0, user.pw_uid}, user.pw_gid
        os.makedirs(opts.logs, mode=0o700, exist_ok=True)
        self._recover()
        self.listener = self._listen()

    # --- socket and clients ---------------------------------------------------------------

    def _listen(self):
        path = self.opts.socket
        if self.group is not None:  # /run/swizzin-store: root and the panel's group only
            os.chown(os.path.dirname(path), 0, self.group)
            os.chmod(os.path.dirname(path), 0o750)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o117)  # created 0660, never readable by others even for a moment
        try:
            s.bind(path)
        finally:
            os.umask(old)
        if self.group is not None:
            os.chown(path, 0, self.group)
        s.listen(16)
        s.setblocking(False)
        self.sel.register(s, selectors.EVENT_READ, None)
        return s

    def run(self):
        """Serves until the panel is gone (PANEL_MARKER), and never stops in the middle of a job for that."""
        next_look = time.monotonic() + self.opts.marker_every
        while True:
            running = self.job is not None and self.job.phase != "done"
            wait = max(next_look - time.monotonic(), 0)
            for key, events in self.sel.select(min(wait, 0.5) if running else wait):
                if key.data is None:
                    self._accept()
                elif isinstance(key.data, Job):
                    self._on_pty(key.data)
                else:
                    c = key.data
                    if events & selectors.EVENT_READ:
                        self._read(c)
                    if events & selectors.EVENT_WRITE and not c.closed:
                        self._write(c)
            self._poll_job()
            if time.monotonic() >= next_look:
                next_look = time.monotonic() + self.opts.marker_every
                if self._panel_gone() and (self.job is None or self.job.phase == "done"):
                    log.warning("stopping: %s is gone, so the panel was removed or is no longer Heliedan's edition."
                                " This service will not start again (heliedan.sh remove-store removes it).",
                                self.opts.panel_marker)
                    return

    def _panel_gone(self):
        """One look at the panel: True once PANEL_MARKER was missing at two looks in a row (updating the panel with
        git replaces the file, so a single miss is not enough)."""
        self.marker_misses = 0 if panel_present(self.opts.panel_marker) else self.marker_misses + 1
        return self.marker_misses >= 2

    def _accept(self):
        try:
            sock, _ = self.listener.accept()
        except BlockingIOError:
            return
        uid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))[1]
        panel_clients = sum(1 for other in self.clients.values() if other.uid != 0)  # root (install.sh) always gets in
        if uid not in self.allowed or (uid != 0 and panel_clients >= MAX_CLIENTS):
            log.warning("connection refused (uid %d, %d clients)", uid, len(self.clients))
            sock.close()
            return
        sock.setblocking(False)
        c = Client(sock, uid)
        self.clients[sock] = c
        self.sel.register(sock, selectors.EVENT_READ, c)

    def _read(self, c):
        try:
            data = c.sock.recv(65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self._close(c)
            return
        c.inbuf += data
        while b"\n" in c.inbuf and not c.closed:
            line, _, rest = c.inbuf.partition(b"\n")
            c.inbuf = bytearray(rest)
            self._dispatch(c, bytes(line))
        if len(c.inbuf) > MAX_LINE:
            log.warning("line too long, client dropped")
            self._close(c)

    def _dispatch(self, c, line):
        msg = None
        if len(line) <= MAX_LINE:
            try:
                msg = json.loads(line)
            except (ValueError, RecursionError):  # RecursionError: deeply nested arrays
                pass
        op = msg.get("op") if isinstance(msg, dict) else None
        handler = {"catalogue": self._op_catalogue, "install": self._op_install, "attach": self._op_attach,
                   "input": self._op_input, "resize": self._op_resize, "remove-preview": self._op_remove_preview,
                   "remove": self._op_remove}.get(op) if isinstance(op, str) else None
        if handler is None:
            self._send(c, {"ok": False, "error": "bad-request"})
            return
        try:
            handler(c, msg)
        except Exception:  # no request may stop the service, and with it the installation in progress
            log.exception("request %s failed", op)
            self._send(c, {"ok": False, "error": "bad-request"})

    def _send(self, c, obj):
        if c.closed:
            return
        c.out += json.dumps(obj).encode() + b"\n"
        if len(c.out) > MAX_PENDING_OUTPUT:  # a client that does not read is dropped; it can come back
            log.warning("client too slow, dropped")
            self._close(c)
            return
        self._write(c)

    def _broadcast(self, obj):
        for c in list(self.clients.values()):
            if c.attached:
                self._send(c, obj)

    def _write(self, c):
        try:
            sent = c.sock.send(c.out)
            del c.out[:sent]
        except BlockingIOError:
            pass
        except OSError:
            self._close(c)
            return
        self.sel.modify(c.sock, selectors.EVENT_READ | (selectors.EVENT_WRITE if c.out else 0), c)

    def _close(self, c):
        if c.closed:
            return
        c.closed = True
        self.sel.unregister(c.sock)
        c.sock.close()
        del self.clients[c.sock]
        if self.job is not None and self.job.controller is c:
            self.job.controller = None

    # --- operations --------------------------------------------------------------------------

    def _op_catalogue(self, c, msg):
        running = self.job.summary() if self.job is not None and self.job.phase != "done" else None
        try:
            self._send(c, {"ok": True, "apps": catalogue(self.opts), "job": running, "last": self.last,
                           "features": FEATURES})
        except OSError as e:
            log.error("catalogue: %s", e)
            self._send(c, {"ok": False, "error": "unreadable"})

    def _op_install(self, c, msg):
        app = msg.get("app")
        try:
            if not isinstance(app, str) or not NAME.fullmatch(app):
                error = "invalid"
            elif app not in app_names(self.opts):
                error = "unknown"
            elif app in set(self.opts.blocked.split(",")):
                error = "blocked"
            elif installed(self.opts, app):
                error = "installed"
            elif self.job is not None and self.job.phase != "done":
                error = "busy"
            else:
                error = None
        except OSError as e:
            log.error("install check: %s", e)
            error = "unreadable"
        if error:
            self._send(c, {"ok": False, "error": error})
            return
        for other in self.clients.values():  # the tabs that followed the previous installation do not follow this one
            other.attached = False
        self.job = job = Job(self.opts, app, window(msg))
        job.controller, c.attached = c, True
        self._save(job)
        log.info("installing %s (job %s)", app, job.id)
        self._send(c, {"ok": True, "started": job.summary(), "control_key": job.key})
        self._spawn(job, [self.opts.box, "install", app])

    def _op_attach(self, c, msg):
        job = self.job
        if job is None:
            self._send(c, {"ok": False, "error": "no-job"})
            return
        key = msg.get("control_key")
        take = msg.get("take") is True  # the panel checked the admin password first (unless turned off)
        if job.phase != "done" and (take or (isinstance(key, str) and
                                             secrets.compare_digest(key.encode(), job.key.encode()))):
            previous = job.controller
            if previous is not None and previous is not c:
                self._send(previous, {"control": "lost"})
                if take:  # and its key with it: it cannot take control back silently
                    job.key = secrets.token_urlsafe(24)
            job.controller = c
        c.attached = True
        reply = {"ok": True, "attached": job.summary(), "controller": job.controller is c}
        if job.controller is c:
            reply["control_key"] = job.key
        self._send(c, reply)
        self._send(c, {"output": job.replay_text()})
        if job.phase == "done":
            self._send(c, {"done": job.summary()})

    def _op_input(self, c, msg):
        job, data = self.job, msg.get("data")
        if job is None or job.controller is not c:
            self._send(c, {"ok": False, "error": "not-controller"})
            return
        if job.fd is None:
            return  # between two phases, or finished: no installer to type into
        if not isinstance(data, str) or len(data) > MAX_INPUT:
            self._send(c, {"ok": False, "error": "invalid"})
            return
        try:
            os.write(job.fd, data.encode("utf-8", "replace"))
            job.typed += len(data)  # only the amount is logged, never what was typed
        except OSError:
            self._send(c, {"ok": False, "error": "input-lost"})

    def _op_resize(self, c, msg):
        job = self.job
        if job is None or job.controller is not c or job.fd is None:
            return
        job.size = window(msg)
        try:
            fcntl.ioctl(job.fd, termios.TIOCSWINSZ, struct.pack("HHHH", job.size[0], job.size[1], 0, 0))
        except OSError:
            pass

    # --- uninstalling ------------------------------------------------------------------------

    def _runner_args(self, app, retry=False):
        o = self.opts
        args = ["--app", app, "--swizzin", o.swizzin, "--locks", o.locks, "--archives", o.archives, "--root", o.root,
                "--work", o.work]
        if o.table:
            args += ["--table", o.table]
        if retry:
            args.append("--retry")
        return args + list(o.runner_option)

    def _runner_env(self):
        env = dict(JOB_ENV)
        if self.opts.test_path:
            env["PATH"] = self.opts.test_path + ":" + env["PATH"]
        for item in self.opts.test_env:
            name, _, value = item.partition("=")
            env[name] = value
        return env

    def _retry(self, app):
        """Its last removal stopped halfway: trying again is allowed even though the lock may be gone."""
        last = self.last or {}
        return last.get("app") == app and last.get("action") == "remove" and last.get("result") == "incomplete"

    def _remove_check(self, app):
        if not isinstance(app, str) or not NAME.fullmatch(app):
            return "invalid"
        if app not in app_names(self.opts):
            return "unknown"
        status = removal_status(self.opts, removable.load(self.opts.table), app)
        if status != "ok":
            return {"not-offered": "not-removable", "untested": "not-removable",
                    "other-system": "not-removable"}.get(status, status)
        if not installed(self.opts, app) and not self._retry(app):
            return "not-installed"
        if self.job is not None and self.job.phase != "done":
            return "busy"
        return None

    def _op_remove_preview(self, c, msg):
        app = msg.get("app")
        try:
            error = self._remove_check(app)
        except OSError as e:
            log.error("remove check: %s", e)
            error = "unreadable"
        if error:
            self._send(c, {"ok": False, "error": error})
            return
        retry = self._retry(app) and not installed(self.opts, app)
        try:
            opts = remove_runner.options(self._runner_args(app, retry) + ["--state", os.devnull, "--result", os.devnull])
            preview = remove_runner.Removal(opts).describe()
        except remove_runner.Refused as e:
            self._send(c, {"ok": False, "error": "refused", "message": str(e)})
            return
        ticket = secrets.token_urlsafe(18)
        self.tickets = {ticket: (app, time.monotonic() + TICKET_TTL)}  # one window at a time: the newest wins
        preview.update(app=app, retry=retry)
        self._send(c, {"ok": True, "preview": preview, "ticket": ticket})

    def _op_remove(self, c, msg):
        app = msg.get("app")
        try:
            error = self._remove_check(app)
        except OSError as e:
            log.error("remove check: %s", e)
            error = "unreadable"
        if not error:
            ticket, confirm = msg.get("ticket"), msg.get("confirm")
            saved = self.tickets.pop(ticket, None) if isinstance(ticket, str) else None
            if saved is None or saved[0] != app or time.monotonic() > saved[1]:
                error = "bad-ticket"
            elif not isinstance(confirm, str) or confirm.strip().lower() != app:  # the panel checked it too
                error = "confirm-mismatch"
        if error:
            self._send(c, {"ok": False, "error": error})
            return
        retry = self._retry(app) and not installed(self.opts, app)
        requester = msg.get("requester")
        requester = re.sub(r"[^\w.:@ -]", "?", requester)[:100] if isinstance(requester, str) else "?"
        for other in self.clients.values():  # the tabs that followed the previous job do not follow this one
            other.attached = False
        self.job = job = Job(self.opts, app, window(msg), action="remove")
        job.retry = retry
        job.controller, c.attached = c, True
        self._save(job)
        log.info("uninstalling %s for %s (job %s%s)", app, requester, job.id, ", trying again" if retry else "")
        self._send(c, {"ok": True, "started": job.summary(), "control_key": job.key})
        argv = [sys.executable, "-I", "-B", RUNNER, *self._runner_args(app, retry),
                "--state", job.state_path, "--result", job.result_path]
        self._spawn(job, argv, self._runner_env())

    @staticmethod
    def _runner_state(job):
        try:
            with open(job.state_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _follow_runner(self, job):
        """The uninstaller's step, read from its state file, passed on to the pages."""
        phase = self._runner_state(job).get("phase")
        if phase in REMOVE_PHASES and phase != job.phase:
            job.phase = phase
            self._broadcast({"phase": phase})

    def _removal_result(self, job):
        try:
            with open(job.result_path) as f:
                result = json.load(f)
        except (OSError, ValueError):
            result = None
        if isinstance(result, dict):
            job.result = result.get("result") if result.get("result") in ("removed", "incomplete", "failed") \
                else "incomplete"
            job.checks = [c for c in result.get("checks", []) if isinstance(c, dict)][:50]
            job.archive, job.message = result.get("archive"), result.get("message")
        else:  # the uninstaller stopped without a word
            job.result = "incomplete" if self._runner_state(job).get("script_started") else "failed"
            job.message = "the uninstaller stopped unexpectedly (see the log)"

    def _wait_removal(self, job):
        """This service is stopping during a removal. Before Swizzin's script, the uninstaller puts everything back
        (as with Stop); once the script runs, it is never cut off: wait for its end."""
        if job.pid is not None and not self._runner_state(job).get("script_started"):
            try:
                os.kill(job.pid, signal.SIGINT)
            except OSError:
                pass
        end = time.monotonic() + REMOVE_STOP_WAIT
        while job.pid is not None and time.monotonic() < end:
            if job.fd is not None:
                try:
                    data = os.read(job.fd, 65536)
                    if data:
                        self._output(job, data)
                except BlockingIOError:
                    pass
                except OSError:
                    self._close_pty(job)
            try:
                pid, _ = os.waitpid(job.pid, os.WNOHANG)
            except ChildProcessError:
                pid = job.pid
            if pid:
                job.pid = None
                break
            time.sleep(0.2)
        if job.pid is None:
            self._close_pty(job)
            self._removal_result(job)
            self._finish(job)
        else:
            job.result, job.message = "incomplete", "the service stopped during the removal: try again to finish it"
            job.phase, job.ended = "done", time.time()
            self._save(job)

    def _recover_removal(self, last, base):
        """A removal cut off by a crash. Before Swizzin's script, nothing was removed: the app is put back as it was
        (and an unfinished copy deleted). After, it is incomplete, and can be tried again."""
        try:
            with open(base + ".state") as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}
        if state.get("script_started"):
            last.update(result="incomplete", message="the service stopped during the removal: try again to finish it")
            return
        instances = state.get("instances") if isinstance(state.get("instances"), dict) else {}
        if state.get("phase") in ("stop", "copy", "remove"):
            enabled = [u for u, s in instances.items() if UNIT.fullmatch(u) and isinstance(s, dict) and s.get("enabled")]
            active = [u for u, s in instances.items() if UNIT.fullmatch(u) and isinstance(s, dict) and s.get("active")]
            for verb, units in (("enable", enabled), ("start", active)):
                if units:
                    subprocess.run(["systemctl", verb, *units], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=120)
        archive = state.get("archive")
        if state.get("phase") == "copy" and isinstance(archive, str) and \
                os.path.dirname(archive) == os.path.normpath(self.opts.archives):
            try:  # an unfinished copy
                os.unlink(archive)
            except OSError:
                pass
        last.update(result="failed", message="the service stopped before anything was removed: the app was put back")

    # --- the installation ----------------------------------------------------------------

    def _spawn(self, job, argv, env=None):
        rows, cols = job.size
        pid, fd = pty.fork()  # new session, the pty as controlling terminal: installers can read /dev/tty
        if pid == 0:
            try:
                # An ignored signal stays ignored across exec: Python ignores SIGPIPE, and whoever started this
                # service may have made it ignore SIGINT (a background job does), which would deafen Ctrl+C.
                for sig in (signal.SIGINT, signal.SIGQUIT, signal.SIGPIPE, signal.SIGHUP, signal.SIGTERM,
                            signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU):
                    signal.signal(sig, signal.SIG_DFL)
                os.chdir(self.opts.home)
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
                os.execve(argv[0], argv, env or JOB_ENV)
            finally:
                os._exit(127)
        os.set_blocking(fd, False)
        job.pid, job.fd, job.exited_at, job.status = pid, fd, None, None
        job.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.sel.register(fd, selectors.EVENT_READ, job)

    def _on_pty(self, job):
        try:
            data = os.read(job.fd, 65536)
        except BlockingIOError:
            return
        except OSError:  # EIO: nothing holds the terminal any more
            data = b""
        if data:
            self._output(job, data)
        else:
            self._close_pty(job)

    def _output(self, job, data):
        if job.log is not None and job.logged < MAX_LOG:
            try:
                job.log.write(data[:MAX_LOG - job.logged])
                job.logged += len(data)
            except OSError as e:  # a full disk: the installation goes on, without its log
                log.warning("job %s: log stopped: %s", job.id, e)
                job.log.close()
                job.log = None
        text = job.decoder.decode(data)
        if not text:
            return
        job.replay.append(text)
        job.replay_len += len(text)
        while job.replay_len > REPLAY and len(job.replay) > 1:
            job.replay_len -= len(job.replay.pop(0))
            job.trimmed = True
        self._broadcast({"output": text})

    def _close_pty(self, job):
        if job.fd is None:
            return
        self.sel.unregister(job.fd)
        os.close(job.fd)  # anything still attached to the terminal gets SIGHUP
        job.fd = None

    def _poll_job(self):
        """The phase ends when its process exits (not when the terminal closes: a daemon may keep it open)."""
        job = self.job
        if job is None or job.phase == "done":
            return
        if job.action == "remove":
            self._follow_runner(job)
        if job.retest_at is not None:
            if time.monotonic() >= job.retest_at:
                job.retest_at = None
                self._start_checks(job)
            return
        if job.pid is None:
            return
        if job.exited_at is None:
            try:
                pid, status = os.waitpid(job.pid, os.WNOHANG)
            except ChildProcessError:
                pid, status = job.pid, 0
            if pid == 0:
                return
            job.status = os.waitstatus_to_exitcode(status)
            job.exited_at = time.monotonic()
        while job.fd is not None:  # what the process wrote just before exiting
            try:
                data = os.read(job.fd, 65536)
            except BlockingIOError:
                break
            except OSError:
                data = b""
            if not data:
                self._close_pty(job)
                break
            self._output(job, data)
        if job.fd is not None and time.monotonic() - job.exited_at < 1:
            return  # wait up to a second for the rest
        self._close_pty(job)
        self._next_phase(job)

    def _next_phase(self, job):
        job.pid = None
        if job.action == "remove":
            self._removal_result(job)
            self._finish(job)
            return
        if job.phase == "install" and installed(self.opts, job.app):
            job.phase = "test"
            self._start_checks(job)
            return
        if job.phase == "test" and job.status != 0 and not job.retested and self.opts.retest_delay > 0 \
                and job.check == "swizzin":  # the App Store's check already waits for a slow app
            job.retested, job.retest_at = True, time.monotonic() + self.opts.retest_delay
            self._output(job, ("\r\n[App Store] The checks failed. Some apps take a while to start: checking again "
                               "in {} seconds...\r\n".format(self.opts.retest_delay)).encode())
            self._broadcast({"phase": "retest", "seconds": self.opts.retest_delay})
            return
        if job.phase == "test":
            job.result, job.test = "installed", ("passed" if job.status == 0 else "failed")
        else:
            job.result = "failed"
        self._finish(job)

    def _start_checks(self, job):
        self._broadcast({"phase": "test", "generic_test": job.generic_test, "check": job.check})
        if job.check == "app-store":
            self._spawn(job, [sys.executable, "-I", "-B", self.opts.app_check, job.app])
        else:
            self._spawn(job, [self.opts.box, "test", job.app])

    def _finish(self, job):
        job.phase, job.ended = "done", time.time()
        if job.action == "remove":
            checks = "%d/%d passed" % (sum(1 for c in job.checks if c.get("ok")), len(job.checks)) if job.checks else "none"
        else:
            checks = job.test
        log.info("job %s: %s (checks: %s, %d characters typed)", job.id, job.result, checks, job.typed)
        self._save(job)
        self.last = job.summary()
        self._broadcast({"done": job.summary()})
        if job.log is not None:
            job.log.close()
        self._prune_logs()

    def interrupt(self):
        """The service is stopping in the middle of an installation."""
        job = self.job
        if job is None or job.phase == "done":
            return
        if job.action == "remove":
            self._wait_removal(job)
            return
        if job.pid is not None:
            try:
                os.killpg(job.pid, signal.SIGTERM)  # the installer leads its own session
            except OSError:
                pass
        job.result = "interrupted"
        job.phase, job.ended = "done", time.time()
        self._save(job)

    def _save(self, job):
        path = os.path.join(self.opts.logs, job.id + ".json")
        try:
            fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(job.summary(), f)
            os.replace(path + ".tmp", path)
        except OSError as e:  # a full disk: the summary is lost, the installation is not
            log.warning("job %s: summary not saved: %s", job.id, e)

    def _recover(self):
        """Last installation of a previous run; one cut off by a stop or a crash is marked interrupted."""
        summaries = sorted(glob.glob(os.path.join(self.opts.logs, "*.json")))
        if not summaries:
            return
        try:
            with open(summaries[-1]) as f:
                last = json.load(f)
        except (OSError, ValueError):
            return
        last.setdefault("action", "install")  # saved by a version that only installed
        if last.get("phase") != "done":
            if last["action"] == "remove":
                self._recover_removal(last, summaries[-1][:-len(".json")])
            else:
                last.update(result="interrupted")
            last["phase"] = "done"
            with open(summaries[-1], "w") as f:
                json.dump(last, f)
        self.last = last

    def _prune_logs(self):
        for kind in ("*.log", "*.json", "*.state", "*.result"):
            for path in sorted(glob.glob(os.path.join(self.opts.logs, kind)))[:-KEEP_LOGS]:
                try:
                    os.unlink(path)
                except OSError:
                    pass


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    opts = options(argv)
    if not panel_present(opts.panel_marker):  # before anything is created: no socket, no log, no recovery
        log.warning("not started: %s is missing, so the panel was removed or is no longer Heliedan's edition"
                    " (heliedan.sh status says more, heliedan.sh remove-store removes this service).", opts.panel_marker)
        return
    helper = Helper(opts)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log.info("listening on %s", opts.socket)
    try:
        helper.run()
    finally:
        helper.interrupt()
        try:
            os.unlink(opts.socket)
        except OSError:
            pass


if __name__ == "__main__":
    main()
