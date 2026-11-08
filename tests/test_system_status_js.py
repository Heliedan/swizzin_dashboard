"""The pills Reboot required and pending updates (static/js/system_status.js), run for real in Node.js on the
Overview page the panel renders, in the fake DOM of tests/fake_dom.js: what they say, when they ask again, and that
they insert text only. Skipped when node is not installed, except with REQUIRE_NODE=1 (the automatic tests).

Run from the repository root: python tests/test_system_status_js.py
(uses a throwaway htpasswd file: nothing on the machine is touched)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# The page, the fake DOM and its runner are those of the disk banner's tests. Importing them points the panel to a
# throwaway htpasswd file (the admin "tester", and "friend") and writes swizzin.cfg, removed at the end.
from test_disk_alert_js import FAKE_DOM, NODE, NODE_REQUIRED, ROOT, Tree, dashboard, swizzin  # noqa: E402

_tmp = tempfile.mkdtemp()
swizzin.layout_choices.path = os.path.join(_tmp, "layouts.json")
SCRIPT = os.path.join(ROOT, "static", "js", "system_status.js")
# Run after the script, in the page (the "poll" of tests/fake_dom.js): notes in attributes of #ov-status, which the
# snapshots give, the delay of each timer the script sets (the fake DOM keeps none) and where window.location.replace()
# sends the page; and the websocket event "answer-401" answers the latest request with HTTP 401, as jQuery does
# (its statusCode handler, then its error handler).
NOTES = r"""(function () {
  var box = document.getElementById('ov-status');
  if (!box) { return; }
  var keep = setTimeout, ajax = $.ajax, delays = [], last = null;
  setTimeout = function (f, ms) { delays.push(ms); box.setAttribute('data-delays', delays.join(' ')); return keep(f, ms); };
  $.ajax = function (options) { last = options; return ajax(options); };
  window.location.replace = function (url) { box.setAttribute('data-replaced', url); };
  io.connect().on('answer-401', function () { last.statusCode[401](); last.error(); });
})();"""
PILLS = ("reboot", "updates")
NOV_7 = 1794031920  # 7 November 2026, 06:12 UTC
NO_REBOOT = {"required": False, "packages": [], "more": 0, "kernel": None, "running": None}


def ok(total=0, security=0, names=(), more=0, lists=NOV_7, days=0, old=False, checking=False):
    """The "updates" of /stats/system-status, as core/system_status.py sends them."""
    return {"state": "ok", "checking": checking, "total": total, "security": security, "names": list(names),
            "more": more, "lists": lists, "lists_days": days, "lists_old": old}


def answer(updates=None, reboot=None):
    return {"ajax": {"reboot": reboot or NO_REBOOT, "updates": updates or ok()}}


@unittest.skipUnless(NODE or NODE_REQUIRED, "node is not installed")
class PillsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not NODE:
            raise RuntimeError("node is not installed, and REQUIRE_NODE=1: these tests must run here")

    def run_page(self, steps, user="tester"):
        """The admin's Overview with this script, in the fake DOM: a snapshot after loading and after each step."""
        with mock.patch.object(swizzin.layout_choices, "get", return_value="overview"):
            page = dashboard(user, ["/"], False)
        tree = Tree()
        tree.feed(page)
        watch = [f"ov-{p}-{part}" for p in PILLS for part in ("text", "more")]
        attrs = [["ov-status", "hidden"], ["ov-status", "data-delays"], ["ov-status", "data-replaced"]] + \
            [[f"ov-{p}", a] for p in PILLS for a in ("hidden", "data-level", "open")]
        payload = {"page": tree.root, "scripts": [SCRIPT], "poll": NOTES, "socketio": True, "watch": watch,
                   "attrs": attrs, "steps": steps}
        r = subprocess.run([NODE, FAKE_DOM], input=json.dumps(payload), capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, TZ="UTC"))  # the dates in the browser's time zone: UTC here
        if r.returncode:
            self.fail(r.stderr)
        return json.loads(r.stdout)

    def pill(self, snap, name):
        """(text, details, level) of a shown pill; None when it is hidden."""
        if snap["attrs"][f"ov-{name} hidden"] is not None:
            return None
        return (snap["watch"][f"ov-{name}-text"], snap["watch"][f"ov-{name}-more"],
                snap["attrs"][f"ov-{name} data-level"])

    def pills(self, snap):
        """Both pills, and whether their box shows."""
        return self.pill(snap, "reboot"), self.pill(snap, "updates"), snap["attrs"]["ov-status hidden"] is None

    def delays(self, snap):
        return [int(d) for d in (snap["attrs"]["ov-status data-delays"] or "").split()]


class Pills(PillsTest):
    def test_hidden_until_the_answer(self):
        loaded = self.run_page([])[0]
        self.assertEqual(self.pills(loaded), (None, None, False))
        self.assertEqual([s["url"] for s in loaded["sent"]], ["/stats/system-status"])
        self.assertEqual(loaded["timers"], 0)

    def test_nothing_to_say(self):
        """Up to date, package lists of today, no reboot: nothing shows. Next look in 10 minutes."""
        snap = self.run_page([answer()])[1]
        self.assertEqual(self.pills(snap), (None, None, False))
        self.assertEqual(self.delays(snap), [600000])

    def test_updates_with_security(self):
        snap = self.run_page([answer(ok(12, 3, ["libc6", "openssl", "libssl3"]))])[1]
        self.assertEqual(self.pills(snap), (None, ("12 updates · 3 security", "Security: libc6, openssl, libssl3. "
                                                   "Package lists from Nov 7, 06:12.", "warning"), True))

    def test_updates_without_security(self):
        snap = self.run_page([answer(ok(1))])[1]
        self.assertEqual(self.pill(snap, "updates"), ("1 update", "Package lists from Nov 7, 06:12.", "quiet"))

    def test_many_security_updates(self):
        names = ["lib%d" % i for i in range(10)]
        snap = self.run_page([answer(ok(40, 11, names, more=1))])[1]
        self.assertEqual(self.pill(snap, "updates")[1], "Security: " + ", ".join(names) + " and 1 other. "
                         "Package lists from Nov 7, 06:12.")
        snap = self.run_page([answer(ok(40, 12, names, more=2))])[1]
        self.assertIn(" and 2 others. ", self.pill(snap, "updates")[1])

    def test_reboot_required(self):
        asked = dict(NO_REBOOT, required=True, packages=["linux-image-6.8.0-50-generic", "libc6"])
        cases = [(asked, "Asked for by linux-image-6.8.0-50-generic, libc6."),
                 (dict(asked, packages=["libc6"], more=3), "Asked for by libc6 and 3 others."),
                 (dict(asked, packages=[]), "The system asks for a reboot."),
                 (dict(asked, packages=[], kernel="6.1.0-28-amd64", running="6.1.0-26-amd64"),
                  "Kernel 6.1.0-28-amd64 is installed, and 6.1.0-26-amd64 is running.")]
        for reboot, details in cases:
            with self.subTest(details):
                snap = self.run_page([answer(reboot=reboot)])[1]
                self.assertEqual(self.pills(snap), (("Reboot required", details, "warning"), None, True))

    def test_both(self):
        snap = self.run_page([answer(ok(2), dict(NO_REBOOT, required=True))])[1]
        reboot, updates, shown = self.pills(snap)
        self.assertEqual((reboot[0], updates[0], shown), ("Reboot required", "2 updates", True))

    def test_checking(self):
        """The first count is being worked out: it says so and asks again 5 seconds later, until it has the count;
        a new count after apt worked shows the previous one meanwhile."""
        snaps = self.run_page([answer({"state": "checking", "checking": True}), {"timer": True},
                               answer({"state": "checking", "checking": True}), {"timer": True},
                               answer(ok(3, 3, ["apt", "libapt-pkg6.0", "tzdata"], checking=True)), {"timer": True},
                               answer(ok(0)), {"timer": True}])
        self.assertEqual(self.pill(snaps[1], "updates"),
                         ("Checking for updates…", "apt-get is working out the pending updates: a few seconds.",
                          "quiet"))
        self.assertEqual(self.pill(snaps[5], "updates")[0], "3 updates · 3 security")
        self.assertEqual(self.pills(snaps[7]), (None, None, False))
        self.assertEqual(self.delays(snaps[7]), [5000, 5000, 5000, 600000])
        self.assertEqual(snaps[8]["requests"], 5)

    def test_unknown(self):
        cases = [({"state": "error", "checking": False, "reason": "apt-get gave no answer within 60 seconds"},
                  "apt-get could not tell: apt-get gave no answer within 60 seconds."),
                 ({"state": "no-lists", "checking": False},
                  "apt has no package lists yet: apt update downloads them.")]
        for updates, details in cases:
            with self.subTest(updates["state"]):
                snap = self.run_page([answer(updates)])[1]
                self.assertEqual(self.pills(snap), (None, ("Updates unknown", details, "quiet"), True))

    def test_old_package_lists(self):
        """Lists 7 days old or more: the count may miss newer updates; it says so even when there are none."""
        old = NOV_7 - 12 * 86400
        snap = self.run_page([answer(ok(lists=old, days=12, old=True))])[1]
        self.assertEqual(self.pill(snap, "updates"), (
            "Package lists 12 days old", "No update in them. Package lists from Oct 26, 06:12 (12 days ago): newer "
            "updates show once apt update runs.", "quiet"))
        snap = self.run_page([answer(ok(2, lists=old, days=12, old=True))])[1]
        self.assertEqual(self.pill(snap, "updates")[:2], (
            "2 updates", "Package lists from Oct 26, 06:12 (12 days ago): newer updates show once apt update runs."))
        snap = self.run_page([answer(ok(2, lists=None, days=None))])[1]
        self.assertEqual(self.pill(snap, "updates")[:2], ("2 updates", ""))

    def test_turned_off_or_no_apt(self):
        for state in ("off", "no-apt", "a state of a newer panel"):
            with self.subTest(state):
                snap = self.run_page([answer({"state": state, "checking": False})])[1]
                self.assertEqual(self.pills(snap), (None, None, False))

    def test_a_failed_request_hides_them(self):
        """Until the next request, a minute later: nothing stale stays on show."""
        snaps = self.run_page([answer(ok(12, 3, ["a", "b", "c"]), dict(NO_REBOOT, required=True)), {"timer": True},
                               {"fail": True}, {"timer": True}, answer(ok(12, 3, ["a", "b", "c"]))])
        self.assertEqual(self.pills(snaps[3]), (None, None, False))
        self.assertEqual(self.delays(snaps[3]), [600000, 60000])
        self.assertEqual(self.pill(snaps[5], "updates")[0], "12 updates · 3 security")
        self.assertIsNone(self.pill(snaps[5], "reboot"))

    def test_logged_out(self):
        snaps = self.run_page([answer(), {"timer": True}, {"event": "answer-401"}])
        self.assertEqual(snaps[3]["attrs"]["ov-status data-replaced"], "/logout")

    def test_text_only(self):
        """Package names and apt's messages come from the system: inserted as text (the fake DOM refuses HTML)."""
        trap = "<img src=x onerror=alert(1)>"
        snap = self.run_page([answer(ok(1, 1, [trap]), dict(NO_REBOOT, required=True, packages=[trap]))])[1]
        self.assertEqual(self.pill(snap, "updates")[1], "Security: %s. Package lists from Nov 7, 06:12." % trap)
        self.assertEqual(self.pill(snap, "reboot")[1], "Asked for by %s." % trap)
        snap = self.run_page([answer({"state": "error", "checking": False, "reason": trap})])[1]
        self.assertEqual(self.pill(snap, "updates")[1], "apt-get could not tell: %s." % trap)

    def test_one_pill_open_at_a_time(self):
        """Their details show at the same place: opening one closes the other."""
        snaps = self.run_page([answer(ok(2), dict(NO_REBOOT, required=True)), {"click": "ov-reboot-text"},
                               {"click": "ov-updates-text"}, {"click": "ov-updates-text"}])
        opened = [(s["attrs"]["ov-reboot open"], s["attrs"]["ov-updates open"]) for s in snaps[1:]]
        self.assertEqual(opened, [(None, None), ("", None), (None, ""), (None, None)])

    def test_a_pill_that_goes_comes_back_closed(self):
        snaps = self.run_page([answer(ok(2)), {"click": "ov-updates-text"}, {"timer": True}, answer(ok(0)),
                               {"timer": True}, answer(ok(3))])
        self.assertEqual([s["attrs"]["ov-updates open"] for s in snaps[2:]], ["", "", None, None, None])
        self.assertEqual(self.pill(snaps[6], "updates")[0], "3 updates")

    def test_another_account(self):
        """The script quits at once: no element to fill, no request, no timer."""
        loaded = self.run_page([], user="friend")[0]
        self.assertEqual((loaded["requests"], loaded["timers"]), (0, 0))


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
