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
import re
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
ZONES = list(overview_blocks.ZONES)
COLUMNS = {"left": "right", "right": "left"}
LISTS = ["ov-arrange-list-" + zone for zone in ZONES]
WATCH = ["ov-arrange-status", "ov-arrange-message"]
ATTRS = [["ov-customize", "hidden"], ["ov-customize", "aria-expanded"], ["ov-customize", "aria-controls"],
         ["ov-customize", "data-test-left-for"], ["ov-arrange", "hidden"], ["ov-arrange-done", "aria-disabled"],
         ["ov-arrange-cancel", "aria-disabled"], ["ov-arrange-reset", "aria-disabled"]]
# The blocks named here are those of the first Overview; the others of the list (such as those added since) come
# after them, in each zone: the tests work out where from what the page lists on its Customize button.
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


def lists_of(data, default=False):
    """The lists the editor should show for the blocks the page lists on its Customize button (data-blocks): by zone,
    the blocks in the page's order (or in the default one), None for a zone it does not show (no block, and not a
    column whose other column has some)."""
    home = {b["id"]: b["home"] for b in data["blocks"]}
    if default:
        ids = {zone: [i for i in data["order"] if home[i] == zone] for zone in ZONES}
    else:
        ids = {zone: [b["id"] for b in data["blocks"] if b["zone"] == zone] for zone in ZONES}
    return {zone: ids[zone] if ids[zone] or (zone in COLUMNS and ids[COLUMNS[zone]]) else None for zone in ZONES}


def flat(lists):
    """Every block of the lists, zone after zone: the order Done sends."""
    return [i for zone in ZONES for i in (lists[zone] or [])]


def moved(items, item, step):
    """`items` with `item` one place earlier (-1) or later (1)."""
    out, i = list(items), items.index(item)
    out[i], out[i + step] = out[i + step], out[i]
    return out


def without(items, item):
    return [i for i in items if i != item]


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
        html = r.get_data(as_text=True)
        self.data = json.loads(re.search(r"data-blocks='([^']*)'", html).group(1))  # what the editor is made from
        self.label = {b["id"]: b["label"] for b in self.data["blocks"]}
        tree = Tree()
        tree.feed(html)
        return tree.root

    def expected(self, user="tester", arrangement=None):
        """The lists the editor should show on the page of `user` with `arrangement` (see lists_of())."""
        self.page(user, arrangement)
        return lists_of(self.data)

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
        expected = self.expected()
        loaded, opened = self.run_page([{"press": "ov-customize"}], watch=[line("system-status"), line("tile-cpu")])
        self.assertIsNone(self.attr(opened, "ov-arrange", "hidden"))
        self.assertEqual(self.attr(opened, "ov-customize", "aria-expanded"), "true")
        self.assertEqual(self.attr(opened, "ov-customize", "aria-controls"), "ov-arrange")
        self.assertEqual(opened["focus"], "ov-arrange")
        self.assertEqual(self.lists(opened), expected)
        self.assertEqual(sorted(flat(expected)), sorted(b["id"] for b in self.data["blocks"]))  # each block once
        # The blocks added to core/overview_blocks.py after Customize are listed like the others.
        self.assertIn("processes", expected["left"])
        self.assertIn("signins", expected["right"])
        # Alone in its zone, the pills only hide; the tiles move too. The "Hidden" tag is there, hidden.
        self.assertEqual(opened["watch"][line("system-status")], "Reboot and updatesHiddenHide")
        self.assertEqual(opened["watch"][line("tile-cpu")], "CPUHidden↑↓Hide")
        self.assertEqual(opened["sent"], [])

    def test_move_later_keeps_the_focus_and_says_where(self):
        tiles = self.expected()["tiles"]
        first, n = tiles[0], len(tiles)
        name = self.label[first]
        attrs = [(line(first) + "-earlier", "aria-disabled"), (line(first) + "-later", "aria-disabled"),
                 (line(first) + "-later", "aria-label")]
        snaps = self.run_page([{"press": "ov-customize"}] + [press(first, "later")] * n, attrs=attrs)
        opened, once, last, again = snaps[1], snaps[2], snaps[n], snaps[n + 1]
        self.assertEqual(self.attr(opened, line(first) + "-earlier", "aria-disabled"), "true")
        self.assertEqual(self.attr(opened, line(first) + "-later", "aria-label"), "Move %s later" % name)
        self.assertEqual(self.lists(once)["tiles"], moved(tiles, first, 1))
        self.assertEqual(once["focus"], line(first) + "-later")
        self.assertEqual(once["watch"]["ov-arrange-status"], "%s: position 2 of %d in Tiles." % (name, n))
        self.assertEqual(self.attr(once, line(first) + "-earlier", "aria-disabled"), "false")
        self.assertEqual(self.lists(last)["tiles"], tiles[1:] + [first])
        self.assertEqual(last["watch"]["ov-arrange-status"], "%s: position %d of %d in Tiles." % (name, n, n))
        # At the bottom, Move later says it does nothing (aria-disabled, so that the focus stays), and why.
        self.assertEqual(self.attr(last, line(first) + "-later", "aria-disabled"), "true")
        self.assertEqual(self.lists(again)["tiles"], self.lists(last)["tiles"])
        self.assertEqual(again["focus"], line(first) + "-later")
        self.assertEqual(again["watch"]["ov-arrange-status"], "%s is already last in Tiles." % name)
        self.assertEqual([s["focus"] for s in snaps[2:]], [line(first) + "-later"] * n)
        self.assertEqual(again["sent"], [])

    def test_move_earlier(self):
        right = self.expected()["right"]
        last, n = right[-1], len(right)
        name, where = self.label[last], "Details, right column"
        snaps = self.run_page([{"press": "ov-customize"}] + [press(last, "earlier")] * (n + 1))
        self.assertEqual(snaps[2]["watch"]["ov-arrange-status"],
                         "%s: position %d of %d in %s." % (name, n - 1, n, where))
        top = snaps[n]
        self.assertEqual(self.lists(top)["right"], [last] + right[:-1])
        self.assertEqual(top["watch"]["ov-arrange-status"], "%s: position 1 of %d in %s." % (name, n, where))
        self.assertEqual(snaps[n + 1]["watch"]["ov-arrange-status"], "%s is already first in %s." % (name, where))
        # The same words again are new text, so that a screen reader says them again.
        self.assertEqual(snaps[n + 2]["watch"]["ov-arrange-status"],
                         "%s is already first in %s. " % (name, where))
        self.assertEqual(self.lists(snaps[n + 2])["right"], [last] + right[:-1])
        self.assertEqual(snaps[n + 2]["focus"], line(last) + "-earlier")

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
        self.assertEqual(self.lists(shown), self.lists(opened))  # hiding moves nothing

    def test_to_the_other_column_and_back(self):
        """The section goes to the end of the other column; the focus, taken away with it by the browser, comes back
        to its button, which now names the column it came from."""
        expected = self.expected()
        left, right = expected["left"], expected["right"]
        attrs = [(line("disks") + "-column", "aria-label"), (line("cpu") + "-column", "aria-label")]
        opened, there, back = self.run_page([{"press": "ov-customize"}, press("disks", "column"),
                                             press("disks", "column")], attrs=attrs,
                                            watch=[line("disks") + "-column"])[1:]
        self.assertEqual(self.attr(opened, line("disks") + "-column", "aria-label"),
                         "Move Disk Info to the left column")
        self.assertEqual(opened["watch"][line("disks") + "-column"], "To the left")
        self.assertEqual(self.attr(opened, line("cpu") + "-column", "aria-label"), "Move CPU Info to the right column")
        self.assertEqual(self.lists(there)["left"], left + ["disks"])
        self.assertEqual(self.lists(there)["right"], without(right, "disks"))
        self.assertEqual(there["focus"], line("disks") + "-column")
        self.assertEqual(there["watch"][line("disks") + "-column"], "To the right")
        self.assertEqual(self.attr(there, line("disks") + "-column", "aria-label"),
                         "Move Disk Info to the right column")
        self.assertEqual(there["watch"]["ov-arrange-status"],
                         "Disk Info: position %d of %d in Details, left column." % (len(left) + 1, len(left) + 1))
        self.assertEqual(self.lists(back)["right"], without(right, "disks") + ["disks"])
        self.assertEqual(back["focus"], line("disks") + "-column")

    def test_done_sends_the_arrangement_and_loads_the_page_again(self):
        expected = self.expected()
        first = expected["tiles"][0]
        steps = [{"press": "ov-customize"}, press(first, "later"), press("ram", "hide"), press("disks", "column"),
                 {"press": "ov-arrange-done"}, {"ajax": {"arrangement": {}}}]
        snaps = self.run_page(steps)
        sent, saved = snaps[-2], snaps[-1]
        after = dict(expected, tiles=moved(expected["tiles"], first, 1), left=expected["left"] + ["disks"],
                     right=without(expected["right"], "disks"))
        body = {"order": flat(after), "hidden": ["ram"], "other_column": ["disks"]}
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
        expected = self.expected()
        first = expected["tiles"][0]
        steps = [{"press": "ov-customize"}, press(first, "later"), {"press": "ov-arrange-done"},
                 press(first, "later"), {"press": "ov-arrange-cancel"}, {"press": "ov-arrange-reset"},
                 {"press": "ov-customize"}, {"press": "ov-arrange-done"}]
        snaps = self.run_page(steps)
        self.assertEqual(len(snaps[-1]["sent"]), 1)
        self.assertEqual(self.lists(snaps[-1])["tiles"], moved(expected["tiles"], first, 1))
        self.assertIsNone(self.attr(snaps[-1], "ov-arrange", "hidden"))

    def test_a_failure_says_so_and_keeps_the_editor(self):
        expected = self.expected()
        first = expected["tiles"][0]
        steps = [{"press": "ov-customize"}, press(first, "later"), {"press": "ov-arrange-done"}, {"fail": True},
                 {"press": "ov-arrange-done"}]
        snaps = self.run_page(steps)
        failed = snaps[4]
        self.assertEqual(failed["watch"]["ov-arrange-message"], "Not saved: try again.")
        self.assertEqual(failed["watch"]["ov-arrange-status"], "Not saved: try again.")
        self.assertIsNone(self.attr(failed, "ov-arrange", "hidden"))
        self.assertEqual(self.attr(failed, "ov-arrange-done", "aria-disabled"), "false")
        self.assertEqual(self.lists(failed)["tiles"], moved(expected["tiles"], first, 1))
        self.assertEqual((failed["reloads"], failed["session"]), (0, {}))
        self.assertEqual(len(snaps[5]["sent"]), 2)  # Done again: a new try

    def test_logged_out_goes_to_the_logout_page(self):
        first = self.expected()["tiles"][0]
        snaps = self.run_page([{"press": "ov-customize"}, press(first, "later"), {"press": "ov-arrange-done"}],
                              status=401)
        self.assertEqual(self.attr(snaps[-1], "ov-customize", "data-test-left-for"), "/logout")
        self.assertEqual(snaps[-1]["reloads"], 0)

    def test_cancel_forgets_the_changes(self):
        expected = self.expected()
        steps = [{"press": "ov-customize"}, press(expected["tiles"][0], "later"), press("ram", "hide"),
                 press("disks", "column"), {"press": "ov-arrange-cancel"}, {"press": "ov-customize"}]
        snaps = self.run_page(steps, attrs=[(line("ram") + "-hide", "aria-pressed")])
        cancelled, reopened = snaps[-2], snaps[-1]
        self.assertEqual(self.attr(cancelled, "ov-arrange", "hidden"), "")
        self.assertEqual(self.attr(cancelled, "ov-customize", "aria-expanded"), "false")
        self.assertEqual(cancelled["focus"], "ov-customize")
        self.assertEqual(cancelled["watch"]["ov-arrange-status"], "Changes discarded.")
        self.assertEqual((cancelled["sent"], cancelled["reloads"]), ([], 0))
        self.assertEqual(self.lists(reopened), expected)
        self.assertEqual(self.attr(reopened, line("ram") + "-hide", "aria-pressed"), "false")

    def test_customize_again_closes_the_editor_as_cancel_does(self):
        opened, closed = self.run_page([{"press": "ov-customize"}, {"press": "ov-customize"}])[1:]
        self.assertEqual(self.attr(closed, "ov-arrange", "hidden"), "")
        self.assertEqual(closed["focus"], "ov-customize")
        self.assertEqual(closed["watch"]["ov-arrange-status"], "")  # nothing was changed: nothing to say

    def test_done_without_a_change_sends_nothing(self):
        first = self.expected()["tiles"][0]
        steps = [{"press": "ov-customize"}, press(first, "later"), press(first, "earlier"),
                 {"press": "ov-arrange-done"}]
        done = self.run_page(steps)[-1]
        self.assertEqual((done["sent"], done["reloads"]), ([], 0))
        self.assertEqual(self.attr(done, "ov-arrange", "hidden"), "")
        self.assertEqual(done["focus"], "ov-customize")
        self.assertEqual(done["watch"]["ov-arrange-status"], "Nothing changed.")

    def test_an_arranged_page_opens_as_arranged(self):
        """In the account's order: the blocks it lists first, in its order; then, in each zone, those it does not
        list (such as a block added to the list since), in the order of core/overview_blocks.py."""
        expected = self.expected(arrangement=ARRANGED)
        attrs = [(line(b) + "-hide", "aria-pressed") for b in ("ram", "tile-disk", "cpu")]
        opened, = self.run_page([{"press": "ov-customize"}], arrangement=ARRANGED, attrs=attrs)[1:]
        found = self.lists(opened)
        self.assertEqual(found, expected)
        for zone, listed in (("tiles", ["tile-ram", "tile-cpu", "tile-net", "tile-disk"]),
                             ("row", ["services", "bandwidth"]), ("left", ["disks", "cpu", "ram"]),
                             ("right", ["network"])):
            with self.subTest(zone):
                self.assertEqual(found[zone][:len(listed)], listed)
                self.assertEqual(found[zone][len(listed):], [b["id"] for b in overview_blocks.zone(zone)
                                                             if b["id"] not in ARRANGED["order"]
                                                             and b["id"] in flat(found)])
        self.assertIn("processes", found["left"][3:])
        self.assertIn("signins", found["right"][1:])
        self.assertEqual([self.attr(opened, line(b) + "-hide", "aria-pressed") for b in ("ram", "tile-disk", "cpu")],
                         ["true", "true", "false"])

    def test_reset_to_default_then_done(self):
        """Reset puts the default arrangement in the editor only: that of the page without an arrangement. Done then
        asks the panel for the default one."""
        plain = self.expected()
        steps = [{"press": "ov-customize"}, {"press": "ov-arrange-reset"}, {"press": "ov-arrange-done"},
                 {"ajax": {"arrangement": None}}]
        opened, reset, sent, saved = self.run_page(steps, arrangement=ARRANGED,
                                                   attrs=[(line("ram") + "-hide", "aria-pressed")])[1:]
        self.assertNotEqual(self.lists(opened), plain)
        self.assertEqual(self.lists(reset), plain)
        self.assertEqual(self.lists(reset), lists_of(self.data, default=True))
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
        self.assertEqual(self.lists(reopened)["left"][:3], ["disks", "cpu", "ram"])
        self.assertEqual(reopened["sent"], [])

    def test_after_done_the_page_gives_the_focus_back_and_says_it_is_saved(self):
        loaded, later = self.run_page([{"timer": True}], session={"overview.arranged": "1", "other": "x"})
        self.assertEqual(loaded["focus"], "ov-customize")
        self.assertEqual(loaded["session"], {"other": "x"})  # once only
        self.assertEqual(later["watch"]["ov-arrange-status"], "Your arrangement of Overview is saved.")
        plain, = self.run_page([], session={})
        self.assertIsNone(plain["focus"])

    def test_blocked_storage_changes_nothing_else(self):
        first = self.expected()["tiles"][0]
        steps = [{"press": "ov-customize"}, press(first, "later"), {"press": "ov-arrange-done"},
                 {"ajax": {"arrangement": {}}}]
        snaps = self.run_page(steps, session="throws")
        self.assertEqual(snaps[-1]["reloads"], 1)
        self.assertIsNone(snaps[0]["focus"])

    def test_another_account_arranges_its_own_blocks_only(self):
        """The admin's blocks are not listed for another account (the pills, the Network tile and graph, Panel
        sign-ins); its own are, Top processes included."""
        expected = self.expected("friend")
        admin = flat(self.expected("tester"))
        self.assertIsNone(expected["glance"])
        for block in ("system-status", "tile-net", "bandwidth", "signins"):
            self.assertIn(block, admin)
            self.assertNotIn(block, flat(expected))
        self.assertIn("processes", expected["left"])
        steps = [{"press": "ov-customize"}, press("tile-disk", "earlier"), {"press": "ov-arrange-done"}]
        opened, there, sent = self.run_page(steps, user="friend")[1:]
        self.assertEqual(self.lists(opened), expected)
        after = dict(expected, tiles=moved(expected["tiles"], "tile-disk", -1))
        self.assertEqual(json.loads(sent["sent"][0]["data"]), {"order": flat(after), "hidden": [], "other_column": []})

    def test_a_block_added_to_the_list_is_in_the_editor(self):
        """Nothing in arrange.js names a block: a new one is listed, moved and sent like the others, and its label
        goes in as text."""
        loader = swizzin.app.jinja_env.loader
        with mock.patch.object(overview_blocks, "BLOCKS", overview_blocks.BLOCKS + [EXTRA]), \
                mock.patch.object(swizzin.app.jinja_env, "loader",
                                  jinja2.ChoiceLoader([jinja2.DictLoader({EXTRA["template"]: EXTRA_HTML}), loader])):
            expected = self.expected()
            steps = [{"press": "ov-customize"}, press("extra", "column"), press("extra", "earlier"),
                     {"press": "ov-arrange-done"}]
            opened, there, earlier, sent = self.run_page(steps, watch=[line("extra")])[1:]
        left = expected["left"]
        self.assertEqual(expected["right"][-1], "extra")  # not in an arrangement: at the end of its zone
        self.assertEqual(self.lists(opened), expected)
        self.assertEqual(opened["watch"][line("extra")], "Extra <b>block</b>Hidden↑↓HideTo the left")
        self.assertEqual(self.lists(earlier)["left"], left[:-1] + ["extra", left[-1]])
        self.assertEqual(earlier["watch"]["ov-arrange-status"],
                         "Extra <b>block</b>: position %d of %d in Details, left column." % (len(left), len(left) + 1))
        after = dict(expected, left=left[:-1] + ["extra", left[-1]], right=without(expected["right"], "extra"))
        data = json.loads(sent["sent"][0]["data"])
        self.assertEqual((data["order"], data["hidden"], data["other_column"]), (flat(after), [], ["extra"]))


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
