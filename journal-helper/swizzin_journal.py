#!/usr/bin/python3
"""Swizzin journal reader: the last lines of a service's journal, for the Logs buttons of the panel. Read-only.

The journal of the system is readable by root and the groups systemd-journal and adm only, never by the panel's
account. This small program reads it for the panel, and does nothing else: it does not run as root, but as its own
system user (swizzin-journal) with the group systemd-journal, under a hardened systemd unit (swizzin-journal@.service:
no privileges, no network, a read-only system, a few system calls). systemd starts it for each connection to its
socket (swizzin-journal.socket, Accept=yes), which only root and the panel's group can open; it checks the account of
the other end again (SO_PEERCRED), reads one request, answers it and exits.

  {"op": "journal", "unit", "lines", "requester"} -> {"ok": true, "lines": [text], "cut", "status"}
                                                      or {"ok": false, "error"}
  {"op": "hello"}                                   -> {"ok": true, "features": FEATURES} (install.sh's check)

Only the services of the panel's profiles (JOURNAL_UNITS), passed as --unit=<name> to a fixed journalctl command,
200 lines, 256 KiB and 5 seconds at most; terminal escape sequences are removed from the lines and control characters
replaced. Each read is noted in this service's own log (journalctl -u 'swizzin-journal@*'), with who asked.

Installed by journal-helper/install.sh into /usr/local/lib/swizzin-journal/ (root-owned), never run from
/opt/swizzin, which the panel user can write. Standard library only, Python 3.8 or newer.
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
import subprocess
import sys
import time

PANEL_USER = "swizzin"
FEATURES = ["journal"]  # the operations of this version
MAX_REQUEST = 64 * 1024
REQUEST_TIMEOUT = 5  # seconds the panel has to send its request
# The journal, for the Logs buttons of the panel (admin only, checked by the panel). Only the services of the panel's
# profiles (core/profiles.py and core/profiles_shared.py; tests/test_journal_helper.py keeps this list in step with
# them), never another unit of the system: the panel's own list lives where the panel user can write, so this program
# keeps its copy. A name that ends with @ is a template: any one instance of it (rtorrent@<user>.service).
JOURNAL_UNITS = frozenset((
    "audiobookshelf@", "autobrr@", "bazarr", "bazarr@", "btsync@", "calibrecs", "calibreweb", "couchpotato",
    "deluge-web@", "deluged@", "emby-server", "emby@", "filebrowser", "filebrowser@", "flood@", "headphones", "irssi@",
    "jackett@", "jellyfin", "jellyfin@", "lidarr", "lidarr@", "lounge", "mango", "medusa", "medusa@", "mylar",
    "navidrome", "netdata", "netronome@", "nextcloud@", "nzbget@", "nzbhydra", "ombi", "ombi@", "plex@",
    "plexmediaserver", "plexpy@", "prowlarr", "prowlarr@", "pyload", "qbittorrent@", "quasselcore", "qui@", "radarr",
    "radarr@", "readarr", "readarr@", "resilio-sync", "rtorrent@", "sabnzbd", "sabnzbd@", "shellinabox", "sickchill",
    "sickgear", "sonarr", "sonarr@", "subsonic", "syncthing@", "tautulli", "tautulli@", "thelounge@", "transmission@",
    "webmin", "wg-quick@", "xmrig", "znc"))
# A unit's name as the panel sends it: never an option (it starts with a letter or a digit), a path (no /, and
# journal_unit refuses ..), a pattern (journalctl takes * ? [ as one) or another type of unit.
UNIT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._-]{0,200}\.service")
JOURNAL_LINES = 100  # lines read when the panel says nothing usable
MAX_JOURNAL_LINES = 200
JOURNAL_TIMEOUT = 5  # seconds journalctl has to answer
MAX_JOURNAL_OUTPUT = 256 * 1024  # bytes of its output kept, the newest
MAX_JOURNAL_ROWS = 2000  # lines sent at most (a message of several lines prints several)
MAX_JOURNAL_LINE = 1000  # characters of a line shown
JOURNALCTL = ("/usr/bin/journalctl", "/bin/journalctl")  # /bin only, on a Debian 11 whose /usr is not merged
# journalctl's whole environment: plain text, no pager, no colours, no links.
JOURNAL_ENV = {"LANG": "C.UTF-8", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "PAGER": "cat", "SYSTEMD_PAGER": "cat",
               "SYSTEMD_COLORS": "0", "SYSTEMD_URLIFY": "0", "LESSSECURE": "1"}
# Removed from the lines: the terminal's escape sequences (colours, titles, links... an app may write them in its
# log); then every control character left is shown as U+FFFD, and so are the characters that reorder text on screen.
ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]?|\][^\x07\x1b\n]*(?:\x07|\x1b\\)?|[PX^_][^\x1b\n]*(?:\x1b\\)?"
                    r"|[ -/]*[0-~]?)")
CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]")

log = logging.getLogger("swizzin-journal")


def options(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--panel-user", default=PANEL_USER,
                   help="the only account besides root whose requests are answered")
    p.add_argument("--allow-uid", type=int, help="tests: the account allowed instead of the panel user's")
    p.add_argument("--journalctl", help="tests: the journalctl to run (default: the system's, see JOURNALCTL)")
    p.add_argument("--journal-timeout", type=float, default=JOURNAL_TIMEOUT, help="tests: seconds journalctl has")
    return p.parse_args(argv)


def journal_unit(unit):
    """None when the panel may read the journal of this unit, else the error: invalid (not a service's name as the
    panel sends it) or not-allowed (no profile of the panel has it, see JOURNAL_UNITS)."""
    if not isinstance(unit, str) or not UNIT_NAME.fullmatch(unit) or ".." in unit:
        return "invalid"
    template, at, instance = unit[:-len(".service")].partition("@")
    if at:
        allowed = template + "@" in JOURNAL_UNITS and instance and "@" not in instance
    else:
        allowed = template in JOURNAL_UNITS
    return None if allowed else "not-allowed"


def journal_lines(value):
    """How many lines to read: a whole number from 1 to MAX_JOURNAL_LINES, else JOURNAL_LINES."""
    if isinstance(value, bool) or not isinstance(value, int):
        return JOURNAL_LINES
    return min(max(value, 1), MAX_JOURNAL_LINES)


def journal_text(data, cut):
    """journalctl's output as lines a page can show as text, and whether some were left out (cut: its oldest
    output was dropped, so its first line lost its beginning)."""
    lines = data.decode("utf-8", "replace").split("\n")
    if lines[-1] == "":
        lines.pop()
    if cut and len(lines) > 1:  # one line alone is the end of a very long one: kept, cut below
        lines.pop(0)
    if len(lines) > MAX_JOURNAL_ROWS:
        lines, cut = lines[-MAX_JOURNAL_ROWS:], True
    out = []
    for line in lines:
        line = CONTROL.sub("\ufffd", ESCAPE.sub("", line))
        out.append(line[:MAX_JOURNAL_LINE] + "\u2026" if len(line) > MAX_JOURNAL_LINE else line)
    return out, cut


def requester(msg):
    """Who the panel says asked ("<account> from <address>"), for this service's log only."""
    who = msg.get("requester")
    return re.sub(r"[^\w.:@ -]", "?", who)[:100] if isinstance(who, str) else "?"


def peer_uid(conn):
    """The account of the other end of the connection, as the kernel says (SO_PEERCRED)."""
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", creds)[1]


def allowed_uids(opts):
    if opts.allow_uid is not None:
        return {0, opts.allow_uid}
    try:
        return {0, pwd.getpwnam(opts.panel_user).pw_uid}
    except KeyError:  # no panel: root only
        return {0}


def read_request(conn):
    """The request: one line of JSON, within REQUEST_TIMEOUT seconds. None when it is not one."""
    conn.settimeout(REQUEST_TIMEOUT)
    data = bytearray()
    try:
        while b"\n" not in data and len(data) <= MAX_REQUEST:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
    except OSError:
        return None
    line = bytes(data).split(b"\n", 1)[0]
    if len(line) > MAX_REQUEST:
        return None
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None


def find_journalctl(opts):
    paths = (opts.journalctl,) if opts.journalctl else JOURNALCTL
    return next((p for p in paths if os.path.isfile(p) and os.access(p, os.X_OK)), None)


def run_journalctl(journalctl, unit, lines, timeout):
    """(error, data, cut, status): journalctl's output, its newest MAX_JOURNAL_OUTPUT bytes, gathered as it comes."""
    argv = [journalctl, "--unit=" + unit, "--lines=%d" % lines, "--output=short-iso", "--no-hostname", "--all",
            "--quiet", "--no-pager"]
    try:  # its own session: killed with whatever it started
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=JOURNAL_ENV, cwd="/", start_new_session=True)
    except OSError as e:
        log.error("journal of %s: %s", unit, e)
        return "failed", b"", False, None
    data, cut, error = bytearray(), False, None
    deadline = time.monotonic() + timeout
    fd = proc.stdout.fileno()
    with selectors.DefaultSelector() as sel:
        sel.register(fd, selectors.EVENT_READ)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                log.warning("journal of %s: no end within %g seconds", unit, timeout)
                error = "timeout"
                break
            if not sel.select(left):
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_JOURNAL_OUTPUT:  # the newest lines come last: the oldest output goes
                del data[:len(data) - MAX_JOURNAL_OUTPUT]
                cut = True
    proc.stdout.close()
    status = None
    if error is None:  # the end of its output: it is exiting
        try:
            status = proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    if status is None or error:  # still there, or cut off: with whatever it started (its session)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
    return error, bytes(data), cut, status


def answer(opts, msg):
    """What to send back for this request."""
    if msg is not None and msg.get("op") == "hello":
        return {"ok": True, "features": FEATURES}
    if msg is None or msg.get("op") != "journal":
        return {"ok": False, "error": "bad-request"}
    unit = msg.get("unit")
    error = journal_unit(unit)
    journalctl = find_journalctl(opts) if error is None else None
    if error is None and journalctl is None:
        error = "no-journal"
    if error:
        if error in ("invalid", "not-allowed"):  # the panel never asks for these: noted
            log.warning("journal of %s refused (%s) for %s", repr(unit)[:120], error, requester(msg))
        return {"ok": False, "error": error}
    lines = journal_lines(msg.get("lines"))
    log.info("journal of %s (last %d lines) read for %s", unit, lines, requester(msg))
    error, data, cut, status = run_journalctl(journalctl, unit, lines, opts.journal_timeout)
    if error:
        return {"ok": False, "error": error}
    lines, cut = journal_text(data, cut)
    return {"ok": True, "lines": lines, "cut": cut, "status": status}


def serve(opts, conn):
    """One connection: the account checked, one request, one answer."""
    uid = peer_uid(conn)
    if uid not in allowed_uids(opts):
        log.warning("connection refused: uid %d is not the panel's", uid)
        return
    reply = answer(opts, read_request(conn))
    conn.settimeout(REQUEST_TIMEOUT)
    try:
        conn.sendall(json.dumps(reply).encode() + b"\n")
        conn.shutdown(socket.SHUT_WR)
    except OSError:  # the panel left: nobody waits for these lines any more
        pass


def main(argv=None):
    opts = options(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    # systemd hands over the connection as standard input (StandardInput=socket, Accept=yes)
    conn = socket.socket(fileno=os.dup(0))
    try:
        serve(opts, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
