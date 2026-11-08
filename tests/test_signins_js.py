"""The section Panel sign-ins (static/js/signins.js), run for real in Node.js on the Overview page the panel renders,
in the fake DOM of tests/fake_dom.js, with static/js/overview.js which opens and closes the sections: when it asks, what
it shows, and that it inserts text only. Skipped when node is not installed, except with REQUIRE_NODE=1 (the automatic
tests).

Run from the repository root: python tests/test_signins_js.py
(uses a throwaway htpasswd file: nothing on the machine is touched)."""
import calendar
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
SCRIPTS = [os.path.join(ROOT, "static", "js", name) for name in ("overview.js", "signins.js")]
STORE_KEY = "overview.open"  # the open sections, kept by static/js/overview.js
# Run after the scripts, in the page (the "poll" of tests/fake_dom.js): notes in attributes of #ov-signins, which the
# snapshots give: the delay of each timer the script sets, where window.location.replace() sends the page, how many
# times the lists changed, and each list as [cell class, its text, class and text of its second line, elements in it]
# for each cell of each row. The websocket event "answer-401" answers the latest request with HTTP 401, as jQuery does
# (its statusCode handler, then its error handler).
NOTES = r"""(function () {
  var box = document.getElementById('ov-signins');
  if (!box) { return; }
  var keep = setTimeout, ajax = $.ajax, delays = [], last = null, renders = 0;
  setTimeout = function (f, ms) {
    delays.push(ms);
    box.setAttribute('data-delays', delays.join(' '));
    return keep(f, ms);
  };
  $.ajax = function (options) { last = options; return ajax(options); };
  window.location.replace = function (url) { box.setAttribute('data-left-for', url); };
  io.connect().on('answer-401', function () { last.statusCode[401](); last.error(); });
  function rows(body) {
    return body.children.map(function (tr) {
      return tr.children.map(function (td) {
        var more = td.children[0];
        return [td.className, td.childNodes.length ? td.childNodes[0].textContent : '', more ? more.className : null,
                more ? more.textContent : null, td.children.length];
      });
    });
  }
  ['ok', 'failed'].forEach(function (list) {
    var body = document.getElementById('ov-signins-' + list);
    new MutationObserver(function () {
      renders += 1;
      box.setAttribute('data-renders', renders);
      box.setAttribute('data-' + list, JSON.stringify(rows(body)));
    }).observe(body, { childList: true });
  });
})();"""
WATCH = ["ov-signins-message", "ov-signins-ok-note", "ov-signins-failed-note", "ov-signins-about"]
ATTRS = [["ov-signins", a] for a in ("data-delays", "data-left-for", "data-renders", "data-ok", "data-failed")] + \
    [["ov-signins-message", "hidden"], ["ov-signins-ok-table", "hidden"], ["ov-signins-failed-table", "hidden"],
     ["ov-signins-lists", "hidden"]]
NOV8 = calendar.timegm((2026, 11, 8, 14, 2, 0))  # 8 November 2026, 14:02 UTC (TZ=UTC for node)
NOW = {"at": NOV8 * 1000 + 60000}  # the browser's clock, for a step
TRAP = "<img src=x onerror=alert(1)> & \"quotes\""
YOU = "192.0.2.10"  # documentation addresses (RFC 5737, RFC 3849)


def signin(user="tester", address=YOU, at=NOV8):
    return {"at": at, "user": user, "address": address}


def failure(user="tester", address="198.51.100.7", last=NOV8, count=1, blocked=0, at=None):
    return {"at": at or last, "last": last, "user": user, "address": address, "count": count, "blocked": blocked}


def answer(signins=(), failures=(), signins_total=None, failures_total=None, you=YOU, days=30, on_disk=True):
    """What GET /signins answers (core/signins.py)."""
    return {"signins": list(signins), "signins_total": len(signins) if signins_total is None else signins_total,
            "failures": list(failures), "failures_total": len(failures) if failures_total is None else failures_total,
            "your_address": you, "days": days, "on_disk": on_disk, "kept": 100, "session": 30, "group": 10}


def step(data):
    return dict(NOW, ajax=data)


OPEN = {"summary": "signins"}  # a click on the section's title: it opens, or closes


@unittest.skipUnless(NODE or NODE_REQUIRED, "node is not installed")
class SigninsJs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not NODE:
            raise RuntimeError("node is not installed, and REQUIRE_NODE=1: these tests must run here")

    def run_page(self, steps, user="tester", storage=None):
        """The account's Overview with these scripts, in the fake DOM: a snapshot after loading and after each step."""
        with mock.patch.object(swizzin.layout_choices, "get", return_value="overview"):
            page = dashboard(user, ["/"], False)
        tree = Tree()
        tree.feed(page)
        payload = {"page": tree.root, "scripts": SCRIPTS, "poll": NOTES, "socketio": True, "watch": WATCH,
                   "attrs": ATTRS, "storage": {} if storage is None else storage, "steps": steps}
        r = subprocess.run([NODE, FAKE_DOM], input=json.dumps(payload), capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, TZ="UTC"))
        if r.returncode:
            self.fail(r.stderr)
        return json.loads(r.stdout)

    @staticmethod
    def attr(snap, name, element="ov-signins"):
        return snap["attrs"][element + " " + name]

    def rows(self, snap, which):
        value = self.attr(snap, "data-" + which)
        return json.loads(value) if value else []

    def texts(self, snap, which):
        """Each row as the texts of its cells, with their second line after " / "."""
        return [[cell[1] + (" / " + cell[3] if cell[3] is not None else "") for cell in row]
                for row in self.rows(snap, which)]

    def asked(self, snap):
        return [s["url"] for s in snap["sent"]]

    def delays(self, snap):
        return [int(d) for d in (self.attr(snap, "data-delays") or "").split()]


class WhenItAsks(SigninsJs):
    def test_a_closed_section_asks_nothing(self):
        snaps = self.run_page([{"timer": True}])
        for snap in snaps:
            self.assertEqual(self.asked(snap), [])
        self.assertEqual(snaps[0]["watch"]["ov-signins-message"], "Loading…")
        self.assertEqual(self.attr(snaps[0], "hidden", "ov-signins-lists"), "")  # no list before the first answer

    def test_opened_it_asks_then_every_minute(self):
        snaps = self.run_page([OPEN, step(answer()), {"timer": True}, step(answer())])
        self.assertEqual([len(self.asked(s)) for s in snaps], [0, 1, 1, 2, 2])
        self.assertEqual(self.asked(snaps[-1]), ["/signins", "/signins"])
        self.assertEqual(self.delays(snaps[-1]), [60000, 60000])

    def test_closed_again_it_stops_and_asks_at_once_when_opened(self):
        snaps = self.run_page([OPEN, step(answer()), OPEN, {"timer": True}, OPEN])
        self.assertEqual([len(self.asked(s)) for s in snaps], [0, 1, 1, 1, 1, 2])
        self.assertEqual(snaps[-1]["sections"]["signins"], True)

    def test_one_request_and_one_timer_at_a_time(self):
        snaps = self.run_page([OPEN, OPEN, OPEN, step(answer()), OPEN, OPEN, step(answer())])
        self.assertEqual(len(self.asked(snaps[3])), 1)  # opened, closed, opened: still the first request
        self.assertEqual(len(self.asked(snaps[-1])), 2)
        self.assertEqual(self.delays(snaps[-1]), [60000])  # the timer of the first answer was still waiting

    def test_open_when_the_page_loads(self):
        """Remembered open by static/js/overview.js: one request at once."""
        loaded = self.run_page([], storage={STORE_KEY: "signins"})[0]
        self.assertEqual(self.asked(loaded), ["/signins"])
        self.assertTrue(loaded["sections"]["signins"])

    def test_nothing_for_another_account(self):
        """The script is loaded on every Overview (core/overview_blocks.py), and finds nothing to fill."""
        snaps = self.run_page([{"timer": True}], user="friend", storage={STORE_KEY: "signins"})
        self.assertEqual(snaps[-1]["sent"], [])
        self.assertEqual(snaps[-1]["watch"]["ov-signins-about"], None)
        self.assertNotIn("signins", snaps[-1]["sections"])

    def test_logged_out(self):
        snap = self.run_page([OPEN, {"event": "answer-401"}])[-1]
        self.assertEqual(self.attr(snap, "data-left-for"), "/logout")

    def test_a_failed_request(self):
        """It says so, keeps the lists shown, and tries again a minute later."""
        snaps = self.run_page([OPEN, step(answer([signin()])), {"timer": True}, {"fail": True}, {"timer": True},
                               step(answer([signin()]))])
        failed = snaps[4]
        self.assertEqual(failed["watch"]["ov-signins-message"], "The sign-ins could not be read: trying again in a "
                                                                "minute.")
        self.assertIsNone(self.attr(failed, "hidden", "ov-signins-message"))
        self.assertEqual(self.texts(failed, "ok"), [["Nov 8, 14:02", "tester", YOU + " / your address"]])
        self.assertEqual(self.delays(failed), [60000, 60000])
        self.assertEqual(len(self.asked(snaps[5])), 3)
        self.assertEqual(self.attr(snaps[6], "hidden", "ov-signins-message"), "")

    def test_a_failure_before_any_answer(self):
        """The message only: no empty list under it."""
        snap = self.run_page([OPEN, {"fail": True}])[-1]
        self.assertIn("could not be read", snap["watch"]["ov-signins-message"])
        self.assertEqual(self.attr(snap, "hidden", "ov-signins-lists"), "")
        snap = self.run_page([OPEN, step(answer()), {"timer": True}, {"fail": True}])[-1]
        self.assertIsNone(self.attr(snap, "hidden", "ov-signins-lists"))  # what was read stays shown

    def test_an_answer_that_is_not_one(self):
        snap = self.run_page([OPEN, step({"error": "x"})])[-1]
        self.assertIn("could not be read", snap["watch"]["ov-signins-message"])
        self.assertEqual(self.delays(snap), [60000])


class WhatItShows(SigninsJs):
    def test_the_sign_ins(self):
        snap = self.run_page([OPEN, step(answer([signin(), signin("friend", "2001:db8::7", NOV8 - 3600),
                                                 signin(TRAP, None, NOV8 - 7200)]))])[-1]
        self.assertEqual(self.texts(snap, "ok"), [["Nov 8, 14:02", "tester", YOU + " / your address"],
                                                 ["Nov 8, 13:02", "friend", "2001:db8::7"],
                                                 ["Nov 8, 12:02", TRAP, "invalid address"]])
        cells = self.rows(snap, "ok")
        self.assertEqual([c[0] for c in cells[0]], ["ov-signins-when", "", "ov-signins-address"])
        self.assertEqual(cells[0][2][2], "ov-signins-you")
        self.assertEqual(cells[2][2][0], "ov-signins-address ov-signins-quiet")
        self.assertIsNone(self.attr(snap, "hidden", "ov-signins-ok-table"))
        self.assertEqual(self.attr(snap, "hidden", "ov-signins-message"), "")

    def test_the_failed_attempts(self):
        snap = self.run_page([OPEN, step(answer(failures=[
            failure(count=12), failure(None, YOU, NOV8 - 60, 5, 900), failure(TRAP, "2001:db8::66", NOV8 - 120)]))])[-1]
        self.assertEqual(self.texts(snap, "failed"), [
            ["Nov 8, 14:02", "tester", "198.51.100.7", "×12"],
            ["Nov 8, 14:01", "unknown account", YOU + " / your address", "×5 / blocked 15\u00a0min"],
            ["Nov 8, 14:00", TRAP, "2001:db8::66", "×1"]])
        cells = self.rows(snap, "failed")
        self.assertEqual(cells[1][1][0], "ov-signins-quiet")
        self.assertEqual(cells[1][3][2], "ov-signins-blocked")
        self.assertEqual(cells[0][3][4], 0)  # no second line without a block

    def test_the_time_of_a_failed_line_is_its_last_try(self):
        snap = self.run_page([OPEN, step(answer(failures=[failure(at=NOV8 - 3600, last=NOV8, count=40)]))])[-1]
        self.assertEqual(self.texts(snap, "failed")[0][0], "Nov 8, 14:02")

    def test_another_year_says_so(self):
        dec30 = calendar.timegm((2025, 12, 30, 21, 4, 0))
        snap = self.run_page([OPEN, step(answer([signin(at=dec30)]))])[-1]
        self.assertEqual(self.texts(snap, "ok")[0][0], "Dec 30 2025, 21:04")

    def test_nothing_kept(self):
        snap = self.run_page([OPEN, step(answer())])[-1]
        self.assertEqual(self.attr(snap, "hidden", "ov-signins-ok-table"), "")
        self.assertEqual(self.attr(snap, "hidden", "ov-signins-failed-table"), "")
        self.assertEqual(snap["watch"]["ov-signins-ok-note"], "None in the last 30 days.")
        self.assertEqual(snap["watch"]["ov-signins-failed-note"], "None in the last 30 days.")
        self.assertEqual(snap["watch"]["ov-signins-about"],
                         "A sign-in is the first request of an account from an address after 30 minutes without one: "
                         "a page left open counts once. Failed attempts of the same name from the same address less "
                         "than 10 minutes apart make one line; a name that is not an account of the panel is never "
                         "kept. Kept 30 days after their last request, 100 of each at most, in a file only the panel "
                         "can read.")

    def test_in_memory_only(self):
        snap = self.run_page([OPEN, step(answer(days=0, on_disk=False))])[-1]
        self.assertEqual(snap["watch"]["ov-signins-ok-note"], "None since the panel started.")
        self.assertTrue(snap["watch"]["ov-signins-about"].endswith(
            "Kept in memory only: the panel forgets them when it restarts."))
        snap = self.run_page([OPEN, step(answer(days=1))])[-1]
        self.assertEqual(snap["watch"]["ov-signins-ok-note"], "None in the last 1 day.")

    def test_more_than_ten(self):
        snap = self.run_page([OPEN, step(answer([signin(at=NOV8 - i) for i in range(10)], [failure()],
                                                signins_total=37))])[-1]
        self.assertEqual(len(self.rows(snap, "ok")), 10)
        self.assertEqual(snap["watch"]["ov-signins-ok-note"], "The 10 most recent of 37 in the last 30 days.")
        self.assertEqual(snap["watch"]["ov-signins-failed-note"], "")

    def test_a_long_address_may_break_anywhere(self):
        """On a phone, rather than widen the table; a short one (IPv4, most IPv6) stays on its line."""
        long = "2001:db8:85a3:8d3:1319:8a2e:370:7348"
        snap = self.run_page([OPEN, step(answer([signin(address=long), signin(address="2001:db8:85a3::8a2e"),
                                                 signin(address="255.255.255.255")]))])[-1]
        self.assertEqual([row[2][0] for row in self.rows(snap, "ok")],
                         ["ov-signins-address ov-signins-long", "ov-signins-address", "ov-signins-address"])
        self.assertEqual(self.texts(snap, "ok")[0][2], long)

    def test_your_address_from_an_ipv6_page(self):
        snap = self.run_page([OPEN, step(answer([signin(address="2001:db8::7"), signin(address=YOU)],
                                                you="2001:db8::7"))])[-1]
        self.assertEqual([row[2] for row in self.texts(snap, "ok")], ["2001:db8::7 / your address", YOU])
        snap = self.run_page([OPEN, step(answer([signin(address=None)], you=None))])[-1]
        self.assertEqual(self.texts(snap, "ok")[0][2], "invalid address")  # never "your address"

    def test_the_lists_are_replaced(self):
        snaps = self.run_page([OPEN, step(answer([signin(), signin("friend")])), {"timer": True},
                               step(answer([signin("friend")]))])
        self.assertEqual([r[1] for r in self.texts(snaps[-1], "ok")], ["friend"])

    def test_the_same_answer_changes_nothing(self):
        data = answer([signin()], [failure()])
        snaps = self.run_page([OPEN, step(data), {"timer": True}, step(data)])
        self.assertEqual(self.attr(snaps[2], "data-renders"), self.attr(snaps[4], "data-renders"))
        self.assertEqual(self.texts(snaps[4], "ok"), self.texts(snaps[2], "ok"))


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
