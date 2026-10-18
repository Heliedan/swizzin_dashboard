"""App Store page of the panel: every Swizzin application, installed or not, and installing one.

The list comes from the swizzin-store helper (store-helper/ in this repository), a small
root service: /etc/swizzin is readable by root only, and the panel runs as `swizzin`. The
helper also runs the installations (`box install <app>` in a pseudo-terminal); this module
relays them to the page over the Socket.IO namespace /store, one helper connection per tab.
Admin only: the page, the token and the namespace check the admin's Basic login, the
namespace also wants the page's token, and installing or taking control asks for the admin
password again (counted with the other failed logins), unless swizzin.cfg says
STORE_ASK_PASSWORD = False. Uninstalling always asks for it, and for the app's name typed in:
the helper saves the app's settings first and removes only the apps it audited (see
store-helper/removable.py); this module only words what the user is told.

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

# From the compatibility campaign (tests/campaign/: each app installed through the helper in a fresh Swizzin,
# Ubuntu 24.04, 2026-10-16). The App Store aims at installing in one click. An entry can be limited to some
# systems (VERSION_CODENAME in /etc/os-release): the campaign only tested Ubuntu 24.04 ("noble").
# Not offered for now: the installation fails, the app does not work afterwards, or it needs too many answers.
UNAVAILABLE = {
    "calibre": ("Swizzin's Calibre installer fails on Ubuntu 24.04 (swizzin/swizzin#1212).", {"noble"}),
    "calibrecs": ("Needs Calibre, whose Swizzin installer fails on Ubuntu 24.04 (swizzin/swizzin#1212).", {"noble"}),
    "jfago": ("Its page is not found at the address Swizzin sets up for it.", None),
    "lounge": ("The current The Lounge needs Node.js 22, and Swizzin installs Node.js 20.", None),
    "navidrome": ("Not checked yet: its service needs system rights that the test machine did not give.", None),
    "netdata": ("Not checked yet: its service needs system rights that the test machine did not give.", None),
    "nextcloud": ("It asks for three passwords in the terminal, and its page then reports missing PHP modules.", None),
    "ombi": ("Ombi's own package fails to install (Ombi-app/Ombi#5448).", None),
    "rapidleech": ("Swizzin's web setup for Rapidleech does not work, and Rapidleech is no longer maintained.", None),
    "sabnzbd": ("Its page is not found at the address Swizzin sets up for it.", None),
    "sickchill": ("Its service stops right after starting.", None),
    "znc": ("Its setup asks many questions in the terminal: install it over SSH for now.", None),
}
_EXPOSED = "Its own page is also open on port {}, without your Swizzin password in front: a firewall should block that port."
# Offered, with something to know before installing.
NOTES = {
    "airsonic": ("Its default login is admin / admin, with no Swizzin password in front: change it right after "
                 "installing.", None),
    "btsync": (_EXPOSED.format(8888), None),
    "calibreweb": ("When it offers to install Calibre, answer n: Swizzin's Calibre installer fails on Ubuntu 24.04, "
                   "and Calibre-Web works without it.", {"noble"}),
    "emby": ("Finish its setup right after installing: the first visitor creates the admin account, with no Swizzin "
             "password in front. " + _EXPOSED.format(8096), None),
    "jackett": (_EXPOSED.format(9117), None),
    "librespeed": ("Its page is public, with no password: anyone who can reach your server can run speed tests.", None),
    "nzbhydra": (_EXPOSED.format(5076), None),
    "quassel": ("Opens port 4242, where the Quassel apps connect.", None),
    "rtorrent": ("Takes about 20 minutes: rTorrent is compiled. On Ubuntu 24.04 it also installs Performance Co-Pilot, "
                 "which opens ports 44321 to 44323 and 4330: a firewall should block them.", None),
    "rutorrent": ("Takes about 6 minutes.", None),
    "sickgear": (_EXPOSED.format(8081), None),
    "sonarr": ("If the installer says that Sonarr took longer than 30 seconds to start, install it again: it can "
               "happen on a busy server.", None),
    "syncthing": ("Opens port 22000, to sync with your other devices.", None),
    "tautulli": ("Takes about 15 minutes. Its page has no Swizzin password in front: set a login in its settings right "
                 "after installing.", None),
    "transmission": (_EXPOSED.format(9091), None),
}
# Swizzin's checks (box test) fail for these although the app works (the generic check cannot find the port or
# the service, or does not expect what the app answers). The helper checks them itself (store-helper/check_app.py,
# same apps); with a helper older than that, the page says Swizzin's check cannot verify them.
UNRELIABLE_CHECK = {"bazarr", "btsync", "emby", "filebrowser", "flood", "jackett", "lidarr", "medusa", "organizr",
                    "prowlarr", "quassel", "qui", "radarr", "sickgear", "sonarr", "syncthing"}

# Uninstalling (store-helper/removable.py decides what is done; this says it in plain words).
# What goes with the app, and what stays (on top of "your downloads and media are not touched").
REMOVE_TEXTS = {
    "airsonic": ("Airsonic and its settings: accounts, playlists, play history.", ""),
    "autobrr": ("autobrr and its settings, for every user: filters, indexers, IRC networks, download clients.", ""),
    "bazarr": ("Bazarr and its settings: providers, languages, subtitle history.",
               "The subtitles it already downloaded, next to your media."),
    "calibreweb": ("Calibre-Web and its settings: accounts, shelves, reading status, Kobo sync.", "Your Calibre library."),
    "emby": ("Emby.", "Emby's own data (/var/lib/emby): reinstalling Emby finds it again."),
    "filebrowser": ("File Browser and its settings: accounts, shared links.", "Your files."),
    "flood": ("Flood and its settings, for every user: accounts, connections to the torrent client.",
              "rTorrent and its torrents."),
    "jackett": ("Jackett and its settings: indexers and their logins, API key.", ""),
    "librespeed": ("LibreSpeed.", ""),
    "lidarr": ("Lidarr.", "Its settings and library (~/.config/Lidarr): reinstalling Lidarr finds them again."),
    "medusa": ("Medusa and its settings: show list, history, providers.", ""),
    "mylar": ("Mylar and its settings: watch list, history.", "Your comics."),
    "netronome": ("Netronome and its settings, for every user: speed test history.", ""),
    "nzbhydra": ("NZBHydra and its settings: indexers, history.", ""),
    "organizr": ("Organizr and its settings: tabs, users.", ""),
    "plex": ("Plex Media Server and its library: settings, watch history, posters and other metadata.", ""),
    "prowlarr": ("Prowlarr and its settings: indexers, history.", ""),
    "qbittorrent": ("qBittorrent: its torrents stop.", "qBittorrent's settings and torrent list, in your home folder."),
    "qui": ("qui and its settings, for every user.", "qBittorrent and its torrents."),
    "radarr": ("Radarr.", "Its settings and library (~/.config/Radarr): reinstalling Radarr finds them again."),
    "sickgear": ("SickGear and its settings: show list, history.", ""),
    "syncthing": ("Syncthing: syncing stops.", "Its settings, its device identity and every synced folder."),
    "tautulli": ("Tautulli and its settings: watch history, statistics.", "Plex."),
    "transmission": ("Transmission: its torrents stop, and its settings file is deleted for every user.",
                     "Transmission's torrent list."),
}
# Installed apps that may use it, and would stop working with it.
# Why an app cannot be uninstalled while another one is installed (refuse_if in store-helper/removable.py).
REMOVE_CONFLICT = {
    "qbittorrent": "Not while Deluge is installed: removing qBittorrent this way would also remove a library Deluge uses.",
}
REMOVE_AFFECTS = {"qbittorrent": ["qui", "autobrr"], "plex": ["tautulli"], "radarr": ["bazarr"],
                  "jackett": ["sonarr", "radarr", "lidarr", "medusa", "sickgear", "mylar", "autobrr"],
                  "prowlarr": ["sonarr", "radarr", "lidarr"],
                  "nzbhydra": ["sonarr", "radarr", "lidarr", "medusa", "sickgear", "mylar"],
                  "transmission": ["sonarr", "radarr", "lidarr", "autobrr"]}
# Installed but not uninstalled from here (store-helper/removable.py, EXCLUDED), and why.
REMOVE_SSH_ONLY = {
    "rtorrent": "it would also remove ruTorrent and Flood, and stop all your torrents.",
    "rutorrent": "it would delete the saved .torrent files, RSS filters and settings of every user.",
    "deluge": "Swizzin's removal handles only the first user and removes a library other apps use.",
    "btsync": "Swizzin's removal deletes your synced files.",
    "nzbget": "Swizzin's removal deletes its downloads.",
    "jellyfin": "Swizzin's removal can stop halfway and leave Jellyfin installed with no data.",
    "sonarr": "Swizzin's removal never stops Sonarr and removes packages system-wide.",
    "quassel": "Swizzin's removal updates apt halfway and can stop there.",
}
# Why the helper does not offer to uninstall an installed app right now.
REMOVE_STATUS = {
    "not-offered": "The App Store does not uninstall this app: use box remove over SSH.",
    "untested": "Uninstalling it from here is not offered yet.",
    "other-system": "Uninstalling it from here was tested on Ubuntu 24.04 only, not on this system.",
    "conflict": "Not while another installed app depends on what its removal takes away.",
    "disabled": "Uninstalling from the App Store is turned off on this server.",
    "script-changed": "An update of Swizzin changed its removal since the App Store checked it: uninstall it over "
                      "SSH, or update the App Store.",
}


def os_codename(path="/etc/os-release"):
    """'noble', 'bookworm'... or None."""
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("VERSION_CODENAME="):
                    return line.split("=", 1)[1].strip().strip('"') or None
    except OSError:
        pass
    return None


def for_system(table, name, codename):
    """The text of table[name] if it applies to this system, else None."""
    text, systems = table.get(name, (None, None))
    return text if text and (systems is None or codename in systems) else None


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


def remove_info(name, status, installed, pretty):
    """What the page says about uninstalling an installed app: (status, why not, removed, kept, affected)."""
    if status is None:  # a helper that does not uninstall: nothing is said
        return None, None, None, None, []
    if status != "ok":
        reason = REMOVE_STATUS.get(status, REMOVE_STATUS["not-offered"])
        if status == "not-offered" and name in REMOVE_SSH_ONLY:
            reason = "Uninstall it over SSH only: " + REMOVE_SSH_ONLY[name]
        elif status == "conflict":
            reason = REMOVE_CONFLICT.get(name, reason)
        return status, reason, None, None, []
    removed, kept = REMOVE_TEXTS.get(name, (pretty + " and its settings.", ""))
    affects = [pretty_of(a) for a in REMOVE_AFFECTS.get(name, []) if a in installed]
    return "ok", None, removed, kept, affects


def pretty_of(name):
    profile = getattr(util, name + "_meta", None)
    return getattr(profile, "pretty_name", None) or PRETTY.get(name, name)


def entries(apps, static_folder, codename=None, features=()):
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
        pretty = getattr(profile, "pretty_name", None) or PRETTY.get(name, name)
        status = a.get("remove") if "remove" in features and a.get("installed") else None
        remove, remove_reason, removed, kept, affects = remove_info(name, status, installed, pretty)
        shown.append({
            "name": name,
            "pretty": pretty,
            "description": a.get("description") or DESCRIPTIONS.get(name, ""),
            "installed": bool(a.get("installed")),
            "missing": [r for r in REQUIRES.get(name, []) if r not in installed],
            "conflicts": [c for c in CONFLICTS.get(name, []) if c in installed],
            "icon": icon if os.path.isfile(os.path.join(static_folder, icon)) else None,
            "unavailable": for_system(UNAVAILABLE, name, codename),
            "note": for_system(NOTES, name, codename),
            "unreliable_check": name in UNRELIABLE_CHECK,
            # the address of the app's page, completed in the browser like the sidebar does (core/util.py)
            "path": getattr(profile, "baseurl", None),
            "scheme": getattr(profile, "scheme", None),
            "remove": remove,  # "ok": offered; another value: why not (remove_reason says it)
            "remove_reason": remove_reason,
            "removed": removed,
            "kept": kept,
            "affects": affects,
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
    # sids whose connection the connect handler explicitly accepted: if that handler fails, python-socketio
    # keeps the connection open and would still deliver its events, so every handler checks this set
    authorized = set()
    app.extensions["store"] = {"links": links, "authorized": authorized}

    def admin_basic(user):
        auth = flask.request.authorization
        return user == app.config["ADMIN_USER"] and auth is not None and auth.type == "basic"

    def socket_path():
        return app.config.get("STORE_SOCKET", SOCKET)

    def ask_password():
        """Only an explicit False in swizzin.cfg turns it off: a typo or another value keeps asking."""
        return app.config.get("STORE_ASK_PASSWORD") is not False

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
        shown = entries(reply.get("apps", []), app.static_folder,
                        os_codename(app.config.get("STORE_OS_RELEASE", "/etc/os-release")), reply.get("features", ()))
        vendor = "sbio" if os.path.isfile("/install/.sbio.lock") else "swizzin"
        response = flask.make_response(flask.render_template(
            "store/store.html", title="App Store", user=user, pages=util.generate_page_list(user),
            vendor=vendor, apps=shown, error=error, job=reply.get("job"), last=reply.get("last"),
            ask_password=ask_password()))
        response.headers["Cache-Control"] = "no-store"
        # no other site may show this page in a frame and trick a click on Install or Uninstall
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
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

    def connect_allowed(auth):
        is_valid, user = htpasswd.authenticate()
        site = flask.request.headers.get("Sec-Fetch-Site")
        given = auth.get("token") if isinstance(auth, dict) else None
        return bool(is_valid and admin_basic(user) and (site is None or site == "same-origin") and
                    isinstance(given, str) and given.isascii() and hmac.compare_digest(given, nonce))

    def accepted():
        return flask.request.sid in authorized

    @socketio.on("connect", namespace="/store")
    def store_connect(auth=None):
        try:
            allowed = connect_allowed(auth)
        except Exception:  # fail closed: an error here must refuse the connection
            app.logger.exception("App Store: connection check failed")
            allowed = False
        if not allowed:
            return False
        authorized.add(flask.request.sid)

    @socketio.on("disconnect", namespace="/store")
    def store_disconnect():
        authorized.discard(flask.request.sid)
        current = links.pop(flask.request.sid, None)
        if current is not None:
            current.close()

    @socketio.on("install", namespace="/store")
    def store_install(msg):
        if not accepted():
            return
        msg = msg if isinstance(msg, dict) else {}
        if ask_password() and not password_ok(msg.get("password")):
            emit("helper", {"ok": False, "error": "password"})
            return
        relay({"op": "install", "app": msg.get("app"), "rows": msg.get("rows"), "cols": msg.get("cols")})

    @socketio.on("remove-preview", namespace="/store")
    def store_remove_preview(msg):
        """What the confirmation shows (users, size of the copy), with the helper's one-time ticket."""
        if not accepted():
            return
        msg = msg if isinstance(msg, dict) else {}
        relay({"op": "remove-preview", "app": msg.get("app")})

    @socketio.on("remove", namespace="/store")
    def store_remove(msg):
        """Uninstalling: always the admin password (whatever STORE_ASK_PASSWORD says) and the app's name typed in."""
        if not accepted():
            return
        msg = msg if isinstance(msg, dict) else {}
        app_name, confirm = msg.get("app"), msg.get("confirm")
        if not password_ok(msg.get("password")):
            emit("helper", {"ok": False, "error": "password"})
            return
        if not isinstance(app_name, str) or not isinstance(confirm, str) or confirm.strip().lower() != app_name:
            emit("helper", {"ok": False, "error": "confirm-mismatch"})
            return
        auth = flask.request.authorization
        requester = "{} from {}".format(auth.username if auth else "?", client_address(flask.request.environ))
        relay({"op": "remove", "app": app_name, "ticket": msg.get("ticket"), "confirm": confirm,
               "requester": requester, "rows": msg.get("rows"), "cols": msg.get("cols")})

    @socketio.on("attach", namespace="/store")
    def store_attach(msg):
        if not accepted():
            return
        msg = msg if isinstance(msg, dict) else {}
        request = {"op": "attach"}
        if isinstance(msg.get("control_key"), str):
            request["control_key"] = msg["control_key"]
        if msg.get("take"):
            if ask_password() and not password_ok(msg.get("password")):
                emit("helper", {"ok": False, "error": "password"})
                return
            request["take"] = True
        relay(request)

    @socketio.on("input", namespace="/store")
    def store_input(msg):
        if accepted() and isinstance(msg, dict) and flask.request.sid in links:
            relay({"op": "input", "data": msg.get("data")})

    @socketio.on("resize", namespace="/store")
    def store_resize(msg):
        if accepted() and isinstance(msg, dict) and flask.request.sid in links:
            relay({"op": "resize", "rows": msg.get("rows"), "cols": msg.get("cols")})
