"""Panel sign-ins in the panel: core/htpasswd.py telling the history of each login it checks (the account only when it
is one, the address as the login protection sees it, IPv6 and behind nginx included), the route /signins (the admin
only, never cached), the section in the admin's Overview only, and the login protection answering exactly as without
the history. The history itself is tested in tests/test_signins.py.

Run from the repository root: python tests/test_signins_page.py
(a throwaway htpasswd file and throwaway folders: nothing on the machine is touched)."""
import base64
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
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
LEFT_BEFORE = os.path.exists(os.path.join(ROOT, "signins.json"))  # a file of a panel run from this folder

import swizzin  # noqa: E402  (first: it runs eventlet.monkey_patch(), as when the panel runs)
from core import signins  # noqa: E402
from core.htpasswd import FailedLogins, HtPasswdAuth  # noqa: E402

URL = "/signins"
ADMIN_AT = "192.0.2.10"  # documentation addresses (RFC 5737, RFC 3849)
OTHER_AT = "198.51.100.7"
PAGES = [{"name": "rtorrent", "pretty_name": "rTorrent", "url": False, "systemd": "rtorrent@", "img": "rtorrent"}]


def basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


class PanelTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.history = signins.History(30, swizzin.app.logger)
        for patcher in (mock.patch.object(swizzin.htpasswd, "signin_history", self.history),
                        mock.patch.dict(swizzin.app.extensions, {"signins": self.history}),
                        mock.patch.object(swizzin.htpasswd, "failed_logins", FailedLogins(5, 600, 900)),
                        mock.patch.object(swizzin.layout_choices, "path", os.path.join(self.dir, "layouts.json")),
                        mock.patch.object(swizzin.limiter, "enabled", False)):  # it caps anonymous requests
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = swizzin.app.test_client()

    def get(self, user="tester", password=None, client=ADMIN_AT, peer="127.0.0.1", path="/stats/loadavg"):
        """A dashboard request as the local nginx forwards it (peer 127.0.0.1 + X-Forwarded-For)."""
        headers = {"X-Requested-With": "XMLHttpRequest"}
        if client is not None:
            headers["X-Forwarded-For"] = client
        if user is not None:
            headers["Authorization"] = basic(user, user if password is None else password)
        return self.client.get(path, headers=headers, environ_base={"REMOTE_ADDR": peer})

    def rows(self):
        """(sign-ins, failures) as tuples, oldest first; read without a request (which would add one)."""
        found = self.history.recent(None)
        return ([(r["user"], r["address"]) for r in found["signins"]][::-1],
                [(r["user"], r["address"], r["count"], r["blocked"]) for r in found["failures"]][::-1])


class Route(PanelTest):
    def test_the_admin_only(self):
        self.assertEqual(self.get(user=None, path=URL).status_code, 401)
        self.assertEqual(self.get(user="tester", password="wrong", path=URL).status_code, 401)
        r = self.get(user="friend", path=URL)
        self.assertEqual(r.status_code, 403)
        self.assertNotIn(ADMIN_AT, r.get_data(as_text=True))
        r = self.get(path=URL)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual(r.mimetype, "application/json")

    def test_what_the_admin_reads(self):
        self.get(user="friend", client=OTHER_AT)
        self.get(user="friend", password="typo", client=OTHER_AT)
        data = self.get(path=URL).get_json()
        self.assertEqual([(r["user"], r["address"]) for r in data["signins"]],
                         [("tester", ADMIN_AT), ("friend", OTHER_AT)])  # the admin's own request first
        self.assertEqual([(r["user"], r["address"], r["count"]) for r in data["failures"]], [("friend", OTHER_AT, 1)])
        self.assertEqual(data["your_address"], ADMIN_AT)
        self.assertEqual((data["days"], data["on_disk"]), (30, False))  # imported: in memory only

    def test_the_panel_names_its_file_only_when_it_serves(self):
        """Imported (the tests), its history is in memory only; python swizzin.py names signins.json next to
        swizzin.cfg before it serves."""
        self.assertIsNone(swizzin.signin_history.file)
        with open(os.path.join(ROOT, "swizzin.py"), encoding="utf-8") as f:
            source = f.read()
        main = source[source.index("if __name__ == '__main__':"):]
        self.assertIn("signin_history.keep_in(os.path.join(app.root_path, signins.FILE))", main)
        self.assertLess(main.index("keep_in"), main.index("socketio.run"))
        self.assertEqual(signins.FILE, "signins.json")


class Logins(PanelTest):
    def test_a_sign_in_and_an_open_page(self):
        for _ in range(5):
            self.assertEqual(self.get().status_code, 200)
        self.assertEqual(self.rows(), ([("tester", ADMIN_AT)], []))

    def test_a_wrong_password_from_the_page(self):
        """An XHR of the dashboard with a wrong password: refused as before, and seen by the admin."""
        r = self.get(password="wrong", client=OTHER_AT)
        self.assertEqual((r.status_code, r.get_data(as_text=True)), (401, "UNAUTHORIZED"))
        data = self.get(path=URL).get_json()
        self.assertEqual([(r["user"], r["address"], r["count"], r["blocked"]) for r in data["failures"]],
                         [("tester", OTHER_AT, 1, 0)])

    def test_an_unknown_name_is_never_kept(self):
        """It may be a password typed in the wrong box: neither the route nor the file ever has it."""
        self.history.keep_in(os.path.join(self.dir, "signins.json"))
        for name in ("hunter2-not-a-user", "Tester", "tester "):
            self.get(user=name, password="x", client=OTHER_AT)
        self.get()  # a sign-in: the file is written
        r = self.get(path=URL)
        self.assertEqual([(f["user"], f["count"]) for f in r.get_json()["failures"]], [(None, 3)])
        with open(os.path.join(self.dir, "signins.json"), encoding="utf-8") as f:
            text = f.read()
        for name in ("hunter2", "Tester", "tester "):
            self.assertNotIn(name, text)
            self.assertNotIn(name, r.get_data(as_text=True))

    def test_no_credentials_no_line(self):
        """Browsers ask once without credentials; a token is no login: neither is a sign-in or a failure."""
        self.get(user=None)
        self.client.get("/stats/loadavg", headers={"Authorization": "Bearer abc", "X-Forwarded-For": OTHER_AT},
                        environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(self.rows(), ([], []))

    def test_the_failure_that_blocks_the_address(self):
        for i in range(5):
            self.assertEqual(self.get(password=f"wrong-{i}", client=OTHER_AT).status_code, 401)
        for _ in range(3):  # refused without being checked: not noted
            self.assertEqual(self.get(client=OTHER_AT).status_code, 429)
        self.assertEqual(self.rows(), ([], [("tester", OTHER_AT, 5, 900)]))

    def test_the_websocket_counts_too(self):
        with mock.patch.object(swizzin, "thread", object()), mock.patch.object(swizzin, "thread2", object()), \
                mock.patch.object(swizzin, "thread3", object()):
            ws = swizzin.socketio.test_client(swizzin.app, namespace="/websocket", flask_test_client=self.client,
                                              headers={"Authorization": basic("friend", "friend"),
                                                       "X-Forwarded-For": OTHER_AT})
            self.assertTrue(ws.is_connected("/websocket"))
            ws.disconnect(namespace="/websocket")
            refused = swizzin.socketio.test_client(swizzin.app, namespace="/websocket", flask_test_client=self.client,
                                                   headers={"Authorization": basic("friend", "nope"),
                                                            "X-Forwarded-For": OTHER_AT})
            self.assertFalse(refused.is_connected("/websocket"))
        self.assertEqual(self.rows(), ([("friend", OTHER_AT)], [("friend", OTHER_AT, 1, 0)]))


class Addresses(PanelTest):
    """The address of the login protection (client_address in core/htpasswd.py), written the usual way."""

    def test_behind_nginx(self):
        self.get(client="203.0.113.9, " + OTHER_AT)  # the address nginx adds comes last
        self.assertEqual(self.rows()[0], [("tester", OTHER_AT)])

    def test_ipv6(self):
        self.get(client="2001:DB8:0:0::7")
        self.get(client="2001:db8::7")
        self.get(client="::ffff:192.0.2.10")
        self.get(client="2001:db8::8", peer="::1")  # nginx reached over IPv6 loopback
        self.assertEqual(self.rows()[0], [("tester", "2001:db8::7"), ("tester", ADMIN_AT), ("tester", "2001:db8::8")])

    def test_an_ipv6_client_reaching_the_panel_directly(self):
        self.get(client=None, peer="2001:db8::99")
        self.get(user="friend", password="x", client=None, peer="2001:db8::99")
        self.assertEqual(self.rows(), ([("tester", "2001:db8::99")], [("friend", "2001:db8::99", 1, 0)]))

    def test_a_forwarded_address_is_not_believed_from_outside(self):
        self.get(client="192.0.2.99", peer=OTHER_AT)
        self.assertEqual(self.rows()[0], [("tester", OTHER_AT)])

    def test_an_address_that_is_not_one(self):
        self.get(client="<img src=x onerror=alert(1)>")
        self.get(user="friend", password="x", client="unknown")
        self.assertEqual(self.rows(), ([("tester", None)], [("friend", None, 1, 0)]))

    def test_your_address(self):
        self.get(client="2001:DB8::10")
        self.assertEqual(self.get(path=URL, client="2001:DB8::10").get_json()["your_address"], "2001:db8::10")
        self.assertEqual(self.get(path=URL, client="garbage").get_json()["your_address"], None)


class LoginProtectionUnchanged(PanelTest):
    """The same requests, with and without the history: the same answers. tests/test_login_limit.py runs unchanged."""

    STEPS = [("tester", "tester", ADMIN_AT), ("tester", "old", ADMIN_AT)] * 3 + \
        [("tester", f"wrong-{i}", OTHER_AT) for i in range(5)] + [("tester", "tester", OTHER_AT)] * 2 + \
        [("ghost", "x", ADMIN_AT), (None, None, ADMIN_AT), ("friend", "friend", "2001:db8::1")] + \
        [("friend", f"w{i}", "2001:db8::1") for i in range(6)] + [("friend", "friend", "2001:db8::1")]

    def answers(self):
        swizzin.htpasswd.failed_logins = FailedLogins(5, 600, 900)
        out = []
        for user, password, client in self.STEPS:
            r = self.get(user=user, password=password, client=client)
            out.append((r.status_code, r.headers.get("Retry-After"), r.headers.get("WWW-Authenticate"),
                        r.get_data(as_text=True) if r.status_code != 200 else ""))
        return out

    def test_the_same_answers(self):
        with_history = self.answers()
        with mock.patch.object(swizzin.htpasswd, "signin_history", None):
            without = self.answers()
        self.assertEqual(with_history, without)
        self.assertEqual([a[0] for a in without].count(429), 4)  # 2 blocks, each refusing what follows
        self.assertEqual(self.rows()[1], [("tester", ADMIN_AT, 3, 0), ("tester", OTHER_AT, 5, 900),
                                          (None, ADMIN_AT, 1, 0), ("friend", "2001:db8::1", 5, 900)])

    def test_a_broken_history_changes_nothing(self):
        """Whatever the history does, the login goes on: the problem is in the panel's log."""
        without = None
        with mock.patch.object(swizzin.htpasswd, "signin_history", None):
            without = self.answers()
        broken = mock.Mock()
        broken.checked.side_effect = RuntimeError("broken history")
        with mock.patch.object(swizzin.htpasswd, "signin_history", broken), \
                self.assertLogs("core.htpasswd", "ERROR") as logs:
            self.assertEqual(self.answers(), without)
        self.assertIn("Panel sign-ins: this login could not be noted", logs.output[0])

    def test_the_history_never_gets_a_password(self):
        seen = mock.Mock()
        with mock.patch.object(swizzin.htpasswd, "signin_history", seen):
            self.answers()
        passwords = {p for _, p, _ in self.STEPS if p} - {"tester", "friend"}
        for call in seen.checked.call_args_list:
            for value in list(call.args) + list(call.kwargs.values()):
                self.assertNotIn(value, passwords)
        self.assertNotIn("ghost", [c.args[0] for c in seen.checked.call_args_list])


class Page(PanelTest):
    def page(self, user, layout="overview"):
        swizzin.layout_choices.set(user, layout)
        real_isfile = os.path.isfile
        with mock.patch.object(swizzin, "get_mounts", return_value=["/"]), \
                mock.patch.object(swizzin, "generate_page_list", return_value=PAGES), \
                mock.patch.object(swizzin.os.path, "isfile",
                                  side_effect=lambda p: False if p.startswith("/install/") else real_isfile(p)):
            r = self.client.get("/", headers={"Authorization": basic(user, user)})
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def test_the_section_of_the_admin(self):
        page = self.page("tester")
        self.assertEqual(page.count('data-section="signins"'), 1)
        self.assertIn('<details class="card ov-section" data-section="signins"><summary class="card-header">'
                      'Panel sign-ins</summary>', page)
        self.assertIn('id="ov-signins" data-url="/signins" data-logout="/logout"', page)
        self.assertEqual(page.count('src="/static/js/signins.js"'), 1)
        details = page[page.index('id="ov-details"'):]
        self.assertLess(details.index('data-section="disks"'), details.index('data-section="signins"'))
        self.assertLess(details.index('data-section="signins"'), details.index('class="ov-wide"'))  # a column's
        self.assertNotIn('data-section="signins" open', page)  # closed by default

    def test_nothing_for_another_account_nor_in_classic(self):
        """Another account's Overview loads the script, as every block's, and it finds nothing to fill (tested in
        tests/test_signins_js.py); Classic has neither."""
        for user, layout in (("friend", "overview"), ("tester", "classic"), ("friend", "classic")):
            with self.subTest(user=user, layout=layout):
                page = self.page(user, layout)
                for text in ("ov-signins", "Panel sign-ins", '"/signins"', "Recent sign-ins"):
                    self.assertNotIn(text, page)
                self.assertEqual(page.count("js/signins.js"), 1 if layout == "overview" else 0)

    def test_without_the_route_no_section(self):
        """When core/signins.py could not be loaded (swizzin.py says so in the log), the page still shows."""
        with mock.patch.dict(swizzin.app.config, {"SIGNINS_CARD": False}):
            page = self.page("tester")
        self.assertIn('id="ov-details"', page)
        self.assertNotIn('data-section="signins"', page)


class Hygiene(unittest.TestCase):
    def test_importing_the_panel_writes_no_file(self):
        """The requests of these tests were noted in memory only: no signins.json next to swizzin.cfg."""
        if not LEFT_BEFORE:
            self.assertFalse(os.path.exists(os.path.join(ROOT, "signins.json")))

    def test_the_file_is_the_panel_s_own(self):
        """keep_in, as python swizzin.py does: mode 0600, and only the panel's fields."""
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        history = signins.History(30, swizzin.app.logger)
        history.keep_in(os.path.join(folder, signins.FILE))
        with mock.patch.object(swizzin.htpasswd, "signin_history", history), \
                mock.patch.object(swizzin.limiter, "enabled", False):
            swizzin.app.test_client().get("/stats/loadavg", headers={"Authorization": basic("friend", "friend"),
                                                                     "X-Forwarded-For": "2001:db8::5"},
                                          environ_base={"REMOTE_ADDR": "127.0.0.1"})
        path = os.path.join(folder, signins.FILE)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual([(r["user"], r["address"]) for r in data["signins"]], [("friend", "2001:db8::5")])


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
