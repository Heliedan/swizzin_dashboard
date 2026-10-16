#!/usr/bin/python3
"""Swizzin App Store helper: the only part of the App Store that runs as root.

The panel runs as the `swizzin` user and cannot even read the list of Swizzin
applications (/etc/swizzin is readable by root only), so it asks this service over
a unix socket. One JSON object per line, in both directions. The service answers a
few fixed operations and never runs a command, a path or an environment that comes
from the client. Only root and the panel user may connect (checked with SO_PEERCRED).

Operation: {"op": "catalogue"} -> {"ok": true, "apps": [{name, description,
installed, blocked}], "job": null}

Installed by store-helper/install.sh into /usr/local/lib/swizzin-store/ (root-owned),
never run from /opt/swizzin, which the panel user can write. Standard library only.
"""
import argparse
import json
import logging
import os
import pwd
import re
import selectors
import signal
import socket
import struct
import sys

NAME = re.compile(r"[a-z0-9]{1,32}")
# Installed over SSH only: they change how the server itself is reached or secured,
# give root access from the web, or do something unexpected (xmrig mines to a pool).
BLOCKED = ("panel", "nginx", "letsencrypt", "quota", "wireguard", "x2go", "xmrig", "webmin", "shellinabox")
MAX_CLIENTS = 8
MAX_LINE = 64 * 1024
MAX_PENDING_OUTPUT = 2 * 1024 * 1024

log = logging.getLogger("swizzin-store")


def options(argv=None):
    p = argparse.ArgumentParser(description="Swizzin App Store helper (root service).")
    p.add_argument("--socket", default="/run/swizzin-store/store.sock")
    p.add_argument("--scripts", default="/etc/swizzin/scripts/install", help="Swizzin install scripts")
    p.add_argument("--lang", default="/etc/swizzin/sources/lang_en", help="Swizzin descriptions")
    p.add_argument("--locks", default="/install", help="Swizzin lock files (.<app>.lock)")
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


def catalogue(opts):
    """Every Swizzin install script, read afresh at each request."""
    names = sorted(f[:-3] for f in os.listdir(opts.scripts) if f.endswith(".sh") and NAME.fullmatch(f[:-3]))
    installed = {f[1:-5] for f in os.listdir(opts.locks) if f.startswith(".") and f.endswith(".lock")}
    text = descriptions(opts.lang)
    blocked = set(opts.blocked.split(","))
    return [{"name": n, "description": text.get(n.upper(), ""), "installed": n in installed,
             "blocked": n in blocked} for n in names]


class Client:
    def __init__(self, sock):
        self.sock = sock
        self.inbuf = bytearray()
        self.out = bytearray()
        self.closed = False


class Helper:
    def __init__(self, opts):
        self.opts = opts
        self.sel = selectors.DefaultSelector()
        self.clients = {}
        if opts.allow_uid is not None:
            self.allowed, self.group = {0, opts.allow_uid}, None
        else:
            user = pwd.getpwnam(opts.panel_user)
            self.allowed, self.group = {0, user.pw_uid}, user.pw_gid
        self.listener = self._listen()

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
            for key, events in self.sel.select():
                if key.data is None:
                    self._accept()
                    continue
                c = key.data
                if events & selectors.EVENT_READ:
                    self._read(c)
                if events & selectors.EVENT_WRITE and not c.closed:
                    self._write(c)

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
        op = None
        if len(line) <= MAX_LINE:
            try:
                msg = json.loads(line)
                op = msg.get("op") if isinstance(msg, dict) else None
            except ValueError:
                pass
        if op == "catalogue":
            try:
                self._send(c, {"ok": True, "apps": catalogue(self.opts), "job": None})
            except OSError as e:
                log.error("catalogue: %s", e)
                self._send(c, {"ok": False, "error": "unreadable"})
        else:
            self._send(c, {"ok": False, "error": "bad-request"})

    def _send(self, c, obj):
        c.out += json.dumps(obj).encode() + b"\n"
        if len(c.out) > MAX_PENDING_OUTPUT:  # a client that does not read is dropped; it can come back
            log.warning("client too slow, dropped")
            self._close(c)
            return
        self._write(c)

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


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    opts = options(argv)
    helper = Helper(opts)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log.info("listening on %s", opts.socket)
    try:
        helper.run()
    finally:
        try:
            os.unlink(opts.socket)
        except OSError:
            pass


if __name__ == "__main__":
    main()
