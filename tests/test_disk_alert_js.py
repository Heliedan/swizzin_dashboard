"""The disk banner, the disk bars and the /stats/disk poll, run for real in Node.js.

static/js/live_stats.js and the diskusage() poll of the dashboard run on the HTML the panel renders, in a small
fake DOM (tests/fake_dom.js): no npm package to install. Skipped when node is not installed, except with
REQUIRE_NODE=1, set by the automatic tests (.github/workflows/tests.yml): there, a missing node fails them.

Run from the repository root: python tests/test_disk_alert_js.py
(uses a throwaway htpasswd file and fake disks: nothing on the machine is touched)."""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
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

import swizzin  # noqa: E402

NODE = shutil.which("node")
NODE_REQUIRED = os.environ.get("REQUIRE_NODE") == "1"  # the automatic tests: never skipped in silence
FAKE_DOM = os.path.join(ROOT, "tests", "fake_dom.js")
LIVE_STATS = os.path.join(ROOT, "static", "js", "live_stats.js")
TRAP = "/mnt/<img src=x onerror=alert(1)> it's \"mine\" & yours"
MOUNTS = ["/", "/srv/media", TRAP]
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


def disk(pct, free="7.66 GiB", alert=None):
    """One disk of the "live" event, as core/custom/live_stats.py sends it."""
    return {"disktotal": "100.00 GiB", "diskused": "x GiB", "diskfree": free, "perutil": pct,
            "read": "0.00 B/s", "write": "0.00 B/s", "alert": alert}


def polled(pct, free="7.66 GiB", alert=None):
    """One disk of /stats/disk."""
    return {"disktotal": "100.00 GiB", "diskused": "x GiB", "diskfree": free, "perutil": pct, "alert": alert}


ALERTS = {"/": disk("50.00"), "/srv/media": disk("92.34", "7.66 GiB", "warning"),
          TRAP: disk("96.10", "3.90 GiB", "critical")}
CALM = {"/": disk("50.00"), "/srv/media": disk("89.99", "10.01 GiB"), TRAP: disk("99.00", "1.00 GiB")}


class Tree(HTMLParser):
    """The rendered page as {tag, attrs, children} / {text}, the way a browser reads it (entities decoded)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "#document", "attrs": {}, "children": []}
        self.open = [self.root]

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": {k: v or "" for k, v in attrs}, "children": []}
        self.open[-1]["children"].append(node)
        if tag not in VOID:
            self.open.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.open) - 1, 0, -1):
            if self.open[i]["tag"] == tag:
                del self.open[i:]
                return

    def handle_data(self, data):
        self.open[-1]["children"].append({"text": data})


def dashboard(user, mounts, quota):
    real_isfile = os.path.isfile
    client = swizzin.app.test_client()
    with mock.patch.object(swizzin, "get_mounts", return_value=mounts), \
            mock.patch.object(swizzin, "generate_page_list", return_value=[]), \
            mock.patch.object(swizzin.os.path, "isfile",
                              side_effect=lambda p: quota if p == "/install/.quota.lock" else real_isfile(p)):
        r = client.get("/", headers={"Authorization": "Basic " + base64.b64encode(f"{user}:{user}".encode()).decode()})
    assert r.status_code == 200, r.status_code
    return r.get_data(as_text=True)


@unittest.skipUnless(NODE or NODE_REQUIRED, "node is not installed")
class NodeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not NODE:
            raise RuntimeError("node is not installed, and REQUIRE_NODE=1: these tests must run here")

    def run_page(self, steps, mounts=MOUNTS, user="tester", quota=False, socketio=True, poll=False):
        """Loads the dashboard in the fake DOM, plays the steps; returns a snapshot after loading and after each."""
        page = dashboard(user, mounts, quota)
        tree = Tree()
        tree.feed(page)
        source = None
        if poll:
            found = re.search(r"\(function diskusage\(\) \{.*?\n\}\)\(\);", page, re.S)
            self.assertIsNotNone(found, "diskusage() not found in the dashboard")
            source = found.group(0)
        payload = {"page": tree.root, "live_stats": LIVE_STATS, "poll": source, "socketio": socketio, "steps": steps}
        r = subprocess.run([NODE, FAKE_DOM], input=json.dumps(payload), capture_output=True, text=True, timeout=60)
        if r.returncode:
            self.fail(r.stderr)
        return json.loads(r.stdout)


class Banner(NodeTest):
    def test_one_line_per_disk_in_alert_the_fullest_first(self):
        before, after = self.run_page([{"live": ALERTS}])
        self.assertEqual(before["banner"]["lines"], [])
        self.assertEqual([line["text"] for line in after["banner"]["lines"]],
                         [f"Disk almost full: {TRAP} is 96% used (3.90 GiB free)",
                          "Disk almost full: /srv/media is 92% used (7.66 GiB free)"])
        self.assertEqual(after["bannerAdded"], [[line["cls"], line["text"]] for line in after["banner"]["lines"]])

    def test_critical_is_red_and_warning_orange(self):
        lines = self.run_page([{"live": ALERTS}])[1]["banner"]["lines"]
        self.assertEqual([line["cls"].split() for line in lines],
                         [["alert", "mt-3", "mb-0", "text-break", "alert-danger"],
                          ["alert", "mt-3", "mb-0", "text-break", "alert-warning"]])

    def test_the_banner_empties_when_the_disks_go_back_below(self):
        snaps = self.run_page([{"live": ALERTS}, {"live": CALM}, {"live": ALERTS}])
        self.assertEqual(len(snaps[1]["banner"]["lines"]), 2)
        self.assertEqual(snaps[2]["banner"]["lines"], [])
        self.assertEqual(len(snaps[3]["banner"]["lines"]), 2)

    def test_the_percent_is_truncated_not_rounded(self):
        snaps = self.run_page([{"live": {"/srv/media": disk("94.60", "5.40 GiB", "warning")}}])
        self.assertEqual(snaps[1]["banner"]["lines"][0]["text"],
                         "Disk almost full: /srv/media is 94% used (5.40 GiB free)")  # 95 % would be the red threshold

    def test_an_unavailable_disk_leaves_the_banner(self):
        snaps = self.run_page([
            {"live": ALERTS},
            {"live": dict(ALERTS, **{TRAP: {"unavailable": True}})},  # unreadable: what the server sends
            {"live": dict(ALERTS, **{TRAP: {"unavailable": True, "alert": "critical", "perutil": "96.10",
                                            "diskfree": "3.90 GiB"}})},  # a level next to "unavailable" is ignored
            {"live": {"/": disk("50.00"), "/srv/media": disk("92.34", "7.66 GiB", "warning")}},  # unmounted
        ])
        for snap in snaps[2:]:
            self.assertEqual([line["text"] for line in snap["banner"]["lines"]],
                             ["Disk almost full: /srv/media is 92% used (7.66 GiB free)"])
            self.assertEqual(snap["disks"][TRAP]["badge"], ["disk-unavailable"])

    def test_a_trapped_mount_path_stays_plain_text(self):
        """fake_dom.js throws on any HTML insertion: the path can only go in as text."""
        line = self.run_page([{"live": ALERTS}])[1]["banner"]["lines"][0]
        self.assertIn(TRAP, line["text"])
        self.assertEqual(line["elements"], 0)

    def test_a_disk_outside_the_card_raises_nothing(self):
        snaps = self.run_page([{"live": {"/": disk("50.00"), "/not/in/the/card": disk("99.00", "1 GiB", "critical")}}])
        self.assertEqual(snaps[1]["banner"]["lines"], [])

    def test_lines_are_written_only_when_their_text_changes(self):
        moved = dict(ALERTS, **{TRAP: disk("96.11", "3.89 GiB", "critical")})
        snaps = self.run_page([{"live": ALERTS}, {"live": ALERTS}, {"live": moved}])
        self.assertEqual(snaps[2]["bannerChanges"], 0)  # same text: nothing written
        self.assertEqual(snaps[3]["bannerChanges"], 1)  # only the text of the line that changed
        self.assertEqual(snaps[3]["banner"]["lines"][0]["text"], f"Disk almost full: {TRAP} is 96% used (3.89 GiB free)")

    def test_only_a_hidden_summary_is_a_live_region(self):
        """During a download, the free space of a line changes nearly every second: as a live region, the banner
        was read out again and again. Its lines are no longer live; screen readers read a hidden summary."""
        for snap in self.run_page([{"live": ALERTS}, {"live": CALM}]):
            self.assertEqual((snap["banner"]["role"], snap["banner"]["live"], snap["banner"]["hidden"]),
                             (None, None, False))
            self.assertEqual((snap["summary"]["role"], snap["summary"]["live"], snap["summary"]["cls"]),
                             ("status", "polite", "sr-only"))

    def test_the_summary_names_the_disks_in_alert_and_their_level_only(self):
        before, after = self.run_page([{"live": ALERTS}])
        self.assertEqual(before["summary"]["text"], "")
        self.assertEqual(after["summary"]["text"], f"Disk almost full, critical: {TRAP}; warning: /srv/media")

    def test_the_summary_changes_only_when_a_disk_enters_leaves_or_changes_level(self):
        red = {"/": disk("50.00"), "/srv/media": disk("96.50", "3.50 GiB", "critical"),
               TRAP: disk("96.10", "3.90 GiB", "critical")}
        snaps = self.run_page([
            {"live": ALERTS},  # two disks enter the alert
            {"live": dict(ALERTS, **{TRAP: disk("96.11", "3.89 GiB", "critical")})},  # only the free space moves
            {"live": red},  # /srv/media goes from orange to red
            {"live": dict(red, **{TRAP: disk("97.00", "3.00 GiB", "critical")})},  # the fullest one is another
            {"live": dict(ALERTS, **{"/srv/media": disk("80.00", "20.00 GiB")})},  # /srv/media leaves the alert
            {"live": CALM},  # the last one leaves
            {"live": CALM},
        ])
        self.assertEqual([snap["summaryChanges"] for snap in snaps], [0, 1, 0, 1, 0, 1, 1, 0])
        self.assertEqual([snap["summary"]["text"] for snap in snaps],
                         ["", f"Disk almost full, critical: {TRAP}; warning: /srv/media",
                          f"Disk almost full, critical: {TRAP}; warning: /srv/media",
                          f"Disk almost full, critical: {TRAP}, /srv/media",
                          f"Disk almost full, critical: {TRAP}, /srv/media",
                          f"Disk almost full, critical: {TRAP}",
                          "No disk is almost full anymore.", "No disk is almost full anymore."])
        # The visible lines still follow every reading.
        self.assertEqual(snaps[2]["banner"]["lines"][0]["text"], f"Disk almost full: {TRAP} is 96% used (3.89 GiB free)")
        self.assertEqual([line["text"] for line in snaps[4]["banner"]["lines"]],
                         [f"Disk almost full: {TRAP} is 97% used (3.00 GiB free)",
                          "Disk almost full: /srv/media is 96% used (3.50 GiB free)"])

    def test_a_disk_in_alert_that_becomes_unavailable_is_not_said_to_be_full_no_more(self):
        """Unreadable (a dead network share...), it may still be full: the summary does not say it no longer is."""
        calm = {"/": disk("50.00"), "/srv/media": disk("50.00")}
        snaps = self.run_page([
            {"live": dict(calm, **{TRAP: {"unavailable": True}})},  # no alert yet: nothing to say
            {"live": dict(calm, **{TRAP: disk("96.10", "3.90 GiB", "critical")})},
            {"live": dict(calm, **{TRAP: {"unavailable": True}})},  # the last disk in alert
            {"live": dict(calm, **{TRAP: {"unavailable": True}})},
            {"live": dict(calm, **{TRAP: disk("50.00")})},  # readable again, below the thresholds
        ])
        self.assertEqual([snap["summary"]["text"] for snap in snaps],
                         ["", "", f"Disk almost full, critical: {TRAP}",
                          f"No readable disk is almost full anymore. Unavailable: {TRAP}.",
                          f"No readable disk is almost full anymore. Unavailable: {TRAP}.",
                          "No disk is almost full anymore."])
        self.assertEqual([snap["summaryChanges"] for snap in snaps], [0, 0, 1, 1, 0, 1])
        self.assertEqual(snaps[3]["banner"]["lines"], [])  # the visible banner leaves it, as before

    def test_an_unavailable_disk_outside_the_card_is_not_named(self):
        """A disk plugged in after the page was loaded is not in the Disk Info card: the summary does not name it."""
        calm = {"/": disk("50.00"), "/srv/media": disk("50.00"), TRAP: disk("50.00")}
        snaps = self.run_page([
            {"live": dict(calm, **{TRAP: disk("96.10", "3.90 GiB", "critical")})},
            {"live": dict(calm, **{"/not/in/the/card": {"unavailable": True}})},
        ])
        self.assertEqual([snap["summary"]["text"] for snap in snaps],
                         ["", f"Disk almost full, critical: {TRAP}", "No disk is almost full anymore."])

    def test_no_disk_info_card_no_banner(self):
        snaps = self.run_page([{"live": ALERTS}], user="friend", quota=True)
        self.assertIsNone(snaps[1]["banner"])
        self.assertIsNone(snaps[1]["summary"])
        self.assertEqual(snaps[1]["disks"], {})


class Bars(NodeTest):
    def test_bars_and_badges_follow_the_alert_levels(self):
        snaps = self.run_page([{"live": ALERTS}, {"live": CALM}])
        colours = [{m: (d["bar"], d["badge"]) for m, d in snap["disks"].items()} for snap in snaps[1:]]
        self.assertEqual(colours[0], {"/": (["bg-success"], ["bg-success"]),
                                      "/srv/media": (["bg-warning"], ["bg-warning"]),  # 92 %: 75/90 said red
                                      TRAP: (["bg-danger"], ["bg-danger"])})
        self.assertEqual(colours[1], {"/": (["bg-success"], ["bg-success"]),
                                      "/srv/media": (["bg-success"], ["bg-success"]),  # 89.99 %: 75/90 said orange
                                      TRAP: (["bg-success"], ["bg-success"])})  # 99 %, alert turned off: green
        self.assertEqual(snaps[1]["disks"]["/srv/media"]["width"], "92.34%")


class Poll(NodeTest):
    def test_the_poll_fills_the_card_until_the_first_live_reading(self):
        snaps = self.run_page([
            {"ajax": {"/srv/media": polled("92.34", "7.66 GiB", "warning")}},
            {"timer": True},
            {"live": ALERTS},
            {"ajax": {"/srv/media": polled("10.00", "90.00 GiB")}},  # answer that comes after the live reading
            {"timer": True},
        ], poll=True)
        loaded, answered, again, live, late, after = snaps
        self.assertEqual((loaded["requests"], loaded["liveMark"]), (1, False))
        self.assertEqual(answered["disks"]["/srv/media"]["free"], "7.66 GiB")
        self.assertEqual(answered["disks"]["/srv/media"]["bar"], ["bg-warning"])  # same colours as the live card
        self.assertEqual(answered["disks"]["/srv/media"]["badge"], ["bg-warning"])
        self.assertEqual(answered["disks"]["/srv/media"]["width"], "92%")
        self.assertEqual((answered["timers"], again["requests"]), (1, 2))  # no live reading yet: it polls again
        self.assertTrue(live["liveMark"])
        self.assertEqual(late["disks"]["/srv/media"]["free"], "7.66 GiB")  # the late answer paints nothing
        self.assertEqual(late["timers"], 0)  # and the poll stops
        self.assertEqual(after["requests"], 2)

    def test_the_poll_stops_at_its_next_turn_once_live(self):
        snaps = self.run_page([{"ajax": {"/": polled("50.00")}}, {"live": ALERTS}, {"timer": True}], poll=True)
        self.assertEqual((snaps[-1]["requests"], snaps[-1]["timers"]), (1, 0))

    def test_a_mount_without_dev_device_keeps_its_numbers_once_live(self):
        """ZFS, NFS...: filled by /stats/disk when the page loads, then by every live reading (they were marked
        UNAVAILABLE by the first one). They have no read and write speeds."""
        tank = dict(disk("95.00", "51.20 GiB", "critical"), read=None, write=None)  # what the server sends
        snaps = self.run_page([{"ajax": {"/srv/media": polled("95.00", "51.20 GiB", "critical")}},
                               {"live": {"/": disk("50.00"), "/srv/media": tank, TRAP: disk("50.00")}},
                               {"live": {"/": disk("50.00"), "/srv/media": tank, TRAP: disk("50.00")}}], poll=True)
        for snap in snaps[1:]:
            shown = snap["disks"]["/srv/media"]
            self.assertEqual((shown["free"], shown["percent"], shown["bar"], shown["badge"]),
                             ("51.20 GiB", "95.00", ["bg-danger"], ["bg-danger"]))
        self.assertEqual((snaps[2]["disks"]["/srv/media"]["read"], snaps[2]["disks"]["/srv/media"]["write"]),
                         ("—", "—"))
        self.assertEqual([line["text"] for line in snaps[3]["banner"]["lines"]],
                         ["Disk almost full: /srv/media is 95% used (51.20 GiB free)"])

    def test_without_socketio_the_poll_keeps_filling_the_card(self):
        snaps = self.run_page([
            {"ajax": {"/srv/media": polled("50.00", "50.00 GiB")}},
            {"timer": True},
            {"ajax": {"/srv/media": polled("95.00", "5.00 GiB", "critical")}},
            {"timer": True},
        ], socketio=False, poll=True)
        self.assertEqual(snaps[1]["disks"]["/srv/media"]["free"], "50.00 GiB")
        self.assertEqual(snaps[3]["disks"]["/srv/media"]["free"], "5.00 GiB")
        self.assertEqual(snaps[3]["disks"]["/srv/media"]["bar"], ["bg-danger"])
        self.assertEqual(snaps[3]["disks"]["/srv/media"]["badge"], ["bg-danger"])
        self.assertEqual(snaps[3]["disks"]["/srv/media"]["width"], "95%")
        self.assertEqual((snaps[4]["requests"], snaps[4]["liveMark"]), (3, False))


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
