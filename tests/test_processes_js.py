"""Top processes (static/js/processes.js), run for real in Node.js on the Overview page the panel renders, in the fake
DOM of tests/fake_dom.js, with static/js/overview.js which opens and closes the sections: when the page asks (only
while the section is open, not hidden with Customize, and the tab shows), what the lists and the lines say, and that
they insert text only. The section is open by default: most tests start with the browser remembering it closed
(CLOSED), and open it when they need to.
Skipped when node is not installed, except with REQUIRE_NODE=1 (the automatic tests).

Run from the repository root: python tests/test_processes_js.py
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
from core import layouts  # noqa: E402

_tmp = tempfile.mkdtemp()
swizzin.layout_choices.path = os.path.join(_tmp, "layouts.json")
SCRIPTS = [os.path.join(ROOT, "static", "js", name) for name in ("overview.js", "processes.js")]
URL = "/stats/processes"
# Run after the scripts, in the page (the "poll" of tests/fake_dom.js): notes in attributes, which the snapshots give:
# the delay of each timer the script sets (the fake DOM keeps none), where window.location.replace() sends the page,
# and the rows of each list (the text and class of each cell; the bar's track and fill, and its width), written each
# time a list changes.
# The websocket event "answer-401" answers the latest request with HTTP 401, as jQuery does (its statusCode handler,
# then its error handler).
NOTES = r"""(function () {
  var box = document.getElementById('ov-processes');
  if (!box) { return; }
  var keep = setTimeout, ajax = $.ajax, delays = [], last = null;
  setTimeout = function (f, ms) {
    delays.push(ms);
    box.setAttribute('data-delays', delays.join(' '));
    return keep(f, ms);
  };
  $.ajax = function (options) { last = options; return ajax(options); };
  window.location.replace = function (url) { box.setAttribute('data-replaced', url); };
  io.connect().on('answer-401', function () { last.statusCode[401](); last.error({ status: 401 }); });
  ['cpu', 'memory'].forEach(function (list) {
    var body = document.getElementById('ov-processes-' + list);
    new MutationObserver(function () {
      body.setAttribute('data-rows', JSON.stringify(body.children.map(function (tr) {
        return tr.children.map(function (td) {
          var track = td.children[0], fill = track && track.children[0];
          return td.className === 'ov-processes-bar'
            ? [td.className, td.getAttribute('aria-hidden'), track.className, fill.className, fill.style.width,
               fill.style.minWidth]
            : [td.className, td.textContent, td.children.map(function (s) { return s.className; })];
        });
      })));
    }).observe(body, { childList: true });
  });
})();"""
WATCH = ["ov-processes-message", "ov-processes-info", "ov-processes-cpu-empty", "ov-processes-memory-empty"]
ATTRS = [["ov-processes", "data-delays"], ["ov-processes", "data-replaced"], ["ov-processes-cpu", "data-rows"],
         ["ov-processes-memory", "data-rows"], ["ov-processes-cpu-table", "hidden"],
         ["ov-processes-memory-table", "hidden"], ["ov-processes-cpu-empty", "hidden"],
         ["ov-processes-memory-empty", "hidden"]]
RTORRENT = {"name": "rtorrent main", "user": "tester", "cpu": 25.0, "memory": "586.0 MiB", "memory_share": 7.2}
AUTOBRR = {"name": "autobrr", "user": "tester", "cpu": 0.01, "memory": "46.9 MiB", "memory_share": 0.6}
NGINX = {"name": "nginx", "user": "www-data", "cpu": 0.0, "memory": "3.5 MiB", "memory_share": 0.04}


def ok(cpu=(RTORRENT, AUTOBRR), memory=(RTORRENT, AUTOBRR, NGINX), own=False, hidden=False, cores=4, count=7,
       seconds=5.0):
    """An answer of /stats/processes, as core/processes.py makes it."""
    return {"state": "ok", "own": own, "hidden": hidden, "cores": cores, "seconds": seconds, "count": count,
            "cpu": list(cpu), "memory": list(memory)}


def answer(data):
    return {"ajax": data}


OPEN = {"summary": "processes"}  # a click on the section's header: it opens, or closes
CLOSED = {"overview.closed": "processes"}  # the browser remembers it closed (static/js/overview.js)


@unittest.skipUnless(NODE or NODE_REQUIRED, "node is not installed")
class ProcessesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not NODE:
            raise RuntimeError("node is not installed, and REQUIRE_NODE=1: these tests must run here")

    def run_page(self, steps, user="tester", storage=CLOSED, arrangement=None, **config):
        """The Overview of `user` (with `arrangement`, as POST /layout/arrange takes it) with overview.js and this
        script, in the fake DOM: a snapshot after loading and after each step."""
        with mock.patch.object(swizzin.layout_choices, "get", return_value="overview"), \
                mock.patch.object(swizzin.layout_choices, "arrangement",
                                  return_value=arrangement and layouts.checked(arrangement)), \
                mock.patch.dict(swizzin.app.config, config):
            page = dashboard(user, ["/"], False)
        tree = Tree()
        tree.feed(page)
        payload = {"page": tree.root, "scripts": SCRIPTS, "poll": NOTES, "socketio": True, "watch": WATCH,
                   "attrs": ATTRS, "storage": storage, "steps": steps}
        r = subprocess.run([NODE, FAKE_DOM], input=json.dumps(payload), capture_output=True, text=True, timeout=60)
        if r.returncode:
            self.fail(r.stderr)
        return json.loads(r.stdout)

    def asked(self, snap):
        return [s["url"] for s in snap["sent"]]

    def delays(self, snap):
        return [int(d) for d in (snap["attrs"]["ov-processes data-delays"] or "").split()]

    def rows(self, snap, name):
        return json.loads(snap["attrs"]["ov-processes-%s data-rows" % name] or "null")

    def texts(self, snap, name):
        """Each row as its texts: program, account, CPU, memory (and its share)."""
        return [[cell[1] for cell in row[:4]] for row in self.rows(snap, name)]

    def message(self, snap):
        return snap["watch"]["ov-processes-message"]


class WhenItAsks(ProcessesTest):
    def test_closed_it_asks_nothing(self):
        snaps = self.run_page([{"summary": "cpu"}, {"summary": "ram"}, {"hidden": True}, {"hidden": False}])
        for snap in snaps:
            self.assertEqual((snap["requests"], snap["timers"]), (0, 0))
        self.assertFalse(snaps[-1]["sections"]["processes"])

    def test_open_it_asks_at_once_then_every_5_seconds(self):
        snaps = self.run_page([OPEN, answer(ok()), {"timer": True}, answer(ok()), {"timer": True}])
        self.assertTrue(snaps[1]["sections"]["processes"])
        self.assertEqual(self.asked(snaps[1]), [URL])
        self.assertEqual(self.message(snaps[1]), "Reading the processes\u2026")
        self.assertEqual(self.message(snaps[2]), "")
        self.assertEqual(self.delays(snaps[2]), [5000])
        self.assertEqual(self.asked(snaps[3]), [URL] * 2)
        self.assertEqual(snaps[3]["pendingRequests"], 1)  # one at a time: the next after the answer
        self.assertEqual(self.asked(snaps[5]), [URL] * 3)
        self.assertEqual(self.delays(snaps[5]), [5000, 5000])

    def test_closed_again_it_stops(self):
        snaps = self.run_page([OPEN, answer(ok()), OPEN, {"timer": True}, OPEN, answer(ok()), {"timer": True}])
        self.assertFalse(snaps[3]["sections"]["processes"])
        self.assertEqual(snaps[4]["requests"], 1)  # the timer of the first opening: nothing
        self.assertEqual(snaps[5]["requests"], 2)  # opened again: at once
        self.assertEqual(snaps[7]["requests"], 3)

    def test_an_answer_after_closing_is_left(self):
        snaps = self.run_page([OPEN, OPEN, answer(ok()), {"timer": True}])
        self.assertIsNone(self.rows(snaps[3], "cpu"))
        self.assertEqual((snaps[3]["timers"], snaps[4]["requests"]), (0, 1))

    def test_open_by_default_it_asks_as_the_page_loads(self):
        """Open as the panel renders it ("open" in core/overview_blocks.py): one request at once, then every 5
        seconds; the same when the storage is blocked, or still holds what the previous version kept (the sections
        opened, among them this one)."""
        for storage in (None, {}, "throws", {"overview.open": "ram,processes"}):
            with self.subTest(storage=storage):
                snaps = self.run_page([answer(ok()), {"timer": True}], storage=storage)
                self.assertTrue(snaps[0]["sections"]["processes"])
                self.assertEqual(self.asked(snaps[0]), [URL])
                self.assertEqual(self.texts(snaps[1], "cpu")[0][0], "rtorrent main")
                self.assertEqual(self.delays(snaps[1]), [5000])
                self.assertEqual(self.asked(snaps[2]), [URL] * 2)

    def test_remembered_closed_it_asks_nothing_until_opened(self):
        """overview.js closes it as the page loads (localStorage "overview.closed"): no request, not even for its
        toggle; opened again, at once, and the browser forgets that it was closed."""
        snaps = self.run_page([{"timer": True}, OPEN, answer(ok())])
        self.assertFalse(snaps[0]["sections"]["processes"])
        self.assertEqual((snaps[0]["requests"], snaps[1]["requests"]), (0, 0))
        self.assertTrue(snaps[2]["sections"]["processes"])
        self.assertEqual(self.asked(snaps[2]), [URL])
        self.assertEqual(snaps[2]["storage"], {"overview.closed": ""})

    def test_hidden_with_customize_it_asks_nothing(self):
        """Hidden by the account's arrangement, the section is still in the page, open by default, but nobody sees it:
        the panel reads no process for it, whatever happens to it or to the tab."""
        snaps = self.run_page([OPEN, OPEN, {"hidden": True}, {"hidden": False}, {"timer": True}], storage={},
                              arrangement={"hidden": ["processes"]})
        self.assertTrue(snaps[0]["sections"]["processes"])
        for snap in snaps:
            self.assertEqual((snap["requests"], snap["timers"]), (0, 0))
        shown = self.run_page([], storage={}, arrangement={"hidden": ["disks"]})[0]  # another block hidden: it asks
        self.assertEqual(self.asked(shown), [URL])

    def test_a_hidden_tab_asks_nothing(self):
        snaps = self.run_page([OPEN, answer(ok()), {"hidden": True}, {"timer": True}, {"hidden": False},
                               {"hidden": False}])
        self.assertEqual(snaps[4]["requests"], 1)  # hidden: the timer does nothing
        self.assertEqual(snaps[5]["requests"], 2)  # shown again: at once
        self.assertEqual(snaps[6]["requests"], 2)

    def test_a_failed_request(self):
        """Says so, keeps the lists, and asks again 15 seconds later."""
        snaps = self.run_page([OPEN, answer(ok()), {"timer": True}, {"fail": True}, {"timer": True}, answer(ok())])
        self.assertEqual(self.message(snaps[4]), "The panel did not answer: trying again in a moment.")
        self.assertEqual(self.texts(snaps[4], "cpu"), self.texts(snaps[2], "cpu"))
        self.assertEqual(self.delays(snaps[4]), [5000, 15000])
        self.assertEqual(snaps[5]["requests"], 3)
        self.assertEqual(self.message(snaps[6]), "")

    def test_logged_out(self):
        snaps = self.run_page([OPEN, answer(ok()), {"timer": True}, {"event": "answer-401"}])
        self.assertEqual(snaps[4]["attrs"]["ov-processes data-replaced"], "/logout")
        self.assertEqual(self.delays(snaps[4]), [5000])  # nothing more asked
        self.assertEqual(self.message(snaps[4]), "")

    def test_turned_off(self):
        """TOP_PROCESSES = False: no section; the script, loaded with the others of Overview, quits at once."""
        loaded = self.run_page([], TOP_PROCESSES=False)[0]
        self.assertNotIn("processes", loaded["sections"])
        self.assertEqual((loaded["requests"], loaded["timers"]), (0, 0))


class WhatItShows(ProcessesTest):
    def test_the_lists(self):
        snap = self.run_page([OPEN, answer(ok())])[2]
        self.assertEqual(self.texts(snap, "cpu"), [["rtorrent main", "tester", "25.0%", "586.0 MiB7.2%"],
                                                   ["autobrr", "tester", "<0.1%", "46.9 MiB0.6%"]])
        self.assertEqual(self.texts(snap, "memory")[2], ["nginx", "www-data", "0.0%", "3.5 MiB<0.1%"])
        first = self.rows(snap, "cpu")[0]
        self.assertEqual(first[3], ["ov-processes-number", "586.0 MiB7.2%",
                                    ["ov-processes-amount", "ov-processes-share"]])
        # the bar repeats the number the list is sorted by, hidden from screen readers
        self.assertEqual(first[4], ["ov-processes-bar", "true", "ov-processes-track", "ov-processes-fill", "25%",
                                    "2px"])
        self.assertEqual(self.rows(snap, "memory")[0][4][4], "7.2%")
        self.assertEqual(self.rows(snap, "memory")[2][4][4:], ["0.04%", "2px"])
        self.assertEqual(self.rows(snap, "cpu")[1][4][4:], ["0.01%", "2px"])
        for name in ("cpu", "memory"):
            self.assertIsNone(snap["attrs"]["ov-processes-%s-table hidden" % name])
            self.assertEqual(snap["attrs"]["ov-processes-%s-empty hidden" % name], "")

    def test_the_lists_are_made_again(self):
        snaps = self.run_page([OPEN, answer(ok()), {"timer": True}, answer(ok(cpu=[AUTOBRR], memory=[NGINX]))])
        self.assertEqual(self.texts(snaps[4], "cpu"), [["autobrr", "tester", "<0.1%", "46.9 MiB0.6%"]])
        self.assertEqual(len(self.rows(snaps[4], "memory")), 1)

    def test_the_line_about_the_lists(self):
        cases = [(ok(), "7 processes. CPU: share of the whole processor over the last 5 s (4 cores: a program that "
                        "keeps one busy shows 25.0%)."),
                 (ok(own=True, count=1, cores=6, seconds=1.0), "1 process of yours. CPU: share of the whole "
                  "processor over the last 1 s (6 cores: a program that keeps one busy shows 16.7%)."),
                 (ok(own=True, count=3, cores=1, seconds=4.6), "3 processes of yours. CPU: share of the whole "
                  "processor over the last 5 s (1 core).")]
        for data, line in cases:
            with self.subTest(line):
                self.assertEqual(self.run_page([OPEN, answer(data)])[2]["watch"]["ov-processes-info"], line)

    def test_empty_lists(self):
        snap = self.run_page([OPEN, answer(ok(cpu=[], memory=[], own=True, count=0))], user="friend")[2]
        self.assertEqual(snap["watch"]["ov-processes-cpu-empty"],
                         "None of your programs used the processor in the last 5 s.")
        self.assertEqual(snap["watch"]["ov-processes-memory-empty"], "You have no program running.")
        for name in ("cpu", "memory"):
            self.assertEqual(snap["attrs"]["ov-processes-%s-table hidden" % name], "")
            self.assertIsNone(snap["attrs"]["ov-processes-%s-empty hidden" % name])
        snap = self.run_page([OPEN, answer(ok(cpu=[], memory=[NGINX]))])[2]
        self.assertEqual(snap["watch"]["ov-processes-cpu-empty"], "No program used the processor in the last 5 s.")
        self.assertEqual(snap["attrs"]["ov-processes-memory-empty hidden"], "")

    def test_what_keeps_the_lists_from_showing(self):
        cases = [({"state": "no-account", "own": True}, "This server has no Linux account named like yours: the "
                  "panel cannot tell which programs are yours."),
                 ({"state": "unreadable", "own": False}, "The panel cannot read the processes of this server (/proc)."),
                 ({"state": "a state of a newer panel"}, "The panel sent an answer this page does not know: reload "
                  "the page."),
                 ([], "The panel sent an answer this page does not know: reload the page.")]
        for data, message in cases:
            with self.subTest(message):
                snaps = self.run_page([OPEN, answer(ok()), {"timer": True}, answer(data)])
                snap = snaps[4]
                self.assertEqual(self.message(snap), message)
                self.assertEqual(snap["watch"]["ov-processes-info"], "")
                self.assertEqual(self.rows(snap, "cpu"), [])
                for name in ("cpu", "memory"):
                    self.assertEqual(snap["attrs"]["ov-processes-%s-table hidden" % name], "")
                    self.assertEqual(snap["attrs"]["ov-processes-%s-empty hidden" % name], "")
                self.assertEqual(self.delays(snap), [5000, 5000])  # it asks again all the same

    def test_hidepid(self):
        snaps = self.run_page([OPEN, answer(ok(hidden=True)), {"timer": True}, answer(ok())])
        self.assertEqual(self.message(snaps[2]), "This server hides the processes of other accounts from the panel "
                         "(/proc is mounted with hidepid): only the panel's own show.")
        self.assertEqual(self.message(snaps[4]), "")
        snap = self.run_page([OPEN, answer(ok(cpu=[], memory=[], own=True, count=0, hidden=True))], user="friend")[2]
        self.assertEqual(self.message(snap), "This server hides the processes of other accounts from the panel "
                         "(/proc is mounted with hidepid): yours cannot show.")

    def test_text_only(self):
        """The names of the programs and of the accounts come from the system (a program names itself): inserted as
        text (the fake DOM refuses HTML)."""
        trap = "<img src=x onerror=alert(1)>"
        row = {"name": trap, "user": trap + "&amp;", "cpu": 3.0, "memory": "<b>1 MiB</b>", "memory_share": 1.0}
        snap = self.run_page([OPEN, answer(ok(cpu=[row], memory=[row]))])[2]
        self.assertEqual(self.texts(snap, "cpu"), [[trap, trap + "&amp;", "3.0%", "<b>1 MiB</b>1.0%"]])

    def test_odd_values(self):
        """Numbers out of range, a row that is not one: never a bar wider than the cell, never a script error."""
        odd = {"name": "x", "user": "y", "cpu": 250, "memory": "1 MiB", "memory_share": -3}
        snap = self.run_page([OPEN, answer(ok(cpu=[odd, None, "z"], memory=[odd]))])[2]
        self.assertEqual(self.texts(snap, "cpu"), [["x", "y", "100.0%", "1 MiB0.0%"]])
        self.assertEqual(self.rows(snap, "cpu")[0][4][4], "100%")
        self.assertEqual(self.rows(snap, "memory")[0][4][4:], ["0%", "0"])

    def test_another_account(self):
        snaps = self.run_page([OPEN, answer(ok(cpu=[], memory=[dict(NGINX, user="friend")], own=True, count=1))],
                              user="friend")
        self.assertEqual(self.asked(snaps[1]), [URL])
        self.assertEqual(self.texts(snaps[2], "memory"), [["nginx", "friend", "0.0%", "3.5 MiB<0.1%"]])


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
