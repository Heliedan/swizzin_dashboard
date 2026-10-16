"""A mount that cannot be read must not break disk statistics.

Run from the repository root: python tests/test_disk_robustness.py
(uses a throwaway htpasswd file and a fake list of mounts: nothing on the machine is touched)."""
import base64
import io
import os
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
_ht.save()
with open(os.path.join(ROOT, "swizzin.cfg"), "w") as f:
    f.write(f"FLASK_HTPASSWD_PATH = '{_tmp}/htpasswd'\nADMIN_USER = 'tester'\n")

import swizzin  # noqa: E402
import core.util as util  # noqa: E402
from core.custom import live_stats  # noqa: E402

MISSING = "/mnt/missing disk"
AUTH = {"Authorization": "Basic " + base64.b64encode(b"tester:tester").decode()}


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


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
