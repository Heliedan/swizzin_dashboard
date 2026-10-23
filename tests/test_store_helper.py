"""The App Store helper (store-helper/swizzin_store.py), run without root against fake Swizzin folders.

Run from the repository root: python tests/test_store_helper.py
(the helper is started as a subprocess on a temporary socket: nothing on the machine is touched)."""
import json
import os
import shutil
import resource
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(ROOT, "store-helper", "swizzin_store.py")


def fake_swizzin(base):
    """Install scripts, descriptions and locks laid out like on a Swizzin server."""
    os.makedirs(os.path.join(base, "scripts"))
    os.makedirs(os.path.join(base, "locks"))
    for name in ("jellyfin", "sonarr", "nginx", "letsencrypt", "Bad Name", "UPPER", "a" * 33):
        open(os.path.join(base, "scripts", name + ".sh"), "w").close()
    open(os.path.join(base, "scripts", "notes.txt"), "w").close()
    for name in ("nginx", "jellyfin", "mysite"):
        open(os.path.join(base, "locks", "." + name + ".lock"), "w").close()
    with open(os.path.join(base, "lang_en"), "w") as f:
        f.write("<?php\n$L['JELLYFIN'] = 'A TV, Film and Music player.';\n"
                "$L['SONARR'] = 'Watches series and it\\'s great ';\n$L['CALIBREWEB'] = 'Not calibre';\n")


class Helper:
    def __init__(self, base, allow_uid=None, extra=(), ignore_sigint=False, max_file_size=None, env=None, marker=None):
        self.path = os.path.join(base, "store.sock")
        uid = os.getuid() if allow_uid is None else allow_uid
        try:
            os.unlink(self.path)  # a previous helper's socket must not be mistaken for this one's
        except FileNotFoundError:
            pass
        # The file of Heliedan's edition of the panel without which the helper does not run (PANEL_MARKER):
        # there, unless the test gives its own path.
        self.marker = marker or os.path.join(base, "panel", "store-helper", "swizzin_store.py")
        if marker is None:
            os.makedirs(os.path.dirname(self.marker), exist_ok=True)
            with open(self.marker, "w") as f:
                f.write("# the panel's copy of the helper\n")
        self.proc = subprocess.Popen(
            [sys.executable, "-I", "-B", HELPER, "--socket", self.path, "--scripts", os.path.join(base, "scripts"),
             "--lang", os.path.join(base, "lang_en"), "--locks", os.path.join(base, "locks"),
             "--allow-uid", str(uid), "--logs", os.path.join(base, "logs"), "--panel-marker", self.marker, *extra],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env,
            preexec_fn=lambda: self._limits(ignore_sigint, max_file_size))
        for _ in range(100):
            if os.path.exists(self.path) or self.proc.poll() is not None:
                break
            time.sleep(0.05)

    @staticmethod
    def _limits(ignore_sigint, max_file_size):
        if ignore_sigint:  # like a background job of a shell script: SIGINT ignored, inherited by everything it starts
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        if max_file_size:  # a full disk: writing past this size fails (EFBIG, as ENOSPC would)
            resource.setrlimit(resource.RLIMIT_FSIZE, (max_file_size, max_file_size))

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(self.path)
        return s

    def stop(self, timeout=5):
        self.proc.terminate()
        return self.proc.communicate(timeout=timeout)[1].decode()


def ask(s, raw):
    """The answer, or None when the helper closed the connection instead."""
    try:
        s.sendall(raw)
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                return None
            data += chunk
    except (ConnectionResetError, BrokenPipeError):  # closed with our request still unread
        return None
    return json.loads(data)


class StoreHelper(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        fake_swizzin(self.base)
        self.helper = Helper(self.base)

    def tearDown(self):
        self.helper.stop()
        shutil.rmtree(self.base)

    def test_catalogue(self):
        with self.helper.connect() as s:
            reply = ask(s, b'{"op": "catalogue"}\n')
        self.assertTrue(reply["ok"])
        self.assertIsNone(reply["job"])
        apps = {a["name"]: a for a in reply["apps"]}
        # odd file names are left out; locks of non-Swizzin apps (mysite) do not add entries
        self.assertEqual(sorted(apps), ["jellyfin", "letsencrypt", "nginx", "sonarr"])
        self.assertEqual(apps["jellyfin"]["description"], "A TV, Film and Music player.")
        self.assertEqual(apps["sonarr"]["description"], "Watches series and it's great")
        self.assertEqual(apps["letsencrypt"]["description"], "")  # an exact key, not a prefix
        self.assertEqual({n for n, a in apps.items() if a["installed"]}, {"jellyfin", "nginx"})
        self.assertEqual({n for n, a in apps.items() if a["blocked"]}, {"letsencrypt", "nginx"})
        self.assertIn("install", reply["features"])  # the panel shows only what this version can do

    def test_list_is_read_again_at_each_request(self):
        with self.helper.connect() as s:
            ask(s, b'{"op": "catalogue"}\n')
            open(os.path.join(self.base, "scripts", "radarr.sh"), "w").close()
            open(os.path.join(self.base, "locks", ".sonarr.lock"), "w").close()
            apps = {a["name"]: a for a in ask(s, b'{"op": "catalogue"}\n')["apps"]}
        self.assertIn("radarr", apps)
        self.assertTrue(apps["sonarr"]["installed"])

    def test_bad_requests_are_answered_and_the_connection_stays_usable(self):
        with self.helper.connect() as s:
            for raw in (b"not json\n", b"[1, 2]\n", b'{"op": "shell", "cmd": "id"}\n', b'{"op": "rm -rf /"}\n'):
                self.assertEqual(ask(s, raw), {"ok": False, "error": "bad-request"})
            self.assertTrue(ask(s, b'{"op": "catalogue"}\n')["ok"])

    def test_socket_is_not_readable_by_others(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.helper.path).st_mode), 0o660)

    def test_line_too_long_drops_the_client(self):
        with self.helper.connect() as s:
            s.sendall(b"x" * (70 * 1024))
            self.assertEqual(s.recv(10), b"")  # closed by the helper
        with self.helper.connect() as s:  # the helper itself is fine
            self.assertTrue(ask(s, b'{"op": "catalogue"}\n')["ok"])

    @unittest.skipIf(os.geteuid() == 0, "as root, the test connects as uid 0, which the helper always lets in")
    def test_other_users_are_refused(self):
        self.helper.stop()
        self.helper = Helper(self.base, allow_uid=os.getuid() + 12345)
        with self.helper.connect() as s:
            self.assertIsNone(ask(s, b'{"op": "catalogue"}\n'))
        self.assertIn("connection refused (uid {}".format(os.getuid()), self.helper.stop())
        self.helper = Helper(self.base)

    @unittest.skipIf(os.geteuid() == 0, "as root, the test connects as uid 0, which the 8-client limit does not count")
    def test_at_most_eight_clients(self):
        clients = [self.helper.connect() for _ in range(8)]
        try:
            for s in clients:  # each one is really accepted
                self.assertTrue(ask(s, b'{"op": "catalogue"}\n')["ok"])
            with self.helper.connect() as extra:
                self.assertIsNone(ask(extra, b'{"op": "catalogue"}\n'))
        finally:
            for s in clients:
                s.close()

    def test_custom_blocked_list(self):
        self.helper.stop()
        self.helper = Helper(self.base, extra=("--blocked", "sonarr"))
        with self.helper.connect() as s:
            apps = {a["name"]: a for a in ask(s, b'{"op": "catalogue"}\n')["apps"]}
        self.assertEqual({n for n, a in apps.items() if a["blocked"]}, {"sonarr"})


class PanelMarker(unittest.TestCase):
    """The helper serves Heliedan's edition of the panel only: without the panel's copy of its source (PANEL_MARKER,
    deleted by box remove panel and by box upgrade panel), it does not start, and once it is gone, it stops."""

    def setUp(self):
        self.base = tempfile.mkdtemp()
        fake_swizzin(self.base)
        self.helper = None

    def tearDown(self):
        if self.helper is not None and self.helper.proc.poll() is None:
            self.helper.stop()
        shutil.rmtree(self.base)

    def refused(self, marker):
        helper = Helper(self.base, marker=marker)
        err = helper.proc.communicate(timeout=10)[1].decode()
        self.assertEqual(helper.proc.returncode, 0)  # 0: systemd does not start it again (Restart=on-failure)
        self.assertIn("not started: %s is missing" % marker, err)
        self.assertFalse(os.path.exists(helper.path))  # nothing was created: no socket, no log folder
        self.assertFalse(os.path.exists(os.path.join(self.base, "logs")))

    def test_does_not_start_without_the_panel(self):
        self.refused(os.path.join(self.base, "gone", "store-helper", "swizzin_store.py"))

    def test_only_a_file_that_is_not_empty_counts(self):
        real = os.path.join(self.base, "real.py")
        with open(real, "w") as f:
            f.write("x\n")
        link = os.path.join(self.base, "link.py")
        os.symlink(real, link)  # the panel user could point it at any file: root does not follow it
        self.refused(link)
        empty = os.path.join(self.base, "empty.py")
        open(empty, "w").close()
        self.refused(empty)
        folder = os.path.join(self.base, "folder.py")
        os.mkdir(folder)
        self.refused(folder)

    def test_stops_by_itself_once_the_panel_is_gone(self):
        self.helper = Helper(self.base, extra=("--marker-every", "0.2"))
        with self.helper.connect() as s:
            self.assertTrue(ask(s, b'{"op": "catalogue"}\n')["ok"])
            time.sleep(1)  # many looks while the panel is there: it keeps serving
            self.assertTrue(ask(s, b'{"op": "catalogue"}\n')["ok"])
            os.unlink(self.helper.marker)  # box remove panel, or box upgrade panel
            err = self.helper.proc.communicate(timeout=5)[1].decode()
        self.assertEqual(self.helper.proc.returncode, 0)
        self.assertIn("stopping: %s is gone" % self.helper.marker, err)
        self.assertFalse(os.path.exists(self.helper.path))

    def test_a_single_miss_is_not_enough(self):
        """git replaces the file when the panel is updated: the panel is gone only at two looks in a row."""
        sys.path.insert(0, os.path.dirname(HELPER))
        import swizzin_store
        marker = os.path.join(self.base, "marker.py")
        watch = types.SimpleNamespace(opts=types.SimpleNamespace(panel_marker=marker), marker_misses=0)
        looks = []
        for present in (True, False, True, False, True, False, False, False):
            if present:
                with open(marker, "w") as f:
                    f.write("x\n")
            elif os.path.exists(marker):
                os.unlink(marker)
            looks.append(swizzin_store.Helper._panel_gone(watch))
        self.assertEqual(looks, [False, False, False, False, False, False, True, True])


if __name__ == "__main__":
    unittest.main(verbosity=2)
