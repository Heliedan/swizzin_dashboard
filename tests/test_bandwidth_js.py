"""The refresh of the bandwidth card (vnstat_top10() of the dashboard), run for real in Node.js.

The poll found in the dashboard runs on the HTML the panel renders, in the small fake DOM of tests/fake_dom.js, and
inserts what /stats/vnstat answers with a fake vnstat (tests/fake_vnstat.py). The page, its parser, node and the
fake DOM come from tests/test_disk_alert_js.py. Skipped when node is not installed, except with REQUIRE_NODE=1,
set by the automatic tests (.github/workflows/tests.yml): there, a missing node fails them.

Run from the repository root: python tests/test_bandwidth_js.py
(uses a throwaway htpasswd file and a fake vnstat: nothing on the machine is touched)."""
import base64
import json
import os
import re
import subprocess
import unittest
from unittest import mock

from test_disk_alert_js import FAKE_DOM, LIVE_STATS, NODE, NODE_REQUIRED, ROOT, Tree, dashboard, swizzin
from fake_vnstat import EMPTY, FakeVnstat, traffic, vnstat_json

AUTH = {"Authorization": "Basic " + base64.b64encode(b"tester:tester").decode()}


@unittest.skipUnless(NODE or NODE_REQUIRED, "node is not installed")
class Refresh(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not NODE:
            raise RuntimeError("node is not installed, and REQUIRE_NODE=1: these tests must run here")

    def setUp(self):
        self.vnstat = FakeVnstat()
        for patcher in (mock.patch.dict(os.environ, {"PATH": self.vnstat.dir + os.pathsep + os.environ["PATH"]}),
                        mock.patch.object(swizzin, "get_default_interface", return_value="eth0")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def card(self, data=None):
        """What /stats/vnstat answers the admin, parsed as the browser would: an answer for the {ajax} step."""
        if data is not None:
            self.vnstat.say(vnstat_json(data))
        r = swizzin.app.test_client().get("/stats/vnstat", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        tree = Tree()
        tree.feed(r.get_data(as_text=True))
        return {"tree": tree.root}

    def run_page(self, steps, user="tester"):
        """Loads the dashboard with its vnstat_top10() poll in the fake DOM, plays the steps; returns a snapshot after
        loading and after each."""
        page = dashboard(user, ["/"], False)
        found = re.search(r"\(function vnstat_top10\(\) \{.*?\n\}\)\(\);", page, re.S)
        self.assertIsNotNone(found, "vnstat_top10() not found in the dashboard")
        tree = Tree()
        tree.feed(page)
        payload = {"page": tree.root, "live_stats": LIVE_STATS, "poll": found.group(0), "socketio": True,
                   "steps": steps}
        r = subprocess.run([NODE, FAKE_DOM], input=json.dumps(payload), capture_output=True, text=True, timeout=60)
        if r.returncode:
            self.fail(r.stderr)
        return json.loads(r.stdout)

    @staticmethod
    def panes(snap):
        return {p["id"]: (p["active"], p["entries"], p["first"], p["scrollTop"]) for p in snap["top10"]["panes"]}

    @staticmethod
    def tabs(snap):
        return {t["id"]: (t["active"], t["selected"]) for t in snap["top10"]["tabs"]}

    def test_the_card_is_filled_then_refreshed_every_10_minutes(self):
        loaded, answered, again = self.run_page([{"ajax": self.card()}, {"timer": True}])
        self.assertEqual((loaded["requests"], loaded["timers"]), (1, 1))  # the next refresh is planned at once
        self.assertEqual(self.panes(answered), {"bw-days": (True, 62, "Nov 7, 2026", 0),
                                                "bw-months": (False, 25, "Nov 2026", 0)})
        self.assertEqual(self.tabs(answered), {"bw-days-tab": (True, "true"), "bw-months-tab": (False, "false")})
        self.assertEqual((again["requests"], again["timers"]), (2, 1))

    def test_a_failed_request_does_not_stop_the_refresh(self):
        """The poll used to plan the next refresh only after an answer: one failure (the panel restarting, the
        network...) stopped it until the page was reloaded."""
        snaps = self.run_page([{"fail": True}, {"timer": True}, {"ajax": self.card()}])
        self.assertEqual(snaps[1]["timers"], 1)
        self.assertEqual(snaps[2]["requests"], 2)
        self.assertEqual(self.panes(snaps[3])["bw-days"][:2], (True, 62))

    def test_the_open_tab_and_its_scroll_stay_after_a_refresh(self):
        card = self.card()
        snaps = self.run_page([{"ajax": card}, {"tab": "bw-months-tab"}, {"scroll": ["bw-months", 240]},
                               {"timer": True}, {"ajax": card}])
        refreshed = snaps[-1]
        self.assertEqual(self.tabs(refreshed), {"bw-days-tab": (False, "false"), "bw-months-tab": (True, "true")})
        self.assertEqual(self.panes(refreshed), {"bw-days": (False, 62, "Nov 7, 2026", 0),
                                                 "bw-months": (True, 25, "Nov 2026", 240)})
        self.assertEqual(refreshed["top10"]["tabsShown"], ["bw-months-tab"])

    def test_the_days_tab_keeps_its_scroll_too(self):
        card = self.card()
        snaps = self.run_page([{"ajax": card}, {"scroll": ["bw-days", 90]}, {"timer": True}, {"ajax": card}])
        self.assertEqual(self.panes(snaps[-1]), {"bw-days": (True, 62, "Nov 7, 2026", 90),
                                                 "bw-months": (False, 25, "Nov 2026", 0)})

    def test_a_message_then_the_history_again(self):
        """vnstat has no data for a while: the card says so, then shows the history again, Days open."""
        history, empty = self.card(), self.card(EMPTY)
        snaps = self.run_page([{"ajax": history}, {"tab": "bw-months-tab"}, {"timer": True}, {"ajax": empty},
                               {"timer": True}, {"ajax": self.card(traffic())}])
        self.assertEqual(snaps[4]["top10"]["text"], "No bandwidth data yet for eth0.")
        self.assertEqual(snaps[4]["top10"]["panes"], [])
        self.assertEqual(self.tabs(snaps[6]), {"bw-days-tab": (True, "true"), "bw-months-tab": (False, "false")})

    def test_another_user_never_asks(self):
        """Only the admin has the card (templates/netinfo.html), and /stats/vnstat refuses the others."""
        loaded = self.run_page([], user="friend")[0]
        self.assertNotIn("top10", loaded)
        self.assertEqual((loaded["requests"], loaded["timers"]), (0, 0))
        self.assertEqual(self.vnstat.calls(), [])


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
