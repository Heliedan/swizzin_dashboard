"""App Store page of the panel: every Swizzin application, installed or not, and installing one.

The list comes from the swizzin-store helper (store-helper/ in this repository), a small
root service: /etc/swizzin is readable by root only, and the panel runs as `swizzin`. The
helper also runs the installations (`box install <app>` in a pseudo-terminal); this module
relays them to the page over the Socket.IO namespace /store, one helper connection per tab.
Admin only: the page, the token and the namespace check the admin's Basic login, the
namespace also wants the page's token, and installing or taking control asks for the admin
password again (counted with the other failed logins).

This package lives in core/custom/ so that `box upgrade panel` keeps it. register() runs
once per import of swizzin.py (it is imported twice at startup), so it creates its own
Blueprint each time and has no other side effect.
"""
import hmac
import json
import os
import secrets
import socket
import threading

import flask
from flask_socketio import emit

import core.util as util  # module import only: core.util and swizzin.py import each other at startup
from core.htpasswd import client_address

SOCKET = "/run/swizzin-store/store.sock"

# Names for apps without a panel profile (the others use the profile's pretty_name).
PRETTY = {"airsonic": "Airsonic", "calibre": "Calibre", "jfago": "jfa-go"}
# Swizzin has no description for these two.
DESCRIPTIONS = {"calibre": "The Calibre e-book manager, used by Calibre Content Server and Calibre-Web.",
                "calibrecs": "Calibre Content Server: browse and read your Calibre library from the web."}
# What an installer expects but does not check, or aborts without.
REQUIRES = {"rutorrent": ["nginx", "rtorrent"], "flood": ["rtorrent"], "qui": ["qbittorrent"],
            "jfago": ["jellyfin"], "tautulli": ["plex"], "calibrecs": ["calibre"],
            "nextcloud": ["nginx"], "librespeed": ["nginx"], "rapidleech": ["nginx"], "organizr": ["nginx"]}
# The installer offers to disable the other one.
CONFLICTS = {"emby": ["jellyfin"], "jellyfin": ["emby"], "medusa": ["sickchill", "sickgear"],
             "sickchill": ["medusa", "sickgear"], "sickgear": ["medusa", "sickchill"]}


class StoreUnavailable(Exception):
    pass


def helper_call(request, path, timeout=3):
    """One request to the helper, one JSON answer. StoreUnavailable when it cannot be reached."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path)
            s.sendall(json.dumps(request).encode() + b"\n")
            data = bytearray()
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk or len(data) > 4 * 1024 * 1024:
                    raise StoreUnavailable("incomplete answer")
                data += chunk
        return json.loads(data)
    except (OSError, ValueError) as e:
        raise StoreUnavailable(str(e))


def entries(apps, static_folder):
    """What the page shows for each app. The apps the helper does not offer (system components and
    tools set up on the machine itself, see BLOCKED in store-helper/swizzin_store.py) are left out."""
    installed = {a["name"] for a in apps if a.get("installed")}
    shown = []
    for a in apps:
        name = a["name"]
        if a.get("blocked"):
            continue
        profile = getattr(util, name + "_meta", None)  # the class only: some profiles read files when created
        img = getattr(profile, "img", name)
        icon = "img/apps/{}.png".format(img)
        shown.append({
            "name": name,
            "pretty": getattr(profile, "pretty_name", None) or PRETTY.get(name, name),
            "description": a.get("description") or DESCRIPTIONS.get(name, ""),
            "installed": bool(a.get("installed")),
            "missing": [r for r in REQUIRES.get(name, []) if r not in installed],
            "conflicts": [c for c in CONFLICTS.get(name, []) if c in installed],
            "icon": icon if os.path.isfile(os.path.join(static_folder, icon)) else None,
        })
    return sorted(shown, key=lambda e: e["pretty"].lower())


class HelperLink:
    """The helper connection of one browser tab: what the helper says goes back to that tab only."""

    def __init__(self, path, sid, socketio):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # green: opened after monkey_patch
        self.sock.settimeout(5)
        self.sock.connect(path)
        self.sock.settimeout(None)
        self.sid, self.socketio = sid, socketio
        self.closed = False
        self.lock = threading.Lock()

    def send(self, obj):
        with self.lock:
            self.sock.sendall(json.dumps(obj).encode() + b"\n")

    def reader(self):
        buf = b""
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    self.socketio.emit("helper", json.loads(line), to=self.sid, namespace="/store")
        except (OSError, ValueError, EOFError):  # EOFError: eventlet, when close() interrupts the recv
            pass
        if not self.closed:  # the helper went away (restarted, stopped): the page says so and retries
            self.socketio.emit("helper", {"ok": False, "error": "helper-down"}, to=self.sid, namespace="/store")
        self.closed = True

    def close(self):
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)  # wakes the reader up
        except OSError:
            pass
        self.sock.close()


def register(app, socketio, htpasswd):
    """Add the App Store page to this app, unless it is a shared server or disabled in swizzin.cfg."""
    if app.config.get("SHAREDSERVER") or app.config.get("STORE_ENABLED") is False:
        app.config["STORE_ENABLED"] = False
        return
    bp = flask.Blueprint("store", __name__, template_folder="templates",
                         static_folder="static", static_url_path="/store/static")
    nonce = secrets.token_urlsafe(32)  # per process: after a panel restart the page simply fetches the new one
    links = {}  # Socket.IO sid -> HelperLink
    app.extensions["store"] = {"links": links}

    def admin_basic(user):
        auth = flask.request.authorization
        return user == app.config["ADMIN_USER"] and auth is not None and auth.type == "basic"

    def socket_path():
        return app.config.get("STORE_SOCKET", SOCKET)

    @bp.route("/apps")
    @htpasswd.required
    def catalogue(user):
        if not admin_basic(user):
            flask.abort(403)
        error = None
        try:
            reply = helper_call({"op": "catalogue"}, socket_path())
            if not reply.get("ok"):
                error = "helper-error"
        except StoreUnavailable as e:
            app.logger.warning("App Store helper unavailable: %s", e)
            reply, error = {}, "unavailable"
        shown = entries(reply.get("apps", []), app.static_folder)
        vendor = "sbio" if os.path.isfile("/install/.sbio.lock") else "swizzin"
        response = flask.make_response(flask.render_template(
            "store/store.html", title="App Store", user=user, pages=util.generate_page_list(user),
            vendor=vendor, apps=shown, error=error, job=reply.get("job"), last=reply.get("last")))
        response.headers["Cache-Control"] = "no-store"
        return response

    @bp.route("/store/token")
    @htpasswd.required
    def token(user):
        """The page's key to the /store namespace. Another site cannot read it (same-origin policy)."""
        if not admin_basic(user):
            flask.abort(403)
        response = flask.jsonify(token=nonce)
        response.headers["Cache-Control"] = "no-store"
        return response

    app.register_blueprint(bp)
    app.config["STORE_ENABLED"] = True
    if socketio is None:
        return

    def password_ok(password):
        """The admin password typed again in the page, before anything is installed or taken over."""
        guard, address = htpasswd.failed_logins, client_address(flask.request.environ)
        if guard.retry_after(address):
            return False
        admin = app.config["ADMIN_USER"]
        htpasswd.load_users(app)
        if isinstance(password, str) and password and htpasswd.users.check_password(admin, password):
            return True
        if guard.failed(address, ["store", admin, password if isinstance(password, str) else ""]):
            app.logger.warning("Too many failed logins from %s: refused for %d minutes",
                               address, -(-guard.block_time // 60))
        return False

    def link():
        sid = flask.request.sid
        current = links.get(sid)
        if current is None or current.closed:
            current = links[sid] = HelperLink(socket_path(), sid, socketio)
            socketio.start_background_task(current.reader)
        return current

    def relay(request):
        try:
            link().send(request)
        except OSError:
            emit("helper", {"ok": False, "error": "unavailable"})

    @socketio.on("connect", namespace="/store")
    def store_connect(auth=None):
        is_valid, user = htpasswd.authenticate()
        site = flask.request.headers.get("Sec-Fetch-Site")
        given = auth.get("token") if isinstance(auth, dict) else None
        if not is_valid or not admin_basic(user) or (site is not None and site != "same-origin") or \
                not isinstance(given, str) or not hmac.compare_digest(given, nonce):
            return False

    @socketio.on("disconnect", namespace="/store")
    def store_disconnect():
        current = links.pop(flask.request.sid, None)
        if current is not None:
            current.close()

    @socketio.on("install", namespace="/store")
    def store_install(msg):
        msg = msg if isinstance(msg, dict) else {}
        if not password_ok(msg.get("password")):
            emit("helper", {"ok": False, "error": "password"})
            return
        relay({"op": "install", "app": msg.get("app"), "rows": msg.get("rows"), "cols": msg.get("cols")})

    @socketio.on("attach", namespace="/store")
    def store_attach(msg):
        msg = msg if isinstance(msg, dict) else {}
        request = {"op": "attach"}
        if isinstance(msg.get("control_key"), str):
            request["control_key"] = msg["control_key"]
        if msg.get("take"):
            if not password_ok(msg.get("password")):
                emit("helper", {"ok": False, "error": "password"})
                return
            request["take"] = True
        relay(request)

    @socketio.on("input", namespace="/store")
    def store_input(msg):
        if isinstance(msg, dict) and flask.request.sid in links:
            relay({"op": "input", "data": msg.get("data")})

    @socketio.on("resize", namespace="/store")
    def store_resize(msg):
        if isinstance(msg, dict) and flask.request.sid in links:
            relay({"op": "resize", "rows": msg.get("rows"), "cols": msg.get("cols")})
