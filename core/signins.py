"""Sign-ins of the panel, for the admin: the section Panel sign-ins of the Overview layout (see the README).

With the Basic login, every request carries the password: there is no moment when someone "logs in". A sign-in here
is the first request of an account from an address after SESSION seconds without one from that pair (an open page asks
every few seconds, so it counts once). A failed attempt is a password refused by core/htpasswd.py; the failures of the
same account (or of the same unknown name) from the same address, less than GROUP seconds apart, make one line with a
count, so that a forgotten tab sending an old password does not fill the list. When the login protection refuses the
address after one of them, that line says for how long.
What is kept, and nothing else: the time (UTC), the account, the address and the result (the count, the block). The
name typed is kept only when it is an account of the panel: an unknown name may be a password typed in the wrong box.
Never a password, a fingerprint or a browser. The address is the one the login protection uses (client_address in
core/htpasswd.py): the one the local nginx passes on, IPv6 included, written the usual way (2001:db8::7, an IPv4
mapped in IPv6 as IPv4); something else is "invalid address".
The history lives in memory, and in signins.json next to swizzin.cfg (core/statefile.py: readable by the panel's
account only), so that it survives a restart of the panel. Only the panel that serves (python swizzin.py) keeps it on
disk: imported, as the tests do, it keeps it in memory only. SIGNIN_HISTORY_DAYS in swizzin.cfg: how long a line is
kept after its last request (30 days); 0 keeps it in memory only, until the panel restarts, and removes the file. Each
list keeps its newest KEEP lines (200 in all), so that failures never push the sign-ins out. A sign-in is written at
once; a failure at most once a minute, and what only moves a time or a count at most every 10 minutes, so that an
attack does not make the panel write its disk in a loop. Those writes are made by the next request, after that time.

GET /signins: the newest lines of each list, for the admin only (403 for the others), never cached. register() runs
once per import of swizzin.py, which is imported twice at startup: it reads and writes no file."""
import collections
import ipaddress
import logging
import os
import threading
import time

import flask

from core import statefile
from core.htpasswd import client_address

FILE = "signins.json"  # in the panel's folder, next to swizzin.cfg
DAYS, MAX_DAYS = 30, 365  # SIGNIN_HISTORY_DAYS: its default, and the most it may say
SESSION = 30 * 60  # seconds without a request of an account from an address before its next one is a new sign-in
GROUP = 10 * 60  # seconds between two failures of the same name from the same address that keep them on one line
KEEP = 100  # lines kept in each list: sign-ins, failed attempts
PAIRS = 1000  # (account, address) pairs remembered in memory, to tell an open page from a new sign-in
SHOWN = 10  # lines of each list that the page shows
FAILURE_WRITE = 60  # seconds between two writes made for failures
TOUCH_WRITE = 600  # seconds between two writes that only move a time or a count
MAX_COUNT = 10 ** 9

log = logging.getLogger(__name__)


def settings(config, logger=log):
    """SIGNIN_HISTORY_DAYS from swizzin.cfg, 0 to 365. An invalid value falls back to 30, with a warning in the log:
    a badly written setting must not stop the panel."""
    days = config.get("SIGNIN_HISTORY_DAYS", DAYS)
    if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= MAX_DAYS:
        logger.warning("swizzin.cfg: SIGNIN_HISTORY_DAYS = %r is invalid (expected: 0 to %d), %d used instead",
                       days, MAX_DAYS, DAYS)
        days = DAYS
    return days


def address_of(text):
    """The address written the usual way (2001:db8::7, an IPv4 mapped in IPv6 as IPv4, no zone), or None."""
    if not isinstance(text, str):
        return None
    try:
        ip = ipaddress.ip_address(text.strip().split("%", 1)[0])
    except ValueError:
        return None
    return str(getattr(ip, "ipv4_mapped", None) or ip)


def _time(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _signin(row):
    """A sign-in read from the file, or None when it is not one."""
    if not isinstance(row, dict):
        return None
    at, user = _time(row.get("at")), row.get("user")
    if at is None or not isinstance(user, str) or not user or len(user) > 255:
        return None
    last = _time(row.get("last"))
    return {"at": at, "last": max(at, last or 0), "user": user, "address": address_of(row.get("address"))}


def _failure(row):
    """A failed attempt read from the file, or None when it is not one."""
    if not isinstance(row, dict):
        return None
    at, user, count, blocked = _time(row.get("at")), row.get("user"), _time(row.get("count")), _time(row.get("blocked"))
    if at is None or not (user is None or isinstance(user, str) and 0 < len(user) <= 255) or not count:
        return None
    last = _time(row.get("last"))
    return {"at": at, "last": max(at, last or 0), "user": user, "address": address_of(row.get("address")),
            "count": min(count, MAX_COUNT), "blocked": blocked or 0}


class History:
    """The sign-ins and failed attempts of the panel: in memory, and in a file once keep_in() names one."""

    def __init__(self, days=DAYS, logger=log):
        self.days = days
        self.logger = logger
        self.clock = time.time
        self.lock = threading.Lock()  # green under eventlet (swizzin.py patches threading)
        self.file = None  # a statefile.StateFile once keep_in() names one; None: in memory only
        self.signins = []  # oldest first: {"at", "last", "user", "address"}
        self.failures = []  # oldest first: {"at", "last", "user", "address", "count", "blocked"}
        self.sessions = collections.OrderedDict()  # (user, address) -> its newest sign-in, least recent first
        self.written = 0  # when the file was last written (or a write failed)
        self.due = None  # when the file should be written next; None: nothing new
        self.problem = None  # the last error logged, so that it is logged once

    def keep_in(self, path):
        """Keep the history in the file `path` from now on, and read what it holds (lines too old, or that cannot be
        read, are left out, and the file is written again without them). The panel calls it once it serves, never
        when it is imported. With 0 days, nothing is kept on disk: a file left by an earlier setting is removed."""
        with self.lock:
            if self.days == 0:
                try:
                    os.remove(path)
                    self.logger.warning("Panel sign-ins: SIGNIN_HISTORY_DAYS = 0, %s removed (kept in memory only)",
                                        path)
                except FileNotFoundError:
                    pass
                except OSError as e:
                    self.logger.warning("Panel sign-ins: %s could not be removed (%s)", path, e)
                return
            self.file = statefile.StateFile(path)
            data = self.file.load(self.logger, "the sign-ins start empty")
            now = self._now()
            found = {}
            for key, read in (("signins", _signin), ("failures", _failure)):
                rows = data.get(key)
                rows = rows if isinstance(rows, list) else []
                found[key] = [row for row in map(read, rows) if row]
                if len(found[key]) != len(rows) or data.get("version") != 1:
                    self.due = now  # written again in the format of this version
            self.signins = sorted(found["signins"] + self.signins, key=lambda r: r["at"])[-KEEP:]
            self.failures = sorted(found["failures"] + self.failures, key=lambda r: r["last"])[-KEEP:]
            for row in self.signins:  # an open page goes on after a restart: no new sign-in for it
                self._remember((row["user"], row["address"]), row)
            if not data:
                self.due = None  # nothing to write: no file yet, or one that could not be read and stays as it is
            self._prune(now)
            self._write(now)

    def checked(self, user, address, ok, blocked=0):
        """A login checked by core/htpasswd.py: `user` is the account, None when the name is not one; `blocked`, the
        seconds this failure got the address refused for. Never raises: a problem is logged, and the login goes on."""
        try:
            with self.lock:
                now = self._now()
                address = address_of(address)
                if ok:
                    if isinstance(user, str) and user:
                        self._signed_in(user, address, now)
                else:
                    self._failed(user if isinstance(user, str) and user else None, address, now, blocked)
                self._prune(now)
                self._write(now)
        except Exception as e:  # pylint: disable=broad-except
            self._log("Panel sign-ins: a login could not be noted (%r)", e)

    def recent(self, address):
        """What GET /signins answers: the newest lines of each list, and the address of the request."""
        with self.lock:
            self._prune(self._now())
            # newest first; at the same second, the line made last first
            signins = sorted(self.signins[::-1], key=lambda r: r["at"], reverse=True)
            failures = sorted(self.failures[::-1], key=lambda r: r["last"], reverse=True)
            return {
                "signins": [{k: r[k] for k in ("at", "user", "address")} for r in signins[:SHOWN]],
                "signins_total": len(signins),
                "failures": [{k: r[k] for k in ("at", "last", "user", "address", "count", "blocked")}
                             for r in failures[:SHOWN]],
                "failures_total": len(failures),
                "your_address": address_of(address),
                "days": self.days, "on_disk": self.file is not None, "kept": KEEP,
                "session": SESSION // 60, "group": GROUP // 60,
            }

    # Under the lock.
    def _now(self):
        now = int(self.clock())
        if now < self.written:  # the clock went back: writes must not wait for it
            self.written = now
        return now

    def _remember(self, pair, row):
        self.sessions[pair] = row
        self.sessions.move_to_end(pair)
        while len(self.sessions) > PAIRS:
            self.sessions.popitem(last=False)

    def _signed_in(self, user, address, now):
        pair = (user, address)
        row = self.sessions.get(pair)
        if row is not None and now - row["last"] <= SESSION:  # the same visit (an open page)
            if now > row["last"]:
                row["last"] = now
                self._soon(TOUCH_WRITE)
            self.sessions.move_to_end(pair)
            return
        row = {"at": now, "last": now, "user": user, "address": address}
        self.signins.append(row)
        del self.signins[:-KEEP]
        self._remember(pair, row)
        self._soon(0)

    def _failed(self, user, address, now, blocked):
        blocked = blocked if isinstance(blocked, int) and not isinstance(blocked, bool) and blocked > 0 else 0
        row = next((r for r in reversed(self.failures) if r["user"] == user and r["address"] == address), None)
        if row is not None and not row["blocked"] and now - row["last"] < GROUP:
            row["count"] = min(row["count"] + 1, MAX_COUNT)
            row["last"] = max(row["last"], now)
            row["blocked"] = blocked
            self._soon(FAILURE_WRITE if blocked else TOUCH_WRITE)
            return
        self.failures.append({"at": now, "last": now, "user": user, "address": address, "count": 1,
                              "blocked": blocked})
        del self.failures[:-KEEP]
        self._soon(FAILURE_WRITE)

    def _soon(self, wait):
        """The file is to be written `wait` seconds after its last write, or sooner if already asked."""
        when = self.written + wait
        self.due = when if self.due is None else min(self.due, when)

    def _prune(self, now):
        """Leave out the lines whose last request is older than the days kept (none in memory only)."""
        if not self.days:
            return
        oldest = now - self.days * 86400
        for rows in (self.signins, self.failures):
            kept = [r for r in rows if r["last"] >= oldest]
            if len(kept) != len(rows):
                rows[:] = kept
                self._soon(TOUCH_WRITE)

    def _write(self, now):
        if self.file is None or self.due is None or now < self.due:
            return
        self.written = now
        try:
            self.file.write({"version": 1, "signins": self.signins, "failures": self.failures})
        except OSError as e:  # kept in memory, and written by a later request: a minute later, or a sign-in
            self.due = now + FAILURE_WRITE
            self._log("Panel sign-ins: %s could not be written (%s)", self.file.path, e.strerror or e)
            return
        self.due = None

    def _log(self, message, *args):
        text = message % args
        if text != self.problem:
            self.problem = text
            self.logger.warning(text)


def register(app, htpasswd):
    """Make the history of this panel, tell core/htpasswd.py to note each login in it, and add GET /signins. Returns
    the history: swizzin.py names its file once the panel serves (History.keep_in). Reads and writes no file."""
    history = History(settings(app.config, app.logger), app.logger)
    bp = flask.Blueprint("signins", __name__)

    @bp.route("/signins")
    @htpasswd.required
    def recent(user):
        if user != app.config["ADMIN_USER"]:
            flask.abort(403)
        found = app.extensions["signins"].recent(client_address(flask.request.environ))
        response = flask.jsonify(found)
        response.headers["Cache-Control"] = "no-store"
        return response

    app.register_blueprint(bp)
    app.extensions["signins"] = htpasswd.signin_history = history  # what the route reads, what the logins tell
    app.config["SIGNINS_CARD"] = True  # the section shows (templates/overview/signins.html)
    return history
