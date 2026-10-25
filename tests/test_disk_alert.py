"""The "disk almost full" banner of the live Disk Info card.

Run from the repository root: python tests/test_disk_alert.py
(uses a throwaway htpasswd file, fake disks and a fake websocket: nothing on the machine is touched).

The panel is started with invalid DISK_WARN_PERCENT and DISK_CRIT_PERCENT in swizzin.cfg, on purpose:
it must still start, and use the default thresholds."""
import base64
import os
import re
import sys
import tempfile
import unittest
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
    f.write(f"FLASK_HTPASSWD_PATH = '{_tmp}/htpasswd'\nADMIN_USER = 'tester'\n"
            "DISK_WARN_PERCENT = 'ninety'\nDISK_CRIT_PERCENT = -5\n")

import swizzin  # noqa: E402
import core.util as util  # noqa: E402
from core.custom import live_stats  # noqa: E402

DEFAULTS = (90, 95)
TRAP = "/mnt/<img src=x onerror=alert(1)> it's \"mine\" & yours"
CPU_A = {"cpu": [1, 0, 1, 10, 0, 0, 0, 0], "cpu0": [1, 0, 1, 10, 0, 0, 0, 0]}
CPU_B = {"cpu": [2, 0, 2, 20, 0, 0, 0, 0], "cpu0": [2, 0, 2, 20, 0, 0, 0, 0]}


def auth(user):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{user}".encode()).decode()}


def fake_disks(percents):
    """disk_usage() of the panel, for mounts used at the given percentages ('92.34', like the real one)."""
    return mock.patch.object(live_stats.util, "disk_usage",
                             side_effect=lambda mnt: ("100.00 GiB", "x GiB", "7.66 GiB", percents[mnt]))


class Thresholds(unittest.TestCase):
    """DISK_WARN_PERCENT and DISK_CRIT_PERCENT, as read from swizzin.cfg."""

    def test_defaults(self):
        self.assertEqual(live_stats.read_thresholds({}), DEFAULTS)

    def test_settings_are_used(self):
        cfg = {"DISK_WARN_PERCENT": 80, "DISK_CRIT_PERCENT": 97.5}
        self.assertEqual(live_stats.read_thresholds(cfg), (80, 97.5))
        self.assertEqual(live_stats.read_thresholds({"DISK_WARN_PERCENT": "85"}), (85, 95))  # a number in quotes

    def test_invalid_values_fall_back_to_the_defaults(self):
        for bad in ("ninety", "", "90%", None, -1, -0.01, 100.01, 1000, float("nan"), float("inf"),
                    True, False, [90], {"x": 1}, 10 ** 400):
            with self.subTest(value=bad):
                logger = mock.Mock()
                cfg = {"DISK_WARN_PERCENT": bad, "DISK_CRIT_PERCENT": bad}
                self.assertEqual(live_stats.read_thresholds(cfg, logger), DEFAULTS)
                self.assertEqual(logger.warning.call_count, 2)  # the admin can see why in the log

    def test_one_invalid_value_keeps_the_other_setting(self):
        cfg = {"DISK_WARN_PERCENT": 85, "DISK_CRIT_PERCENT": "full"}
        self.assertEqual(live_stats.read_thresholds(cfg), (85, 95))

    def test_zero_is_valid_and_not_logged(self):
        logger = mock.Mock()
        cfg = {"DISK_WARN_PERCENT": 0, "DISK_CRIT_PERCENT": 0}
        self.assertEqual(live_stats.read_thresholds(cfg, logger), (0, 0))
        logger.warning.assert_not_called()

    def test_warning_not_below_critical_turns_orange_off(self):
        """Orange from 96 % and red from 95 %: orange could never show. It is turned off, and the log says why."""
        for warn, crit in ((96, 95), (95, 95), ("ninety", 85)):
            with self.subTest(warn=warn, crit=crit):
                logger = mock.Mock()
                cfg = {"DISK_WARN_PERCENT": warn, "DISK_CRIT_PERCENT": crit}
                self.assertEqual(live_stats.read_thresholds(cfg, logger), (0, crit))
                self.assertIn("DISK_WARN_PERCENT", logger.warning.call_args.args[0])
                self.assertIn("orange", logger.warning.call_args.args[0])

    def test_a_level_turned_off_is_not_an_inversion(self):
        for cfg, expected in (({"DISK_WARN_PERCENT": 96, "DISK_CRIT_PERCENT": 0}, (96, 0)),
                              ({"DISK_WARN_PERCENT": 0, "DISK_CRIT_PERCENT": 50}, (0, 50)),
                              ({"DISK_WARN_PERCENT": 94.99, "DISK_CRIT_PERCENT": 95}, (94.99, 95))):
            with self.subTest(cfg=cfg):
                logger = mock.Mock()
                self.assertEqual(live_stats.read_thresholds(cfg, logger), expected)
                logger.warning.assert_not_called()

    def test_the_panel_starts_with_invalid_settings(self):
        """swizzin.cfg of this test holds invalid values: the panel started, and uses the defaults."""
        self.assertEqual(swizzin.app.config["DISK_WARN_PERCENT"], "ninety")
        self.assertEqual(live_stats.read_thresholds(swizzin.app.config), DEFAULTS)


class Levels(unittest.TestCase):
    """Warning from DISK_WARN_PERCENT (included), critical from DISK_CRIT_PERCENT (included)."""

    def test_just_below_and_just_above_each_threshold(self):
        for pct, level in ((0, None), (89.99, None), (90, "warning"), (90.01, "warning"), (94.99, "warning"),
                           (95, "critical"), (95.01, "critical"), (100, "critical")):
            with self.subTest(pct=pct):
                self.assertEqual(live_stats.alert_level(pct, DEFAULTS), level)

    def test_zero_turns_a_level_off(self):
        self.assertEqual(live_stats.alert_level(99, (0, 95)), "critical")
        self.assertEqual(live_stats.alert_level(94.99, (0, 95)), None)
        self.assertEqual(live_stats.alert_level(100, (90, 0)), "warning")
        self.assertEqual(live_stats.alert_level(100, (0, 0)), None)

    def test_critical_wins_when_both_are_reached(self):
        self.assertEqual(live_stats.alert_level(97, (96, 95)), "critical")


class Percent(unittest.TestCase):
    """The percentage of the card and of the alert is df's Use%: the blocks reserved for root are not free."""
    GiB = 2 ** 30

    def usage(self, total, used, available):
        with mock.patch.object(util.shutil, "disk_usage", return_value=(total, used, available)):
            return util.disk_usage("/")

    def test_reserved_blocks_do_not_count_as_free(self):
        """1000 GiB ext4 disk, 50 GiB reserved for root, 900 GiB used: 50 GiB left for the apps."""
        total, used, free, pct = self.usage(1000 * self.GiB, 900 * self.GiB, 50 * self.GiB)
        self.assertEqual(pct, "94.74")  # used / total said 90.00
        self.assertEqual((total, used, free), ("1000.00 GiB", "900.00 GiB", "50.00 GiB"))

    def test_a_disk_full_for_the_apps_is_100_and_critical(self):
        _, _, free, pct = self.usage(1000 * self.GiB, 950 * self.GiB, 0)
        self.assertEqual((pct, free), ("100.00", "0.00 B"))  # used / total said 95.00
        self.assertEqual(live_stats.alert_level(float(pct), DEFAULTS), "critical")

    def test_an_empty_filesystem_does_not_divide_by_zero(self):
        self.assertEqual(self.usage(0, 0, 0)[3], "0.00")

    def test_the_real_root_disk_matches_df(self):
        st = os.statvfs("/")
        used, available = (st.f_blocks - st.f_bfree) * st.f_frsize, st.f_bavail * st.f_frsize
        pct = float(util.disk_usage("/")[3])
        self.assertAlmostEqual(pct, used / (used + available) * 100, delta=0.05)  # the disk may change meanwhile


class LiveEvent(unittest.TestCase):
    """The level travels with each disk in the "live" event the cards already receive."""

    def test_each_disk_carries_its_level(self):
        devs = {"/": "sda1", "/a": "sdb1", "/b": "sdc1", "/c": "sdd1", "/gone": "sde1"}
        pcts = {"/": "89.99", "/a": "90.00", "/b": "94.99", "/c": "95.00"}

        def usage(mnt):
            if mnt == "/gone":
                raise OSError("unreadable")
            return "100.00 GiB", "x GiB", "7.66 GiB", pcts[mnt]
        with mock.patch.object(live_stats.util, "disk_usage", side_effect=usage), \
                mock.patch.object(live_stats.os.path, "ismount", return_value=True), swizzin.app.app_context():
            disks = live_stats.measure((CPU_A, {}), (CPU_B, {}), 1.0, "test", "", devs, DEFAULTS)["disks"]
        self.assertEqual({m: disks[m]["alert"] for m in pcts},
                         {"/": None, "/a": "warning", "/b": "warning", "/c": "critical"})
        self.assertEqual(disks["/gone"], {"unavailable": True})  # no alert for a disk that cannot be read

    def run_thread(self, config, percents):
        """Runs the live_stats background task for two readings; returns the "live" events it sent."""
        class Stop(Exception):
            pass

        class FakeSocketIO:
            def __init__(self):
                self.sent, self.sleeps = [], 0

            def emit(self, event, data, namespace=None):
                self.sent.append((event, data, namespace))

            def sleep(self, seconds):
                self.sleeps += 1
                if self.sleeps == 2:  # the 1st reading only prepares the 2nd one
                    raise Stop

        sio = FakeSocketIO()
        devs = {m: "sd" + chr(97 + i) for i, m in enumerate(percents)}
        with mock.patch.dict(swizzin.app.config, config), fake_disks(percents), \
                mock.patch.object(live_stats.os.path, "ismount", return_value=True), \
                mock.patch.object(live_stats, "_model", return_value=("test", "")), \
                mock.patch.object(live_stats, "mount_devices", return_value=devs), \
                mock.patch.object(live_stats, "mounts_read_apart", return_value=[]), \
                mock.patch.object(live_stats, "_cpu", side_effect=[CPU_A, CPU_B]), \
                mock.patch.object(live_stats, "_disks", return_value={}):
            with self.assertRaises(Stop):
                live_stats.live_stats(swizzin.app, sio)
        return sio.sent

    def test_thresholds_of_swizzin_cfg_reach_the_websocket(self):
        sent = self.run_thread({"DISK_WARN_PERCENT": 50, "DISK_CRIT_PERCENT": 60},
                               {"/": "49.99", "/data": "50.00", "/media": "60.00"})
        self.assertEqual(len(sent), 1)
        event, data, namespace = sent[0]
        self.assertEqual((event, namespace), ("live", "/websocket"))
        self.assertEqual({m: d["alert"] for m, d in data["disks"].items()},
                         {"/": None, "/data": "warning", "/media": "critical"})

    def test_swapped_thresholds_keep_red_only(self):
        with self.assertLogs(swizzin.app.logger, "WARNING"):
            sent = self.run_thread({"DISK_WARN_PERCENT": 96, "DISK_CRIT_PERCENT": 95},
                                   {"/": "94.99", "/data": "95.00", "/media": "97.00"})
        self.assertEqual({m: d["alert"] for m, d in sent[0][1]["disks"].items()},
                         {"/": None, "/data": "critical", "/media": "critical"})

    def test_zero_turns_the_alert_off(self):
        sent = self.run_thread({"DISK_WARN_PERCENT": 0, "DISK_CRIT_PERCENT": 0}, {"/": "100.00"})
        self.assertIsNone(sent[0][1]["disks"]["/"]["alert"])

    def test_invalid_settings_use_the_defaults_and_are_logged(self):
        with self.assertLogs(swizzin.app.logger, "WARNING") as log:
            sent = self.run_thread({}, {"/": "89.99", "/data": "90.00", "/media": "95.00"})  # swizzin.cfg of this test
        self.assertEqual({m: d["alert"] for m, d in sent[0][1]["disks"].items()},
                         {"/": None, "/data": "warning", "/media": "critical"})
        self.assertEqual(len(log.records), 2)
        self.assertIn("DISK_WARN_PERCENT", log.output[0])
        self.assertIn("DISK_CRIT_PERCENT", log.output[1])


class StatsRoute(unittest.TestCase):
    """/stats/disk, polled until the live card takes over, carries the same level: both paint a disk alike."""

    def get_levels(self, config, percents):
        client = swizzin.app.test_client()
        with mock.patch.dict(swizzin.app.config, config), \
                mock.patch.object(swizzin, "get_mounts", return_value=list(percents)), \
                mock.patch.object(swizzin, "mount_devices", side_effect=lambda ms: dict.fromkeys(ms, "sda")), \
                mock.patch.object(swizzin, "mounts_read_apart", return_value=[]), \
                mock.patch.object(swizzin, "disk_usage", side_effect=lambda m: ("1 GiB", "1 GiB", "1 GiB", percents[m])):
            r = client.get("/stats/disk", headers=auth("tester"))
        self.assertEqual(r.status_code, 200)
        return {m: d["alert"] for m, d in r.get_json().items()}

    def test_default_thresholds(self):  # swizzin.cfg of this test is invalid: defaults
        self.assertEqual(self.get_levels({}, {"/": "89.99", "/a": "90.00", "/b": "95.00"}),
                         {"/": None, "/a": "warning", "/b": "critical"})

    def test_thresholds_of_swizzin_cfg(self):
        self.assertEqual(self.get_levels({"DISK_WARN_PERCENT": 50, "DISK_CRIT_PERCENT": 0}, {"/": "49.99", "/a": "99.00"}),
                         {"/": None, "/a": "warning"})


class Page(unittest.TestCase):
    """The banner has the same rights as the Disk Info card, and a mount path cannot inject HTML."""

    def get_dashboard(self, user, mounts, quota=False):
        real_isfile = os.path.isfile

        def isfile(path):
            return quota if path == "/install/.quota.lock" else real_isfile(path)
        client = swizzin.app.test_client()
        with mock.patch.object(swizzin, "get_mounts", return_value=mounts), \
                mock.patch.object(swizzin, "generate_page_list", return_value=[]), \
                mock.patch.object(swizzin.os.path, "isfile", side_effect=isfile):
            r = client.get("/", headers=auth(user))
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def test_only_the_summary_of_the_banner_is_a_live_region(self):
        """The summary is always there and empty (not hidden from screen readers): they read what appears in it.
        The visible lines change nearly every second during a download: they are not a live region."""
        page = self.get_dashboard("tester", ["/"])
        self.assertIn('<div id="disk-alert"></div>\n'
                      '        <div id="disk-alert-summary" class="sr-only" role="status" aria-live="polite"></div>',
                      page)
        self.assertEqual(page.count("aria-live"), 1)
        self.assertIn('id="disk-card"', page)

    def test_the_minute_poll_stops_only_once_the_live_card_has_data(self):
        """Without socket.io (CDN blocked), the poll must keep filling the card, as it did before."""
        guard = "if (card && card.hasAttribute('data-live')) { return; }"
        page = self.get_dashboard("tester", ["/"])
        self.assertRegex(page, r"\(function diskusage\(\) \{\s*var card = document\.getElementById\('disk-card'\);\s*"
                         + re.escape(guard) + r"\s*\$\.ajax\(\{")
        self.assertRegex(page, r"success: function\(data\) \{\s*" + re.escape(guard))

    def test_same_rights_as_the_disk_info_card(self):
        for user, quota, shown in (("tester", False, True), ("friend", False, True),
                                   ("tester", True, True), ("friend", True, False)):
            with self.subTest(user=user, quota=quota):
                page = self.get_dashboard(user, ["/"], quota)
                self.assertEqual('id="disk-card"' in page, shown)
                self.assertEqual('id="disk-alert"' in page, shown)
                self.assertEqual('id="disk-alert-summary"' in page, shown)

    def test_a_trapped_mount_path_is_escaped(self):
        page = self.get_dashboard("tester", ["/", TRAP])
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", page)
        self.assertNotIn("it's \"mine\"", page)  # quotes escaped too: no way out of the id="..." attributes
        self.assertIn("it&#39;s &#34;mine&#34; &amp; yours", page)


class Script(unittest.TestCase):
    """static/js/live_stats.js writes the banner as text, never as HTML."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "static", "js", "live_stats.js"), encoding="utf-8") as f:
            cls.js = f.read()
        found = re.search(r"\n  function alerts\(d\) \{\n(.*?)\n  \}\n", cls.js, re.S)
        cls.banner = found.group(1) if found else ""

    def test_the_banner_function_is_there_and_used(self):
        self.assertIn("'Disk almost full: ' + m + ' is ' + pct + '% used (' + x.diskfree + ' free)'", self.banner)
        self.assertIn("alerts(d.disks)", self.js)

    def test_no_html_is_ever_inserted(self):
        """The mount path (and the free space) go through textContent: a trapped path stays plain text."""
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", ".html(", "$(",
                     "createContextualFragment", "DOMParser"):
            with self.subTest(sink=sink):
                self.assertNotIn(sink, self.js)
        self.assertIn("item.textContent = l.text;", self.banner)

    def test_a_long_mount_path_wraps_inside_the_banner(self):
        """A long path with no space ran out of the banner on a phone, hiding which disk is full."""
        self.assertIn("var cls = 'alert mt-3 mb-0 text-break ' +", self.banner)

    def test_only_disks_of_the_card_raise_the_banner(self):
        """No Disk Info card (rights): no banner; a disk missing from the card does not show either."""
        self.assertIn("if (!banner || !card) { return; }", self.banner)
        self.assertIn("card.contains(document.getElementById(m + 'diskused'))", self.banner)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
