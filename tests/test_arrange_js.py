"""Customize, on the Overview layout (static/js/arrange.js), run for real in Node.js.

The Overview page the panel renders runs in the small fake DOM of tests/fake_dom.js (no npm package to install), which
keeps the focus as a browser does: an element that leaves the page, or gets hidden, takes the focus away with it. The
keyboard is {press: id}: the focus on a button, then Enter. A few lines run first, for these tests only:
window.location.replace() (noted on the Customize button, data-test-left-for), and a request answered with an HTTP
status (401...). Skipped when node is not installed, except with REQUIRE_NODE=1, set by the automatic tests
(.github/workflows/tests.yml): there, a missing node fails them.

Run from the repository root: python tests/test_arrange_js.py
(uses a throwaway htpasswd file and a throwaway file of choices: nothing on the machine is touched)."""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import jinja2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# The fake DOM and its runner are those of the disk banner's tests. Importing them points the panel to a throwaway
# htpasswd file (the admin "tester", and "friend") and writes swizzin.cfg, removed at the end.
from test_disk_alert_js import FAKE_DOM, NODE, NODE_REQUIRED, ROOT, Tree, swizzin  # noqa: E402
from core import layouts, overview_blocks  # noqa: E402

SCRIPT = os.path.join(ROOT, "static", "js", "arrange.js")
PAGES = [{"name": "sonarr", "pretty_name": "Sonarr", "url": "https://demo.invalid/sonarr", "systemd": "sonarr",
          "img": "sonarr"}]
PRELUDE = r"""
(function () {
  var TEST = %s;
  window.location.replace = function (url) {
    document.getElementById('ov-customize').setAttribute('data-test-left-for', url);
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
ZONES = ["glance", "tiles", "row", "left", "right"]
LISTS = ["ov-arrange-list-" + zone for zone in ZONES]
WATCH = ["ov-arrange-status", "ov-arrange-message"]
ATTRS = [["ov-customize", "hidden"], ["ov-customize", "aria-expanded"], ["ov-customize", "aria-controls"],
         ["ov-customize", "data-test-left-for"], ["ov-arrange", "hidden"], ["ov-arrange-done", "aria-disabled"],
         ["ov-arrange-cancel", "aria-disabled"], ["ov-arrange-reset", "aria-disabled"]]
DEFAULT = {"glance": ["system-status"], "tiles": ["tile-cpu", "tile-ram", "tile-disk", "tile-net"],
           "row": ["bandwidth", "services"], "left": ["cpu", "ram"], "right": ["disks", "network"]}
ARRANGED = {"order": ["tile-ram", "tile-cpu", "tile-net", "tile-disk", "services", "bandwidth", "disks", "cpu", "ram",
                      "network"],
            "hidden": ["ram", "tile-disk"], "other_column": ["disks"]}
EXTRA = {"id": "extra", "zone": "right", "template": "overview/extra.html", "label": "Extra <b>block</b>"}
EXTRA_HTML = ('<details class="card ov-section" data-section="extra"><summary class="card-header">Extra</summary>'
              '</details>')


def line(block):
    return "ov-arrange-" + block


def press(block, button):
    return {"press": "ov-arrange-%s-%s" % (block, button)}


@unittest.skipUnless(NODE or NODE_REQUIRED, "node is not installed")
class ArrangeJs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not NODE:
            raise RuntimeError("node is not installed, and REQUIRE_NODE=1: these tests must run here")

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        patcher = mock.patch.object(swizzin.layout_choices, "path", os.path.join(self.dir, "layouts.json"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def page(self, user, arrangement=None):
        swizzin.layout_choices.set(user, "overview")
        swizzin.layout_choices.arrange(user, layouts.checked(arrangement) if arrangement else None)
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

    def run_page(self, steps, user="tester", arrangement=None, status=None, session=None, attrs=(), watch=()):
        """Loads the Overview page in the fake DOM with arrange.js, plays the steps; returns a snapshot after loading
        and after each one."""
        prelude = os.path.join(self.dir, "prelude.js")
        with open(prelude, "w") as f:
            f.write(PRELUDE % json.dumps({"status": status}))
        payload = {"page": self.page(user, arrangement), "scripts": [prelude, SCRIPT], "poll": None,
                   "socketio": False, "watch": WATCH + list(watch), "attrs": ATTRS + [list(a) for a in attrs],
                   "storage": None, "session": {} if session is None else session, "children": LISTS, "steps": steps}
        r = subprocess.run([NODE, FAKE_DOM], input=json.dumps(payload), capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, TZ="UTC"))
        if r.returncode:
            self.fail(r.stderr)
        return json.loads(r.stdout)

    @staticmethod
    def attr(snap, element, name):
        return snap["attrs"].get("%s %s" % (element, name))

    @staticmethod
    def lists(snap):
        """The lines of each zone of the editor, by block; None for a zone it does not show."""
        return {zone: None if snap["children"][list_id] is None else
                [i[len("ov-arrange-"):] for i in snap["children"][list_id]] for zone, list_id in zip(ZONES, LISTS)}

    def test_loading_shows_the_button_and_asks_nothing(self):
        loaded, = self.run_page([])
        self.assertIsNone(self.attr(loaded, "ov-customize", "hidden"))
        self.assertEqual(self.attr(loaded, "ov-customize", "aria-expanded"), "false")
        self.assertEqual(loaded["watch"]["ov-arrange-status"], "")  # the live region is there before it speaks
        self.assertIsNone(loaded["watch"]["ov-arrange-message"])  # no editor yet
        self.assertEqual((loaded["sent"], loaded["reloads"], loaded["focus"]), ([], 0, None))

    def test_customize_opens_the_editor_in_the_order_of_the_page(self):
        loaded, opened = self.run_page([{"press": "ov-customize"}], watch=[line("system-status"), line("tile-cpu")])
        self.assertIsNone(self.attr(opened, "ov-arrange", "hidden"))
        self.assertEqual(self.attr(opened, "ov-customize", "aria-expanded"), "true")
        self.assertEqual(self.attr(opened, "ov-customize", "aria-controls"), "ov-arrange")
        self.assertEqual(opened["focus"], "ov-arrange")
        self.assertEqual(self.lists(opened), DEFAULT)
        # Alone in its zone, the pills only hide; the tiles move too. The "Hidden" tag is there, hidden.
        self.assertEqual(opened["watch"][line("system-status")], "Reboot and updatesHiddenHide")
        self.assertEqual(opened["watch"][line("tile-cpu")], "CPUHidden↑↓Hide")
        self.assertEqual(opened["sent"], [])

    def test_move_later_keeps_the_focus_and_says_where(self):
        attrs = [(line("tile-cpu") + "-earlier", "aria-disabled"), (line("tile-cpu") + "-later", "aria-disabled"),
                 (line("tile-cpu") + "-later", "aria-label")]
        snaps = self.run_page([{"press": "ov-customize"}] + [press("tile-cpu", "later")] * 4, attrs=attrs)
        opened, once, twice, last, again = snaps[1:]
        self.assertEqual(self.attr(opened, line("tile-cpu") + "-earlier", "aria-disabled"), "true")
        self.assertEqual(self.attr(opened, line("tile-cpu") + "-later", "aria-label"), "Move CPU later")
        self.assertEqual(self.lists(once)["tiles"], ["tile-ram", "tile-cpu", "tile-disk", "tile-net"])
        self.assertEqual(once["focus"], line("tile-cpu") + "-later")
        self.assertEqual(once["watch"]["ov-arrange-status"], "CPU: position 2 of 4 in Tiles.")
        self.assertEqual(self.attr(once, line("tile-cpu") + "-earlier", "aria-disabled"), "false")
        self.assertEqual(self.lists(last)["tiles"], ["tile-ram", "tile-disk", "tile-net", "tile-cpu"])
        self.assertEqual(last["watch"]["ov-arrange-status"], "CPU: position 4 of 4 in Tiles.")
        # At the bottom, Move later says it does nothing (aria-disabled, so that the focus stays), and why.
        self.assertEqual(self.attr(last, line("tile-cpu") + "-later", "aria-disabled"), "true")
        self.assertEqual(self.lists(again)["tiles"], self.lists(last)["tiles"])
        self.assertEqual(again["focus"], line("tile-cpu") + "-later")
        self.assertEqual(again["watch"]["ov-arrange-status"], "CPU is already last in Tiles.")
        self.assertEqual([s["focus"] for s in snaps[2:]], [line("tile-cpu") + "-later"] * 4)
        self.assertEqual(again["sent"], [])

    def test_move_earlier(self):
        snaps = self.run_page([{"press": "ov-customize"}, press("network", "earlier"), press("network", "earlier"),
                               press("ram", "earlier"), press("ram", "earlier"), press("ram", "earlier")])
        self.assertEqual(self.lists(snaps[2])["right"], ["network", "disks"])
        self.assertEqual(snaps[2]["watch"]["ov-arrange-status"],
                         "Network Info: position 1 of 2 in Details, right column.")
        self.assertEqual(snaps[3]["watch"]["ov-arrange-status"],
                         "Network Info is already first in Details, right column.")
        self.assertEqual(self.lists(snaps[5])["left"], ["ram", "cpu"])
        self.assertEqual(snaps[4]["watch"]["ov-arrange-status"], "RAM Info: position 1 of 2 in Details, left column.")
        self.assertEqual(snaps[5]["watch"]["ov-arrange-status"], "RAM Info is already first in Details, left column.")
        self.assertEqual(snaps[5]["focus"], line("ram") + "-earlier")
        # The same words again are new text, so that a screen reader says them again.
        self.assertEqual(snaps[6]["watch"]["ov-arrange-status"],
                         "RAM Info is already first in Details, left column.\u00a0")

    def test_hide_and_show(self):
        attrs = [(line("tile-ram") + "-hide", "aria-pressed"), (line("tile-ram"), "data-hidden"),
                 (line("tile-ram") + "-hide", "aria-label")]
        opened, hidden, shown = self.run_page([{"press": "ov-customize"}, press("tile-ram", "hide"),
                                               press("tile-ram", "hide")], attrs=attrs)[1:]
        self.assertEqual(self.attr(opened, line("tile-ram") + "-hide", "aria-pressed"), "false")
        self.assertEqual(self.attr(opened, line("tile-ram") + "-hide", "aria-label"), "Hide Memory")
        self.assertEqual(self.attr(hidden, line("tile-ram") + "-hide", "aria-pressed"), "true")
        self.assertEqual(self.attr(hidden, line("tile-ram"), "data-hidden"), "")
        self.assertEqual(hidden["watch"]["ov-arrange-status"], "Memory: hidden.")
        self.assertEqual(hidden["focus"], line("tile-ram") + "-hide")
        self.assertEqual(self.attr(shown, line("tile-ram") + "-hide", "aria-pressed"), "false")
        self.assertIsNone(self.attr(shown, line("tile-ram"), "data-hidden"))
        self.assertEqual(shown["watch"]["ov-arrange-status"], "Memory: shown.")
        self.assertEqual(self.lists(shown), DEFAULT)  # hiding moves nothing

    def test_to_the_other_column_and_back(self):
        """The section goes to the end of the other column; the focus, taken away with it by the browser, comes back
        to its button, which now names the column it came from."""
        attrs = [(line("disks") + "-column", "aria-label"), (line("cpu") + "-column", "aria-label")]
        opened, moved, back = self.run_page([{"press": "ov-customize"}, press("disks", "column"),
                                             press("disks", "column")], attrs=attrs,
                                            watch=[line("disks") + "-column"])[1:]
        self.assertEqual(self.attr(opened, line("disks") + "-column", "aria-label"),
                         "Move Disk Info to the left column")
        self.assertEqual(opened["watch"][line("disks") + "-column"], "To the left")
        self.assertEqual(self.attr(opened, line("cpu") + "-column", "aria-label"), "Move CPU Info to the right column")
        self.assertEqual(self.lists(moved)["left"], ["cpu", "ram", "disks"])
        self.assertEqual(self.lists(moved)["right"], ["network"])
        self.assertEqual(moved["focus"], line("disks") + "-column")
        self.assertEqual(moved["watch"][line("disks") + "-column"], "To the right")
        self.assertEqual(self.attr(moved, line("disks") + "-column", "aria-label"),
                         "Move Disk Info to the right column")
        self.assertEqual(moved["watch"]["ov-arrange-status"], "Disk Info: position 3 of 3 in Details, left column.")
        self.assertEqual(self.lists(back)["right"], ["network", "disks"])
        self.assertEqual(back["focus"], line("disks") + "-column")

    def test_done_sends_the_arrangement_and_loads_the_page_again(self):
        steps = [{"press": "ov-customize"}, press("tile-cpu", "later"), press("ram", "hide"), press("disks", "column"),
                 {"press": "ov-arrange-done"}, {"ajax": {"arrangement": {}}}]
        snaps = self.run_page(steps)
        sent, saved = snaps[-2], snaps[-1]
        body = {"order": ["system-status", "tile-ram", "tile-cpu", "tile-disk", "tile-net", "bandwidth", "services",
                          "cpu", "ram", "disks", "network"], "hidden": ["ram"], "other_column": ["disks"]}
        self.assertEqual(len(sent["sent"]), 1)
        self.assertEqual(dict(sent["sent"][0], data=json.loads(sent["sent"][0]["data"])),
                         {"type": "POST", "url": "/layout/arrange", "data": body, "contentType": "application/json"})
        self.assertEqual(sent["watch"]["ov-arrange-message"], "Saving…")
        self.assertEqual(self.attr(sent, "ov-arrange-done", "aria-disabled"), "true")
        self.assertEqual(saved["reloads"], 1)
        self.assertEqual(saved["session"], {"overview.arranged": "1"})
        # What the route takes: the panel's own check accepts it as it is.
        self.assertEqual(layouts.checked(body), body)

    def test_nothing_else_while_saving(self):
        steps = [{"press": "ov-customize"}, press("tile-cpu", "later"), {"press": "ov-arrange-done"},
                 press("tile-cpu", "later"), {"press": "ov-arrange-cancel"}, {"press": "ov-arrange-reset"},
                 {"press": "ov-customize"}, {"press": "ov-arrange-done"}]
        snaps = self.run_page(steps)
        self.assertEqual(len(snaps[-1]["sent"]), 1)
        self.assertEqual(self.lists(snaps[-1])["tiles"], ["tile-ram", "tile-cpu", "tile-disk", "tile-net"])
        self.assertIsNone(self.attr(snaps[-1], "ov-arrange", "hidden"))

    def test_a_failure_says_so_and_keeps_the_editor(self):
        steps = [{"press": "ov-customize"}, press("tile-cpu", "later"), {"press": "ov-arrange-done"}, {"fail": True},
                 {"press": "ov-arrange-done"}]
        snaps = self.run_page(steps)
        failed = snaps[4]
        self.assertEqual(failed["watch"]["ov-arrange-message"], "Not saved: try again.")
        self.assertEqual(failed["watch"]["ov-arrange-status"], "Not saved: try again.")
        self.assertIsNone(self.attr(failed, "ov-arrange", "hidden"))
        self.assertEqual(self.attr(failed, "ov-arrange-done", "aria-disabled"), "false")
        self.assertEqual(self.lists(failed)["tiles"], ["tile-ram", "tile-cpu", "tile-disk", "tile-net"])
        self.assertEqual((failed["reloads"], failed["session"]), (0, {}))
        self.assertEqual(len(snaps[5]["sent"]), 2)  # Done again: a new try

    def test_logged_out_goes_to_the_logout_page(self):
        snaps = self.run_page([{"press": "ov-customize"}, press("tile-cpu", "later"), {"press": "ov-arrange-done"}],
                              status=401)
        self.assertEqual(self.attr(snaps[-1], "ov-customize", "data-test-left-for"), "/logout")
        self.assertEqual(snaps[-1]["reloads"], 0)

    def test_cancel_forgets_the_changes(self):
        steps = [{"press": "ov-customize"}, press("tile-cpu", "later"), press("ram", "hide"), press("disks", "column"),
                 {"press": "ov-arrange-cancel"}, {"press": "ov-customize"}]
        snaps = self.run_page(steps, attrs=[(line("ram") + "-hide", "aria-pressed")])
        cancelled, reopened = snaps[-2], snaps[-1]
        self.assertEqual(self.attr(cancelled, "ov-arrange", "hidden"), "")
        self.assertEqual(self.attr(cancelled, "ov-customize", "aria-expanded"), "false")
        self.assertEqual(cancelled["focus"], "ov-customize")
        self.assertEqual(cancelled["watch"]["ov-arrange-status"], "Changes discarded.")
        self.assertEqual((cancelled["sent"], cancelled["reloads"]), ([], 0))
        self.assertEqual(self.lists(reopened), DEFAULT)
        self.assertEqual(self.attr(reopened, line("ram") + "-hide", "aria-pressed"), "false")

    def test_customize_again_closes_the_editor_as_cancel_does(self):
        opened, closed = self.run_page([{"press": "ov-customize"}, {"press": "ov-customize"}])[1:]
        self.assertEqual(self.attr(closed, "ov-arrange", "hidden"), "")
        self.assertEqual(closed["focus"], "ov-customize")
        self.assertEqual(closed["watch"]["ov-arrange-status"], "")  # nothing was changed: nothing to say

    def test_done_without_a_change_sends_nothing(self):
        steps = [{"press": "ov-customize"}, press("tile-cpu", "later"), press("tile-cpu", "earlier"),
                 {"press": "ov-arrange-done"}]
        done = self.run_page(steps)[-1]
        self.assertEqual((done["sent"], done["reloads"]), ([], 0))
        self.assertEqual(self.attr(done, "ov-arrange", "hidden"), "")
        self.assertEqual(done["focus"], "ov-customize")
        self.assertEqual(done["watch"]["ov-arrange-status"], "Nothing changed.")

    def test_an_arranged_page_opens_as_arranged(self):
        attrs = [(line(b) + "-hide", "aria-pressed") for b in ("ram", "tile-disk", "cpu")]
        opened, = self.run_page([{"press": "ov-customize"}], arrangement=ARRANGED, attrs=attrs)[1:]
        self.assertEqual(self.lists(opened), {"glance": ["system-status"],
                                              "tiles": ["tile-ram", "tile-cpu", "tile-net", "tile-disk"],
                                              "row": ["services", "bandwidth"], "left": ["disks", "cpu", "ram"],
                                              "right": ["network"]})
        self.assertEqual([self.attr(opened, line(b) + "-hide", "aria-pressed") for b in ("ram", "tile-disk", "cpu")],
                         ["true", "true", "false"])

    def test_reset_to_default_then_done(self):
        """Reset puts the default arrangement in the editor only; Done then asks the panel for the default one."""
        steps = [{"press": "ov-customize"}, {"press": "ov-arrange-reset"}, {"press": "ov-arrange-done"},
                 {"ajax": {"arrangement": None}}]
        opened, reset, sent, saved = self.run_page(steps, arrangement=ARRANGED,
                                                   attrs=[(line("ram") + "-hide", "aria-pressed")])[1:]
        self.assertEqual(self.lists(reset), DEFAULT)
        self.assertEqual(self.attr(reset, line("ram") + "-hide", "aria-pressed"), "false")
        self.assertEqual(reset["focus"], "ov-arrange-reset")
        self.assertEqual(reset["watch"]["ov-arrange-status"], "Default arrangement in the editor: Done keeps it.")
        self.assertEqual(reset["sent"], [])
        self.assertEqual(json.loads(sent["sent"][0]["data"]), {"reset": True})
        self.assertEqual(saved["reloads"], 1)

    def test_reset_then_cancel_keeps_the_arrangement(self):
        steps = [{"press": "ov-customize"}, {"press": "ov-arrange-reset"}, {"press": "ov-arrange-cancel"},
                 {"press": "ov-customize"}]
        reopened = self.run_page(steps, arrangement=ARRANGED)[-1]
        self.assertEqual(self.lists(reopened)["left"], ["disks", "cpu", "ram"])
        self.assertEqual(reopened["sent"], [])

    def test_after_done_the_page_gives_the_focus_back_and_says_it_is_saved(self):
        loaded, later = self.run_page([{"timer": True}], session={"overview.arranged": "1", "other": "x"})
        self.assertEqual(loaded["focus"], "ov-customize")
        self.assertEqual(loaded["session"], {"other": "x"})  # once only
        self.assertEqual(later["watch"]["ov-arrange-status"], "Your arrangement of Overview is saved.")
        plain, = self.run_page([], session={})
        self.assertIsNone(plain["focus"])

    def test_blocked_storage_changes_nothing_else(self):
        steps = [{"press": "ov-customize"}, press("tile-cpu", "later"), {"press": "ov-arrange-done"},
                 {"ajax": {"arrangement": {}}}]
        snaps = self.run_page(steps, session="throws")
        self.assertEqual(snaps[-1]["reloads"], 1)
        self.assertIsNone(snaps[0]["focus"])

    def test_another_account_arranges_its_own_blocks_only(self):
        steps = [{"press": "ov-customize"}, press("tile-disk", "earlier"), {"press": "ov-arrange-done"}]
        opened, moved, sent = self.run_page(steps, user="friend")[1:]
        self.assertEqual(self.lists(opened), {"glance": None, "tiles": ["tile-cpu", "tile-ram", "tile-disk"],
                                              "row": ["services"], "left": ["cpu", "ram"], "right": ["disks"]})
        self.assertEqual(json.loads(sent["sent"][0]["data"]),
                         {"order": ["tile-cpu", "tile-disk", "tile-ram", "services", "cpu", "ram", "disks"],
                          "hidden": [], "other_column": []})

    def test_a_block_added_to_the_list_is_in_the_editor(self):
        """Nothing in arrange.js names a block: a new one is listed, moved and sent like the others, and its label
        goes in as text."""
        loader = swizzin.app.jinja_env.loader
        with mock.patch.object(overview_blocks, "BLOCKS", overview_blocks.BLOCKS + [EXTRA]), \
                mock.patch.object(swizzin.app.jinja_env, "loader",
                                  jinja2.ChoiceLoader([jinja2.DictLoader({EXTRA["template"]: EXTRA_HTML}), loader])):
            steps = [{"press": "ov-customize"}, press("extra", "column"), press("extra", "earlier"),
                     {"press": "ov-arrange-done"}]
            opened, moved, earlier, sent = self.run_page(steps, watch=[line("extra")])[1:]
        self.assertEqual(self.lists(opened)["right"], ["disks", "network", "extra"])
        self.assertEqual(opened["watch"][line("extra")], "Extra <b>block</b>Hidden↑↓HideTo the left")
        self.assertEqual(self.lists(earlier)["left"], ["cpu", "extra", "ram"])
        self.assertEqual(earlier["watch"]["ov-arrange-status"],
                         "Extra <b>block</b>: position 2 of 3 in Details, left column.")
        data = json.loads(sent["sent"][0]["data"])
        self.assertEqual((data["order"][-5:], data["other_column"]), (["cpu", "extra", "ram", "disks", "network"],
                                                                      ["extra"]))


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
