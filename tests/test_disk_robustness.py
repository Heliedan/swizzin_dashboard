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
USB = NFS + "/usb"  # a /dev/ disk mounted inside the network share
TANK = ("1.00 TiB", "972.80 GiB", "51.20 GiB", "95.00")
NAS = ("100.00 GiB", "10.00 GiB", "90.00 GiB", "10.00")
STICK = ("500.00 GiB", "460.00 GiB", "40.00 GiB", "92.00")
SHARE_WITH_A_DISK = ("/dev/sda1 / ext4 rw 0 0\n"
                     "192.168.1.10:/export /mnt/nas nfs4 rw 0 0\n"
                     "/dev/sdb1 /mnt/nas/usb ext4 rw 0 0\n")
REAL_SLEEP = live_stats.original("time").sleep  # blocks the system thread that calls it, as a slow disk does


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
            data = live_stats.measure((cpu, {}), (cpu2, {}), 1.0, "test", "", {"/": "sda1", MISSING: "sdb1"})
        self.assertIn("cpu", data)
        self.assertIn("ram", data)
        self.assertIn("/", data["disks"])
        self.assertEqual(data["disks"][MISSING], {"unavailable": True})  # shown as "—", not stale numbers

    def test_unmounted_disk_is_unavailable(self):
        """A disk unmounted while the panel runs leaves an empty folder: it must not show the parent disk's numbers."""
        folder = tempfile.mkdtemp()  # exists, but is not a mount point
        cpu = {"cpu": [1, 0, 1, 10, 0, 0, 0, 0]}
        cpu2 = {"cpu": [2, 0, 2, 20, 0, 0, 0, 0]}
        with swizzin.app.app_context():
            data = live_stats.measure((cpu, {}), (cpu2, {}), 1.0, "test", "", {"/": "sda1", folder: "sdb1"})
        self.assertEqual(data["disks"][folder], {"unavailable": True})
        self.assertIn("disktotal", data["disks"]["/"])


class DeadShare:
    """disk_usage() of the panel, where NFS is a network share that stopped answering: reading it blocks the system
    thread that asks, as statvfs() does, until answer() (or 10 s, so that a broken test cannot hang). So does reading
    a disk mounted inside it (USB): the path lookup crosses the share first.
    answers: readings of NFS (and of USB) answered before it stops answering. slow: seconds a reading of ZFS takes."""

    def __init__(self):
        self.calls = []
        self.answers = 0
        self.slow = 0
        self.answered = live_stats.original("threading").Event()  # a real one: it blocks a real thread

    def __call__(self, mnt):
        self.calls.append(mnt)
        if mnt in (NFS, USB):
            if self.calls.count(mnt) > self.answers:
                self.answered.wait(10)
            return NAS if mnt == NFS else STICK
        REAL_SLEEP(self.slow)
        return TANK

    def answer(self):
        self.answered.set()


def no_thread_can_start():
    """The Probe's threads, as at the limit of processes: starting one raises RuntimeError."""
    thread = mock.MagicMock()
    thread.return_value.start.side_effect = RuntimeError("can't start new thread")
    return mock.patch.object(live_stats, "_Thread", thread)


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
            self.assertEqual(live_stats.mount_devices(["/", ZFS, NFS, "/mnt/over", "/not/mounted"]),
                             {"/": "sda1", ZFS: None, NFS: None, "/mnt/over": None, "/not/mounted": None})
            self.assertEqual(live_stats.mount_devices(), {"/": "sda1", ZFS: None, NFS: None, "/mnt/over": None})

    def test_they_are_shown_live_with_their_alert(self):
        with swizzin.app.app_context():
            disks = live_stats.measure((CPU_A, {}), (CPU_B, {}), 1.0, "test", "", {"/": "sda1", ZFS: None, NFS: None},
                                      (90, 95), {ZFS: TANK, NFS: None})["disks"]
        self.assertEqual(disks[ZFS], {"disktotal": "1.00 TiB", "diskused": "972.80 GiB", "diskfree": "51.20 GiB",
                                      "perutil": "95.00", "read": None, "write": None, "alert": "critical"})
        self.assertEqual(disks[NFS], {"unavailable": True})
        self.assertEqual(self.share.calls, ["/"])  # the live reading itself reads only the /dev/ disks

    def test_a_dead_share_is_never_waited_for_and_read_once(self):
        probe = live_stats.Probe(timeout=0.3)
        start = time.monotonic()
        self.assertIsNone(probe.read([ZFS, NFS])[NFS])  # no answer yet
        self.assertLess(time.monotonic() - start, 0.1)  # and not waited for
        self.assertEqual(probe.read([ZFS, NFS], wait=True), {ZFS: TANK, NFS: None})
        self.assertLess(time.monotonic() - start, 1)  # waited for the share 0.3 s at most
        self.assertEqual(probe.read([ZFS, NFS]), {ZFS: TANK, NFS: None})
        self.assertEqual(self.share.calls.count(NFS), 1)  # one blocked thread, not one more at each reading
        self.assertGreaterEqual(self.share.calls.count(ZFS), 2)
        self.share.answer()  # the share answers again: its blocked reading ends, the next one is shown
        end = time.monotonic() + 5
        while probe.read([NFS])[NFS] is None and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual(probe.read([NFS])[NFS], NAS)

    def test_a_share_that_stops_answering_shows_its_last_answer_then_is_unavailable(self):
        """The live cards show its last answer meanwhile, never one older than the delay."""
        self.share.answers = 1
        probe = live_stats.Probe(timeout=0.5)
        self.assertEqual(probe.read([NFS], wait=True), {NFS: NAS})  # it answers once
        start = time.monotonic()
        self.assertEqual(probe.read([NFS]), {NFS: NAS})  # its next reading blocks: the last answer, at once
        self.assertLess(time.monotonic() - start, 0.1)
        while time.monotonic() - start < 0.6:
            time.sleep(0.01)
        self.assertEqual(probe.read([NFS]), {NFS: None})  # silent for longer than the delay
        self.assertEqual(probe.read([NFS], wait=True), {NFS: None})  # /stats/disk does not wait for it again
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(self.share.calls.count(NFS), 2)

    def test_a_disk_mounted_inside_a_share_is_read_apart_too(self):
        """Reaching /mnt/nas/usb crosses /mnt/nas: on a dead share, the path lookup waits before the disk is even read.
        Every mount of /proc/mounts counts, even one the panel does not show (sshfs)."""
        mounts = (SHARE_WITH_A_DISK +
                  "/dev/sdc1 /mnt/nas2 ext4 rw 0 0\n"  # beside the share, not inside: read directly
                  "me@host:/home /mnt/ssh fuse.sshfs rw 0 0\n"
                  "/dev/sdd1 /mnt/ssh/disk ext4 rw 0 0\n"
                  "192.168.1.10:/top /mnt/top nfs4 rw 0 0\n"
                  "/dev/sde1 /mnt/top ext4 rw 0 0\n")  # a disk over a share: the share may still be crossed
        with mock.patch("builtins.open", fake_open(mounts)):
            devs = live_stats.mount_devices()
            self.assertEqual(devs, {"/": "sda1", NFS: None, USB: "sdb1", "/mnt/nas2": "sdc1",
                                    "/mnt/ssh/disk": "sdd1", "/mnt/top": "sde1"})  # devices kept for the speeds
            self.assertEqual(live_stats.mounts_read_apart(devs), [NFS, USB, "/mnt/ssh/disk", "/mnt/top"])
            self.assertEqual(live_stats.mounts_read_apart({"/": "sda1", "/not/mounted": None}), ["/not/mounted"])

    def test_with_no_dev_device_at_the_root_every_disk_is_read_apart(self):
        """/ on NFS (a server without a disk of its own), overlay (a container)...: reaching any disk crosses it."""
        for root in ("192.168.1.10:/root / nfs4 rw 0 0\n", "overlay / overlay rw 0 0\n"):
            with mock.patch("builtins.open", fake_open(root + "/dev/sdb1 /mnt/data ext4 rw 0 0\n")):
                self.assertEqual(live_stats.mounts_read_apart({"/mnt/data": "sdb1"}), ["/mnt/data"], root)

    def test_a_disk_inside_a_share_whose_path_holds_a_space_is_read_apart(self):
        """/proc/mounts writes a space as \\040: the share's path is decoded before it is compared."""
        mounts = ("/dev/sda1 / ext4 rw 0 0\n"
                  "192.168.1.10:/export /mnt/my\\040nas nfs4 rw 0 0\n"
                  "/dev/sdb1 /mnt/my\\040nas/usb ext4 rw 0 0\n")
        with mock.patch("builtins.open", fake_open(mounts)):
            self.assertEqual(live_stats.mounts_read_apart(live_stats.mount_devices()), ["/mnt/my nas", "/mnt/my nas/usb"])

    def test_a_disk_inside_a_share_keeps_its_speeds(self):
        with swizzin.app.app_context():
            disks = live_stats.measure((CPU_A, {"sdb1": (0, 0)}), (CPU_B, {"sdb1": (4096, 1024)}), 1.0, "test", "",
                                      {"/": "sda1", NFS: None, USB: "sdb1"}, (90, 95), {NFS: NAS, USB: STICK})["disks"]
        self.assertEqual(disks[USB], {"disktotal": "500.00 GiB", "diskused": "460.00 GiB", "diskfree": "40.00 GiB",
                                      "perutil": "92.00", "read": "2.00 MiB/s", "write": "512.00 KiB/s",
                                      "alert": "warning"})
        self.assertEqual(self.share.calls, ["/"])  # read by the Probe, not here

    def test_the_live_cards_go_on_when_a_share_with_a_disk_inside_stops_answering(self):
        class Stop(Exception):
            pass

        class FakeSocketIO:
            def __init__(self):
                self.sent = []

            def emit(self, event, data, namespace=None):
                self.sent.append(data["disks"])

            def sleep(self, seconds):
                if len(self.sent) == 4:
                    raise Stop
                time.sleep(0.1)

        sio = FakeSocketIO()
        start = time.monotonic()
        with mock.patch("builtins.open", fake_open(SHARE_WITH_A_DISK)), \
                mock.patch.object(live_stats, "PROBE", live_stats.Probe(timeout=2)), \
                mock.patch.object(live_stats, "_model", return_value=("test", "")), \
                mock.patch.object(live_stats, "_cpu", return_value=CPU_A), \
                mock.patch.object(live_stats, "_disks", return_value={}):
            with self.assertRaises(Stop):
                live_stats.live_stats(swizzin.app, sio)
        self.assertLess(time.monotonic() - start, 2)  # one reading every 0.1 s: nothing waited for the share
        self.assertEqual([disks[USB] for disks in sio.sent], [{"unavailable": True}] * 4)
        self.assertEqual([disks[NFS] for disks in sio.sent], [{"unavailable": True}] * 4)
        self.assertEqual(self.share.calls.count(USB), 1)  # one blocked thread

    def test_waiting_stops_at_the_delay_even_when_the_live_loop_starts_new_readings(self):
        """A slow share (0.4 s a reading, 0.5 s allowed): while /stats/disk waits, the live loop starts a new reading as
        soon as the last one ends. The route must not wait for those too: it stops at its start + the delay at most."""
        self.share.slow = 0.4
        probe = live_stats.Probe(timeout=0.5)
        start = time.monotonic()

        class LiveLoop:  # the time module seen by the Probe: each pause of the wait lets the live loop run
            monotonic = staticmethod(time.monotonic)

            @staticmethod
            def sleep(seconds):
                time.sleep(seconds)
                if time.monotonic() - start < 2:  # then it stops, so that a wait without end ends all the same
                    probe.read([ZFS])
        with mock.patch.object(live_stats, "time", LiveLoop):
            self.assertEqual(probe.read([ZFS], wait=True), {ZFS: TANK})
        self.assertLess(time.monotonic() - start, 0.5 + 0.2)
        self.assertGreaterEqual(self.share.calls.count(ZFS), 2)  # the live loop did start another one meanwhile

    def test_a_reading_that_cannot_start_counts_as_no_answer(self):
        """At the limit of processes (pids.max of the service...), starting a thread raises RuntimeError."""
        probe = live_stats.Probe(timeout=5)
        self.assertEqual(probe.read([ZFS], wait=True), {ZFS: TANK})  # it answered once
        with no_thread_can_start():
            start = time.monotonic()
            self.assertEqual(probe.read([ZFS, NFS]), {ZFS: None, NFS: None})  # not its last answer either
            self.assertEqual(probe.read([ZFS], wait=True), {ZFS: None})
            self.assertLess(time.monotonic() - start, 0.1)  # nothing waited for
        self.assertEqual(probe.read([ZFS], wait=True), {ZFS: TANK})  # threads start again: it comes back

    def test_the_live_cards_go_on_when_no_thread_can_start(self):
        class Stop(Exception):
            pass

        class FakeSocketIO:
            def __init__(self):
                self.sent, self.sleeps = [], 0

            def emit(self, event, data, namespace=None):
                self.sent.append(data)

            def sleep(self, seconds):
                self.sleeps += 1
                if self.sleeps == 4:
                    raise Stop

        sio = FakeSocketIO()
        with no_thread_can_start(), \
                mock.patch.object(live_stats, "PROBE", live_stats.Probe(timeout=2)), \
                mock.patch.object(live_stats, "mount_devices", return_value={"/": "sda1", ZFS: None}), \
                mock.patch.object(live_stats, "mounts_read_apart", return_value=[ZFS]), \
                mock.patch.object(live_stats, "_model", return_value=("test", "")), \
                mock.patch.object(live_stats, "_cpu", return_value=CPU_A), \
                mock.patch.object(live_stats, "_disks", return_value={}):
            with self.assertRaises(Stop):
                live_stats.live_stats(swizzin.app, sio)
        self.assertEqual(len(sio.sent), 3)  # CPU and RAM go on: the 1st reading only prepares the 2nd one
        for data in sio.sent:
            self.assertEqual(set(data), {"cpu", "ram", "disks"})
            self.assertEqual(data["disks"][ZFS], {"unavailable": True})
            self.assertEqual(data["disks"]["/"]["perutil"], "95.00")

    def test_the_stats_route_answers_when_no_thread_can_start(self):
        client = swizzin.app.test_client()
        with no_thread_can_start(), \
                mock.patch.object(swizzin, "PROBE", live_stats.Probe(timeout=0.3)), \
                mock.patch.object(swizzin, "get_mounts", return_value=["/", ZFS]), \
                mock.patch.object(swizzin, "mount_devices", return_value={"/": "sda1", ZFS: None}), \
                mock.patch.object(swizzin, "mounts_read_apart", return_value=[ZFS]), \
                self.assertLogs(swizzin.app.logger, "WARNING"):
            r = client.get("/stats/disk", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(sorted(r.get_json()), ["/"])

    def test_an_unreadable_mount_is_unavailable(self):
        probe = live_stats.Probe(timeout=5)
        with mock.patch.object(live_stats.os.path, "ismount", side_effect=lambda m: m != ZFS):  # unmounted since
            self.assertEqual(probe.read([ZFS], wait=True), {ZFS: None})
        with mock.patch.object(live_stats.util, "disk_usage", side_effect=OSError("I/O error")):
            self.assertEqual(probe.read([ZFS], wait=True), {ZFS: None})

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
        # A delay longer than the five readings: a loop that waited for the share would take 2 s or more.
        with mock.patch.object(live_stats, "PROBE", live_stats.Probe(timeout=2)), \
                mock.patch.object(live_stats, "mount_devices", return_value={"/": "sda1", ZFS: None, NFS: None}), \
                mock.patch.object(live_stats, "mounts_read_apart", return_value=[ZFS, NFS]), \
                mock.patch.object(live_stats, "_model", return_value=("test", "")), \
                mock.patch.object(live_stats, "_cpu", return_value=CPU_A), \
                mock.patch.object(live_stats, "_disks", return_value={}):
            with self.assertRaises(Stop):
                live_stats.live_stats(swizzin.app, sio)
        self.assertLess(time.monotonic() - start, 2)  # one reading every 0.1 s: nothing waited for the share
        self.assertEqual([disks[NFS] for _, disks in sio.sent], [{"unavailable": True}] * 4)
        self.assertEqual([disks[ZFS]["perutil"] for _, disks in sio.sent], ["95.00"] * 4)
        self.assertEqual([disks[ZFS]["alert"] for _, disks in sio.sent], ["critical"] * 4)
        self.assertEqual(self.share.calls.count(NFS), 1)

    def test_the_stats_route_leaves_a_dead_share_out_in_a_few_seconds(self):
        """It waits for the answers (ZFS takes 0.1 s here), and for the dead share until the delay only."""
        self.share.slow = 0.1
        client = swizzin.app.test_client()
        start = time.monotonic()
        with mock.patch.object(swizzin, "PROBE", live_stats.Probe(timeout=0.3)), \
                mock.patch.object(swizzin, "get_mounts", return_value=["/", ZFS, NFS]), \
                mock.patch.object(swizzin, "mount_devices", return_value={"/": "sda1", ZFS: None, NFS: None}), \
                mock.patch.object(swizzin, "mounts_read_apart", return_value=[ZFS, NFS]), \
                self.assertLogs(swizzin.app.logger, "WARNING") as log:
            r = client.get("/stats/disk", headers=AUTH)
        self.assertGreaterEqual(time.monotonic() - start, 0.3)
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(sorted(r.get_json()), ["/", ZFS])
        self.assertEqual((r.get_json()[ZFS]["perutil"], r.get_json()[ZFS]["alert"]), ("95.00", "critical"))
        self.assertIn("skipping /mnt/nas (unreadable, or no answer within 0.3 seconds)", log.output[0])


    def test_the_stats_route_leaves_a_disk_inside_a_dead_share_out_in_a_few_seconds(self):
        client = swizzin.app.test_client()
        start = time.monotonic()
        with mock.patch("builtins.open", fake_open(SHARE_WITH_A_DISK)), \
                mock.patch.object(swizzin, "disk_usage", side_effect=self.share), \
                mock.patch.object(swizzin, "PROBE", live_stats.Probe(timeout=0.3)), \
                self.assertLogs(swizzin.app.logger, "WARNING") as log:
            r = client.get("/stats/disk", headers=AUTH)
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(sorted(r.get_json()), ["/"])
        self.assertIn("skipping /mnt/nas/usb (unreadable, or no answer within 0.3 seconds)", "\n".join(log.output))


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
