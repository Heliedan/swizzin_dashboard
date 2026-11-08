"""Top processes in the panel: route /stats/processes (who sees what, never cached, the setting) and the section of
the Details of Overview; and, under eventlet as the panel runs, that a reading never holds up the rest of the panel.

The reading itself is tested in tests/test_processes.py, whose fake /proc is used here.

Run from the repository root: python tests/test_processes_page.py
(throwaway htpasswd file and a fake /proc: nothing on the machine is touched)."""
import base64
import logging
import os
import re
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
for _user in ("tester", "friend", "ghost"):
    _ht.set_password(_user, _user)
_ht.save()
with open(os.path.join(ROOT, "swizzin.cfg"), "w") as f:
    f.write(f"FLASK_HTPASSWD_PATH = '{_tmp}/htpasswd'\nADMIN_USER = 'tester'\n")

import swizzin  # noqa: E402  (first: it runs eventlet.monkey_patch(), as when the panel runs)
import eventlet  # noqa: E402
import flask  # noqa: E402
from core import overview_blocks, processes  # noqa: E402
from test_processes import ADMIN, FRIEND, PLANTED, FakeProc, getpwuid, server  # noqa: E402

URL = "/stats/processes"
PANEL = swizzin.app.extensions["processes"]  # the panel's own, before the tests put theirs in its place
swizzin.layout_choices.path = os.path.join(_tmp, "layouts.json")


def auth(user, xhr=True):
    headers = {"Authorization": "Basic " + base64.b64encode(f"{user}:{user}".encode()).decode()}
    if xhr:
        headers["X-Requested-With"] = "XMLHttpRequest"
    return headers


def getpwnam(name):
    """pwd.getpwnam() for the accounts of these tests: "ghost" has no Linux account."""
    uids = {"tester": ADMIN, "friend": FRIEND}
    if name not in uids:
        raise KeyError(name)
    return mock.Mock(pw_uid=uids[name])


class PageTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeProc()
        self.addCleanup(self.fake.close)
        server(self.fake)
        self.reader = self.fake.reader()
        accounts = mock.patch.object(processes, "pwd")
        self.accounts = accounts.start()
        self.addCleanup(accounts.stop)
        self.accounts.getpwuid.side_effect = getpwuid
        self.accounts.getpwnam.side_effect = getpwnam
        for patcher in (mock.patch.dict(swizzin.app.extensions, {"processes": self.reader}),
                        mock.patch.object(swizzin.limiter, "enabled", False)):  # it caps anonymous requests
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = swizzin.app.test_client()

    def get(self, user="tester", path=URL, **kw):
        return self.client.get(path, headers=auth(user) if user else {"X-Requested-With": "XMLHttpRequest"}, **kw)


class Route(PageTest):
    def test_logged_in_only(self):
        self.assertEqual(self.get(None).status_code, 401)
        self.assertEqual(self.client.get(URL).status_code, 302)  # the login page
        self.assertEqual(self.reader.readings, 0)

    def test_the_admin_sees_every_program(self):
        r = self.get()
        self.assertEqual((r.status_code, r.headers["Cache-Control"], r.mimetype), (200, "no-store", "application/json"))
        data = r.get_json()
        self.assertEqual({k: data[k] for k in ("state", "own", "hidden", "cores", "count")},
                         {"state": "ok", "own": False, "hidden": False, "cores": 4, "count": 7})
        self.assertEqual({row["user"] for row in data["memory"]}, {"tester", "friend", "root"})
        self.assertEqual(set(data["memory"][0]), {"name", "user", "cpu", "memory", "memory_share"})

    def test_another_account_sees_its_own(self):
        data = self.get("friend").get_json()
        self.assertEqual((data["state"], data["own"], data["count"]), ("ok", True, 2))
        self.assertEqual([(row["name"], row["user"]) for row in data["memory"]],
                         [("rtorrent main", "friend"), ("bash", "friend")])

    def test_an_account_without_a_linux_account(self):
        """Nothing to show, and nothing read for it."""
        r = self.get("ghost")
        self.assertEqual((r.status_code, r.headers["Cache-Control"]), (200, "no-store"))
        self.assertEqual(r.get_json(), {"state": "no-account", "own": True})
        self.assertEqual(self.reader.readings, 0)

    def test_on_a_shared_server(self):
        """The same: every program for the admin, their own for the others."""
        normal = {user: self.get(user).get_json() for user in ("tester", "friend", "ghost")}
        with mock.patch.dict(swizzin.app.config, {"SHAREDSERVER": True}):
            shared = {user: self.get(user).get_json() for user in ("tester", "friend", "ghost")}
        self.assertEqual(shared, normal)

    def test_one_reading_for_every_account(self):
        for user in ("tester", "friend", "tester", "friend"):
            self.get(user)
        self.assertEqual(self.reader.readings, 2)  # the first look: two readings

    def test_never_the_command_line(self):
        for user in ("tester", "friend"):
            self.assertNotIn(PLANTED, self.get(user).get_data(as_text=True))

    def test_proc_unreadable(self):
        with mock.patch.dict(swizzin.app.extensions, {"processes": processes.Processes("/nonexistent/proc", gap=0)}):
            self.assertEqual(self.get().get_json(), {"state": "unreadable", "own": False})
            self.assertEqual(self.get("friend").get_json(), {"state": "unreadable", "own": True})

    def test_the_panels_own(self):
        self.assertIs(swizzin.app.config["TOP_PROCESSES"], True)
        self.assertEqual((PANEL.root, PANEL.keep, PANEL.gap, PANEL.fresh), ("/proc", 3, 1, 30))
        self.assertIs(PANEL.owner, processes.file_owner)


class Setting(unittest.TestCase):
    def app(self, value):
        app = flask.Flask("setting-test")
        app.config["TOP_PROCESSES"] = value
        processes.register(app, swizzin.htpasswd)
        return app

    def test_turned_off(self):
        app = self.app(False)
        self.assertIs(app.config["TOP_PROCESSES"], False)
        self.assertNotIn(URL, [rule.rule for rule in app.url_map.iter_rules()])
        self.assertNotIn("processes", app.extensions)

    def test_invalid(self):
        with self.assertLogs(level="WARNING") as logs:
            app = self.app("no")
        self.assertIs(app.config["TOP_PROCESSES"], True)
        self.assertIn(URL, [rule.rule for rule in app.url_map.iter_rules()])
        self.assertIn("TOP_PROCESSES = 'no' is invalid", logs.output[0])


class Pages(PageTest):
    def page(self, user, layout="overview", **config):
        with mock.patch.object(swizzin.layout_choices, "get", return_value=layout), \
                mock.patch.object(swizzin, "get_mounts", return_value=["/"]), \
                mock.patch.object(swizzin, "generate_page_list", return_value=[]), \
                mock.patch.dict(swizzin.app.config, config):
            r = self.client.get("/", headers=auth(user, xhr=False))
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def test_a_section_of_details_after_ram_info(self):
        """In the left column, the machine's, after RAM Info; closed, like every section."""
        page = self.page("tester")
        details = page[page.index('id="ov-details"'):]
        found = re.findall(r'class="(live-stack|ov-column)"|data-section="([\w-]+)"', details)
        self.assertEqual([a or b for a, b in found][:6], ["live-stack", "ov-column", "cpu", "ram", "processes",
                                                          "ov-column"])
        self.assertIn('<details class="card ov-section" data-section="processes"><summary class="card-header">Top '
                      'processes</summary>', page)
        self.assertIn('id="ov-processes" data-url="/stats/processes" data-logout="/logout"', page)
        for name in ("cpu", "memory"):
            self.assertIn('<tbody id="ov-processes-%s"></tbody>' % name, page)
        self.assertEqual(self.reader.readings, 0)  # the page reads nothing: its script asks, once it is open

    def test_what_each_account_is_told(self):
        self.assertIn(">The programs of every account, by their name, updated every 5 seconds while this section is "
                      "open.</p>", self.page("tester"))
        self.assertIn(">Your programs, by their name, updated every 5 seconds while this section is open.</p>",
                      self.page("friend"))

    def test_its_script_and_its_stylesheet(self):
        for user in ("tester", "friend"):
            page = self.page(user)
            files = re.findall(r'(?:src|href)="/static/((?:css|js)/[\w-]+\.(?:css|js))"', page)
            for name in ("js/processes.js", "css/processes.css"):
                self.assertEqual(files.count(name), 1, name)
            self.assertLess(files.index("js/overview.js"), files.index("js/processes.js"))
            self.assertLess(files.index("css/overview.css"), files.index("css/processes.css"))
        self.assertIn("js/processes.js", overview_blocks.scripts())
        self.assertIn("css/processes.css", overview_blocks.stylesheets())

    def test_nothing_in_classic(self):
        for user in ("tester", "friend"):
            page = self.page(user, "classic")
            self.assertNotIn("processes", page)

    def test_turned_off(self):
        """No section; the script, loaded with the others of Overview, finds none and asks nothing."""
        page = self.page("tester", TOP_PROCESSES=False)
        self.assertNotIn('data-section="processes"', page)
        self.assertNotIn("/stats/processes", page)


class NeverBlocks(PageTest):
    def test_the_reading_lets_the_rest_of_the_panel_run(self):
        """As the panel runs (one process, eventlet): a green thread that only waits for its turn gets one at every
        pause of a reading of 2000 processes (every 200), and none without the pauses."""
        for pid in range(2000, 4000):
            self.fake.add(pid, "p%d" % pid, ticks=1, rss=1)
        turns = []

        def other():  # the live cards, the websocket, another request: anything waiting for its turn
            while True:
                turns.append(1)
                eventlet.sleep(0)
        green = eventlet.spawn(other)
        self.addCleanup(green.kill)
        eventlet.sleep(0)
        counts = []
        for every in (processes.PAUSE_EVERY, 10 ** 9):
            with mock.patch.object(processes, "PAUSE_EVERY", every):
                before = len(turns)
                reading = processes.read(self.fake.root, self.fake.owner)
                counts.append(len(turns) - before)
            self.assertEqual(len(reading.processes), 2007)
        self.assertGreaterEqual(counts[0], 9)  # 10 pauses
        self.assertEqual(counts[1], 0)

    def test_the_panel_answers_during_a_first_look(self):
        """The first look waits 1 second between its two readings: a green sleep. Meanwhile, /stats/loadavg answers
        at once, a green thread ticking every 20 ms is never held up, and a second account's request waits for the
        same look instead of making its own."""
        reader = self.fake.reader(gap=1)
        ticks, answered, done = [], [], {}

        def ticker():
            while True:
                ticks.append(time.monotonic())
                eventlet.sleep(0.02)

        def ask(user):
            t = time.monotonic()
            r = swizzin.app.test_client().get(URL, headers=auth(user))
            done[user] = (time.monotonic() - t, r.get_json())
        with mock.patch.dict(swizzin.app.extensions, {"processes": reader}):
            green = eventlet.spawn(ticker)
            self.addCleanup(green.kill)
            eventlet.sleep(0.1)
            began = time.monotonic()
            first = eventlet.spawn(ask, "tester")
            eventlet.sleep(0.2)
            second = eventlet.spawn(ask, "friend")
            while len(done) < 2:
                t = time.monotonic()
                self.assertEqual(self.get(path="/stats/loadavg").status_code, 200)
                answered.append(time.monotonic() - t)
                eventlet.sleep(0.1)
            first.wait()
            second.wait()
        gaps = [b - a for a, b in zip(ticks, ticks[1:]) if a >= began]
        self.assertGreaterEqual(done["tester"][0], 1)  # it waited for its second reading
        self.assertEqual((done["tester"][1]["state"], done["friend"][1]["own"]), ("ok", True))
        self.assertEqual(reader.readings, 2)  # one look for both
        self.assertGreaterEqual(len(answered), 5)
        self.assertLess(max(answered), 0.5, answered)
        self.assertLess(max(gaps), 0.5, max(gaps))


if __name__ == "__main__":
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
