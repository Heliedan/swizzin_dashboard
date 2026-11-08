"""The Logs buttons of Service Info (static/js/service_logs.js), run for real in Node.js.

The Overview page the panel renders runs in the small fake DOM of tests/fake_dom.js (no npm package to install). A
few lines run first, for these tests only, add what that fake DOM does not do: focus() (the element that has the
focus carries data-test-focus), window.location.replace() (noted on the panel, data-test-left-for), and a request
answered with an HTTP status (401, 403...). Skipped when node is not installed, except with REQUIRE_NODE=1, set by the
automatic tests (.github/workflows/tests.yml): there, a missing node fails them.

Run from the repository root: python tests/test_service_logs_js.py
(uses a throwaway htpasswd file: nothing on the machine is touched)."""
import base64
import calendar
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# The fake DOM and its runner are those of the disk banner's tests. Importing them points the panel to a throwaway
# htpasswd file (the admin "tester", and "friend") and writes swizzin.cfg, removed at the end.
from test_disk_alert_js import FAKE_DOM, NODE, NODE_REQUIRED, ROOT, Tree, swizzin  # noqa: E402

SCRIPT = os.path.join(ROOT, "static", "js", "service_logs.js")
PAGES = [{"name": "sonarr", "pretty_name": "Sonarr", "url": "https://demo.invalid/sonarr", "systemd": "sonarr",
          "img": "sonarr"},
         {"name": "rtorrent", "pretty_name": "rTorrent", "url": False, "systemd": "rtorrent@", "img": "rtorrent"}]
PRELUDE = r"""
(function () {
  var TEST = %s;
  var proto = Object.getPrototypeOf(document.createElement('div'));
  proto.focus = function () {
    document.querySelectorAll('[data-test-focus]').forEach(function (e) { e.removeAttribute('data-test-focus'); });
    this.setAttribute('data-test-focus', '');
  };
  window.location.replace = function (url) {
    document.getElementById('ov-logs').setAttribute('data-test-left-for', url);
  };
  if (TEST.status) {  // the next request is answered with this HTTP status, as jQuery does: statusCode, then error
    var ajax = $.ajax;
    $.ajax = function (options) {
      $.ajax = ajax;
      if (options.statusCode && options.statusCode[TEST.status]) { options.statusCode[TEST.status](); }
      options.error({ status: TEST.status });
    };
  }
})();
"""
NOV8 = calendar.timegm((2026, 11, 8, 12, 0, 0)) * 1000  # the browser's clock (ms), UTC (TZ=UTC for node)
SINCE = calendar.timegm((2026, 11, 7, 2, 20, 0))
TRAP = "<img src=x onerror=alert(1)> & \"quotes\""
WATCH = ["ov-logs-title", "ov-logs-state", "ov-logs-message", "ov-logs-lines"]
ATTRS = [["ov-logs", "hidden"], ["ov-logs-lines", "hidden"], ["ov-logs", "data-test-focus"],
         ["logs_sonarr", "data-test-focus"], ["logs_sonarr", "aria-expanded"], ["logs_rtorrent", "aria-expanded"],
         ["logs_sonarr", "aria-disabled"], ["logs_rtorrent", "aria-disabled"], ["ov-logs-refresh", "aria-disabled"],
         ["logs_sonarr", "class"], ["ov-logs-state", "title"], ["ov-logs", "data-test-left-for"]]

_tmp = tempfile.mkdtemp()
swizzin.layout_choices.path = os.path.join(_tmp, "layouts.json")
swizzin.layout_choices.default = "overview"


def answer(lines=("line one", "line two"), state="running", message=None, name="Sonarr", unit="sonarr.service",
           journal="ok"):
    """What POST /apps/logs answers (core/service_logs.py)."""
    states = {"running": {"load": "loaded", "active": "active", "sub": "running", "result": "success", "restarts": 4,
                          "since": SINCE}}
    return {"name": name, "unit": unit, "state": states[state] if isinstance(state, str) else state,
            "journal": journal, "lines": list(lines), "message": message}


def click(app, at=NOV8):
    return {"clickOn": 'button[data-logs="%s"]' % app, "at": at}


@unittest.skipUnless(NODE or NODE_REQUIRED, "node is not installed")
class LogsJs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not NODE:
            raise RuntimeError("node is not installed, and REQUIRE_NODE=1: these tests must run here")

    def page(self, user):
        real_isfile = os.path.isfile
        auth = {"Authorization": "Basic " + base64.b64encode(("%s:%s" % (user, user)).encode()).decode()}
        with mock.patch.object(swizzin, "get_mounts", return_value=["/"]), \
                mock.patch.object(swizzin, "generate_page_list", return_value=PAGES), \
                mock.patch.object(swizzin.os.path, "isfile",
                                  side_effect=lambda p: False if p.startswith("/install/") else real_isfile(p)):
            r = swizzin.app.test_client().get("/", headers=auth)
        self.assertEqual(r.status_code, 200)
        tree = Tree()
        tree.feed(r.get_data(as_text=True))
        return tree.root

    def run_page(self, steps, user="tester", status=None):
        """Loads the Overview page in the fake DOM with service_logs.js, plays the steps; returns a snapshot after
        loading and after each one."""
        prelude = os.path.join(_tmp, "prelude.js")
        with open(prelude, "w") as f:
            f.write(PRELUDE % json.dumps({"status": status}))
        payload = {"page": self.page(user), "scripts": [prelude, SCRIPT], "poll": None, "socketio": False,
                   "watch": WATCH, "attrs": ATTRS, "storage": None, "steps": steps}
        r = subprocess.run([NODE, FAKE_DOM], input=json.dumps(payload), capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, TZ="UTC"))
        if r.returncode:
            self.fail(r.stderr)
        return json.loads(r.stdout)

    @staticmethod
    def attr(snap, element, name):
        return snap["attrs"]["%s %s" % (element, name)]

    def test_nothing_is_asked_before_a_click(self):
        loaded, = self.run_page([])
        self.assertEqual(loaded["sent"], [])
        self.assertEqual(self.attr(loaded, "ov-logs", "hidden"), "")
        self.assertEqual(self.attr(loaded, "logs_sonarr", "aria-expanded"), "false")

    def test_a_click_shows_the_lines_as_text(self):
        lines = ["2026-11-08T02:20:01+0100 sonarr[812]: started", TRAP, "\ttab and  spaces  "]
        loaded, clicked, answered = self.run_page([click("sonarr"), {"ajax": answer(lines)}])
        self.assertEqual(clicked["sent"], [{"type": "POST", "url": "/apps/logs", "data": '{"application":"sonarr"}',
                                            "contentType": "application/json"}])
        self.assertIsNone(self.attr(clicked, "ov-logs", "hidden"))
        self.assertEqual(self.attr(clicked, "ov-logs", "data-test-focus"), "")  # the focus goes to the panel
        self.assertEqual(self.attr(clicked, "logs_sonarr", "aria-expanded"), "true")
        self.assertEqual(clicked["watch"]["ov-logs-title"], "Logs of Sonarr")
        self.assertEqual(clicked["watch"]["ov-logs-message"], "Reading the journal\u2026")
        self.assertEqual(answered["watch"]["ov-logs-lines"], "\n".join(lines))  # as written: text, never HTML
        self.assertIsNone(self.attr(answered, "ov-logs-lines", "hidden"))
        self.assertEqual(answered["watch"]["ov-logs-message"], "")
        self.assertEqual(answered["watch"]["ov-logs-state"],
                         "sonarr.service \u00b7 Running since Nov 7, 02:20 \u00b7 Automatic restarts: 4")
        self.assertIn("since the service was last started by hand", self.attr(answered, "ov-logs-state", "title"))

    def test_one_read_at_a_time(self):
        """While a read runs, the buttons say they are unavailable, and another click asks nothing."""
        snaps = self.run_page([click("sonarr"), click("rtorrent"), {"click": "ov-logs-refresh"}, {"ajax": answer()},
                               click("rtorrent")])
        busy = snaps[1]
        for element in ("logs_sonarr", "logs_rtorrent", "ov-logs-refresh"):
            self.assertEqual(self.attr(busy, element, "aria-disabled"), "true")
        self.assertIn("disabled", self.attr(busy, "logs_sonarr", "class"))
        self.assertEqual(len(snaps[3]["sent"]), 1)
        self.assertEqual(snaps[3]["watch"]["ov-logs-title"], "Logs of Sonarr")
        self.assertEqual(snaps[3]["attrs"]["logs_rtorrent aria-expanded"], "false")
        done = snaps[4]
        for element in ("logs_sonarr", "logs_rtorrent", "ov-logs-refresh"):
            self.assertIsNone(self.attr(done, element, "aria-disabled"))
        self.assertNotIn("disabled", self.attr(done, "logs_sonarr", "class"))
        other = snaps[5]  # once it ended, another service
        self.assertEqual(other["sent"][-1]["data"], '{"application":"rtorrent"}')
        self.assertEqual((self.attr(other, "logs_sonarr", "aria-expanded"),
                          self.attr(other, "logs_rtorrent", "aria-expanded")), ("false", "true"))
        self.assertEqual(other["watch"]["ov-logs-title"], "Logs of rTorrent")
        self.assertEqual(other["watch"]["ov-logs-lines"], "")  # the lines of the other service are gone

    def test_refresh_and_close(self):
        snaps = self.run_page([click("sonarr"), {"ajax": answer(["old"])}, {"click": "ov-logs-refresh"},
                               {"ajax": answer(["old", "new"])}, {"click": "ov-logs-close"}])
        self.assertEqual([s["data"] for s in snaps[3]["sent"]], ['{"application":"sonarr"}'] * 2)
        self.assertEqual(snaps[4]["watch"]["ov-logs-lines"], "old\nnew")
        closed = snaps[5]
        self.assertEqual(self.attr(closed, "ov-logs", "hidden"), "")
        self.assertEqual(self.attr(closed, "logs_sonarr", "aria-expanded"), "false")
        self.assertEqual(self.attr(closed, "logs_sonarr", "data-test-focus"), "")  # the focus back on its button
        self.assertIsNone(self.attr(closed, "ov-logs", "data-test-focus"))

    def test_an_answer_after_close_is_dropped(self):
        snaps = self.run_page([click("sonarr"), {"click": "ov-logs-close"}, {"ajax": answer(["late"])}])
        self.assertEqual(self.attr(snaps[3], "ov-logs", "hidden"), "")
        self.assertEqual(snaps[3]["watch"]["ov-logs-lines"], "")
        self.assertIsNone(self.attr(snaps[3], "logs_sonarr", "aria-disabled"))  # the buttons work again

    def test_without_the_root_service(self):
        """The panel's sentence, the state systemd gave, and no lines."""
        message = "The lines of the journal are read by the App Store service (swizzin-store), which does not answer."
        snaps = self.run_page([click("sonarr"), {"ajax": answer([], message=message, journal="missing")}])
        self.assertEqual(snaps[2]["watch"]["ov-logs-message"], message)
        self.assertEqual(self.attr(snaps[2], "ov-logs-lines", "hidden"), "")
        self.assertTrue(snaps[2]["watch"]["ov-logs-state"].startswith("sonarr.service \u00b7 Running since"))

    def test_the_states_in_words(self):
        cases = [
            ({"load": "loaded", "active": "failed", "sub": "failed", "result": "exit-code", "restarts": 5,
              "since": SINCE}, "Failed since Nov 7, 02:20 (exit-code) \u00b7 Automatic restarts: 5"),
            ({"load": "loaded", "active": "inactive", "sub": "dead", "result": "success", "restarts": 0,
              "since": SINCE}, "Stopped since Nov 7, 02:20 \u00b7 Automatic restarts: 0"),
            ({"load": "loaded", "active": "active", "sub": "exited", "result": "success", "restarts": None,
              "since": calendar.timegm((2025, 12, 31, 23, 5, 0))}, "Active (exited) since Dec 31 2025, 23:05"),
            ({"load": "loaded", "active": "activating", "sub": "auto-restart", "result": "exit-code", "restarts": 2,
              "since": SINCE}, "Starting \u00b7 Automatic restarts: 2"),
            ({"load": "not-found", "active": "inactive", "sub": "dead", "result": "success", "restarts": 0,
              "since": None}, "systemd does not know this service"),
            ({"load": None, "active": None, "sub": None, "result": None, "restarts": None, "since": None},
             "State unknown"),
            ({"load": "loaded", "active": "constructor", "sub": "dead", "result": "success", "restarts": None,
              "since": None}, "State unknown"),  # a word that is not a state, even one that every object has
            (None, "systemd did not say how it is")]
        steps = []
        for state, _ in cases:
            steps += [{"click": "ov-logs-refresh"} if steps else click("sonarr"), {"ajax": answer(state=state)}]
        snaps = self.run_page(steps)
        for (state, words), snap in zip(cases, snaps[2::2]):
            with self.subTest(state=state):
                self.assertEqual(snap["watch"]["ov-logs-state"], "sonarr.service \u00b7 " + words)
                # the explanation of the restarts, when they are shown
                self.assertEqual(bool(self.attr(snap, "ov-logs-state", "title")), "Automatic restarts" in words)

    def test_a_failed_request(self):
        snaps = self.run_page([click("sonarr"), {"fail": True}, {"click": "ov-logs-refresh"}, {"ajax": answer()}])
        self.assertEqual(snaps[2]["watch"]["ov-logs-message"], "The panel did not answer: try again.")
        self.assertIsNone(self.attr(snaps[2], "ov-logs-refresh", "aria-disabled"))
        self.assertEqual(snaps[4]["watch"]["ov-logs-lines"], "line one\nline two")  # Refresh works after it

    def test_logged_out(self):
        """401: the page goes to the logout page, as the dashboard's other requests do."""
        snaps = self.run_page([click("sonarr")], status=401)
        self.assertEqual(self.attr(snaps[1], "ov-logs", "data-test-left-for"), "/logout")
        self.assertEqual(snaps[1]["watch"]["ov-logs-message"], "Reading the journal\u2026")

    def test_refused(self):
        for status, sentence in ((403, "Only the admin can read the logs."),
                                 (404, "This service is no longer in Service Info: reload the page.")):
            with self.subTest(status):
                snaps = self.run_page([click("sonarr")], status=status)
                self.assertEqual(snaps[1]["watch"]["ov-logs-message"], sentence)
                self.assertIsNone(self.attr(snaps[1], "ov-logs", "data-test-left-for"))

    def test_another_account_has_no_buttons(self):
        """The script is loaded with the Overview layout, for everyone; without the panel, it does nothing."""
        loaded, = self.run_page([], user="friend")
        self.assertEqual(loaded["sent"], [])
        self.assertEqual(loaded["watch"], {name: None for name in WATCH})


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
