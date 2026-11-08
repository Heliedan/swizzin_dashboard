"""The pills Reboot required and pending updates of Overview, in the panel: route /stats/system-status (the admin
only, never cached, never waiting for apt-get, even under eventlet as the panel runs), and where the pages have them.

Run from the repository root: python tests/test_system_status_page.py
(throwaway htpasswd file, throwaway root of the system and a fake apt-get: nothing on the machine is touched)."""
import base64
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.chdir(ROOT)

# The panel reads its users from /etc/htpasswd: point it to a throwaway file before importing it.
_tmp = tempfile.mkdtemp()
from passlib.apache import HtpasswdFile  # noqa: E402
_ht = HtpasswdFile(os.path.join(_tmp, "htpasswd"), new=True)
_ht.set_password("tester", "tester")
_ht.set_password("friend", "friend")
_ht.save()
with open(os.path.join(ROOT, "swizzin.cfg"), "w") as f:
    f.write(f"FLASK_HTPASSWD_PATH = '{_tmp}/htpasswd'\nADMIN_USER = 'tester'\n")

import swizzin  # noqa: E402  (first: it runs eventlet.monkey_patch(), as when the panel runs)
import eventlet  # noqa: E402
from core import system_status  # noqa: E402
from core.system_status import PendingUpdates  # noqa: E402
from test_system_status import REAL, fake_system  # noqa: E402

URL = "/stats/system-status"
PANEL = swizzin.pending_updates  # the panel's own, before the tests put theirs in its place


def auth(user, xhr=True):
    headers = {"Authorization": "Basic " + base64.b64encode(f"{user}:{user}".encode()).decode()}
    if xhr:
        headers["X-Requested-With"] = "XMLHttpRequest"
    return headers


class PageTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.bin = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.bin)
        fake_system(self.root, self.bin, REAL["ubuntu-24.04"])
        self.updates = PendingUpdates(swizzin.app.logger, root=self.root)
        for patcher in (mock.patch.object(system_status, "PATH", self.bin + ":" + system_status.PATH),
                        mock.patch.object(swizzin, "pending_updates", self.updates),
                        mock.patch.object(swizzin, "reboot_status",
                                          lambda: system_status.reboot_status(self.root, "6.1.0-26-amd64")),
                        mock.patch.object(swizzin.limiter, "enabled", False)):  # it caps anonymous requests
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.done)  # first: a run started by the test ends before its folders go
        self.client = swizzin.app.test_client()

    def get(self, user="tester", path=URL, **kw):
        return self.client.get(path, headers=auth(user) if user else {"X-Requested-With": "XMLHttpRequest"}, **kw)

    def done(self):
        end = time.monotonic() + 30
        while self.updates.running:
            self.assertLess(time.monotonic(), end, "the run never ended")
            time.sleep(0.01)  # green: the run goes on meanwhile


class Route(PageTest):
    def test_the_admin_only(self):
        self.assertEqual(self.get(None).status_code, 401)
        self.assertEqual(self.client.get(URL).status_code, 302)  # the login page
        self.assertEqual(self.get("friend").status_code, 403)
        self.assertFalse(self.updates.running)  # nobody else makes the panel run apt-get
        r = self.get()
        self.assertEqual((r.status_code, r.headers["Cache-Control"], r.mimetype), (200, "no-store", "application/json"))
        self.assertEqual(r.get_json(), {"reboot": {"required": False, "packages": [], "more": 0, "kernel": None,
                                                   "running": None},
                                        "updates": {"state": "checking", "checking": True}})

    def test_the_count_once_worked_out(self):
        self.get()
        self.done()
        updates = self.get().get_json()["updates"]
        self.assertEqual({k: updates[k] for k in ("state", "checking", "total", "security", "names", "more")},
                         {"state": "ok", "checking": False, "total": 3, "security": 3, "more": 0,
                          "names": ["libapt-pkg6.0t64", "apt", "libpcre2-8-0"]})

    def test_reboot_required(self):
        os.makedirs(os.path.join(self.root, "run"))
        for name, text in (("reboot-required", "*** System restart required ***\n"),
                           ("reboot-required.pkgs", "linux-image-6.8.0-50-generic\nlibc6\n")):
            with open(os.path.join(self.root, "run", name), "w") as f:
                f.write(text)
        self.assertEqual(self.get().get_json()["reboot"], {"required": True, "packages": [
            "linux-image-6.8.0-50-generic", "libc6"], "more": 0, "kernel": None, "running": None})

    def test_turned_off(self):
        with mock.patch.object(swizzin, "pending_updates", PendingUpdates(swizzin.app.logger, enabled=False)):
            self.assertEqual(self.get().get_json()["updates"], {"state": "off", "checking": False})

    def test_on_by_default(self):
        self.assertIs(swizzin.app.config["UPDATES_CHECK"], True)
        self.assertTrue(PANEL.enabled)
        self.assertEqual(PANEL.root, "/")


class NeverBlocks(PageTest):
    def test_the_panel_keeps_answering_while_apt_get_works(self):
        """As the panel runs (one process, eventlet): while apt-get works (2 seconds here), the route answers at once,
        the other requests too, and the other green threads (the live cards, the websocket) keep running."""
        with open(os.path.join(self.bin, "pause"), "w") as f:
            f.write("2")
        ticks = []

        def ticker():  # what the live cards' thread does: something every so often
            while True:
                ticks.append(time.monotonic())
                eventlet.sleep(0.02)
        green = eventlet.spawn(ticker)
        self.addCleanup(green.kill)
        eventlet.sleep(0.1)
        began = t = time.monotonic()
        self.assertEqual(self.get().get_json()["updates"], {"state": "checking", "checking": True})
        answered = [time.monotonic() - t]
        while self.updates.running:
            t = time.monotonic()
            r = self.get(path="/stats/loadavg")
            self.assertEqual(r.status_code, 200)
            t2 = time.monotonic()
            self.assertEqual(self.get().get_json()["updates"]["state"], "checking")
            answered += [t2 - t, time.monotonic() - t2]
            eventlet.sleep(0.1)
        lasted = time.monotonic() - began
        self.assertEqual(self.get().get_json()["updates"]["total"], 3)
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        self.assertGreater(lasted, 1.9)  # apt-get worked all along,
        self.assertGreaterEqual(len(answered), 5)  # with requests answered meanwhile,
        self.assertLess(max(answered), 0.5, answered)  # each at once
        self.assertGreater(ticks[-1] - ticks[0], 2)  # the ticker went on all along,
        self.assertLess(max(gaps), 0.5, max(gaps))  # never held up


class Pages(PageTest):
    def page(self, user, layout):
        with mock.patch.object(swizzin.layout_choices, "get", return_value=layout), \
                mock.patch.object(swizzin, "get_mounts", return_value=["/"]), \
                mock.patch.object(swizzin, "generate_page_list", return_value=[]):
            r = self.get(user, "/")
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def test_the_pills_in_the_heading_of_the_admins_overview(self):
        page = self.page("tester", "overview")
        heading = re.search(r'<div class="ov-heading">\s*<h2 class="ov-title" id="ov-glance">At a glance</h2>(.*?)'
                            r'<div class="ov-tiles">', page, re.S).group(1)
        self.assertIn('<div class="ov-status" id="ov-status" data-url="/stats/system-status"\n'
                      '           data-logout="/logout" hidden>', heading)
        self.assertEqual(re.findall(r'<details class="ov-pill" id="([\w-]+)" hidden>', heading),
                         ["ov-reboot", "ov-updates"])
        scripts = re.findall(r'src="/static/(js/[\w-]+\.js)"', page)
        # after the layout's own script, wherever the other blocks put theirs (core/overview_blocks.py)
        self.assertIn("js/system_status.js", scripts)
        self.assertLess(scripts.index("js/overview.js"), scripts.index("js/system_status.js"))
        self.assertFalse(self.updates.running)  # the page itself never runs apt-get: its script asks

    def test_nothing_for_another_account(self):
        """Their script quits at once (tests/test_system_status_js.py): nothing to fill, nothing to ask."""
        page = self.page("friend", "overview")
        self.assertNotIn("ov-status", page)
        self.assertNotIn("ov-pill", page)
        self.assertNotIn("/stats/system-status", page)

    def test_nothing_in_classic(self):
        for user in ("tester", "friend"):
            page = self.page(user, "classic")
            self.assertNotIn("ov-status", page)
            self.assertNotIn("system_status", page)
            self.assertNotIn("/stats/system-status", page)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
