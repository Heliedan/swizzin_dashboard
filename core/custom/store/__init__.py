"""App Store page of the panel: every Swizzin application, installed or not.

The list comes from the swizzin-store helper (store-helper/ in this repository), a small
root service: /etc/swizzin is readable by root only, and the panel runs as `swizzin`.
For now the page only lists; installing from it comes later. Admin only.

This package lives in core/custom/ so that `box upgrade panel` keeps it. register() runs
once per import of swizzin.py (it is imported twice at startup), so it creates its own
Blueprint each time and has no other side effect.
"""
import json
import os
import socket

import flask

import core.util as util  # module import only: core.util and swizzin.py import each other at startup

SOCKET = "/run/swizzin-store/store.sock"

# Shown under "System & tools" rather than "Applications".
SYSTEM = {"nginx", "panel", "letsencrypt", "quota", "ffmpeg", "rclone", "vsftpd", "wireguard",
          "x2go", "webmin", "shellinabox", "duckdns"}
# Names for apps without a panel profile (the others use the profile's pretty_name).
PRETTY = {"airsonic": "Airsonic", "calibre": "Calibre", "duckdns": "DuckDNS", "ffmpeg": "FFmpeg",
          "jfago": "jfa-go", "letsencrypt": "Let's Encrypt", "nginx": "nginx", "panel": "Swizzin panel",
          "quota": "Quota", "rclone": "rclone", "vsftpd": "vsftpd", "x2go": "X2Go"}
# Swizzin has no description for these two.
DESCRIPTIONS = {"calibre": "The Calibre e-book manager, used by Calibre Content Server and Calibre-Web.",
                "calibrecs": "Calibre Content Server: browse and read your Calibre library from the web."}
# What an installer expects but does not check, or aborts without.
REQUIRES = {"rutorrent": ["nginx", "rtorrent"], "flood": ["rtorrent"], "qui": ["qbittorrent"],
            "jfago": ["jellyfin"], "tautulli": ["plex"], "calibrecs": ["calibre"], "letsencrypt": ["nginx"],
            "nextcloud": ["nginx"], "librespeed": ["nginx"], "rapidleech": ["nginx"], "organizr": ["nginx"]}
# The installer offers to disable the other one.
CONFLICTS = {"emby": ["jellyfin"], "jellyfin": ["emby"], "medusa": ["sickchill", "sickgear"],
             "sickchill": ["medusa", "sickgear"], "sickgear": ["medusa", "sickchill"]}
# Why an app is SSH only (the helper decides which ones are).
SSH_ONLY = {"panel": "It is the panel you are using.",
            "nginx": "Every web page of the server goes through it.",
            "letsencrypt": "It rewrites the server's HTTPS setup and asks for a domain.",
            "quota": "It edits fstab and remounts the disks.",
            "wireguard": "It changes the network (forwarding, NAT) and can cut a VPN you rely on.",
            "x2go": "A heavy desktop install.",
            "xmrig": "A cryptocurrency miner: without a wallet it mines for the Swizzin project.",
            "webmin": "It gives root access from the web.",
            "shellinabox": "It gives a shell from the web."}


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
    """What the page shows for each app, in two sections."""
    installed = {a["name"] for a in apps if a.get("installed")}
    sections = {"apps": [], "system": []}
    for a in apps:
        name = a["name"]
        profile = getattr(util, name + "_meta", None)  # the class only: some profiles read files when created
        img = getattr(profile, "img", name)
        icon = "img/apps/{}.png".format(img)
        sections["system" if name in SYSTEM else "apps"].append({
            "name": name,
            "pretty": getattr(profile, "pretty_name", None) or PRETTY.get(name, name),
            "description": a.get("description") or DESCRIPTIONS.get(name, ""),
            "installed": bool(a.get("installed")),
            "blocked": bool(a.get("blocked")),
            "why": SSH_ONLY.get(name, "Install it over SSH."),
            "missing": [r for r in REQUIRES.get(name, []) if r not in installed],
            "conflicts": [c for c in CONFLICTS.get(name, []) if c in installed],
            "icon": icon if os.path.isfile(os.path.join(static_folder, icon)) else None,
        })
    for items in sections.values():
        items.sort(key=lambda e: e["pretty"].lower())
    return sections


def register(app, socketio, htpasswd):
    """Add the App Store page to this app, unless it is a shared server or disabled in swizzin.cfg."""
    if app.config.get("SHAREDSERVER") or app.config.get("STORE_ENABLED") is False:
        app.config["STORE_ENABLED"] = False
        return
    bp = flask.Blueprint("store", __name__, template_folder="templates",
                         static_folder="static", static_url_path="/store/static")

    @bp.route("/apps")
    @htpasswd.required
    def catalogue(user):
        auth = flask.request.authorization
        if user != app.config["ADMIN_USER"] or auth is None or auth.type != "basic":
            flask.abort(403)
        error = None
        try:
            reply = helper_call({"op": "catalogue"}, app.config.get("STORE_SOCKET", SOCKET))
            if not reply.get("ok"):
                error = "helper-error"
        except StoreUnavailable as e:
            app.logger.warning("App Store helper unavailable: %s", e)
            reply, error = {}, "unavailable"
        sections = entries(reply.get("apps", []), app.static_folder)
        vendor = "sbio" if os.path.isfile("/install/.sbio.lock") else "swizzin"
        response = flask.make_response(flask.render_template(
            "store/store.html", title="App Store", user=user, pages=util.generate_page_list(user),
            vendor=vendor, sections=sections, error=error))
        response.headers["Cache-Control"] = "no-store"
        return response

    app.register_blueprint(bp)
    app.config["STORE_ENABLED"] = True
