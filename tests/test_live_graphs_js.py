"""The live graphs of the dashboard (static/js/live_graphs.js), run for real in Node.js.

The bandwidth graph of the Network Info card and the CPU graph of the CPU Info card are drawn from the websocket
events the page already receives, "speed" and "live", one per second. The page the panel renders runs in the small
fake DOM of tests/fake_dom.js (no npm package to install), with a clock that the tests set. Skipped when node is not
installed, except with REQUIRE_NODE=1, set by the automatic tests (.github/workflows/tests.yml): there, a missing
node fails them. The tests of the page itself (class Page) need no node.

Run from the repository root: python tests/test_live_graphs_js.py
(uses a throwaway htpasswd file: nothing on the machine is touched)."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# The page, the fake DOM and its runner are those of the disk banner's tests. Importing them points the panel to a
# throwaway htpasswd file (the admin "tester", and "friend") and writes swizzin.cfg, removed at the end.
from test_disk_alert_js import MOUNTS, ROOT, NodeTest, Tree, dashboard, page_scripts  # noqa: E402

T0 = 1_700_000_000_000  # ms: the clock (Date.now()) at the first reading of a test
MiB = 1024 * 1024


def subpaths(d):
    """A path's "d" as its subpaths, each a list of (x, y): "M1 2L3 4Z" gives [[(1.0, 2.0), (3.0, 4.0)]]."""
    out = []
    for command, x, y in re.findall(r"([MLZ])(?:(-?[\d.]+) (-?[\d.]+))?", d or ""):
        if command == "M":
            out.append([])
        if command != "Z":
            out[-1].append((float(x), float(y)))
    return out


def at(second, event, data=None):
    """A websocket event received at T0 + second."""
    step = {"at": T0 + round(second * 1000), "event": event}
    if data is not None:
        step["data"] = data
    return step


def speed(rx, tx):
    """A "speed" event, as swizzin.py sends it: core/util.py GetHumanReadableBi, then "/s"."""
    return {"interface": "eth0", "rx": rx, "tx": tx, "total": "--"}


def readings(seconds, rx="1.00 MiB/s", tx="512.00 KiB/s"):
    """One "speed" event at each of these seconds, in one step. rx, tx: a text, or a function of the second."""
    def value(v, s):
        return v(s) if callable(v) else v
    return {"batch": [at(s, "speed", speed(value(rx, s), value(tx, s))) for s in seconds]}


def live(used):
    """A "live" event, as core/custom/live_stats.py sends it, with this CPU used (a text, as sent)."""
    return {"cpu": {"used": used, "idle": "%.1f" % (100 - float(used)), "cores": 2, "model": "Demo CPU", "freq": "",
                    "user": used, "system": "0.0", "iowait": "0.0", "steal": "0.0", "percore": [used, used]},
            "ram": {"ramused": "1.00 GiB", "ramfree": "3.00 GiB", "ramtotal": "4.00 GiB", "perutil": "25.00"}}


def line(snap, graph, key):
    return subpaths(snap["graphs"][graph]["d"][f"{graph}-{key}-line"])


def area(snap, graph, key):
    return subpaths(snap["graphs"][graph]["d"][f"{graph}-{key}-area"])


def find(node, id_):
    """The element with this id in a tree of tests/test_disk_alert_js.py, or None."""
    if node.get("attrs", {}).get("id") == id_:
        return node
    for child in node.get("children", []):
        found = find(child, id_)
        if found:
            return found
    return None


class Page(unittest.TestCase):
    """The containers of the graphs, in the page the panel renders."""

    def tree(self, user):
        tree = Tree()
        tree.feed(dashboard(user, MOUNTS, False))
        return tree.root

    def assert_graph(self, root, graph, keys):
        plot = find(root, graph)
        self.assertIsNotNone(plot, f"#{graph} is missing")
        svg = [c for c in plot["children"] if c.get("tag") == "svg"]
        self.assertEqual(len(svg), 1)
        self.assertEqual((svg[0]["attrs"].get("viewbox"), svg[0]["attrs"].get("preserveaspectratio")),
                         ("0 -1 300 102", "none"))  # 300 units: one per second of the window
        for key in keys:
            for part in ("area", "line"):
                path = find(svg[0], f"{graph}-{key}-{part}")
                self.assertIsNotNone(path, f"#{graph}-{key}-{part} is missing")
                self.assertEqual(path["tag"], "path")
                self.assertNotIn("d", path["attrs"])  # empty when the page loads
        for label in ("top", "middle"):
            self.assertIsNotNone(find(plot, f"{graph}-{label}"), f"#{graph}-{label} is missing")

    def test_the_admin_page_has_both_graphs(self):
        root = self.tree("tester")
        self.assert_graph(root, "cpu-graph", ["used"])
        self.assert_graph(root, "net-graph", ["rx", "tx"])
        for key in ("rx", "tx"):  # the legend's current values
            self.assertIsNotNone(find(root, f"net-graph-{key}-now"))
        # The graphs are in their cards.
        self.assertIsNotNone(find(find(root, "cpu-card"), "cpu-graph"))
        self.assertIsNotNone(find(root, "current_rx"))  # the speed table is still there

    def test_another_user_gets_the_cpu_graph_only(self):
        """The bandwidth is shown to the admin only, like the speed table of the Network Info card."""
        root = self.tree("friend")
        self.assert_graph(root, "cpu-graph", ["used"])
        self.assertIsNone(find(root, "net-graph"))
        self.assertIsNone(find(root, "current_rx"))

    def test_the_graphs_script_loads_before_live_stats(self):
        """live_stats.js hands its websocket to the graphs: no other connection is opened."""
        self.assertEqual([os.path.basename(p) for p in page_scripts(self.tree("tester"))],
                         ["live_graphs.js", "live_stats.js"])


class Bandwidth(NodeTest):
    def test_the_graphs_are_empty_when_the_page_loads(self):
        loaded = self.run_page([])[0]
        self.assertEqual(set(loaded["graphs"]), {"cpu-graph", "net-graph"})
        self.assertEqual(set(loaded["graphs"]["net-graph"]["d"].values()), {None})
        self.assertEqual(loaded["graphs"]["net-graph"]["text"],
                         {"net-graph-rx-now": "--", "net-graph-tx-now": "--", "net-graph-top": "",
                          "net-graph-middle": ""})

    def test_each_reading_is_a_point_at_its_second(self):
        snap = self.run_page([readings(range(10))])[1]
        xs = [float(x) for x in range(291, 301)]  # the newest reading on the right edge (300), one unit per second
        self.assertEqual(line(snap, "net-graph", "rx"), [[(x, 0.0) for x in xs]])  # 1 MiB/s, at the top
        self.assertEqual(line(snap, "net-graph", "tx"), [[(x, 50.0) for x in xs]])  # 512 KiB/s, half way
        self.assertEqual(area(snap, "net-graph", "tx"), [[(291.0, 100.0)] + [(x, 50.0) for x in xs] + [(300.0, 100.0)]])
        self.assertEqual(snap["graphs"]["net-graph"]["text"],
                         {"net-graph-rx-now": "1.00 MiB/s", "net-graph-tx-now": "512.00 KiB/s",  # as received
                          "net-graph-top": "1 MiB/s", "net-graph-middle": "512 KiB/s"})

    def test_the_window_rolls_after_300_readings(self):
        """The last 5 minutes: 301 readings, the oldest on the left edge (0), the newest on the right one (300)."""
        kib = lambda s: "%d.00 KiB/s" % (s + 1)  # noqa: E731
        zero = "0.00 B/s"
        snaps = self.run_page([readings(range(310), rx=kib, tx=zero), readings(range(310, 400), rx=kib, tx=zero)])
        rx = line(snaps[1], "net-graph", "rx")
        self.assertEqual(len(rx), 1)
        self.assertEqual(len(rx[0]), 301)
        self.assertEqual((rx[0][0], rx[0][-1]), ((0.0, 97.5), (300.0, 22.5)))  # 10 KiB/s then 310 KiB/s, of 400
        self.assertEqual(snaps[1]["graphs"]["net-graph"]["text"]["net-graph-top"], "400 KiB/s")
        self.assertEqual(snaps[1]["graphs"]["net-graph"]["text"]["net-graph-middle"], "200 KiB/s")
        self.assertEqual(len(line(snaps[2], "net-graph", "rx")[0]), 301)  # and no more, 90 readings later

    def test_zero_traffic_then_a_spike(self):
        zero = "0.00 B/s"
        snaps = self.run_page([
            readings(range(20), rx=zero, tx=zero),
            readings([20], rx="45.67 MiB/s", tx=zero),
            readings(range(21, 321), rx=zero, tx=zero),  # the spike reaches the left edge
            readings([321], rx=zero, tx=zero),  # and leaves the window
        ])
        texts = [(s["graphs"]["net-graph"]["text"]["net-graph-top"], s["graphs"]["net-graph"]["text"]["net-graph-middle"])
                 for s in snaps[1:]]
        self.assertEqual(texts, [("1 KiB/s", "512 B/s"), ("50 MiB/s", "25 MiB/s"), ("50 MiB/s", "25 MiB/s"),
                                 ("1 KiB/s", "512 B/s")])
        self.assertEqual({y for x, y in line(snaps[1], "net-graph", "rx")[0]}, {100.0})  # flat, at the bottom
        spike = line(snaps[2], "net-graph", "rx")[0]
        self.assertEqual(spike[-1], (300.0, 8.7))  # 45.67 of 50 MiB/s
        self.assertEqual({y for x, y in spike[:-1]}, {100.0})
        self.assertEqual(line(snaps[3], "net-graph", "rx")[0][0], (0.0, 8.7))

    def test_the_scale_steps(self):
        """The top of the scale is the next 1, 1.5, 2, 3, 4, 5, 6 or 8 x 10^n of the unit of the highest reading: that
        reading fills 2/3 of the height or more. Never below 1 KiB/s."""
        cases = [("0.00 B/s", "1 KiB/s", "512 B/s"), ("1.00 KiB/s", "1 KiB/s", "512 B/s"),
                 ("1.01 KiB/s", "1.5 KiB/s", "768 B/s"), ("2.50 MiB/s", "3 MiB/s", "1.5 MiB/s"),
                 ("7.00 MiB/s", "8 MiB/s", "4 MiB/s"), ("45.67 MiB/s", "50 MiB/s", "25 MiB/s"),
                 ("1000.00 KiB/s", "1000 KiB/s", "500 KiB/s"), ("1001.00 KiB/s", "1 MiB/s", "512 KiB/s"),
                 ("1.20 GiB/s", "1.5 GiB/s", "768 MiB/s")]
        # Each reading alone in the window: 1000 seconds after the previous one.
        snaps = self.run_page([readings([1000 * i], rx=rx, tx="0.00 B/s") for i, (rx, _, _) in enumerate(cases)])
        for (rx, top, middle), snap in zip(cases, snaps[1:]):
            with self.subTest(rx):
                text = snap["graphs"]["net-graph"]["text"]
                self.assertEqual((text["net-graph-top"], text["net-graph-middle"]), (top, middle))
                (x, y), = line(snap, "net-graph", "rx")[0]
                if rx != "0.00 B/s":
                    self.assertLessEqual(y, 100 / 3)  # 2/3 of the height or more

    def test_a_gap_cuts_the_line(self):
        """No reading for 60 seconds (the panel restarting...): the line stops and starts again, the gap shows."""
        snap = self.run_page([readings(list(range(10)) + list(range(70, 80)))])[1]
        for key in ("rx", "tx"):
            self.assertEqual([[x for x, y in sub] for sub in line(snap, "net-graph", key)],
                             [[float(x) for x in range(221, 231)], [float(x) for x in range(291, 301)]])
            self.assertEqual([(sub[0], sub[-1]) for sub in area(snap, "net-graph", key)],
                             [((221.0, 100.0), (230.0, 100.0)), ((291.0, 100.0), (300.0, 100.0))])

    def test_one_missing_reading_does_not_cut_it_two_do(self):
        snaps = self.run_page([readings([0, 1, 2, 4, 5]), readings([8, 9])])
        self.assertEqual(len(line(snaps[1], "net-graph", "rx")), 1)
        self.assertEqual(len(line(snaps[2], "net-graph", "rx")), 2)

    def test_a_disconnection_cuts_the_line_even_when_short(self):
        snap = self.run_page([{"batch": [readings(range(5)), at(4.5, "disconnect"), readings(range(5, 10))]}])[1]
        self.assertEqual([[x for x, y in sub] for sub in line(snap, "net-graph", "rx")],
                         [[291.0, 292.0, 293.0, 294.0, 295.0], [296.0, 297.0, 298.0, 299.0, 300.0]])

    def test_after_a_long_disconnection_the_graph_starts_again(self):
        snap = self.run_page([{"batch": [readings(range(10)), at(10, "disconnect"), readings([400, 401, 402])]}])[1]
        self.assertEqual(line(snap, "net-graph", "rx"), [[(298.0, 0.0), (299.0, 0.0), (300.0, 0.0)]])

    def test_the_clock_going_back_starts_again(self):
        snap = self.run_page([readings(range(100, 105)), readings([50])])[2]
        self.assertEqual(line(snap, "net-graph", "rx"), [[(300.0, 0.0)]])

    def test_a_hidden_tab_draws_nothing_and_keeps_the_time(self):
        """Hidden, the readings are kept with the time they arrived, and drawn when the tab is shown again: the same
        graph as if it had stayed visible."""
        visible = self.run_page([readings(range(15))])[1]
        snaps = self.run_page([readings(range(5)), {"hidden": True, "at": T0 + 4500}, readings(range(5, 15)),
                               {"hidden": False, "at": T0 + 14000}])
        self.assertEqual(snaps[3]["graphs"]["net-graph"]["changes"], 0)  # nothing written while hidden
        self.assertEqual(snaps[4]["graphs"]["net-graph"]["d"], visible["graphs"]["net-graph"]["d"])
        self.assertEqual(snaps[4]["graphs"]["net-graph"]["text"], visible["graphs"]["net-graph"]["text"])

    def test_an_unreadable_value_leaves_a_hole(self):
        snap = self.run_page([readings(range(10), rx=lambda s: "?" if s == 5 else "1.00 MiB/s")])[1]
        self.assertEqual([len(sub) for sub in line(snap, "net-graph", "rx")], [5, 4])
        self.assertEqual([len(sub) for sub in line(snap, "net-graph", "tx")], [10])

    def test_the_nodes_are_reused_and_written_only_when_they_change(self):
        snaps = self.run_page([readings([0]), readings([1]), readings([2])])
        self.assertEqual([s["graphs"]["net-graph"]["added"] for s in snaps], [0, 0, 0, 0])  # nothing created
        self.assertEqual(snaps[0]["graphs"]["net-graph"]["changes"], 0)
        # Same values, same scale: only the 4 paths move by one second; the texts are not written again.
        self.assertEqual([s["graphs"]["net-graph"]["changes"] for s in snaps[2:]], [4, 4])

    def test_without_socketio_the_graphs_stay_empty(self):
        snap = self.run_page([], socketio=False)[0]
        self.assertEqual(set(snap["graphs"]["net-graph"]["d"].values()), {None})


class Cpu(NodeTest):
    def test_cpu_at_0_then_100_then_50(self):
        snap = self.run_page([{"batch": [at(0, "live", live("0.0")), at(1, "live", live("100.0")),
                                         at(2, "live", live("50.0"))]}], watch=["cpuused"])[1]
        self.assertEqual(line(snap, "cpu-graph", "used"), [[(298.0, 100.0), (299.0, 0.0), (300.0, 50.0)]])
        self.assertEqual(area(snap, "cpu-graph", "used"),
                         [[(298.0, 100.0), (298.0, 100.0), (299.0, 0.0), (300.0, 50.0), (300.0, 100.0)]])
        self.assertEqual(snap["graphs"]["cpu-graph"]["text"], {"cpu-graph-top": "100%", "cpu-graph-middle": "50%"})
        self.assertEqual(snap["watch"], {"cpuused": "50.0"})

    def test_the_cpu_graph_rolls_and_is_cut_like_the_bandwidth(self):
        steps = [{"batch": [at(s, "live", live("10.0")) for s in range(310)]},
                 {"batch": [at(310, "disconnect"), at(311, "live", live("20.0"))]}]
        snaps = self.run_page(steps)
        self.assertEqual([len(sub) for sub in line(snaps[1], "cpu-graph", "used")], [301])
        self.assertEqual([len(sub) for sub in line(snaps[2], "cpu-graph", "used")], [299, 1])

    def test_another_user_has_the_cpu_graph_and_ignores_the_speed(self):
        snaps = self.run_page([readings(range(3)), {"batch": [at(3, "live", live("25.0"))]}], user="friend")
        self.assertEqual(set(snaps[-1]["graphs"]), {"cpu-graph"})
        self.assertEqual(line(snaps[-1], "cpu-graph", "used"), [[(300.0, 75.0)]])


class Cards(NodeTest):
    """The live cards are filled as before: the graphs only add their own handlers."""

    def test_the_cards_still_follow_each_reading(self):
        ids = ["cpuused", "cpuidle", "cpucores", "cpupercent", "cpuuser", "rampercent", "ramused"]
        snaps = self.run_page([{"event": "live", "data": live("37.5"), "at": T0},
                               {"event": "live", "data": live("12.5"), "at": T0 + 1000}], watch=ids)
        self.assertEqual(snaps[1]["watch"], {"cpuused": "37.5", "cpuidle": "62.5", "cpucores": "2",
                                             "cpupercent": "37.5", "cpuuser": "37.5", "rampercent": "25.00",
                                             "ramused": "1.00 GiB"})
        self.assertEqual(snaps[2]["watch"]["cpuused"], "12.5")


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
