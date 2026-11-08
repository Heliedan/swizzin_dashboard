"""Logs of a service, for the admin: the Logs buttons of Service Info, in the Overview layout (see the README).

The journal of the system is readable by root only (and the groups systemd-journal and adm), never by the panel's
account. The App Store's root service reads it for the panel (store-helper/swizzin_store.py, operation "journal"):
for the services of the panel's profiles only, with a fixed command, a few hundred lines and 5 seconds at most, and a
line in its own log for each read. This module asks it for the service of a line of Service Info, as the Restart
button names it, and adds what systemd tells anyone without root (systemctl show): running or not, since when, and how
many times systemd restarted it by itself. Without the root service (heliedan.sh remove-store, or not installed),
the panel says why and still shows that state.

POST /apps/logs, JSON {"application": name}: the admin only (403), an app of their Service Info only (404). JSON
only, like /apps/service: a form of another site cannot make the panel read a journal. One read at a time; nothing is
kept, and the answer is never cached (Cache-Control: no-store): a log can hold secrets (API keys in addresses...).
register() runs once per import of swizzin.py, which is imported twice at startup: it has no other side effect."""
import os
import re
import subprocess
import threading
import time

import flask
from eventlet.patcher import original

import core.util as util  # module import only: core.util and swizzin.py import each other at startup
from core.custom.store import SOCKET, StoreUnavailable, helper_call
from core.htpasswd import client_address

LINES, MAX_LINES = 100, 200  # SERVICE_LOGS_LINES: its default, and the most the root service reads
WAIT = 10  # seconds to wait for the root service, which gives journalctl 5
TURN = WAIT + 5  # seconds a request waits for the read of another one to end
SYSTEMCTL_TIMEOUT = 5
# The same rule as the root service (UNIT_NAME in store-helper/swizzin_store.py): never an option, a path or a pattern.
UNIT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._-]{0,200}\.service")
PROPERTIES = ("LoadState", "ActiveState", "SubState", "Result", "NRestarts", "ActiveEnterTimestampMonotonic",
              "InactiveEnterTimestampMonotonic")
WORD = re.compile(r"[a-z][a-z-]{0,31}")  # the values of LoadState, ActiveState, SubState and Result
# Under eventlet, a timeout raises the TimeoutExpired of eventlet's own copy of the subprocess module (core/util.py).
TIMEOUT_EXPIRED = (subprocess.TimeoutExpired, original("subprocess").TimeoutExpired)
ALTDASH_LOCK = "/install/.altdash.lock"  # this edition installed with box install altdash: box manages the service

# What the panel says when there are no lines to show, by the "journal" of the answer.
MESSAGES = {
    "empty": "The journal has no lines for this service. Some apps write their log to their own files: the journal "
             "then only shows when they start and stop.",
    "cut": "Only the most recent lines are shown: the rest was too long.",
    "status": "journalctl ended with an error (code {}): its message is in the lines below.",
    "missing": "The lines of the journal are read by the App Store service (swizzin-store), which does not answer on "
               "this server. {} sets it up, and systemctl status swizzin-store says why it does not run.",
    "outdated": "The App Store service (swizzin-store) of this server is older than the panel and cannot read the "
                "journal: {} updates it.",
    "not-allowed": "The App Store service reads the journal of the services the panel knows only (its profiles), "
                   "not of this one.",
    "busy": "Another read of the journal is running: try again in a moment.",
    "timeout": "The journal did not answer within 5 seconds: try again later.",
    "no-journal": "journalctl was not found on this server: there is no journal to read.",
    "error": "The journal could not be read: journalctl -u swizzin-store says why.",
}


def settings(config, logger=None):
    """(on, lines) from swizzin.cfg: SERVICE_LOGS (True or False) and SERVICE_LOGS_LINES (1 to 200). An invalid value
    falls back to its default, with a warning in the log: a badly written setting must not stop the panel."""
    on = config.get("SERVICE_LOGS", True)
    if not isinstance(on, bool):
        if logger is not None:
            logger.warning("swizzin.cfg: SERVICE_LOGS = %r is invalid (expected: True or False), True used instead", on)
        on = True
    lines = config.get("SERVICE_LOGS_LINES", LINES)
    if isinstance(lines, bool) or not isinstance(lines, int) or not 1 <= lines <= MAX_LINES:
        if logger is not None:
            logger.warning("swizzin.cfg: SERVICE_LOGS_LINES = %r is invalid (expected: 1 to %d), %d used instead",
                           lines, MAX_LINES, LINES)
        lines = LINES
    return on, lines


def unit_of(systemd, user):
    """The unit that the Restart button of this line acts on (swizzin.py, /apps/service): a template (rtorrent@)
    takes the account's name. None when it is not a service's name that the root service would take."""
    if not isinstance(systemd, str) or not systemd:
        return None
    unit = systemd + user if systemd.endswith("@") else systemd
    if not unit.endswith(".service"):
        unit += ".service"
    return unit if UNIT_NAME.fullmatch(unit) and ".." not in unit else None


def unit_state(unit):
    """What systemctl show says of the unit, without root, or None when it cannot say: {"load", "active", "sub",
    "result", "restarts", "since"}. The properties come back in systemd's order, so they are read by name. restarts:
    NRestarts, the restarts systemd made by itself (Restart=), counted since the service was last started by hand or
    since the boot; since: when it entered its current state (running, stopped...), in seconds since 1970."""
    argv = ["systemctl", "show", "--no-pager"] + ["--property=" + p for p in PROPERTIES] + ["--", unit]
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=SYSTEMCTL_TIMEOUT)
    except TIMEOUT_EXPIRED + (OSError,):
        return None
    if done.returncode != 0:
        return None
    values = {}
    for line in done.stdout.decode("utf-8", "replace").splitlines():
        name, _, value = line.partition("=")
        values[name] = value
    state = {key: values.get(prop) if WORD.fullmatch(values.get(prop, "")) else None
             for key, prop in (("load", "LoadState"), ("active", "ActiveState"), ("sub", "SubState"),
                               ("result", "Result"))}
    restarts = values.get("NRestarts", "")
    state["restarts"] = int(restarts) if restarts.isascii() and restarts.isdigit() else None  # systemd 235 or newer
    entered = values.get("ActiveEnterTimestampMonotonic" if state["active"] in ("active", "reloading")
                         else "InactiveEnterTimestampMonotonic", "")
    # microseconds of the clock that does not jump (CLOCK_MONOTONIC, time.monotonic()); 0: never
    entered = int(entered) if entered.isascii() and entered.isdigit() else 0
    state["since"] = round(time.time() - (time.monotonic() - entered / 1e6)) if entered else None
    return state


def read_journal(unit, lines, path, requester):
    """(journal, lines, message): what the root service sent, in words for the page."""
    # how to set it up or update it, as the App Store page says (and the README of altdash)
    how = "sudo ALTDASH_STORE=yes box upgrade altdash" if os.path.isfile(ALTDASH_LOCK) else \
        "sudo bash heliedan.sh install"
    try:
        reply = helper_call({"op": "journal", "unit": unit, "lines": lines, "requester": requester}, path, WAIT)
    except StoreUnavailable as e:
        flask.current_app.logger.warning("Service logs: the App Store service does not answer (%s)", e)
        return "missing", [], MESSAGES["missing"].format(how)
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        error = reply.get("error") if isinstance(reply, dict) else None
        if error == "bad-request":  # a version of the service from before this operation
            return "outdated", [], MESSAGES["outdated"].format(how)
        journal = error if error in ("not-allowed", "busy", "timeout", "no-journal") else "error"
        return journal, [], MESSAGES[journal]
    found = reply.get("lines") if isinstance(reply.get("lines"), list) else []
    found = [line for line in found if isinstance(line, str)][-MAX_LINES * 10:]
    status = reply.get("status")
    if isinstance(status, int) and not isinstance(status, bool) and status != 0 and found:
        message = MESSAGES["status"].format(status)
    elif not found:
        message = MESSAGES["empty"]
    else:
        message = MESSAGES["cut"] if reply.get("cut") is True else None
    return "ok", found, message


def register(app, htpasswd):
    """Add POST /apps/logs, unless swizzin.cfg says SERVICE_LOGS = False; app.config["SERVICE_LOGS"] then says
    whether the Logs buttons show (templates/overview/services.html)."""
    on, lines = settings(app.config, app.logger)
    app.config["SERVICE_LOGS"] = on
    if not on:
        return
    bp = flask.Blueprint("service_logs", __name__)
    one_at_a_time = threading.Lock()  # green under eventlet: waiting here holds up nothing else
    app.extensions["service_logs"] = {"lock": one_at_a_time}

    @bp.route("/apps/logs", methods=["POST"])
    @htpasswd.required
    def logs(user):
        if user != app.config["ADMIN_USER"]:
            flask.abort(403)
        data = flask.request.get_json(silent=True)  # None for another content type or a body that is not JSON
        name = data.get("application") if isinstance(data, dict) else None
        if not isinstance(name, str):
            return flask.jsonify(error="bad request"), 400
        page = next((p for p in util.generate_page_list(user) if p.get("name") == name and p.get("systemd")), None)
        unit = unit_of(page.get("systemd"), user) if page else None
        if unit is None:
            return flask.jsonify(error="unknown service"), 404
        state = unit_state(unit)
        if one_at_a_time.acquire(timeout=TURN):
            try:
                journal, found, message = read_journal(
                    unit, lines, app.config.get("STORE_SOCKET", SOCKET),
                    "{} from {}".format(user, client_address(flask.request.environ)))
            finally:
                one_at_a_time.release()
        else:
            journal, found, message = "busy", [], MESSAGES["busy"]
        response = flask.jsonify(name=page.get("pretty_name") or name, unit=unit, state=state, journal=journal,
                                 lines=found, message=message)
        response.headers["Cache-Control"] = "no-store"
        return response

    app.register_blueprint(bp)
