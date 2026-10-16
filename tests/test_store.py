"""The App Store page of the panel, with the real helper running on fake Swizzin folders.

Run from the repository root: python tests/test_store.py
(throwaway htpasswd file and temporary socket: nothing on the machine is touched)."""
import base64
import os
import shutil
import sys
import tempfile
import unittest

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
        open(os.path.join(cls.base, "scripts", "rutorrent.sh"), "w").close()
        open(os.path.join(cls.base, "scripts", "emby.sh"), "w").close()
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
        self.assertNotIn('type="password"', html)
        self.assertIn('id="store-take"', html)

    def test_other_users_are_refused(self):
        r, _ = self.page("other")
        self.assertEqual(r.status_code, 403)

    def test_anonymous_is_refused(self):
        r = self.client.get("/apps")
        self.assertNotEqual(r.status_code, 200)
        self.assertNotIn(b"store-item", r.get_data())

    def test_token_authentication_is_refused(self):
        r = self.client.get("/apps", headers={"Authorization": "token abc"})
        self.assertNotEqual(r.status_code, 200)

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


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
