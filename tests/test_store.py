"""The App Store page of the panel, with the real helper running on fake Swizzin folders.

Run from the repository root: python tests/test_store.py
(throwaway htpasswd file and temporary socket: nothing on the machine is touched)."""
import base64
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.chdir(ROOT)

_tmp = tempfile.mkdtemp()
from passlib.apache import HtpasswdFile  # noqa: E402
_ht = HtpasswdFile(os.path.join(_tmp, "htpasswd"), new=True)
_ht.set_password("tester", "tester")
_ht.set_password("other", "other")
_ht.save()
with open(os.path.join(ROOT, "swizzin.cfg"), "w") as f:
    f.write(f"FLASK_HTPASSWD_PATH = '{_tmp}/htpasswd'\nADMIN_USER = 'tester'\n")

import swizzin  # noqa: E402  (first: it runs eventlet.monkey_patch())
import flask  # noqa: E402
from core.custom import store  # noqa: E402
from test_store_helper import Helper, fake_swizzin  # noqa: E402


def auth(user):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{user}".encode()).decode()}


class StorePage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp()
        fake_swizzin(cls.base)
        for app in ("rutorrent", "emby", "lounge", "calibre", "calibreweb"):
            open(os.path.join(cls.base, "scripts", app + ".sh"), "w").close()
        cls.os_release = os.path.join(cls.base, "os-release")
        with open(cls.os_release, "w") as f:
            f.write('NAME="Ubuntu"\nVERSION_CODENAME=noble\n')
        swizzin.app.config["STORE_OS_RELEASE"] = cls.os_release
        with open(os.path.join(cls.base, "lang_en"), "a") as f:
            f.write("$L['EMBY'] = '<script>alert(1)</script> media server';\n")
        cls.helper = Helper(cls.base)
        swizzin.app.config["STORE_SOCKET"] = cls.helper.path

    @classmethod
    def tearDownClass(cls):
        cls.helper.stop()
        shutil.rmtree(cls.base)

    def setUp(self):
        self.client = swizzin.app.test_client()

    def page(self, user="tester"):
        r = self.client.get("/apps", headers=auth(user))
        return r, r.get_data(as_text=True)

    @staticmethod
    def item(html, app):
        """The card of one app in the page."""
        start = html.index('store-item" data-app="%s"' % app)
        end = html.find('store-item" data-app=', start + 1)
        return html[start:end if end != -1 else None]

    def page_on(self, codename):
        path = os.path.join(self.base, "os-release-" + codename)
        with open(path, "w") as f:
            f.write("VERSION_CODENAME=%s\n" % codename)
        swizzin.app.config["STORE_OS_RELEASE"] = path
        try:
            return self.page()[1]
        finally:
            swizzin.app.config["STORE_OS_RELEASE"] = self.os_release

    def test_unavailable_apps_are_greyed_with_the_reason(self):
        lounge = self.item(self.page()[1], "lounge")
        self.assertIn('data-available="no"', lounge)
        self.assertIn("Not available for now", lounge)
        self.assertIn("needs Node.js 22", lounge)
        self.assertNotIn("store-install", lounge)

    def test_some_entries_are_for_ubuntu_24_04_only(self):
        noble, bookworm = self.page_on("noble"), self.page_on("bookworm")
        self.assertIn("Not available for now", self.item(noble, "calibre"))
        self.assertIn("store-install", self.item(bookworm, "calibre"))  # not tested there: offered as usual
        self.assertIn("answer n", self.item(noble, "calibreweb"))
        self.assertNotIn("answer n", self.item(bookworm, "calibreweb"))
        self.assertIn("needs Node.js 22", self.item(bookworm, "lounge"))  # broken everywhere

    def test_notes_checks_and_addresses_for_the_page(self):
        html = self.page()[1]
        emby = self.item(html, "emby")
        self.assertIn('data-note="Finish its setup right after installing', emby)
        self.assertIn('store-note-card">Finish its setup right after installing', emby)
        self.assertIn("also open on port 8096", emby)
        sonarr = self.item(html, "sonarr")
        self.assertIn('data-check="unreliable"', sonarr)
        self.assertIn('data-path="/sonarr"', sonarr)
        self.assertIn('data-note="Takes about 6 minutes."', self.item(html, "rutorrent"))
        self.assertNotIn("data-check", self.item(html, "jellyfin"))
        self.assertNotIn("data-note", self.item(html, "jellyfin"))

    def test_admin_sees_the_catalogue(self):
        r, html = self.page()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertIn('class="store-name">Jellyfin<', html)  # the panel profile's name
        self.assertIn("Requires rtorrent", html)  # rutorrent, with nginx installed but not rtorrent
        self.assertNotIn('data-app="letsencrypt"', html)  # system components are not offered at all
        self.assertNotIn('data-app="nginx"', html)
        self.assertIn("The installer will offer to disable jellyfin", html)  # emby, jellyfin installed
        self.assertIn('data-installed="yes"', html)
        self.assertNotIn("Bad Name", html)

    def test_descriptions_are_escaped(self):
        _, html = self.page()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; media server", html)

    def test_password_fields_unless_swizzin_cfg_turns_them_off(self):
        _, html = self.page()
        self.assertIn('data-ask-password="yes"', html)
        self.assertIn('id="store-confirm-password"', html)
        self.assertIn('id="store-take-password"', html)
        swizzin.app.config["STORE_ASK_PASSWORD"] = False
        try:
            _, html = self.page()
        finally:
            del swizzin.app.config["STORE_ASK_PASSWORD"]
        self.assertIn('data-ask-password="no"', html)
        self.assertNotIn('id="store-confirm-password"', html)
        self.assertNotIn('id="store-take-password"', html)
        self.assertIn('id="store-take"', html)
        self.assertEqual(html.count('type="password"'), 1)  # uninstalling asks for it whatever the setting
        self.assertIn('id="store-remove-password"', html)

    def test_other_users_are_refused(self):
        r, _ = self.page("other")
        self.assertEqual(r.status_code, 403)

    def test_anonymous_is_refused(self):
        r = self.client.get("/apps")
        self.assertNotEqual(r.status_code, 200)
        self.assertNotIn(b"store-item", r.get_data())

    def test_token_authentication_is_refused(self):
        for header in ("token abc", "Bearer abc", "Basic !!!"):
            for path in ("/apps", "/store/token"):  # refused like an anonymous visitor, not an error 500
                r = self.client.get(path, headers={"Authorization": header})
                self.assertIn(r.status_code, (302, 401), (path, header))

    def test_page_explains_when_the_helper_is_missing(self):
        swizzin.app.config["STORE_SOCKET"] = os.path.join(self.base, "missing.sock")
        try:
            r, html = self.page()
        finally:
            swizzin.app.config["STORE_SOCKET"] = self.helper.path
        self.assertEqual(r.status_code, 200)
        self.assertIn("The App Store helper is not running", html)
        self.assertNotIn("store-item", html)

    def test_sidebar_link_for_the_admin_only(self):
        link = 'App Store</a>'
        self.assertIn(link, self.client.get("/", headers=auth("tester")).get_data(as_text=True))
        self.assertNotIn(link, self.client.get("/", headers=auth("other")).get_data(as_text=True))
        self.assertIn(link, self.page()[1])

    def test_disabled_on_shared_servers(self):
        app = flask.Flask("shared")
        app.config.update(SHAREDSERVER=True, ADMIN_USER="tester")
        store.register(app, None, swizzin.htpasswd)
        self.assertFalse(app.config["STORE_ENABLED"])
        self.assertNotIn("store", app.blueprints)

    def test_disabled_in_swizzin_cfg(self):
        app = flask.Flask("off")
        app.config.update(STORE_ENABLED=False, ADMIN_USER="tester")
        store.register(app, None, swizzin.htpasswd)
        self.assertNotIn("store", app.blueprints)


    def test_uninstall_reason_and_no_framing(self):
        r, html = self.page()
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])
        jellyfin = self.item(html, "jellyfin")  # installed, and not uninstalled from the App Store
        self.assertIn("Uninstall it over SSH only: Swizzin&#39;s removal can stop halfway", jellyfin)
        self.assertNotIn('store-remove"', jellyfin)

    def test_uninstall_button_with_its_texts(self):
        catalogue = {"ok": True, "features": ["install", "remove"], "job": None, "last": None, "apps": [
            {"name": "bazarr", "description": "subtitles", "installed": True, "blocked": False, "remove": "ok"},
            {"name": "radarr", "description": "films", "installed": True, "blocked": False, "remove": "ok"}]}
        with mock.patch.object(store, "helper_call", return_value=catalogue):
            html = self.page()[1]
        bazarr = self.item(html, "bazarr")
        self.assertIn('class="btn btn-sm btn-outline-danger ml-2 store-remove" data-app="bazarr"', bazarr)
        self.assertIn('data-removed="Bazarr and its settings: providers, languages, subtitle history."', bazarr)
        self.assertIn('data-kept="The subtitles it already downloaded, next to your media."', bazarr)
        self.assertIn('data-affects=""', bazarr)
        self.assertIn('data-affects="Bazarr"', self.item(html, "radarr"))  # installed, and may use Radarr

    def test_an_incomplete_removal_is_shown_when_the_page_opens(self):
        catalogue = {"ok": True, "features": ["install", "remove"], "job": None, "apps": [],
                     "last": {"app": "bazarr", "action": "remove", "result": "incomplete", "message": "<b>x</b>"}}
        with mock.patch.object(store, "helper_call", return_value=catalogue):
            html = self.page()[1]
        self.assertIn('id="store-incomplete"', html)
        self.assertIn("The uninstallation of <strong>bazarr</strong> stopped halfway (&lt;b&gt;x&lt;/b&gt;)", html)

class UninstallTexts(unittest.TestCase):
    """What the page says about uninstalling, from what the helper reports (entries() alone)."""

    def entry(self, apps, name, features=("install", "remove")):
        with swizzin.app.app_context():
            return {e["name"]: e for e in store.entries(apps, swizzin.app.static_folder, "noble", features)}[name]

    def test_an_app_that_can_be_uninstalled(self):
        apps = [{"name": "qbittorrent", "installed": True, "remove": "ok"},
                {"name": "qui", "installed": True, "remove": "ok"}]
        e = self.entry(apps, "qbittorrent")
        self.assertEqual(e["remove"], "ok")
        self.assertIsNone(e["remove_reason"])
        self.assertIn("its torrents stop", e["removed"])
        self.assertIn("Your downloads", e["kept"])
        self.assertEqual(e["affects"], ["qui"])  # installed, and uses it; autobrr is not installed

    def test_apps_that_are_not_uninstalled_from_here_say_why(self):
        apps = [{"name": "rtorrent", "installed": True, "remove": "not-offered"},
                {"name": "bazarr", "installed": True, "remove": "script-changed"},
                {"name": "radarr", "installed": True, "remove": "disabled"},
                {"name": "sonarr", "installed": False, "remove": None}]
        self.assertIn("over SSH only: it would also remove ruTorrent and Flood", self.entry(apps, "rtorrent")["remove_reason"])
        self.assertIn("An update of Swizzin changed", self.entry(apps, "bazarr")["remove_reason"])
        self.assertIn("turned off on this server", self.entry(apps, "radarr")["remove_reason"])
        self.assertIsNone(self.entry(apps, "sonarr")["remove"])  # not installed: nothing to say

    def test_an_older_helper_without_uninstall_shows_nothing(self):
        apps = [{"name": "bazarr", "installed": True, "remove": "ok"}]
        e = self.entry(apps, "bazarr", features=("install",))
        self.assertIsNone(e["remove"])
        self.assertIsNone(e["remove_reason"])

    def test_every_uninstallable_app_has_its_texts(self):
        sys.path.insert(0, os.path.join(ROOT, "store-helper"))
        import removable
        self.assertEqual(set(store.REMOVE_TEXTS), set(removable.APPS))
        self.assertEqual(set(store.REMOVE_SSH_ONLY), set(removable.EXCLUDED))


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
