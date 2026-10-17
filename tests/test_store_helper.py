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
    def __init__(self, base, allow_uid=None, extra=(), ignore_sigint=False, max_file_size=None, env=None):
        self.path = os.path.join(base, "store.sock")
        uid = os.getuid() if allow_uid is None else allow_uid
        try:
            os.unlink(self.path)  # a previous helper's socket must not be mistaken for this one's
        except FileNotFoundError:
            pass
        self.proc = subprocess.Popen(
            [sys.executable, "-I", "-B", HELPER, "--socket", self.path, "--scripts", os.path.join(base, "scripts"),
             "--lang", os.path.join(base, "lang_en"), "--locks", os.path.join(base, "locks"),
             "--allow-uid", str(uid), *extra], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env,
            preexec_fn=lambda: self._limits(ignore_sigint, max_file_size))
        for _ in range(100):
            if os.path.exists(self.path):
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

    def test_other_users_are_refused(self):
        self.helper.stop()
        self.helper = Helper(self.base, allow_uid=os.getuid() + 12345)
        with self.helper.connect() as s:
            self.assertIsNone(ask(s, b'{"op": "catalogue"}\n'))
        self.assertIn("connection refused (uid {}".format(os.getuid()), self.helper.stop())
        self.helper = Helper(self.base)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
