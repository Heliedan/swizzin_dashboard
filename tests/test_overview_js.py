"""The Overview layout of the dashboard and the layout menu, run for real in Node.js.

The page the panel renders runs in the small fake DOM of tests/fake_dom.js (no npm package to install), with the
scripts it loads: the tiles (static/js/live_stats.js), their small graphs (static/js/live_graphs.js), the sections and
the network totals (static/js/overview.js), and the layout menu (static/js/swizzin.js). Skipped when node is not
installed, except with REQUIRE_NODE=1, set by the automatic tests (.github/workflows/tests.yml): there, a missing node
fails them.

Run from the repository root: python tests/test_overview_js.py
(uses a throwaway htpasswd file, fake disks and a fake vnstat: nothing on the machine is touched)."""
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# The page, the fake DOM and its runner are those of the disk banner's tests. Importing them points the panel to a
# throwaway htpasswd file (the admin "tester", and "friend") and writes swizzin.cfg, removed at the end.
from test_disk_alert_js import FAKE_DOM, NODE, NODE_REQUIRED, ROOT, Tree, dashboard, swizzin  # noqa: E402
from test_live_graphs_js import T0, find, line, speed  # noqa: E402
from fake_vnstat import EMPTY, FakeVnstat, vnstat_json  # noqa: E402
from core import overview_blocks  # noqa: E402

AUTH = {"Authorization": "Basic " + base64.b64encode(b"tester:tester").decode()}
STORE_KEY = "overview.open"  # localStorage: the sections opened, among those closed by default
CLOSED_KEY = "overview.closed"  # localStorage: the sections closed, among those open by default
OPEN_BY_DEFAULT = {block["id"] for block in overview_blocks.BLOCKS if block.get("open")}
TILES = ("cpu", "ram", "disk", "net")
SPARKS = ("cpu-spark", "ram-spark", "disk-spark", "net-spark")
OVERVIEW_SCRIPTS = ("live_graphs.js", "live_stats.js", "overview.js")

# Every account sees the Overview layout in this file, from its own throwaway file of choices.
_tmp = tempfile.mkdtemp()
swizzin.layout_choices.path = os.path.join(_tmp, "layouts.json")
swizzin.layout_choices.default = "overview"


def scripts_of(tree, names):
    """The paths of the scripts named `names` that the page loads (<script src=".../static/js/...">), in its order."""
    out = []

    def walk(node):
        src = node.get("attrs", {}).get("src", "") if node.get("tag") == "script" else ""
        if "/static/js/" in src and os.path.basename(src) in names:
            out.append(os.path.join(ROOT, "static", "js", os.path.basename(src)))
        for child in node.get("children", []):
            walk(child)
    walk(tree)
    return out


def disk(pct, alert=None, free="52.07 GiB", total="98.30 GiB"):
    """One disk of the "live" event, as core/custom/live_stats.py sends it."""
    return {"disktotal": total, "diskused": "x GiB", "diskfree": free, "perutil": pct, "read": "0.00 B/s",
            "write": "0.00 B/s", "alert": alert}


def live(cpu="10.0", ram="25.00", disks=None, cores=8):
    """A "live" event: this CPU used, this RAM used (texts, as sent), these disks (by default / at 41.91 %)."""
    return {"cpu": {"used": cpu, "idle": "%.1f" % (100 - float(cpu)), "cores": cores, "model": "Demo CPU", "freq": "",
                    "user": cpu, "system": "0.0", "iowait": "0.0", "steal": "0.0", "percore": [cpu]},
            "ram": {"ramused": "4.00 GiB", "ramfree": "12.00 GiB", "ramtotal": "16.00 GiB", "perutil": ram},
            "disks": {"/": disk("41.91")} if disks is None else disks}


def at(second, event, data=None):
    step = {"at": T0 + round(second * 1000), "event": event}
    if data is not None:
        step["data"] = data
    return step


def tile_watch():
    return [f"tile-{t}-{part}" for t in TILES for part in ("value", "badge")] + \
        ["tile-cpu-cores", "tile-ram-info", "tile-disk-info", "tile-net-interface", "net-spark-rx-now",
         "net-spark-tx-now"]


def tile(snap, name):
    """(value, badge, data-level) of a tile."""
    return (snap["watch"].get(f"tile-{name}-value"), snap["watch"][f"tile-{name}-badge"],
            snap["attrs"][f"tile-{name} data-level"])


@unittest.skipUnless(NODE or NODE_REQUIRED, "node is not installed")
class OverviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not NODE:
            raise RuntimeError("node is not installed, and REQUIRE_NODE=1: these tests must run here")

    def run_page(self, steps, user="tester", mounts=("/", "/mnt/data"), quota=False, scripts=OVERVIEW_SCRIPTS,
                 storage=None, watch=(), attrs=(), poll=None, layout=None):
        """Loads the dashboard in the fake DOM with these scripts of the page, plays the steps; returns a snapshot after
        loading and after each. poll: the name of a poll of the page to run too (vnstat_top10). layout: the account's
        choice (none: the Overview, the default of this file)."""
        with mock.patch.object(swizzin.layout_choices, "get", return_value=layout or "overview"):
            page = dashboard(user, list(mounts), quota)
        tree = Tree()
        tree.feed(page)
        source = None
        if poll:
            found = re.search(r"\(function " + poll + r"\(\) \{.*?\n\}\)\(\);", page, re.S)
            self.assertIsNotNone(found, f"{poll}() not found in the dashboard")
            source = found.group(0)
        payload = {"page": tree.root, "scripts": scripts_of(tree.root, scripts), "poll": source, "socketio": True,
                   "storage": storage, "watch": list(watch), "attrs": [list(a) for a in attrs], "steps": steps}
        r = subprocess.run([NODE, FAKE_DOM], input=json.dumps(payload), capture_output=True, text=True, timeout=60)
        if r.returncode:
            self.fail(r.stderr)
        return json.loads(r.stdout)

    def run_tiles(self, steps, **kw):
        return self.run_page(steps, watch=tile_watch(), attrs=[(f"tile-{t}", "data-level") for t in TILES], **kw)


class Tiles(OverviewTest):
    """The four tiles: value, line of detail, level in words in the badge, and the level for the colour."""

    def test_empty_until_the_first_reading(self):
        loaded = self.run_tiles([])[0]
        for name in TILES:
            self.assertEqual(tile(loaded, name)[1:], ("--", None))
        self.assertEqual(loaded["watch"]["tile-cpu-value"], "--")

    def test_the_scripts_of_the_page(self):
        """The tiles' graphs and the sections need nothing else: the scripts of Classic, then overview.js."""
        with mock.patch.object(swizzin.layout_choices, "get", return_value="overview"):
            tree = Tree()
            tree.feed(dashboard("tester", ["/"], False))
        names = [os.path.basename(p) for p in scripts_of(tree.root, OVERVIEW_SCRIPTS + ("swizzin.js",))]
        self.assertEqual(names, ["swizzin.js", "live_graphs.js", "live_stats.js", "overview.js"])

    def test_cpu_and_memory_use_the_thresholds_of_the_cards(self):
        """Above 75 %: Watch; above 90 %: Critical, as the badges and bars of the CPU Info and RAM Info cards."""
        cases = [("0.0", "OK", "ok"), ("75.0", "OK", "ok"), ("75.1", "Watch", "warning"), ("90.0", "Watch", "warning"),
                 ("90.1", "Critical", "critical"), ("100.0", "Critical", "critical"), ("12.5", "OK", "ok")]
        snaps = self.run_tiles([at(i, "live", live(cpu=v, ram=v)) for i, (v, _, _) in enumerate(cases)])
        for (v, badge, lvl), snap in zip(cases, snaps[1:]):
            with self.subTest(v):
                self.assertEqual(tile(snap, "cpu"), (v + "%", badge, lvl))
                self.assertEqual(tile(snap, "ram"), (v + "%", badge, lvl))

    def test_the_cards_and_the_tiles_agree(self):
        """The CPU tile and the CPU Info card's badge take their level from the same reading."""
        for v, card in (("50.0", "bg-success"), ("80.0", "bg-warning"), ("95.0", "bg-danger")):
            with self.subTest(v):
                snap = self.run_page([at(0, "live", live(cpu=v))], attrs=[("cpuindicator", "class")])[1]
                self.assertIn(card, snap["attrs"]["cpuindicator class"].split())

    def test_lines_of_detail(self):
        snap = self.run_tiles([at(0, "live", live(cpu="7.25", ram="59.62", cores=6))])[1]
        self.assertEqual(snap["watch"]["tile-cpu-value"], "7.3%")  # one decimal, like the memory's
        self.assertEqual(snap["watch"]["tile-cpu-cores"], "6")
        self.assertEqual(snap["watch"]["tile-ram-value"], "59.6%")
        self.assertEqual(snap["watch"]["tile-ram-info"], "4.00 GiB of 16.00 GiB")
        self.assertEqual(snap["watch"]["tile-disk-info"], "52.07 GiB free of 98.30 GiB")

    def test_the_disk_tile_follows_the_alert_of_the_banner(self):
        """DISK_WARN_PERCENT and DISK_CRIT_PERCENT, computed by the server: the level the banner shows too. The
        percentage is truncated as in the banner: "95%" never shows below the threshold of 95."""
        cases = [(disk("41.91"), ("41%", "OK", "ok")),
                 (disk("89.99"), ("89%", "OK", "ok")),
                 (disk("92.34", "warning"), ("92%", "Watch", "warning")),
                 (disk("94.99", "warning"), ("94%", "Watch", "warning")),
                 (disk("97.00", "critical"), ("97%", "Critical", "critical")),
                 ({"unavailable": True}, ("--", "Unavailable", "unavailable")),
                 (disk("50.00"), ("50%", "OK", "ok"))]
        snaps = self.run_tiles([at(i, "live", live(disks={"/": d, "/mnt/data": disk("10.00")}))
                                for i, (d, _) in enumerate(cases)])
        for (d, expected), snap in zip(cases, snaps[1:]):
            with self.subTest(d):
                self.assertEqual(tile(snap, "disk"), expected)
        self.assertEqual(snaps[6]["watch"]["tile-disk-info"], "Unavailable")

    def test_a_root_disk_missing_from_the_reading_is_unavailable(self):
        """Unmounted since the page loaded: as in the Disk Info card, not its last numbers."""
        snaps = self.run_tiles([at(0, "live", live()), at(1, "live", live(disks={"/mnt/data": disk("10.00")}))])
        self.assertEqual(tile(snaps[2], "disk"), ("--", "Unavailable", "unavailable"))

    def test_the_network_tile(self):
        snaps = self.run_tiles([at(0, "speed", speed("1.18 MiB/s", "512.00 KiB/s"))])
        self.assertEqual(tile(snaps[0], "net")[1:], ("--", None))
        self.assertEqual(tile(snaps[1], "net")[1:], ("Live", "live"))
        ids = ("net-spark-rx-now", "net-spark-tx-now", "tile-net-interface")
        self.assertEqual({k: snaps[1]["watch"][k] for k in ids},
                         {"net-spark-rx-now": "1.18 MiB/s", "net-spark-tx-now": "512.00 KiB/s",
                          "tile-net-interface": "eth0"})

    def test_another_user_has_no_network_tile_and_no_disk_tile_with_a_quota(self):
        snap = self.run_page([at(0, "live", live()), at(1, "speed", speed("1.00 MiB/s", "1.00 MiB/s"))],
                             user="friend", quota=True, watch=["tile-cpu-value"])[2]
        self.assertEqual(set(snap["graphs"]), {"cpu-graph", "cpu-spark", "ram-spark"})
        self.assertEqual(snap["watch"]["tile-cpu-value"], "10.0%")

    def test_no_root_mount_no_disk_tile(self):
        snap = self.run_page([at(0, "live", live(disks={"/srv": disk("10.00")}))], mounts=("/srv",))[1]
        self.assertNotIn("disk-spark", snap["graphs"])


class Graphs(OverviewTest):
    """The small graphs of the tiles: the last 5 minutes, drawn like the cards' graphs (static/js/live_graphs.js)."""

    def readings(self, seconds, disks=lambda s: {"/": disk("41.91")}):
        return {"batch": [step for s in seconds for step in (
            at(s, "live", live(cpu="%.1f" % (s % 100), ram="50.00", disks=disks(s))),
            at(s, "speed", speed("1.00 MiB/s", "512.00 KiB/s")))]}

    def test_each_tile_has_its_last_5_minutes(self):
        snap = self.run_page([self.readings(range(310))])[1]
        self.assertEqual(set(snap["graphs"]), {"cpu-graph", "net-graph"} | set(SPARKS))
        for graph, key in (("cpu-spark", "used"), ("ram-spark", "used"), ("disk-spark", "used"), ("net-spark", "rx"),
                           ("net-spark", "tx"), ("cpu-graph", "used"), ("net-graph", "rx")):
            with self.subTest(graph=graph, key=key):
                self.assertEqual([len(sub) for sub in line(snap, graph, key)], [301])
        # The CPU from 0 to 99 %: the tile's scale is 0 to 100 % too, the same points as the CPU Info card's graph.
        # The memory at 50 %: the middle of its scale (45 to 55 %). / at 41.91 %, of 0 to 100 %.
        self.assertEqual(line(snap, "cpu-spark", "used"), line(snap, "cpu-graph", "used"))
        self.assertEqual({y for x, y in line(snap, "ram-spark", "used")[0]}, {50.0})
        self.assertEqual({y for x, y in line(snap, "disk-spark", "used")[0]}, {58.1})
        self.assertEqual(line(snap, "net-spark", "rx"), line(snap, "net-graph", "rx"))

    def test_an_unavailable_root_disk_leaves_a_hole(self):
        snap = self.run_page([self.readings(range(10), disks=lambda s: {"/": {"unavailable": True}} if s == 5
                                            else {"/": disk("41.91")})])[1]
        self.assertEqual([len(sub) for sub in line(snap, "disk-spark", "used")], [5, 4])

    def test_a_disconnection_cuts_them(self):
        steps = [{"batch": [self.readings(range(5)), at(4.5, "disconnect"), self.readings(range(5, 10))]}]
        snap = self.run_page(steps)[1]
        for graph in SPARKS:
            with self.subTest(graph):
                key = "rx" if graph == "net-spark" else "used"
                self.assertEqual([len(sub) for sub in line(snap, graph, key)], [5, 5])

    def test_the_graphs_are_labelled(self):
        """For screen readers: each small graph is an image with its label (the values are in the tile's text)."""
        with mock.patch.object(swizzin.layout_choices, "get", return_value="overview"):
            page = dashboard("tester", ["/"], False)
        for graph, label in (("cpu-spark", "CPU used over the last 5 minutes"),
                             ("ram-spark", "Memory used over the last 5 minutes"),
                             ("disk-spark", "Space used on / over the last 5 minutes"),
                             ("net-spark", "Download and upload speed over the last 5 minutes")):
            self.assertIn(f'id="{graph}" role="img" aria-label="{label}"', page)


def marks(snap, graph):
    """The labels of a graph's scale that the page has, top first: (top, bottom) for the CPU and Memory tiles."""
    text = snap["graphs"][graph]["text"]
    return tuple(text[f"{graph}-{mark}"] for mark in ("top", "middle", "bottom") if f"{graph}-{mark}" in text)


def ys(snap, graph):
    """The heights of the points of a graph's line, every piece together (0: the top of the scale, 100: its bottom)."""
    return [y for sub in line(snap, graph, "used") for x, y in sub]


class TileScales(OverviewTest):
    """The scale of the CPU and Memory tiles zooms on the readings of the window: from a multiple of 5 % at or below
    the lowest to one at or above the highest, 10 points at least, within 0 to 100 %; their top and bottom labelled."""

    def readings(self, seconds, cpu=lambda s: "10.0", ram=lambda s: "50.00"):
        """One "live" event at each of these seconds, in one step. cpu, ram: the text sent, as a function of the
        second."""
        return {"batch": [at(s, "live", live(cpu=cpu(s), ram=ram(s))) for s in seconds]}

    def test_the_page_labels_their_top_and_bottom(self):
        """#id-top and #id-bottom, a grid line at each; the other graphs keep #id-top and #id-middle, as before."""
        with mock.patch.object(swizzin.layout_choices, "get", return_value="overview"):
            tree = Tree()
            tree.feed(dashboard("tester", ["/"], False))
        for graph in ("cpu-spark", "ram-spark", "disk-spark", "net-spark", "cpu-graph", "net-graph"):
            with self.subTest(graph):
                plot = find(tree.root, graph)
                zoomed = graph in ("cpu-spark", "ram-spark")
                self.assertEqual("live-graph-has-bottom" in plot["attrs"]["class"].split(), zoomed)
                self.assertIsNotNone(find(plot, f"{graph}-top"))
                self.assertEqual(find(plot, f"{graph}-bottom") is not None, zoomed)
                self.assertEqual(find(plot, f"{graph}-middle") is not None, not zoomed)
                svg = [c for c in plot["children"] if c.get("tag") == "svg"][0]
                grid = [c["attrs"]["y1"] for c in svg["children"] if c.get("tag") == "line"]
                self.assertEqual(grid, ["0", "100"] if zoomed else ["0", "50"])

    def test_a_steady_memory_gets_10_points_around_it(self):
        """19.1 to 19.3 %: 15 to 25 %, the readings in the middle; the line stays almost flat (2 % of the height)."""
        snap = self.run_page([self.readings(range(60), ram=lambda s: ("19.10", "19.20", "19.30")[s % 3])])[1]
        self.assertEqual(marks(snap, "ram-spark"), ("25%", "15%"))
        self.assertEqual(set(ys(snap, "ram-spark")), {57.0, 58.0, 59.0})

    def test_a_wide_range_fills_the_height(self):
        cases = [(("5.0", "40.0", "22.5"), ("40%", "5%"), [100.0, 0.0, 50.0]),
                 (("7.0", "83.0", "45.0"), ("85%", "5%"), [97.5, 2.5, 50.0]),
                 (("3.0", "97.0"), ("100%", "0%"), [97.0, 3.0]),
                 (("12.0", "21.0"), ("25%", "10%"), [86.7, 26.7])]
        for values, scale, heights in cases:
            with self.subTest(values):
                snap = self.run_page([self.readings(range(len(values)), cpu=lambda s: values[s])])[1]
                self.assertEqual(marks(snap, "cpu-spark"), scale)
                self.assertEqual(ys(snap, "cpu-spark"), heights)

    def test_near_0_and_near_100(self):
        """10 points at least, kept within 0 to 100 %."""
        cases = [(("0.0", "2.0"), ("10%", "0%"), [100.0, 80.0]), (("0.0", "0.0"), ("10%", "0%"), [100.0, 100.0]),
                 (("4.0", "6.0"), ("10%", "0%"), [60.0, 40.0]), (("97.0", "100.0"), ("100%", "90%"), [30.0, 0.0]),
                 (("100.0", "100.0"), ("100%", "90%"), [0.0, 0.0]), (("91.0", "94.0"), ("100%", "90%"), [90.0, 60.0])]
        for values, scale, heights in cases:
            with self.subTest(values):
                snap = self.run_page([self.readings(range(2), cpu=lambda s: values[s],
                                                    ram=lambda s: "%.2f" % float(values[s]))])[1]
                for graph in ("cpu-spark", "ram-spark"):
                    self.assertEqual(marks(snap, graph), scale)
                    self.assertEqual(ys(snap, graph), heights)

    def test_a_single_reading(self):
        """Centred on it: on the multiple of 5 % closest to it."""
        for value, scale, height in (("19.2", ("25%", "15%"), 58.0), ("20.0", ("25%", "15%"), 50.0),
                                     ("23.0", ("30%", "20%"), 70.0), ("50.0", ("55%", "45%"), 50.0)):
            with self.subTest(value):
                snap = self.run_page([self.readings([0], cpu=lambda s: value)])[1]
                self.assertEqual(marks(snap, "cpu-spark"), scale)
                self.assertEqual(ys(snap, "cpu-spark"), [height])

    def test_empty_until_the_first_reading(self):
        loaded = self.run_page([])[0]
        for graph in ("cpu-spark", "ram-spark"):
            self.assertEqual(marks(loaded, graph), ("", ""))

    def test_a_gap_in_the_readings(self):
        """60 seconds without a reading cut the line; the scale takes the readings on both sides. An unreadable
        value leaves a hole and does not count."""
        snap = self.run_page([self.readings(list(range(10)) + list(range(70, 80)),
                                            cpu=lambda s: "30.0" if s < 10 else "42.0")])[1]
        self.assertEqual([len(sub) for sub in line(snap, "cpu-spark", "used")], [10, 10])
        self.assertEqual(marks(snap, "cpu-spark"), ("45%", "30%"))
        self.assertEqual(set(ys(snap, "cpu-spark")), {100.0, 20.0})
        hole = {"batch": [at(s, "live", dict(live(cpu="30.0"), ram=dict(live()["ram"], perutil="?" if s == 5 else
                                                                            "19.20"))) for s in range(10)]}
        snap = self.run_page([hole])[1]
        self.assertEqual([len(sub) for sub in line(snap, "ram-spark", "used")], [5, 4])
        self.assertEqual(marks(snap, "ram-spark"), ("25%", "15%"))

    def test_the_labels_follow_the_window_as_it_slides(self):
        """A spike at 80 % widens the scale while it is in the last 5 minutes, and only then."""
        cpu = lambda s: "80.0" if s == 20 else "10.0"  # noqa: E731
        snaps = self.run_page([self.readings(range(20), cpu=cpu), self.readings([20], cpu=cpu),
                               self.readings(range(21, 321), cpu=cpu),  # the spike reaches the left edge
                               self.readings([321], cpu=cpu)])  # and leaves the window
        self.assertEqual([marks(s, "cpu-spark") for s in snaps[1:]],
                         [("15%", "5%"), ("80%", "10%"), ("80%", "10%"), ("15%", "5%")])
        self.assertEqual(set(ys(snaps[1], "cpu-spark")), {50.0})
        self.assertEqual(line(snaps[2], "cpu-spark", "used")[0][-1], (300.0, 0.0))
        self.assertEqual(set(ys(snaps[3], "cpu-spark")), {0.0, 100.0})
        self.assertEqual(line(snaps[3], "cpu-spark", "used")[0][0], (0.0, 0.0))
        self.assertEqual(set(ys(snaps[4], "cpu-spark")), {50.0})

    def test_the_cpu_info_card_and_the_disk_tile_keep_0_to_100(self):
        """The same readings: the CPU tile zooms, the CPU Info card's graph keeps 0 to 100 %, and so does the Disk /
        tile; the Network tile keeps the scale of the bandwidth graph."""
        steps = {"batch": [step for s in range(30) for step in (
            at(s, "live", live(cpu=("19.1", "19.3")[s % 2], ram="19.20")),
            at(s, "speed", speed("1.00 MiB/s", "512.00 KiB/s")))]}
        snap = self.run_page([steps])[1]
        self.assertEqual(marks(snap, "cpu-spark"), ("25%", "15%"))
        self.assertEqual(set(ys(snap, "cpu-spark")), {57.0, 59.0})
        self.assertEqual(marks(snap, "cpu-graph"), ("100%", "50%"))
        self.assertEqual(set(ys(snap, "cpu-graph")), {80.7, 80.9})
        self.assertEqual(marks(snap, "disk-spark"), ("100%", "50%"))
        self.assertEqual(set(ys(snap, "disk-spark")), {58.1})
        self.assertEqual(marks(snap, "net-spark"), marks(snap, "net-graph"))
        self.assertEqual(line(snap, "net-spark", "rx"), line(snap, "net-graph", "rx"))


class Sections(OverviewTest):
    """The sections of Details: open or closed as core/overview_blocks.py says ("open": those across the whole width),
    unless the browser remembers otherwise: "overview.open", the sections opened among those closed by default,
    "overview.closed", those closed among those open by default."""

    def defaults(self, snap, opened=(), closed=()):
        """{section: open} for the sections of `snap`: as by default, but those `opened` and `closed`."""
        return {k: (k in OPEN_BY_DEFAULT or k in opened) and k not in closed for k in snap["sections"]}

    def wide(self, snap):
        """The sections of `snap` open by default, in the order of the page."""
        return [k for k in snap["sections"] if k in OPEN_BY_DEFAULT]

    def test_as_by_default_when_the_page_loads(self):
        loaded = self.run_page([], storage={})[0]
        self.assertEqual(loaded["sections"], self.defaults(loaded))
        self.assertEqual(sorted(self.wide(loaded)), sorted(OPEN_BY_DEFAULT))  # each one in the admin's page
        self.assertIn(False, loaded["sections"].values())
        self.assertEqual(loaded["storage"], {})  # nothing written

    def test_a_click_opens_a_section_and_the_browser_remembers_it(self):
        snaps = self.run_page([{"summary": "cpu"}, {"summary": "disks"}, {"summary": "cpu"}], storage={})
        self.assertEqual([s["sections"] for s in snaps[1:]],
                         [self.defaults(snaps[0], ["cpu"]), self.defaults(snaps[0], ["cpu", "disks"]),
                          self.defaults(snaps[0], ["disks"])])
        self.assertEqual([s["storage"] for s in snaps], [{}, {STORE_KEY: "cpu"}, {STORE_KEY: "cpu,disks"},
                                                         {STORE_KEY: "disks"}])  # "overview.closed" never written

    def test_a_click_closes_a_section_open_by_default_and_the_browser_remembers_it(self):
        """Each one closed, the last first, then the first opened again: "overview.closed" lists those closed, in the
        order of the page; "overview.open" is never written. The page loaded again shows them as they were left."""
        wide = self.wide(self.run_page([], storage={})[0])
        steps = [{"summary": name} for name in wide[::-1]] + [{"summary": wide[0]}]
        snaps = self.run_page(steps, storage={})
        closed = [wide[len(wide) - n:] for n in range(1, len(wide) + 1)] + [wide[1:]]
        self.assertEqual([s["sections"] for s in snaps[1:]], [self.defaults(snaps[0], closed=c) for c in closed])
        self.assertEqual([s["storage"] for s in snaps[1:]], [{CLOSED_KEY: ",".join(c)} for c in closed])
        loaded = self.run_page([], storage=snaps[-2]["storage"])[0]  # the page loads again, all of them closed
        self.assertEqual(loaded["sections"], snaps[-2]["sections"])

    def test_the_sections_remembered_are_as_remembered_when_the_page_loads(self):
        """What the previous version kept (the sections open, each one) reads as it was: a section open by default in
        it changes nothing. What cannot be a section is left out. When a section changes as the page loads, its
        "toggle" writes the keys again, with only what differs from the default, and only the keys that change."""
        wide = sorted(OPEN_BY_DEFAULT)[0]
        for storage, opened, closed, after in (
                ({STORE_KEY: "disks," + wide}, ["disks"], [], {STORE_KEY: "disks"}),
                ({STORE_KEY: "nonsense,cpu,"}, ["cpu"], [], {STORE_KEY: "cpu"}),
                ({STORE_KEY: ""}, [], [], {STORE_KEY: ""}),
                ({CLOSED_KEY: wide}, [], [wide], {CLOSED_KEY: wide}),
                ({CLOSED_KEY: "disks,nonsense"}, [], [], {CLOSED_KEY: "disks,nonsense"}),  # nothing changes
                ({STORE_KEY: "cpu", CLOSED_KEY: wide + ",,"}, ["cpu"], [wide], {STORE_KEY: "cpu", CLOSED_KEY: wide})):
            with self.subTest(storage):
                loaded = self.run_page([], storage=storage)[0]
                self.assertEqual(loaded["sections"], self.defaults(loaded, opened, closed))
                self.assertEqual(loaded["storage"], after)

    def test_each_key_is_written_only_when_it_changes(self):
        """A click writes what changed: the key of the previous version loses a section open by default."""
        wide = sorted(OPEN_BY_DEFAULT)[0]
        clicked = self.run_page([{"summary": "cpu"}], storage={STORE_KEY: wide + ",disks"})[1]
        self.assertEqual(clicked["storage"], {STORE_KEY: "cpu,disks"})
        self.assertEqual(clicked["sections"], self.defaults(clicked, ["cpu", "disks"]))

    def test_without_a_usable_storage_as_by_default_and_still_open_and_close(self):
        wide = sorted(OPEN_BY_DEFAULT)[0]
        for storage in (None, "throws", "full"):
            with self.subTest(storage=storage):
                loaded, opened, closed = self.run_page([{"summary": "ram"}, {"summary": wide}], storage=storage)
                self.assertEqual(loaded["sections"], self.defaults(loaded))
                self.assertEqual(opened["sections"], self.defaults(loaded, ["ram"]))
                self.assertEqual(closed["sections"], self.defaults(loaded, ["ram"], [wide]))

    def test_the_sections_follow_the_rights_of_the_cards(self):
        cards = ("cpu", "ram", "disks", "quota", "network")  # the cards of Classic; other blocks test their own
        for quota, expected in ((False, {"cpu": False, "ram": False, "disks": False}),
                                (True, {"cpu": False, "ram": False, "quota": False})):
            with self.subTest(quota=quota):
                loaded = self.run_page([], user="friend", quota=quota)[0]
                self.assertEqual({k: v for k, v in loaded["sections"].items() if k in cards}, expected)


class Totals(OverviewTest):
    """This hour, today, this month and all time, under the bandwidth graph: read from the bandwidth card when the
    dashboard's own refresh fills it."""

    PERIODS = ("hour", "day", "month", "alltime")

    def setUp(self):
        self.vnstat = FakeVnstat()
        for patcher in (mock.patch.dict(os.environ, {"PATH": self.vnstat.dir + os.pathsep + os.environ["PATH"]}),
                        mock.patch.object(swizzin, "get_default_interface", return_value="eth0")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def card(self, data=None):
        """What /stats/vnstat answers the admin, parsed as the browser would; and its totals, {period: (down, up)}."""
        if data is not None:
            self.vnstat.say(vnstat_json(data))
        r = swizzin.app.test_client().get("/stats/vnstat", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        tree = Tree()
        tree.feed(r.get_data(as_text=True))
        totals = {}

        def walk(node):
            period = node.get("attrs", {}).get("data-period")
            if period:
                cells = ["".join(c.get("text", "") for c in td["children"]).strip()
                         for td in node["children"] if td.get("tag") == "td"]
                totals[period] = (cells[1], cells[2])
            for child in node.get("children", []):
                walk(child)
        walk(tree.root)
        return {"tree": tree.root}, totals

    def ids(self):
        return [f"ov-{p}-{k}" for p in self.PERIODS for k in ("rx", "tx")] + ["ov-periods-note"]

    def shown(self, snap):
        return {p: (snap["watch"][f"ov-{p}-rx"], snap["watch"][f"ov-{p}-tx"]) for p in self.PERIODS}

    def test_the_totals_of_the_card(self):
        card, totals = self.card()
        self.assertEqual(set(totals), {"hour", "lasthour", "day", "month", "alltime"})
        loaded, answered = self.run_page([{"ajax": card}], poll="vnstat_top10", watch=self.ids(),
                                         attrs=[("ov-periods-note", "hidden")])
        self.assertEqual(self.shown(loaded), {p: ("--", "--") for p in self.PERIODS})
        self.assertEqual(self.shown(answered), {p: totals[p] for p in self.PERIODS})
        self.assertNotIn("--", str(self.shown(answered)))
        self.assertEqual(answered["attrs"]["ov-periods-note hidden"], "")  # no sentence
        self.assertEqual(answered["requests"], 1)  # the dashboard's own request: none of their own

    def test_no_data_then_data_again(self):
        """vnstat has no totals for a while: the card's sentence instead, then the totals again."""
        history, totals = self.card()
        empty, _ = self.card(EMPTY)
        snaps = self.run_page([{"ajax": history}, {"timer": True}, {"ajax": empty}, {"timer": True}, {"ajax": history}],
                              poll="vnstat_top10", watch=self.ids(), attrs=[("ov-periods-note", "hidden")])
        self.assertEqual(self.shown(snaps[3]), {p: ("--", "--") for p in self.PERIODS})
        self.assertEqual(snaps[3]["watch"]["ov-periods-note"], "No bandwidth data yet for eth0.")
        self.assertIsNone(snaps[3]["attrs"]["ov-periods-note hidden"])
        self.assertEqual(self.shown(snaps[5]), {p: totals[p] for p in self.PERIODS})
        self.assertEqual(snaps[5]["attrs"]["ov-periods-note hidden"], "")


class LayoutMenu(OverviewTest):
    """The layouts in the account menu (static/js/swizzin.js): a click saves the choice, then the page loads again."""

    def run_menu(self, steps, layout):
        return self.run_page(steps, scripts=("swizzin.js",), layout=layout, attrs=[("layout-error", "hidden")])

    def test_a_click_on_another_layout_saves_it_then_reloads(self):
        for current, other in (("classic", "overview"), ("overview", "classic")):
            with self.subTest(current):
                steps = [{"clickOn": f'button[data-layout="{other}"]'}, {"ajax": {"layout": other}}]
                clicked, saved = self.run_menu(steps, current)[1:]
                self.assertEqual(clicked["sent"], [{"type": "POST", "url": "/layout", "contentType": "application/json",
                                                    "data": json.dumps({"layout": other}, separators=(",", ":"))}])
                self.assertEqual((clicked["reloads"], saved["reloads"]), (0, 1))
                self.assertEqual(saved["attrs"]["layout-error hidden"], "")

    def test_when_it_cannot_be_saved_the_menu_says_so(self):
        failed = self.run_menu([{"clickOn": 'button[data-layout="overview"]'}, {"fail": True}], "classic")[2]
        self.assertEqual(failed["reloads"], 0)
        self.assertIsNone(failed["attrs"]["layout-error hidden"])

    def test_the_layout_shown_does_nothing(self):
        snap = self.run_menu([{"clickOn": 'button[data-layout="classic"]'}], "classic")[1]
        self.assertEqual((snap["sent"], snap["reloads"]), ([], 0))


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
