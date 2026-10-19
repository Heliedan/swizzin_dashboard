"""Uninstalling through the App Store helper: its operations, the job, the steps, stops and crashes.

The helper runs as a subprocess without root, with the fake server of test_remove_runner.py (fake root, fake
Swizzin, fake system tools). Run from the repository root: python tests/test_store_remove.py
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from test_store_helper import Helper, fake_swizzin  # noqa: E402
from test_store_install import Conn  # noqa: E402
from test_remove_runner import World, DEMO  # noqa: E402

FAKE_BOX = "#!/bin/bash\necho \"box $*\"; sleep 30\n"  # a long installation, to be busy with


class Remove(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.apps["demo"]["tested"] = ["noble"]
        self.w.write_table()
        self.os_release = os.path.join(self.w.base, "os-release")
        with open(self.os_release, "w") as f:
            f.write('PRETTY_NAME="Ubuntu 24.04.5 LTS"\nVERSION_CODENAME=noble\n')
        fake_swizzin(self.w.base)  # install scripts and descriptions for the catalogue
        open(os.path.join(self.w.base, "scripts", "demo.sh"), "w").close()
        open(os.path.join(self.w.base, "scripts", "other.sh"), "w").close()
        self.box = os.path.join(self.w.base, "box")
        with open(self.box, "w") as f:
            f.write(FAKE_BOX)
        os.chmod(self.box, 0o755)
        self.logs = os.path.join(self.w.base, "logs")
        self.conns = []
        self.start()

    def start(self, *extra):
        self.helper = Helper(self.w.base, env=self.w.env(), extra=(
            "--locks", self.w.path("/install"), "--box", self.box, "--home", self.w.work, "--logs", self.logs,
            "--swizzin", self.w.swizzin, "--archives", self.w.archives, "--root", self.w.root, "--work", self.w.work,
            "--table", os.path.join(self.w.base, "table.json"), "--test-path", self.w.bin,
            "--test-env", "STORE_TEST_ROOT=" + self.w.root, "--no-remove", os.path.join(self.w.base, "no-remove"),
            "--os-release", self.os_release,
            *extra))

    def tearDown(self):
        for c in self.conns:
            c.close()
        self.helper.stop()
        subprocess.run(["pkill", "-9", "-f", self.w.base], stderr=subprocess.DEVNULL)
        os.umask(self.w.umask)
        shutil.rmtree(self.w.base, ignore_errors=True)

    def conn(self):
        c = Conn(self.helper.path)
        self.conns.append(c)
        return c

    def catalogue(self):
        c = Conn(self.helper.path)  # closed at once: the helper takes 8 panel connections at most
        try:
            c.send(op="catalogue")
            return c.until(lambda m: "apps" in m)
        finally:
            c.close()

    def preview(self, c, app="demo"):
        c.send(op="remove-preview", app=app)
        return c.until(lambda m: "ok" in m)

    def remove(self, app="demo", confirm=None, **extra):
        c = self.conn()
        reply = self.preview(c, app)
        self.assertTrue(reply["ok"], reply)
        c.send(op="remove", app=app, ticket=reply["ticket"], confirm=confirm or app.upper(), requester="admin at 10.0.0.2",
               **extra)
        return c, c.until(lambda m: "ok" in m)

    def phases(self, c):
        seen = []
        done = c.until(lambda m: seen.append(m["phase"]) if "phase" in m else "done" in m, timeout=60)["done"]
        return seen, done

    def test_catalogue_says_which_apps_can_be_uninstalled(self):
        reply = self.catalogue()
        self.assertIn("remove", reply["features"])
        apps = {a["name"]: a for a in reply["apps"]}
        self.assertEqual(apps["demo"]["remove"], "ok")
        self.assertEqual(apps["nginx"]["remove"], "not-offered")
        self.assertIsNone(apps["other"]["remove"])  # not installed
        open(os.path.join(self.w.base, "no-remove"), "w").close()  # root turned uninstalling off
        self.assertEqual({a["name"]: a for a in self.catalogue()["apps"]}["demo"]["remove"], "disabled")
        os.unlink(os.path.join(self.w.base, "no-remove"))
        self.w.apps["demo"]["refuse_if"] = ["other"]  # its removal would harm "other" (qBittorrent and Deluge)
        self.w.write_table()
        open(self.w.path("/install/.other.lock"), "w").close()
        self.assertEqual({a["name"]: a for a in self.catalogue()["apps"]}["demo"]["remove"], "conflict")
        c = self.conn()
        c.send(op="remove-preview", app="demo")
        self.assertEqual(c.until(lambda m: "error" in m)["error"], "conflict")  # no confirmation to fill for nothing
        os.unlink(self.w.path("/install/.other.lock"))
        self.w.apps["demo"]["refuse_if"] = []
        self.w.write_table()
        with open(os.path.join(self.w.swizzin, "scripts", "remove", "demo.sh"), "a") as f:
            f.write("# changed by an update of Swizzin\n")
        self.assertEqual({a["name"]: a for a in self.catalogue()["apps"]}["demo"]["remove"], "script-changed")
        with open(self.os_release, "w") as f:  # tested on Ubuntu 24.04 only
            f.write("VERSION_CODENAME=bookworm\n")
        self.assertEqual({a["name"]: a for a in self.catalogue()["apps"]}["demo"]["remove"], "other-system")
        c = self.conn()
        c.send(op="remove-preview", app="demo")
        self.assertEqual(c.until(lambda m: "error" in m)["error"], "not-removable")
        os.unlink(self.os_release)  # a system that does not say what it is
        self.assertEqual({a["name"]: a for a in self.catalogue()["apps"]}["demo"]["remove"], "other-system")
        self.w.apps["demo"]["tested"] = []
        self.w.write_table()
        self.assertEqual({a["name"]: a for a in self.catalogue()["apps"]}["demo"]["remove"], "untested")

    def test_preview_then_removal_with_its_steps(self):
        c = self.conn()
        reply = self.preview(c)
        self.assertTrue(reply["ok"], reply)
        preview = reply["preview"]
        self.assertEqual((preview["app"], preview["users"], preview["services"]), ("demo", ["alice", "bob"], ["demo.service"]))
        self.assertGreater(preview["estimate"], 5000)
        self.assertFalse(preview["retry"])
        c.send(op="remove", app="demo", ticket=reply["ticket"], confirm=" Demo ", requester="admin")
        started = c.until(lambda m: "ok" in m)
        self.assertTrue(started["ok"], started)
        self.assertEqual((started["started"]["action"], started["started"]["phase"]), ("remove", "check"))
        seen, done = self.phases(c)
        order = ["stop", "copy", "remove", "fix", "verify"]  # quick steps may pass between two looks of the helper
        self.assertEqual(seen, [p for p in order if p in seen])
        for step in ("1/5", "2/5", "3/5", "4/5", "5/5"):
            self.assertIn("[App Store] " + step, c.output)
        self.assertEqual(done["result"], "removed", c.output)
        self.assertTrue(all(check["ok"] for check in done["checks"]))
        self.assertTrue(done["archive"].startswith(self.w.archives))
        self.assertIn("demo removed", c.output)  # Swizzin's own output, relayed
        catalogue = self.catalogue()
        self.assertEqual(catalogue["last"]["result"], "removed")
        self.assertFalse({a["name"]: a for a in catalogue["apps"]}["demo"]["installed"])
        with open(glob.glob(os.path.join(self.logs, "*.json"))[0]) as f:
            self.assertEqual(json.load(f)["result"], "removed")

    def test_tickets_and_the_typed_name_are_checked(self):
        c = self.conn()
        c.send(op="remove", app="demo", confirm="demo")
        self.assertEqual(c.until(lambda m: "error" in m)["error"], "bad-ticket")
        ticket = self.preview(c)["ticket"]
        c.send(op="remove", app="demo", ticket=ticket, confirm="dmeo")
        self.assertEqual(c.until(lambda m: "error" in m)["error"], "confirm-mismatch")
        c.send(op="remove", app="demo", ticket=ticket, confirm="demo")  # a ticket is used once
        self.assertEqual(c.until(lambda m: "error" in m)["error"], "bad-ticket")
        self.assertTrue(os.path.exists(self.w.path("/install/.demo.lock")))

    def test_refusals(self):
        c = self.conn()
        for app, error in (("../demo", "invalid"), ("nothere", "unknown"), ("nginx", "not-removable"),
                           ("other", "not-removable")):
            c.send(op="remove-preview", app=app)
            self.assertEqual(c.until(lambda m: "error" in m)["error"], error, app)
        self.w.apps["other"] = dict(self.w.apps["demo"])
        self.w.app("other", DEMO, **self.w.apps["other"])
        c.send(op="remove-preview", app="other")
        self.assertEqual(c.until(lambda m: "error" in m)["error"], "not-installed")
        c.send(op="install", app="other")  # busy with an installation
        self.assertTrue(c.until(lambda m: "ok" in m)["ok"])
        c.send(op="remove-preview", app="demo")
        self.assertEqual(c.until(lambda m: "error" in m)["error"], "busy")
        # Ended here, while the helper can still reap it. Left to tearDown, the fake box outlives the helper and
        # waits as a zombie named "box" until PID 1 collects it, which some containers do late or never:
        # the next removal would then refuse with "box is running".
        c.wait_output("box install other")  # the installer really runs: before its exec, it is not named "box" yet
        subprocess.run(["pkill", "-9", "-P", str(self.helper.proc.pid), "-x", "box"], stderr=subprocess.DEVNULL)
        c.until(lambda m: "done" in m)

    def test_an_incomplete_removal_can_be_tried_again(self):
        broken = 'rm "$STORE_TEST_ROOT/install/.demo.lock"; echo "E: halfway"; exit 1\n'  # leaves its files behind
        self.w.app("demo", broken, **self.w.apps["demo"])
        c, started = self.remove()
        self.assertTrue(started["ok"], started)
        done = self.phases(c)[1]
        self.assertEqual(done["result"], "incomplete")
        self.w.app("demo", DEMO.replace('rm "$R/install/.demo.lock"', "true"), **self.w.apps["demo"])  # fixed
        c2 = self.conn()
        reply = self.preview(c2)
        self.assertTrue(reply["ok"], reply)
        self.assertTrue(reply["preview"]["retry"])  # allowed although the lock is gone
        c2.send(op="remove", app="demo", ticket=reply["ticket"], confirm="demo")
        self.assertTrue(c2.until(lambda m: "ok" in m)["started"]["retry"])
        self.assertEqual(self.phases(c2)[1]["result"], "removed", c2.output)

    def test_stopping_the_service_before_the_script_puts_the_app_back(self):
        self.helper.stop()
        self.start("--test-env", "STORE_TEST_PAUSE=copy:3")
        c, started = self.remove()
        c.until(lambda m: m.get("phase") == "copy", timeout=30)
        self.helper.stop()  # systemctl stop swizzin-store, or a reboot
        with open(glob.glob(os.path.join(self.logs, "*.json"))[0]) as f:
            summary = json.load(f)
        self.assertEqual(summary["result"], "failed")
        self.assertEqual(self.w.load()["units"]["demo.service"]["active"], "active")
        self.assertTrue(os.path.exists(self.w.path("/install/.demo.lock")))
        self.start()

    def test_stopping_the_service_during_the_script_waits_for_its_end(self):
        self.w.app("demo", "sleep 3\n" + DEMO, **self.w.apps["demo"])
        c, started = self.remove()
        c.until(lambda m: m.get("phase") == "remove", timeout=30)
        time.sleep(1)
        self.helper.stop(timeout=60)
        with open(glob.glob(os.path.join(self.logs, "*.json"))[0]) as f:
            summary = json.load(f)
        self.assertEqual(summary["result"], "removed")
        self.assertFalse(os.path.exists(self.w.path("/install/.demo.lock")))
        self.start()

    def test_a_crash_before_the_script_is_put_back_at_the_next_start(self):
        self.helper.stop()
        self.start("--test-env", "STORE_TEST_PAUSE=copy:30")
        c, started = self.remove()
        c.until(lambda m: m.get("phase") == "copy", timeout=30)
        self.helper.proc.kill()  # the service crashes...
        subprocess.run(["pkill", "-9", "-f", "remove_runner.py --app demo"], stderr=subprocess.DEVNULL)  # ...systemd kills the rest
        self.helper.proc.wait()
        self.assertEqual(self.w.load()["units"]["demo.service"]["active"], "inactive")  # left stopped by the crash
        self.start()
        last = self.catalogue()["last"]
        self.assertEqual((last["action"], last["result"]), ("remove", "failed"))
        self.assertIn("put back", last["message"])
        state = self.w.load()["units"]["demo.service"]
        self.assertEqual((state["active"], state["enabled"]), ("active", "enabled"))

    def test_a_crash_during_the_script_is_reported_incomplete(self):
        self.w.app("demo", "sleep 30\n" + DEMO, **self.w.apps["demo"])
        c, started = self.remove()
        c.until(lambda m: m.get("phase") == "remove", timeout=30)
        time.sleep(1)
        self.helper.proc.kill()
        subprocess.run(["pkill", "-9", "-f", self.w.base + "/work"], stderr=subprocess.DEVNULL)
        subprocess.run(["pkill", "-9", "-f", "remove_runner.py --app demo"], stderr=subprocess.DEVNULL)
        self.helper.proc.wait()
        self.start()
        last = self.catalogue()["last"]
        self.assertEqual((last["action"], last["result"]), ("remove", "incomplete"))
        self.assertIn("try again", last["message"])


if __name__ == "__main__":
    unittest.main()
