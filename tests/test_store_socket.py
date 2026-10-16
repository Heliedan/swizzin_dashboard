"""Installing from the App Store page: the panel's /store namespace, relaying to the real helper (fake box).

Run from the repository root: python tests/test_store_socket.py
(throwaway htpasswd file, helper without root on temporary folders: nothing is installed)."""
import base64
import os
import shutil
import sys
import tempfile
import time
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
import eventlet  # noqa: E402
from core.htpasswd import FailedLogins  # noqa: E402
from test_store_helper import Helper  # noqa: E402
from test_store_install import fake_installers  # noqa: E402


def auth(user):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{user}".encode()).decode()}


class StoreSocket(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.helper = Helper(self.base, extra=fake_installers(self.base))
        swizzin.app.config["STORE_SOCKET"] = self.helper.path
        swizzin.htpasswd.failed_logins = FailedLogins(5, 600, 900)
        self.client = swizzin.app.test_client()
        self.tabs = []
        self.pending = {}
        # pretend the dashboard's background threads already run: connecting must not start them
        self.saved = swizzin.thread, swizzin.thread2, swizzin.thread3
        swizzin.thread = swizzin.thread2 = swizzin.thread3 = object()

    def tearDown(self):
        for tab in self.tabs:
            if tab.is_connected("/store"):
                tab.disconnect(namespace="/store")
        eventlet.sleep(0.1)
        swizzin.thread, swizzin.thread2, swizzin.thread3 = self.saved
        self.helper.stop()
        shutil.rmtree(self.base, ignore_errors=True)

    def token(self, user="tester"):
        return self.client.get("/store/token", headers=auth(user)).get_json()["token"]

    def tab(self, user="tester", token=None, headers=None):
        tab = swizzin.socketio.test_client(swizzin.app, namespace="/store", flask_test_client=self.client,
                                           headers={**auth(user), **(headers or {})},
                                           auth={"token": self.token() if token is None else token})
        self.tabs.append(tab)
        return tab

    def wait(self, tab, test, timeout=15):
        """Helper messages relayed to this tab, until one passes `test`; returns (that one, all seen).
        Messages received after it in the same batch are kept for the next wait."""
        pending = self.pending.setdefault(id(tab), [])
        end, seen = time.time() + timeout, []
        while True:
            pending += [e["args"][0] for e in tab.get_received("/store") if e["name"] == "helper"]
            while pending:
                msg = pending.pop(0)
                seen.append(msg)
                if test(msg):
                    return msg, seen
            if time.time() > end:
                raise AssertionError("timed out, seen: {}".format(seen))
            eventlet.sleep(0.05)

    def output(self, seen):
        return "".join(m.get("output", "") for m in seen)

    def test_token_for_the_admin_only(self):
        r = self.client.get("/store/token", headers=auth("tester"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertTrue(r.get_json()["token"])
        self.assertEqual(self.client.get("/store/token", headers=auth("other")).status_code, 403)
        self.assertNotEqual(self.client.get("/store/token").status_code, 200)

    def test_connection_needs_the_admin_and_the_page_token(self):
        self.assertTrue(self.tab().is_connected("/store"))
        self.assertTrue(self.tab(headers={"Sec-Fetch-Site": "same-origin"}).is_connected("/store"))
        self.assertFalse(self.tab(token="guess").is_connected("/store"))
        self.assertFalse(self.tab(user="other", token=self.token()).is_connected("/store"))
        self.assertFalse(self.tab(headers={"Sec-Fetch-Site": "cross-site"}).is_connected("/store"))
        self.assertFalse(self.tab(headers={"Sec-Fetch-Site": "same-site"}).is_connected("/store"))

    def test_install_and_answer_a_question(self):
        tab = self.tab()
        tab.emit("install", {"app": "sonarr", "password": "tester", "rows": 24, "cols": 80}, namespace="/store")
        started, _ = self.wait(tab, lambda m: "started" in m or "error" in m)
        self.assertTrue(started["ok"], started)
        self.assertTrue(started["control_key"])
        _, seen = self.wait(tab, lambda m: "Continue?" in m.get("output", ""))
        tab.emit("input", {"data": "y\r"}, namespace="/store")
        done, seen = self.wait(tab, lambda m: "done" in m)
        self.assertEqual((done["done"]["result"], done["done"]["test"]), ("installed", "passed"))
        self.assertIn("sonarr installed", self.output(seen))

    def test_wrong_password_installs_nothing_and_counts_as_a_failed_login(self):
        tab = self.tab()
        tab.emit("install", {"app": "sonarr", "password": "nope"}, namespace="/store")
        self.assertEqual(self.wait(tab, lambda m: "error" in m)[0], {"ok": False, "error": "password"})
        self.assertFalse(os.path.exists(os.path.join(self.base, "locks", ".sonarr.lock")))
        for i in range(4):  # 5 different wrong passwords in all
            tab.emit("install", {"app": "sonarr", "password": f"nope{i}"}, namespace="/store")
            self.wait(tab, lambda m: "error" in m)
        tab.emit("install", {"app": "sonarr", "password": "tester"}, namespace="/store")
        self.assertEqual(self.wait(tab, lambda m: "error" in m)[0], {"ok": False, "error": "password"})

    def test_second_tab_watches_until_it_takes_control_with_the_password(self):
        first = self.tab()
        first.emit("install", {"app": "sonarr", "password": "tester"}, namespace="/store")
        self.wait(first, lambda m: "Continue?" in m.get("output", ""))
        second = self.tab()
        second.emit("attach", {}, namespace="/store")
        attached, seen = self.wait(second, lambda m: "Continue?" in self.output([m]) or "attached" in m)
        if "attached" in attached:
            self.assertFalse(attached["controller"])
            self.wait(second, lambda m: "Continue?" in m.get("output", ""))  # replayed
        second.emit("input", {"data": "y\r"}, namespace="/store")
        self.assertEqual(self.wait(second, lambda m: "error" in m)[0], {"ok": False, "error": "not-controller"})
        second.emit("attach", {"take": True, "password": "wrong"}, namespace="/store")
        self.assertEqual(self.wait(second, lambda m: "error" in m)[0], {"ok": False, "error": "password"})
        second.emit("attach", {"take": True, "password": "tester"}, namespace="/store")
        taken, _ = self.wait(second, lambda m: "attached" in m)
        self.assertTrue(taken["controller"])
        second.emit("input", {"data": "y\r"}, namespace="/store")
        self.assertEqual(self.wait(second, lambda m: "done" in m)[0]["done"]["result"], "installed")

    def test_closing_the_tab_closes_its_helper_connection(self):
        tab = self.tab()
        tab.emit("attach", {}, namespace="/store")
        self.wait(tab, lambda m: "error" in m or "attached" in m)  # no job yet: "no-job"
        links = swizzin.app.extensions["store"]["links"]
        self.assertEqual(len(links), 1)
        tab.disconnect(namespace="/store")
        eventlet.sleep(0.1)
        self.assertEqual(links, {})

    def test_helper_not_running(self):
        swizzin.app.config["STORE_SOCKET"] = os.path.join(self.base, "missing.sock")
        tab = self.tab()
        tab.emit("install", {"app": "sonarr", "password": "tester"}, namespace="/store")
        self.assertEqual(self.wait(tab, lambda m: "error" in m)[0], {"ok": False, "error": "unavailable"})


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
