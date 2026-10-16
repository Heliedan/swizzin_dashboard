"""Wrong passwords must be limited, without locking out a forgotten browser tab.

Run from the repository root: python tests/test_login_limit.py
(uses a throwaway htpasswd file and a fake clock: nothing on the machine is touched)."""
import base64
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
_ht.set_password("tester", "right")
_ht.save()
with open(os.path.join(ROOT, "swizzin.cfg"), "w") as f:
    f.write(f"FLASK_HTPASSWD_PATH = '{_tmp}/htpasswd'\nADMIN_USER = 'tester'\n")

import swizzin  # noqa: E402
from core.htpasswd import FailedLogins, HtPasswdAuth  # noqa: E402

CLIENT = "203.0.113.5"  # documentation addresses (RFC 5737)
OTHER = "203.0.113.6"


def basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


class FakeClock:
    now = 1000.0

    def __call__(self):
        return self.now


class LoginLimit(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        swizzin.htpasswd.failed_logins = FailedLogins(5, 600, 900)
        swizzin.htpasswd.failed_logins.clock = self.clock
        self.client = swizzin.app.test_client()

    def get(self, password, client=CLIENT, peer="127.0.0.1", user="tester"):
        """A dashboard request as the local nginx forwards it (peer 127.0.0.1 + X-Forwarded-For)."""
        headers = {"X-Forwarded-For": client, "X-Requested-With": "XMLHttpRequest"}
        if password is not None:
            headers["Authorization"] = basic(user, password)
        return self.client.get("/stats/loadavg", headers=headers, environ_base={"REMOTE_ADDR": peer})

    def block(self, client=CLIENT):
        for i in range(5):
            self.assertEqual(self.get(f"wrong-{i}", client).status_code, 401)

    def test_same_wrong_password_never_blocks(self):
        """A tab that keeps sending an old password is not someone guessing."""
        for _ in range(20):
            self.assertEqual(self.get("old-password").status_code, 401)
        self.assertEqual(self.get("right").status_code, 200)

    def test_requests_without_credentials_do_not_count(self):
        """Browsers always ask once without credentials before sending them."""
        with mock.patch.object(swizzin.limiter, "enabled", False):  # Flask-Limiter caps anonymous requests
            for _ in range(20):
                self.assertEqual(self.get(None).status_code, 401)
        for i in range(4):
            self.get(f"wrong-{i}")
        self.assertEqual(self.get("right").status_code, 200)

    def test_different_wrong_passwords_block_the_address(self):
        """Then even the right password is refused, without being checked."""
        self.block()
        with mock.patch.object(HtPasswdAuth, "check_basic_auth", wraps=swizzin.htpasswd.check_basic_auth) as check:
            r = self.get("right")
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.headers["Retry-After"], "900")
        self.assertIn("15 minutes", r.get_data(as_text=True))
        check.assert_not_called()

    def test_other_addresses_still_work(self):
        self.block()
        self.assertEqual(self.get("right", client=OTHER).status_code, 200)

    def test_block_ends(self):
        self.block()
        self.clock.now += 899
        self.assertEqual(self.get("right").status_code, 429)
        self.clock.now += 2
        self.assertEqual(self.get("right").status_code, 200)

    def test_old_failures_are_forgotten(self):
        """Failures older than the window no longer count."""
        for i in range(4):
            self.get(f"wrong-{i}")
        self.clock.now += 601
        self.get("wrong-4")
        self.assertEqual(self.get("right").status_code, 200)

    def test_success_does_not_reset(self):
        """Otherwise one valid account could be used to guess another one."""
        for i in range(4):
            self.get(f"wrong-{i}")
        self.assertEqual(self.get("right").status_code, 200)
        self.get("wrong-4")
        self.assertEqual(self.get("right").status_code, 429)

    def test_forwarded_header_ignored_from_outside(self):
        """Reaching the panel directly, X-Forwarded-For is not believed: the real peer is counted."""
        for i in range(5):
            self.get(f"wrong-{i}", client=f"192.0.2.{i}", peer="198.51.100.7")
        self.assertEqual(self.get("right", client="192.0.2.99", peer="198.51.100.7").status_code, 429)
        self.assertEqual(self.get("right").status_code, 200)  # nginx clients are not affected

    def test_websocket_refused_while_blocked(self):
        self.block()
        # pretend the background threads already run, so that connecting does not start them
        with mock.patch.object(swizzin, "thread", object()), mock.patch.object(swizzin, "thread2", object()), \
                mock.patch.object(swizzin, "thread3", object()):
            blocked = swizzin.socketio.test_client(
                swizzin.app, namespace="/websocket", flask_test_client=self.client,
                headers={"Authorization": basic("tester", "right"), "X-Forwarded-For": CLIENT})
            other = swizzin.socketio.test_client(
                swizzin.app, namespace="/websocket", flask_test_client=self.client,
                headers={"Authorization": basic("tester", "right"), "X-Forwarded-For": OTHER})
        self.assertFalse(blocked.is_connected("/websocket"))
        self.assertTrue(other.is_connected("/websocket"))
        other.disconnect(namespace="/websocket")

    def test_zero_disables_the_limit(self):
        swizzin.htpasswd.failed_logins = FailedLogins(0, 600, 900)
        for i in range(20):
            self.get(f"wrong-{i}")
        self.assertEqual(self.get("right").status_code, 200)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
