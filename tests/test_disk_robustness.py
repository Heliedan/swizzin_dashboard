"""A mount that cannot be read must not break disk statistics.

Run from the repository root: python tests/test_disk_robustness.py
(uses a throwaway htpasswd file and a fake list of mounts: nothing on the machine is touched)."""
import base64
import io
import os
import sys
import tempfile
import time
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
_ht.save()
with open(os.path.join(ROOT, "swizzin.cfg"), "w") as f:
    f.write(f"FLASK_HTPASSWD_PATH = '{_tmp}/htpasswd'\nADMIN_USER = 'tester'\n")

import swizzin  # noqa: E402
import core.util as util  # noqa: E402
from core.custom import live_stats  # noqa: E402

MISSING = "/mnt/missing disk"
AUTH = {"Authorization": "Basic " + base64.b64encode(b"tester:tester").decode()}
CPU_A = {"cpu": [1, 0, 1, 10, 0, 0, 0, 0], "cpu0": [1, 0, 1, 10, 0, 0, 0, 0]}
CPU_B = {"cpu": [2, 0, 2, 20, 0, 0, 0, 0], "cpu0": [2, 0, 2, 20, 0, 0, 0, 0]}
ZFS, NFS = "/tank/media", "/mnt/nas"
TANK = ("1.00 TiB", "972.80 GiB", "51.20 GiB", "95.00")


def fake_open(proc_mounts, fstab=""):
    real_open = open

    def _open(path, *args, **kwargs):
        if path == "/proc/mounts":
            return io.StringIO(proc_mounts)
        if path == "/etc/fstab":
            return io.StringIO(fstab)
        return real_open(path, *args, **kwargs)
    return _open


class DiskRobustness(unittest.TestCase):
    def test_mount_with_space_is_decoded(self):
        """/proc/mounts writes a space as \\040: the panel must show (and query) the real path."""
        mounts = "/dev/sda1 / ext4 rw 0 0\n/dev/sdb1 /mnt/my\\040disk ext4 rw 0 0\n"
        with mock.patch("builtins.open", fake_open(mounts)):
            self.assertEqual(util.get_mounts(), ["/", "/mnt/my disk"])

    def test_stats_route_survives_a_bad_mount(self):
        """/stats/disk must still answer for the good mounts when one of them cannot be read."""
        client = swizzin.app.test_client()
        with mock.patch.object(swizzin, "get_mounts", return_value=["/", MISSING]):
            r = client.get("/stats/disk", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertIn("/", r.get_json())
        self.assertNotIn(MISSING, r.get_json())

    def test_live_cards_survive_a_bad_mount(self):
        """One unreadable mount must not freeze the CPU and RAM live cards."""
        cpu = {"cpu": [1, 0, 1, 10, 0, 0, 0, 0], "cpu0": [1, 0, 1, 10, 0, 0, 0, 0]}
        cpu2 = {"cpu": [2, 0, 2, 20, 0, 0, 0, 0], "cpu0": [2, 0, 2, 20, 0, 0, 0, 0]}
        with swizzin.app.app_context():
            data = live_stats.mesure((cpu, {}), (cpu2, {}), 1.0, "test", "", {"/": "sda1", MISSING: "sdb1"})
        self.assertIn("cpu", data)
        self.assertIn("ram", data)
        self.assertIn("/", data["disks"])
        self.assertEqual(data["disks"][MISSING], {"unavailable": True})  # shown as « — », not stale numbers

    def test_unmounted_disk_is_unavailable(self):
        """A disk unmounted while the panel runs leaves an empty folder: it must not show the parent disk's numbers."""
        dossier = tempfile.mkdtemp()  # exists, but is not a mount point
        cpu = {"cpu": [1, 0, 1, 10, 0, 0, 0, 0]}
        cpu2 = {"cpu": [2, 0, 2, 20, 0, 0, 0, 0]}
        with swizzin.app.app_context():
            data = live_stats.mesure((cpu, {}), (cpu2, {}), 1.0, "test", "", {"/": "sda1", dossier: "sdb1"})
        self.assertEqual(data["disks"][dossier], {"unavailable": True})
        self.assertIn("disktotal", data["disks"]["/"])


class DeadShare:
    """disk_usage() of the panel, where NFS is a network share that stopped answering: reading it blocks the system
    thread that asks, as statvfs() does, until answer() (or 10 s, so that a broken test cannot hang)."""

    def __init__(self):
        self.calls = []
        self.answered = live_stats.original("threading").Event()  # a real one: it blocks a real thread

    def __call__(self, mnt):
        self.calls.append(mnt)
        if mnt == NFS:
            self.answered.wait(10)
            return "100.00 GiB", "10.00 GiB", "90.00 GiB", "10.00"
        return TANK

    def answer(self):
        self.answered.set()


class WithoutDevDevice(unittest.TestCase):
    """ZFS datasets and network shares (NFS...) have no /dev/ device. They are followed live too, read apart: a share
    that stopped answering is shown as unavailable, and never freezes the live cards or the panel."""

    def setUp(self):
        self.share = DeadShare()
        self.addCleanup(self.share.answer)  # frees the blocked thread
        for patch in (mock.patch.object(live_stats.util, "disk_usage", side_effect=self.share),
                      mock.patch.object(live_stats.os.path, "ismount", return_value=True)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_mounts_are_sorted_by_the_device_seen_on_top(self):
        mounts = ("/dev/sda1 / ext4 rw 0 0\n"
                  "tank/media /tank/media zfs rw 0 0\n"
                  "192.168.1.10:/export /mnt/nas nfs4 rw 0 0\n"
                  "/dev/sdb1 /mnt/over ext4 rw 0 0\n"
                  "192.168.1.10:/over /mnt/over nfs4 rw 0 0\n")  # a share mounted over a disk: the share is seen
        with mock.patch("builtins.open", fake_open(mounts)):
            self.assertEqual(live_stats.peripheriques(["/", ZFS, NFS, "/mnt/over", "/not/mounted"]),
                             {"/": "sda1", ZFS: None, NFS: None, "/mnt/over": None, "/not/mounted": None})
            self.assertEqual(live_stats.peripheriques(), {"/": "sda1", ZFS: None, NFS: None, "/mnt/over": None})

    def test_they_are_shown_live_with_their_alert(self):
        with swizzin.app.app_context():
            disks = live_stats.mesure((CPU_A, {}), (CPU_B, {}), 1.0, "test", "", {"/": "sda1", ZFS: None, NFS: None},
                                      (90, 95), {ZFS: TANK, NFS: None})["disks"]
        self.assertEqual(disks[ZFS], {"disktotal": "1.00 TiB", "diskused": "972.80 GiB", "diskfree": "51.20 GiB",
                                      "perutil": "95.00", "read": None, "write": None, "alert": "critical"})
        self.assertEqual(disks[NFS], {"unavailable": True})
        self.assertEqual(self.share.calls, ["/"])  # the live reading itself reads only the /dev/ disks

    def test_a_dead_share_is_never_waited_for_and_read_once(self):
        sonde = live_stats.Sonde(delai=0.3)
        start = time.monotonic()
        self.assertIsNone(sonde.releve([ZFS, NFS])[NFS])  # no answer yet
        self.assertLess(time.monotonic() - start, 0.1)  # and not waited for
        self.assertEqual(sonde.releve([ZFS, NFS], attendre=True), {ZFS: TANK, NFS: None})
        self.assertLess(time.monotonic() - start, 1)  # waited for the share 0.3 s at most
        self.assertEqual(sonde.releve([ZFS, NFS]), {ZFS: TANK, NFS: None})
        self.assertEqual(self.share.calls.count(NFS), 1)  # one blocked thread, not one more at each reading
        self.assertGreaterEqual(self.share.calls.count(ZFS), 2)
        self.share.answer()  # the share answers again: its blocked reading ends, the next one is shown
        end = time.monotonic() + 5
        while sonde.releve([NFS])[NFS] is None and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual(sonde.releve([NFS])[NFS], ("100.00 GiB", "10.00 GiB", "90.00 GiB", "10.00"))

    def test_an_unreadable_mount_is_unavailable(self):
        sonde = live_stats.Sonde(delai=5)
        with mock.patch.object(live_stats.os.path, "ismount", side_effect=lambda m: m != ZFS):  # unmounted since
            self.assertEqual(sonde.releve([ZFS], attendre=True), {ZFS: None})
        with mock.patch.object(live_stats.util, "disk_usage", side_effect=OSError("I/O error")):
            self.assertEqual(sonde.releve([ZFS], attendre=True), {ZFS: None})

    def test_the_live_cards_go_on_when_a_share_stops_answering(self):
        class Stop(Exception):
            pass

        class FakeSocketIO:
            def __init__(self):
                self.sent = []

            def emit(self, event, data, namespace=None):
                self.sent.append((time.monotonic(), data["disks"]))

            def sleep(self, seconds):
                if len(self.sent) == 4:
                    raise Stop
                time.sleep(0.1)

        sio = FakeSocketIO()
        start = time.monotonic()
        with mock.patch.object(live_stats, "SONDE", live_stats.Sonde(delai=0.25)), \
                mock.patch.object(live_stats, "peripheriques", return_value={"/": "sda1", ZFS: None, NFS: None}), \
                mock.patch.object(live_stats, "_modele", return_value=("test", "")), \
                mock.patch.object(live_stats, "_cpu", return_value=CPU_A), \
                mock.patch.object(live_stats, "_disques", return_value={}):
            with self.assertRaises(Stop):
                live_stats.live_stats(swizzin.app, sio)
        self.assertLess(time.monotonic() - start, 2)  # one reading every 0.1 s: nothing waited for the share
        self.assertEqual([disks[NFS] for _, disks in sio.sent], [{"unavailable": True}] * 4)
        self.assertEqual([disks[ZFS]["perutil"] for _, disks in sio.sent], ["95.00"] * 4)
        self.assertEqual([disks[ZFS]["alert"] for _, disks in sio.sent], ["critical"] * 4)
        self.assertEqual(self.share.calls.count(NFS), 1)

    def test_the_stats_route_leaves_a_dead_share_out_in_a_few_seconds(self):
        client = swizzin.app.test_client()
        start = time.monotonic()
        with mock.patch.object(swizzin, "SONDE", live_stats.Sonde(delai=0.3)), \
                mock.patch.object(swizzin, "get_mounts", return_value=["/", ZFS, NFS]), \
                mock.patch.object(swizzin, "peripheriques", return_value={"/": "sda1", ZFS: None, NFS: None}), \
                self.assertLogs(swizzin.app.logger, "WARNING") as log:
            r = client.get("/stats/disk", headers=AUTH)
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(sorted(r.get_json()), ["/", ZFS])
        self.assertEqual((r.get_json()[ZFS]["perutil"], r.get_json()[ZFS]["alert"]), ("95.00", "critical"))
        self.assertIn("skipping /mnt/nas (unreadable, or no answer within 0.3 seconds)", log.output[0])


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
