"""The App Store is off by default (APP_STORE in swizzin.cfg, see core/custom/store): a panel whose swizzin.cfg does
not turn it on has no App Store page, sidebar entry, token, namespace or icon, as the official panel; the Logs
buttons, which do not need it, are there.

Run from the repository root: python tests/test_store_off.py
(throwaway htpasswd file: nothing on the machine is touched)."""
import base64
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

_tmp = tempfile.mkdtemp()
from passlib.apache import HtpasswdFile  # noqa: E402
_ht = HtpasswdFile(os.path.join(_tmp, "htpasswd"), new=True)
_ht.set_password("tester", "tester")
_ht.save()
with open(os.path.join(ROOT, "swizzin.cfg"), "w") as f:  # no APP_STORE: the default
    f.write(f"FLASK_HTPASSWD_PATH = '{_tmp}/htpasswd'\nADMIN_USER = 'tester'\n")

import swizzin  # noqa: E402  (first: it runs eventlet.monkey_patch())

PAGES = [{"name": "sonarr", "pretty_name": "Sonarr", "url": "https://demo.invalid/sonarr", "systemd": "sonarr",
          "img": "sonarr"}]
AUTH = {"Authorization": "Basic " + base64.b64encode(b"tester:tester").decode()}


class StoreOff(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        for patcher in (mock.patch.object(swizzin.layout_choices, "path", os.path.join(self.dir, "layouts.json")),
                        mock.patch.object(swizzin.limiter, "enabled", False)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = swizzin.app.test_client()

    def test_off_by_default(self):
        self.assertIs(swizzin.app.config["APP_STORE"], False)
        self.assertNotIn("store", swizzin.app.blueprints)

    def test_its_addresses_answer_404(self):
        for path in ("/apps", "/store/token", "/store/static/store.svg", "/store/static/store.js"):
            with self.subTest(path):
                self.assertEqual(self.client.get(path, headers=AUTH).status_code, 404)

    def test_no_namespace(self):
        tab = swizzin.socketio.test_client(swizzin.app, namespace="/store", flask_test_client=self.client,
                                           headers=AUTH, auth={"token": "x"})
        self.assertFalse(tab.is_connected("/store"))

    def test_no_sidebar_entry_in_either_layout(self):
        for layout in ("classic", "overview"):
            with self.subTest(layout):
                swizzin.layout_choices.set("tester", layout)
                real_isfile = os.path.isfile
                with mock.patch.object(swizzin, "get_mounts", return_value=["/"]), \
                        mock.patch.object(swizzin, "generate_page_list", return_value=PAGES), \
                        mock.patch.object(swizzin.os.path, "isfile",
                                          side_effect=lambda p: False if p.startswith("/install/") else real_isfile(p)):
                    r = self.client.get("/", headers=AUTH)
                page = r.get_data(as_text=True)
                self.assertEqual(r.status_code, 200)
                self.assertNotIn("App Store", page)
                self.assertNotIn("/apps\"", page)
                self.assertNotIn("store.svg", page)
                self.assertIn(">Sonarr<", page)
                if layout == "overview":  # the Logs buttons do not need the App Store
                    self.assertIn('data-url="/apps/logs"', page)
                    self.assertIn('data-logs="sonarr"', page)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
