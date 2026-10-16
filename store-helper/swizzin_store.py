#!/usr/bin/python3
"""Swizzin App Store helper: the only part of the App Store that runs as root.

The panel runs as the `swizzin` user and cannot even read the list of Swizzin
applications (/etc/swizzin is readable by root only), so it asks this service over
a unix socket. One JSON object per line, in both directions. The service answers a
few fixed operations and never runs a command, a path or an environment that comes
from the client. Only root and the panel user may connect (checked with SO_PEERCRED).

Operations:
  {"op": "catalogue"}                    -> {"ok", "apps": [{name, description, installed, blocked}], "job", "last"}
  {"op": "install", "app", "rows", "cols"} -> {"ok", "started": job, "control_key"} or {"ok": false, "error"}
  {"op": "attach", "control_key"|"take"}   -> {"ok", "attached": job, "controller"}, then {"output"}, {"phase"}, {"done"}
  {"op": "input", "data"} / {"op": "resize", "rows", "cols"}   (from the connection that controls the job only)

An installation runs `box install <app>` in a pseudo-terminal, exactly as over SSH (the
installers ask their questions on /dev/tty and draw whiptail menus), then `box test <app>`
when the app's lock file appeared. One at a time. Whoever started it, or later took
control (the panel asks for the admin password first), may type in it; others only watch.

Installed by store-helper/install.sh into /usr/local/lib/swizzin-store/ (root-owned),
never run from /opt/swizzin, which the panel user can write. Standard library only.
"""
import argparse
import codecs
import fcntl
import glob
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
import struct
import sys
import termios
import time

NAME = re.compile(r"[a-z0-9]{1,32}")
# Installed over SSH only: they change how the server itself is reached or secured,
# give root access from the web, or do something unexpected (xmrig mines to a pool).
BLOCKED = ("panel", "nginx", "letsencrypt", "quota", "wireguard", "x2go", "xmrig", "webmin", "shellinabox")
MAX_CLIENTS = 8
MAX_LINE = 64 * 1024
MAX_PENDING_OUTPUT = 2 * 1024 * 1024
MAX_INPUT = 4096
REPLAY = 512 * 1024
MAX_LOG = 50 * 1024 * 1024
KEEP_LOGS = 20
# The installers' whole environment: nothing comes from the client or from this service's own
# environment (scripts read common names such as user, pass or log). PATH ends like root's over SSH
# (Swizzin adds /usr/local/bin/swizzin in .bashrc). TERM is required: without
# it Swizzin's messages lose their text. NEEDRESTART_SUSPEND: apt must not restart services
# (this one included) in the middle of an installation.
JOB_ENV = {"HOME": "/root", "USER": "root", "LOGNAME": "root", "SHELL": "/bin/bash", "TERM": "xterm-256color",
           "LANG": "C.UTF-8", "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/bin/swizzin",
           "GIT_TERMINAL_PROMPT": "0", "NEEDRESTART_SUSPEND": "1"}

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
    p.add_argument("--blocked", default=",".join(BLOCKED), help="comma-separated apps shown as SSH only")
    return p.parse_args(argv)


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
    return [{"name": n, "description": text.get(n.upper(), ""), "installed": n in locks,
             "blocked": n in blocked} for n in app_names(opts)]


def window(msg):
    """Terminal size asked by the browser, kept within sane bounds."""
    try:
        rows, cols = int(msg.get("rows", 30)), int(msg.get("cols", 100))
    except (TypeError, ValueError):
        rows, cols = 30, 100
    return min(max(rows, 10), 200), min(max(cols, 40), 400)


class Client:
    def __init__(self, sock):
        self.sock = sock
        self.inbuf = bytearray()
        self.out = bytearray()
        self.closed = False
        self.attached = False


class Job:
    """One installation: `box install <app>`, then `box test <app>` if the app's lock appeared."""

    def __init__(self, opts, app, size):
        self.app = app
        self.id = time.strftime("%Y%m%d-%H%M%S") + "-" + app
        self.key = secrets.token_urlsafe(24)
        self.phase = "install"  # install, then test, then done
        self.result = None  # installed / failed / interrupted
        self.test = None  # passed / failed
        self.generic_test = not os.path.isfile(os.path.join(opts.tests, app + ".sh"))
        self.started, self.ended = time.time(), None
        self.size = size
        self.pid = self.fd = self.exited_at = self.status = None
        self.decoder = None
        self.replay, self.replay_len, self.trimmed = [], 0, False
        self.typed = 0
        self.controller = None
        self.log = os.fdopen(os.open(os.path.join(opts.logs, self.id + ".log"),
                                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab", buffering=0)
        self.logged = 0

    def summary(self):
        return {"id": self.id, "app": self.app, "phase": self.phase, "result": self.result, "test": self.test,
                "generic_test": self.generic_test, "started": int(self.started),
                "ended": int(self.ended) if self.ended else None}

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
        while True:
            running = self.job is not None and self.job.phase != "done"
            for key, events in self.sel.select(0.5 if running else None):
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

    def _accept(self):
        try:
            sock, _ = self.listener.accept()
        except BlockingIOError:
            return
        uid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))[1]
        if uid not in self.allowed or len(self.clients) >= MAX_CLIENTS:
            log.warning("connection refused (uid %d, %d clients)", uid, len(self.clients))
            sock.close()
            return
        sock.setblocking(False)
        c = Client(sock)
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
            except ValueError:
                pass
        op = msg.get("op") if isinstance(msg, dict) else None
        handler = {"catalogue": self._op_catalogue, "install": self._op_install, "attach": self._op_attach,
                   "input": self._op_input, "resize": self._op_resize}.get(op)
        if handler is None:
            self._send(c, {"ok": False, "error": "bad-request"})
        else:
            handler(c, msg)

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
            self._send(c, {"ok": True, "apps": catalogue(self.opts), "job": running, "last": self.last})
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
        if job.phase != "done" and (msg.get("take") is True or
                                    (isinstance(key, str) and secrets.compare_digest(key, job.key))):
            job.controller = c  # take: the panel checked the admin password first
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
        if job is None or job.controller is not c or job.fd is None:
            self._send(c, {"ok": False, "error": "not-controller"})
            return
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

    # --- the installation ----------------------------------------------------------------

    def _spawn(self, job, argv):
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
                os.execve(argv[0], argv, JOB_ENV)
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
        if job.logged < MAX_LOG:
            job.log.write(data[:MAX_LOG - job.logged])
            job.logged += len(data)
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
        if job is None or job.phase == "done" or job.pid is None:
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
        if job.phase == "install" and installed(self.opts, job.app):
            job.phase = "test"
            self._broadcast({"phase": "test", "generic_test": job.generic_test})
            self._spawn(job, [self.opts.box, "test", job.app])
            return
        if job.phase == "test":
            job.result, job.test = "installed", ("passed" if job.status == 0 else "failed")
        else:
            job.result = "failed"
        self._finish(job)

    def _finish(self, job):
        job.phase, job.ended = "done", time.time()
        log.info("job %s: %s (checks: %s, %d characters typed)", job.id, job.result, job.test, job.typed)
        self._save(job)
        self.last = job.summary()
        self._broadcast({"done": job.summary()})
        job.log.close()
        self._prune_logs()

    def interrupt(self):
        """The service is stopping in the middle of an installation."""
        job = self.job
        if job is None or job.phase == "done":
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
        fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(job.summary(), f)
        os.replace(path + ".tmp", path)

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
        if last.get("phase") != "done":
            last.update(phase="done", result="interrupted")
            with open(summaries[-1], "w") as f:
                json.dump(last, f)
        self.last = last

    def _prune_logs(self):
        for kind in ("*.log", "*.json"):
            for path in sorted(glob.glob(os.path.join(self.opts.logs, kind)))[:-KEEP_LOGS]:
                try:
                    os.unlink(path)
                except OSError:
                    pass


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    opts = options(argv)
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
