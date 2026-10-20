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
        self.assertEqual(live_stats.lire_seuils({}), DEFAULTS)

    def test_settings_are_used(self):
        cfg = {"DISK_WARN_PERCENT": 80, "DISK_CRIT_PERCENT": 97.5}
        self.assertEqual(live_stats.lire_seuils(cfg), (80, 97.5))
        self.assertEqual(live_stats.lire_seuils({"DISK_WARN_PERCENT": "85"}), (85, 95))  # a number in quotes

    def test_invalid_values_fall_back_to_the_defaults(self):
        for bad in ("ninety", "", "90%", None, -1, -0.01, 100.01, 1000, float("nan"), float("inf"),
                    True, False, [90], {"x": 1}, 10 ** 400):
            with self.subTest(value=bad):
                journal = mock.Mock()
                cfg = {"DISK_WARN_PERCENT": bad, "DISK_CRIT_PERCENT": bad}
                self.assertEqual(live_stats.lire_seuils(cfg, journal), DEFAULTS)
                self.assertEqual(journal.warning.call_count, 2)  # the admin can see why in the log

    def test_one_invalid_value_keeps_the_other_setting(self):
        cfg = {"DISK_WARN_PERCENT": 85, "DISK_CRIT_PERCENT": "full"}
        self.assertEqual(live_stats.lire_seuils(cfg), (85, 95))

    def test_zero_is_valid_and_not_logged(self):
        journal = mock.Mock()
        cfg = {"DISK_WARN_PERCENT": 0, "DISK_CRIT_PERCENT": 0}
        self.assertEqual(live_stats.lire_seuils(cfg, journal), (0, 0))
        journal.warning.assert_not_called()

    def test_the_panel_starts_with_invalid_settings(self):
        """swizzin.cfg of this test holds invalid values: the panel started, and uses the defaults."""
        self.assertEqual(swizzin.app.config["DISK_WARN_PERCENT"], "ninety")
        self.assertEqual(live_stats.lire_seuils(swizzin.app.config), DEFAULTS)


class Levels(unittest.TestCase):
    """Warning from DISK_WARN_PERCENT (included), critical from DISK_CRIT_PERCENT (included)."""

    def test_just_below_and_just_above_each_threshold(self):
        for pct, level in ((0, None), (89.99, None), (90, "warning"), (90.01, "warning"), (94.99, "warning"),
                           (95, "critical"), (95.01, "critical"), (100, "critical")):
            with self.subTest(pct=pct):
                self.assertEqual(live_stats.niveau_alerte(pct, DEFAULTS), level)

    def test_zero_turns_a_level_off(self):
        self.assertEqual(live_stats.niveau_alerte(99, (0, 95)), "critical")
        self.assertEqual(live_stats.niveau_alerte(94.99, (0, 95)), None)
        self.assertEqual(live_stats.niveau_alerte(100, (90, 0)), "warning")
        self.assertEqual(live_stats.niveau_alerte(100, (0, 0)), None)

    def test_critical_wins_when_both_are_reached(self):
        self.assertEqual(live_stats.niveau_alerte(97, (96, 95)), "critical")


class LiveEvent(unittest.TestCase):
    """The level travels with each disk in the « live » event the cards already receive."""

    def test_each_disk_carries_its_level(self):
        devs = {"/": "sda1", "/a": "sdb1", "/b": "sdc1", "/c": "sdd1", "/gone": "sde1"}
        pcts = {"/": "89.99", "/a": "90.00", "/b": "94.99", "/c": "95.00"}

        def usage(mnt):
            if mnt == "/gone":
                raise OSError("unreadable")
            return "100.00 GiB", "x GiB", "7.66 GiB", pcts[mnt]
        with mock.patch.object(live_stats.util, "disk_usage", side_effect=usage), \
                mock.patch.object(live_stats.os.path, "ismount", return_value=True), swizzin.app.app_context():
            disks = live_stats.mesure((CPU_A, {}), (CPU_B, {}), 1.0, "test", "", devs, DEFAULTS)["disks"]
        self.assertEqual({m: disks[m]["alert"] for m in pcts},
                         {"/": None, "/a": "warning", "/b": "warning", "/c": "critical"})
        self.assertEqual(disks["/gone"], {"unavailable": True})  # no alert for a disk that cannot be read

    def run_thread(self, config, percents):
        """Runs the live_stats background task for two readings; returns the « live » events it sent."""
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
                mock.patch.object(live_stats, "_modele", return_value=("test", "")), \
                mock.patch.object(live_stats, "_peripheriques", return_value=devs), \
                mock.patch.object(live_stats, "_cpu", side_effect=[CPU_A, CPU_B]), \
                mock.patch.object(live_stats, "_disques", return_value={}):
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

    def test_the_banner_is_there_hidden_until_a_disk_fills_up(self):
        page = self.get_dashboard("tester", ["/"])
        self.assertIn('<div id="alerte-disque" class="mt-3" hidden></div>', page)
        self.assertIn('id="carte-disque"', page)

    def test_the_minute_poller_leaves_the_live_card_alone(self):
        """/stats/disk, polled every minute, would paint a disk at 92 % red over the orange of the alert."""
        guard = "if (document.getElementById('carte-disque')) { return; }"
        page = self.get_dashboard("tester", ["/"])
        self.assertRegex(page, r"\(function diskusage\(\) \{\s*" + re.escape(guard) + r"\s*\$\.ajax\(\{")

    def test_same_rights_as_the_disk_info_card(self):
        for user, quota, shown in (("tester", False, True), ("friend", False, True),
                                   ("tester", True, True), ("friend", True, False)):
            with self.subTest(user=user, quota=quota):
                page = self.get_dashboard(user, ["/"], quota)
                self.assertEqual('id="carte-disque"' in page, shown)
                self.assertEqual('id="alerte-disque"' in page, shown)

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
        found = re.search(r"\n  function alertes\(d\) \{\n(.*?)\n  \}\n", cls.js, re.S)
        cls.banner = found.group(1) if found else ""

    def test_the_banner_function_is_there_and_used(self):
        self.assertIn("'Disk almost full: ' + m + ' is ' + pct + '% used (' + x.diskfree + ' free)'", self.banner)
        self.assertIn("alertes(d.disks)", self.js)

    def test_no_html_is_ever_inserted(self):
        """The mount path (and the free space) go through textContent: a trapped path stays plain text."""
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", ".html(", "$(",
                     "createContextualFragment", "DOMParser"):
            with self.subTest(sink=sink):
                self.assertNotIn(sink, self.js)
        self.assertIn("encart.textContent = l.texte;", self.banner)

    def test_a_long_mount_path_wraps_inside_the_banner(self):
        """A long path with no space ran out of the banner on a phone, hiding which disk is full."""
        self.assertRegex(self.banner, r"encart\.className = 'alert mb-2 text-break ' \+")

    def test_only_disks_of_the_card_raise_the_banner(self):
        """No Disk Info card (rights): no banner; a disk missing from the card does not show either."""
        self.assertIn("if (!bandeau || !carte) { return; }", self.banner)
        self.assertIn("carte.contains(document.getElementById(m + 'diskused'))", self.banner)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
