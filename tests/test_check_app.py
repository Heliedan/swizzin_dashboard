"""The App Store's own check of an installed app (store-helper/check_app.py), for the apps whose Swizzin check is
wrong, on the fake system of the uninstall tests (fake systemctl, ss and nginx: nothing on the machine is touched).
Run from the repository root: python tests/test_check_app.py
"""
import contextlib
import io
import os
import shutil
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from test_remove_runner import World  # noqa: E402
import check_app  # noqa: E402  (store-helper is on the path, put there by test_remove_runner)


class Check(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.state["units"] = {}
        self.w.save()
        env = mock.patch.dict(os.environ, self.w.env())
        env.start()
        self.addCleanup(env.stop)
        self.owner = {}  # pid -> the service whose cgroup holds it (read from /proc on a real server)
        cgroup = mock.patch.object(check_app, "cgroup_has", lambda pid, unit: self.owner.get(int(pid)) == unit)
        cgroup.start()
        self.addCleanup(cgroup.stop)

    def tearDown(self):
        os.umask(self.w.umask)
        shutil.rmtree(self.w.base, ignore_errors=True)

    def service(self, name, active="active", port=None, pid=None):
        if "@" in name:  # an instance is known as long as its template is there
            self.w.write("/etc/systemd/system/%s@.service" % name.split("@")[0], "[Service]\n")
        state = self.w.load()
        state["units"][name] = {"active": active}
        if port:
            state.setdefault("listening_pids", []).append([port, pid])
            self.owner[pid] = name
        self.w.save()

    def check(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = check_app.main(list(argv))
        return code, out.getvalue().replace("\r\n", "\n")

    def test_every_instance_runs_and_listens(self):
        self.service("jackett@alice.service", port="9117", pid=101)
        self.service("jackett@bob.service", port="9118", pid=102)
        code, out = self.check("jackett", "--wait", "0")
        self.assertEqual(code, 0)
        self.assertIn("Swizzin's own check does not work for Jackett", out)
        self.assertIn("✓ Its service runs: jackett@alice.service", out)
        self.assertIn("✓ It is listening on port 9118", out)
        self.assertIn("Jackett: checks passed.", out)
        self.assertNotIn("Waiting", out)  # ready at once: no waiting line

    def test_the_socket_cgroup_is_enough(self):
        # ss -e gives the socket's cgroup even when root may not see the process (no CAP_SYS_PTRACE, in a container)
        self.service("jackett@alice.service")
        state = self.w.load()
        state["listening_cgroups"] = [["9117", "/system.slice/system-jackett.slice/jackett@alice.service"],
                                      ["9118", "/system.slice/system-jackett.slice/jackett@bob.service"]]
        self.w.save()
        code, out = self.check("jackett", "--wait", "0")
        self.assertEqual(code, 0)
        self.assertIn("✓ It is listening on port 9117", out)
        self.assertNotIn("9118", out)  # bob's instance is not alice's

    def test_a_service_that_does_not_run_fails(self):
        self.service("lidarr.service", active="failed")
        code, out = self.check("lidarr", "--wait", "0")
        self.assertEqual(code, 1)
        self.assertIn("✗ Its service does not run: lidarr.service (failed)", out)
        self.assertIn("Lidarr does not seem to run", out)

    def test_one_instance_down_fails(self):
        self.service("flood@alice.service", port="3000", pid=101)
        self.service("flood@bob.service", active="inactive")
        code, out = self.check("flood", "--wait", "0")
        self.assertEqual(code, 1)
        self.assertIn("✗ Its service does not run: flood@bob.service (inactive)", out)

    def test_a_service_that_listens_on_no_port_fails(self):
        self.service("radarr.service")
        state = self.w.load()
        state["listening_pids"] = [["7878", 999]]  # another program's port
        self.w.save()
        code, out = self.check("radarr", "--wait", "0")
        self.assertEqual(code, 1)
        self.assertIn("✗ Its service runs but listens on no port: radarr.service", out)

    def test_no_service_fails(self):
        code, out = self.check("bazarr", "--wait", "0")
        self.assertEqual(code, 1)
        self.assertIn("✗ No service of Bazarr was found (bazarr.service)", out)

    def test_a_slow_app_is_waited_for(self):
        self.service("emby-server.service", port="8096", pid=101)
        self.owner.pop(101)  # not listening yet
        pauses = []

        def sleep(seconds):  # Emby is up once the check waits for it, however long the first look took
            pauses.append(seconds)
            self.owner[101] = "emby-server.service"
        # check_app's own time only: subprocess sleeps too while it waits for the fake tools
        with mock.patch.object(check_app, "time", mock.Mock(monotonic=time.monotonic, sleep=sleep)):
            code, out = self.check("emby", "--wait", "20")
        self.assertEqual(code, 0)
        self.assertIn("Waiting for Emby to start (20 seconds at most)...", out)
        self.assertIn("✓ It is listening on port 8096", out)
        self.assertEqual(pauses, [2])  # stops waiting as soon as it is up

    def test_waiting_has_an_end(self):
        self.service("medusa.service")
        t = time.monotonic()
        code, out = self.check("medusa", "--wait", "3")
        self.assertEqual(code, 1)
        self.assertLess(time.monotonic() - t, 8)
        self.assertIn("listens on no port", out)

    def test_organizr_is_php_behind_nginx(self):
        self.w.write("/etc/nginx/apps/organizr.conf", "location /organizr {}\n")
        self.service("php8.3-fpm.service")
        apps = self.w.path("/etc/nginx/apps")
        code, out = self.check("organizr", "--nginx-apps", apps)
        self.assertEqual(code, 0)
        self.assertIn("✓ Its page is set up in the web server", out)
        self.assertIn("✓ The web server accepts its configuration", out)
        self.assertIn("✓ PHP runs: php8.3-fpm.service", out)
        os.unlink(os.path.join(apps, "organizr.conf"))
        state = self.w.load()
        state["nginx_ok"] = False
        state["units"]["php8.3-fpm.service"]["active"] = "failed"
        self.w.save()
        code, out = self.check("organizr", "--nginx-apps", apps)
        self.assertEqual(code, 1)
        for line in ("✗ Its page is not set up", "✗ The web server refuses", "✗ PHP (php-fpm), which runs Organizr"):
            self.assertIn(line, out)

    def test_an_unknown_app_is_refused(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.check("airsonic")

    def test_every_app_names_its_service(self):
        for app, base in check_app.SERVICES.items():
            self.assertTrue(base is None or base.replace("@", "").replace("-", "").isalnum(), app)
        self.assertEqual(set(check_app.NAMES), set(check_app.SERVICES))  # and is named as on the page


if __name__ == "__main__":
    unittest.main()
