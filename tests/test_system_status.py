"""Reboot required and pending updates (core/system_status.py): reading apt's output and /run/reboot-required, the
background check of a fake apt-get, and one run of the real apt-get when there is one.

The panel is not imported here: core/system_status.py is tested on its own, with a throwaway folder as the root of
the system (dpkg's status file, apt's lists, /run, /boot...) and a fake apt-get first in its PATH.
Run from the repository root: python tests/test_system_status.py"""
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import system_status  # noqa: E402
from core.system_status import Failure, PendingUpdates, parse, reboot_status  # noqa: E402

# The output of `LC_ALL=C apt-get -s -q dist-upgrade`, run by an account other than root in the containers of the five
# systems of the automatic tests (their package lists from 7 November 2026), as it came; then (total, security names).
REAL = {
    "ubuntu-20.04": """\
NOTE: This is only a simulation!
      apt-get needs root privileges for real execution.
      Keep also in mind that locking is deactivated,
      so don't depend on the relevance to the real current situation!
Reading package lists...
Building dependency tree...
Reading state information...
Calculating upgrade...
The following packages will be upgraded:
  apt libapt-pkg6.0 libc-bin
3 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.
Inst libc-bin [2.31-0ubuntu9.17] (2.31-0ubuntu9.18 Ubuntu:20.04/focal-updates, Ubuntu:20.04/focal-security [amd64])
Conf libc-bin (2.31-0ubuntu9.18 Ubuntu:20.04/focal-updates, Ubuntu:20.04/focal-security [amd64])
Inst libapt-pkg6.0 [2.0.10] (2.0.11 Ubuntu:20.04/focal-updates [amd64])
Conf libapt-pkg6.0 (2.0.11 Ubuntu:20.04/focal-updates [amd64])
Inst apt [2.0.10] (2.0.11 Ubuntu:20.04/focal-updates [amd64])
Conf apt (2.0.11 Ubuntu:20.04/focal-updates [amd64])
""",
    "ubuntu-22.04": """\
NOTE: This is only a simulation!
      apt-get needs root privileges for real execution.
      Keep also in mind that locking is deactivated,
      so don't depend on the relevance to the real current situation!
Reading package lists...
Building dependency tree...
Reading state information...
Calculating upgrade...
The following packages will be upgraded:
  apt libapt-pkg6.0 liblzma5 libpcre2-8-0 libsystemd0 libudev1
6 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.
Inst liblzma5 [5.2.5-2ubuntu1.1] (5.2.5-2ubuntu1.2 Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64])
Conf liblzma5 (5.2.5-2ubuntu1.2 Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64])
Inst libsystemd0 [249.11-0ubuntu3.22] (249.11-0ubuntu3.23 Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64])
Conf libsystemd0 (249.11-0ubuntu3.23 Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64])
Inst libudev1 [249.11-0ubuntu3.22] (249.11-0ubuntu3.23 Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64])
Conf libudev1 (249.11-0ubuntu3.23 Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64])
Inst libapt-pkg6.0 [2.4.14] (2.4.15ubuntu0.1 Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64])
Conf libapt-pkg6.0 (2.4.15ubuntu0.1 Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64])
Inst apt [2.4.14] (2.4.15ubuntu0.1 Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64])
Conf apt (2.4.15ubuntu0.1 Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64])
Inst libpcre2-8-0 [10.39-3ubuntu0.1] (10.39-3ubuntu0.2 Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64])
Conf libpcre2-8-0 (10.39-3ubuntu0.2 Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64])
""",
    "ubuntu-24.04": """\
NOTE: This is only a simulation!
      apt-get needs root privileges for real execution.
      Keep also in mind that locking is deactivated,
      so don't depend on the relevance to the real current situation!
Reading package lists...
Building dependency tree...
Reading state information...
Calculating upgrade...
The following packages will be upgraded:
  apt libapt-pkg6.0t64 libpcre2-8-0
3 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.
Inst libapt-pkg6.0t64 [2.8.3] (2.8.4ubuntu0.1 Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [amd64])
Conf libapt-pkg6.0t64 (2.8.4ubuntu0.1 Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [amd64])
Inst apt [2.8.3] (2.8.4ubuntu0.1 Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [amd64])
Conf apt (2.8.4ubuntu0.1 Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [amd64])
Inst libpcre2-8-0 [10.42-4ubuntu2.1] (10.42-4ubuntu2.2 Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [amd64])
Conf libpcre2-8-0 (10.42-4ubuntu2.2 Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [amd64])
""",
    "debian-11": """\
NOTE: This is only a simulation!
      apt-get needs root privileges for real execution.
      Keep also in mind that locking is deactivated,
      so don't depend on the relevance to the real current situation!
Reading package lists...
Building dependency tree...
Reading state information...
Calculating upgrade...
0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.
""",
    "debian-12": """\
NOTE: This is only a simulation!
      apt-get needs root privileges for real execution.
      Keep also in mind that locking is deactivated,
      so don't depend on the relevance to the real current situation!
Reading package lists...
Building dependency tree...
Reading state information...
Calculating upgrade...
The following packages will be upgraded:
  apt libapt-pkg6.0 tzdata
3 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.
Inst libapt-pkg6.0 [2.6.1] (2.6.2 Debian-Security:12/oldstable-security [amd64])
Conf libapt-pkg6.0 (2.6.2 Debian-Security:12/oldstable-security [amd64])
Inst apt [2.6.1] (2.6.2 Debian-Security:12/oldstable-security [amd64])
Conf apt (2.6.2 Debian-Security:12/oldstable-security [amd64])
Inst tzdata [2026c-0+deb12u1] (2026f-0+deb12u1 Debian-Security:12/oldstable-security [all])
Conf tzdata (2026f-0+deb12u1 Debian-Security:12/oldstable-security [all])
""",
}
EXPECTED = {
    "ubuntu-20.04": (3, ["libc-bin"]),
    "ubuntu-22.04": (6, ["liblzma5", "libsystemd0", "libudev1", "libapt-pkg6.0", "apt", "libpcre2-8-0"]),
    "ubuntu-24.04": (3, ["libapt-pkg6.0t64", "apt", "libpcre2-8-0"]),
    "debian-11": (0, []),
    "debian-12": (3, ["libapt-pkg6.0", "apt", "tzdata"]),
}
# Debian 13 (apt 3.0.4), from a container: apt-get kept the format of these lines.
APT_3 = """\
Calculating upgrade...
The following packages will be upgraded:
  zlib1g
1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.
Inst zlib1g [1:1.3.dfsg+really1.3.1-1+b1] (1:1.3.dfsg+really1.3.1-1+deb13u1 Debian-Security:13/stable-security [amd64])
Conf zlib1g (1:1.3.dfsg+really1.3.1-1+deb13u1 Debian-Security:13/stable-security [amd64])
"""
# A new kernel, as apt-get writes a package that is new (no [installed version]); written for this test.
NEW_KERNEL = """\
2 upgraded, 2 newly installed, 0 to remove and 0 not upgraded.
Inst linux-image-6.8.0-50-generic (6.8.0-50.51 Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [amd64])
Inst linux-modules-6.8.0-50-generic (6.8.0-50.51 Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [amd64])
Inst linux-image-generic [6.8.0-45.45] (6.8.0-50.51 Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [amd64]) []
Inst htop [3.3.0-4build1] (3.3.0-5 Ubuntu:24.04/noble-updates [amd64])
"""
UP_TO_DATE = "Calculating upgrade...\n0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
# The fake apt-get, in a folder of its own: notes its arguments, its environment and its niceness, then turns into a
# long sleep with the file "sleep", or waits for the seconds in "pause", and while the file "wait" is there (10 seconds
# at most: a test that would wait for it in vain fails instead of hanging); then writes "output" and "stderr", and
# exits with "code".
FAKE_APT_GET = """#!/bin/sh
d=$(dirname "$0")
printf '%s\\n' "$*" >> "$d/calls"
env > "$d/env"
nice > "$d/niceness"
if [ -f "$d/sleep" ]; then exec sleep "$(cat "$d/sleep")"; fi
if [ -f "$d/pause" ]; then sleep "$(cat "$d/pause")"; fi
n=0
while [ -f "$d/wait" ] && [ $n -lt 500 ]; do sleep 0.02; n=$((n + 1)); done
cat "$d/output"
if [ -f "$d/stderr" ]; then cat "$d/stderr" >&2; fi
exit "$(cat "$d/code" 2>/dev/null || echo 0)"
"""


def fake_system(root, folder, output):
    """In `root`, what apt-get depends on (dpkg's status file, apt's lists and binary cache); in `folder`, the fake
    apt-get, which writes `output`."""
    for name in ("var/lib/dpkg/status", "var/lib/apt/lists/deb.debian.org_debian_dists_bookworm_main_binary-amd64_"
                 "Packages.lz4", "var/lib/apt/lists/partial/", "var/cache/apt/pkgcache.bin"):
        os.makedirs(os.path.dirname(os.path.join(root, name)), exist_ok=True)
        if not name.endswith("/"):
            open(os.path.join(root, name), "w").close()
    for name, text in (("apt-get", FAKE_APT_GET), ("output", output)):
        with open(os.path.join(folder, name), "w") as f:
            f.write(text)
    os.chmod(os.path.join(folder, "apt-get"), 0o755)


class Parse(unittest.TestCase):
    def test_the_outputs_of_the_five_systems(self):
        for name, output in REAL.items():
            with self.subTest(name):
                self.assertEqual(parse(output), EXPECTED[name])

    def test_apt_3(self):
        self.assertEqual(parse(APT_3), (1, ["zlib1g"]))

    def test_new_packages_count_too(self):
        """dist-upgrade installs the packages of a new kernel: updates too."""
        self.assertEqual(parse(NEW_KERNEL), (4, ["linux-image-6.8.0-50-generic", "linux-modules-6.8.0-50-generic",
                                                 "linux-image-generic"]))

    def test_only_a_security_archive_makes_a_security_update(self):
        """Not a label or a repository whose name says "security": the archive, after the last "/"."""
        output = "2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n" \
                 "Inst tool [1.0-1] (1.0-2 LP-PPA-someone-security-tools:24.04/noble [amd64])\n" \
                 "Inst apt [2.6.1] (2.6.2 Debian-Security:12/oldstable [amd64])\n"
        self.assertEqual(parse(output), (2, []))

    def test_up_to_date(self):
        self.assertEqual(parse(UP_TO_DATE), (0, []))

    def test_security_is_read_from_the_archive(self):
        cases = {"Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [amd64]": True,
                 "Debian-Security:12/oldstable-security [all]": True,
                 "UbuntuESMApps:20.04/focal-apps-security [amd64]": True,
                 "Ubuntu:24.04/noble-updates [amd64]": False,
                 "Debian:12.12/oldstable [amd64]": False,
                 "Debian-Security:12/oldstable [amd64]": False,  # the label alone does not make it so
                 "LP-PPA-someone-security:24.04/noble [amd64]": False,
                 "localhost [amd64]": False,
                 " [amd64]": False}
        for origins, security in cases.items():
            with self.subTest(origins):
                self.assertEqual(system_status._security(origins), security)

    def test_an_output_that_does_not_read_as_one_is_a_failure(self):
        """Never "up to date" from an output that was not understood."""
        cases = {"no summary line": "Inst apt [2.6.1] (2.6.2 Debian:12.12/oldstable [amd64])\n",
                 "nothing": "",
                 "in another language": "Calcul de la mise à jour...\n"
                                        "3 mis à jour, 0 nouvellement installés, 0 à enlever et 0 non mis à jour.\n",
                 "fewer Inst lines than counted": "3 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
                                                  "Inst apt [2.6.1] (2.6.2 Debian:12.12/oldstable [amd64])\n",
                 "an Inst line of another form": UP_TO_DATE + "Inst apt 2.6.1 -> 2.6.2\n"}
        for name, output in cases.items():
            with self.subTest(name):
                with self.assertRaises(Failure):
                    parse(output)


class Folder(unittest.TestCase):
    """A throwaway folder as the root of the system."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)

    def path(self, name):
        return os.path.join(self.root, name)

    def write(self, name, text=""):
        os.makedirs(os.path.dirname(self.path(name)), exist_ok=True)
        with open(self.path(name), "w") as f:
            f.write(text)

    def date(self, name, seconds):
        """Gives `name` this date (seconds since 1970)."""
        os.utime(self.path(name), (seconds, seconds))


class Reboot(Folder):
    def status(self, release="6.1.0-26-amd64"):
        return reboot_status(self.root, release)

    def test_no_reboot(self):
        self.assertEqual(self.status(), {"required": False, "packages": [], "more": 0, "kernel": None,
                                         "running": None})

    def test_reboot_required_without_packages(self):
        self.write("run/reboot-required", "*** System restart required ***\n")
        self.assertEqual(self.status(), {"required": True, "packages": [], "more": 0, "kernel": None,
                                         "running": None})

    def test_reboot_required_and_the_packages_that_asked(self):
        """Each name once, in its order, only package names (the page shows them as text anyway), 10 at most."""
        self.write("run/reboot-required", "")
        names = ["linux-image-6.8.0-50-generic", "libc6", "libc6", "", "  dbus  ", "<img src=x onerror=alert(1)>",
                 "Some Words"] + ["lib%d" % i for i in range(12)]
        self.write("run/reboot-required.pkgs", "\n".join(names) + "\n")
        r = self.status()
        self.assertEqual(r["packages"], ["linux-image-6.8.0-50-generic", "libc6", "dbus", "lib0", "lib1", "lib2",
                                         "lib3", "lib4", "lib5", "lib6"])
        self.assertEqual((r["required"], r["more"]), (True, 5))

    def test_an_unreadable_list_of_packages(self):
        self.write("run/reboot-required", "")
        os.makedirs(self.path("run/reboot-required.pkgs"))  # a folder in its place
        self.assertEqual(self.status()["packages"], [])
        self.assertTrue(self.status()["required"])

    def kernels(self, *releases):
        for release in releases:
            self.write("boot/vmlinuz-" + release)

    def test_a_newer_kernel_without_the_file(self):
        """Debian without unattended-upgrades: nothing writes /run/reboot-required for a new kernel."""
        self.kernels("6.1.0-26-amd64", "6.1.0-28-amd64", "6.1.0-27-amd64")
        self.write("boot/config-6.1.0-30-amd64")
        self.assertEqual(self.status(), {"required": True, "packages": [], "more": 0, "kernel": "6.1.0-28-amd64",
                                         "running": "6.1.0-26-amd64"})

    def test_kernels_that_are_not_newer(self):
        cases = {"older": ("6.1.0-25-amd64",),
                 "another flavour": ("6.1.0-28-cloud-amd64", "6.1.0-28-rt-amd64"),
                 "not a kernel": ("6.1.0-28-amd64.dpkg-tmp", "6.1.0-28-amd64.old", "x-amd64"),
                 "none": ()}
        for name, others in cases.items():
            with self.subTest(name):
                shutil.rmtree(self.path("boot"), ignore_errors=True)
                self.kernels("6.1.0-26-amd64", *others)
                self.assertFalse(self.status()["required"])

    def test_versions_compared_as_numbers(self):
        self.kernels("6.8.0-149-generic", "6.11.0-29-generic")  # Ubuntu's HWE kernel
        self.assertEqual(self.status("6.8.0-149-generic")["kernel"], "6.11.0-29-generic")
        self.kernels("6.1.0-26-amd64", "6.12.9+bpo-amd64")  # Debian's backports
        self.assertEqual(self.status("6.1.0-26-amd64")["kernel"], "6.12.9+bpo-amd64")

    def test_never_without_the_running_kernel_in_boot(self):
        """LXC, OpenVZ, the host's own kernel: what /boot holds does not boot this system."""
        self.kernels("6.1.0-28-amd64")
        self.assertFalse(self.status("6.1.0-26-amd64")["required"])
        self.assertFalse(self.status("6.8.12-4-pve")["required"])

    def test_never_in_a_container(self):
        self.kernels("6.1.0-26-amd64", "6.1.0-28-amd64")
        self.write("run/systemd/container", "lxc\n")
        self.assertFalse(self.status()["required"])

    def test_not_when_update_notifier_or_unattended_upgrades_say_it(self):
        """They write /run/reboot-required for a new kernel: no file means no reboot (a kernel kept back by GRUB's
        settings, for example)."""
        self.kernels("6.1.0-26-amd64", "6.1.0-28-amd64")
        for hook in ("etc/kernel/postinst.d/update-notifier", "etc/kernel/postinst.d/unattended-upgrades"):
            with self.subTest(hook):
                self.write(hook)
                self.assertFalse(self.status()["required"])
                os.remove(self.path(hook))
        self.assertTrue(self.status()["required"])

    def test_kernel_releases(self):
        cases = {"6.8.0-149-generic": ("generic", (6, 8, 0, 149)),
                 "6.1.0-26-cloud-amd64": ("cloud-amd64", (6, 1, 0, 26)),
                 "6.12.9+bpo-amd64": ("amd64", (6, 12, 9)),
                 "5.15.0-1060-raspi": ("raspi", (5, 15, 0, 1060)),
                 "6.6.31+rpt-rpi-v8": ("rpi-v8", (6, 6, 31)),
                 "6.8.0": None, "generic": None, "": None, "-generic": None}
        for release, expected in cases.items():
            with self.subTest(release):
                self.assertEqual(system_status._kernel(release), expected)

    def test_the_real_system_never_fails(self):
        r = reboot_status()
        self.assertIsInstance(r["required"], bool)


class Updates(Folder):
    """PendingUpdates with a fake apt-get, a throwaway root and a clock of its own (seconds)."""

    def setUp(self):
        super().setUp()
        self.bin = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.bin)
        fake_system(self.root, self.bin, REAL["ubuntu-22.04"])
        for patcher in (mock.patch.object(system_status, "PATH", self.bin + ":" + system_status.PATH),
                        mock.patch.dict(os.environ, {"SWIZZIN_TEST_SECRET": "not for apt-get"})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.now = 1000.0
        self.logger = logging.getLogger("test_system_status")
        self.updates = PendingUpdates(self.logger, root=self.root, clock=lambda: self.now)
        self.addCleanup(self.done)  # first: a run started by the test ends before its folders go

    def output(self, text, code=0, stderr=None):
        for name, value in (("output", text), ("code", str(code)), ("stderr", stderr)):
            path = os.path.join(self.bin, name)
            if value is None:
                if os.path.exists(path):
                    os.remove(path)
            else:
                with open(path, "w") as f:
                    f.write(value)

    def hold(self, held=True):
        """While held, the fake apt-get waits before it answers."""
        path = os.path.join(self.bin, "wait")
        if held:
            open(path, "w").close()
        elif os.path.exists(path):
            os.remove(path)

    def calls(self):
        try:
            with open(os.path.join(self.bin, "calls")) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def done(self):
        """Waits until the run in the background is over."""
        end = time.monotonic() + 30
        while self.updates.running:
            self.assertLess(time.monotonic(), end, "the run never ended")
            time.sleep(0.01)

    def check(self):
        """status(), then the same once the run it started (if any) is over."""
        first = self.updates.status()
        self.done()
        return first, self.updates.status()

    def touch(self, name):
        """dpkg or apt changed `name`: a new date."""
        st = os.stat(self.path(name))
        self.date(name, st.st_mtime + 10)

    def test_never_waits_for_apt_get(self):
        self.hold()
        t = time.monotonic()
        first = self.updates.status()
        self.assertLess(time.monotonic() - t, 1)
        self.assertEqual(first, {"state": "checking", "checking": True})
        self.assertEqual(self.updates.status(), first)
        self.hold(False)
        self.done()
        after = self.updates.status()
        self.assertEqual({k: after[k] for k in ("state", "checking", "total", "security", "names", "more")},
                         {"state": "ok", "checking": False, "total": 6, "security": 6, "more": 0,
                          "names": ["liblzma5", "libsystemd0", "libudev1", "libapt-pkg6.0", "apt", "libpcre2-8-0"]})

    def test_one_run_at_a_time(self):
        self.hold()
        threads = [threading.Thread(target=self.updates.status) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.now += 3600
        self.touch("var/lib/dpkg/status")
        self.updates.status()  # still running: no second run, even an hour later and after a change
        time.sleep(0.5)  # a second run would have started its apt-get by now
        self.assertEqual(self.calls(), ["-s -q dist-upgrade"])
        self.hold(False)
        self.done()
        self.assertEqual(self.calls(), ["-s -q dist-upgrade"])

    def test_the_command_its_priority_and_its_environment(self):
        """apt-get -s (a simulation) at the lowest priority, in English, with the PATH of the system and nothing else
        of the panel's environment."""
        self.check()
        self.assertEqual(self.calls(), ["-s -q dist-upgrade"])
        with open(os.path.join(self.bin, "niceness")) as f:
            self.assertEqual(int(f.read()), min(19, os.nice(0) + 19))
        with open(os.path.join(self.bin, "env")) as f:
            env = dict(line.split("=", 1) for line in f.read().splitlines() if "=" in line)
        self.assertEqual((env.get("LC_ALL"), env.get("PATH")), ("C", system_status.PATH))
        self.assertNotIn("SWIZZIN_TEST_SECRET", env)
        self.assertLessEqual(set(env), {"LC_ALL", "PATH", "PWD", "SHLVL", "_", "OLDPWD"})  # those sh adds itself

    def test_runs_again_only_when_dpkg_or_apt_changed_something(self):
        self.check()
        self.now += 3 * 86400
        self.assertEqual(self.check()[0]["checking"], False)
        self.assertEqual(len(self.calls()), 1)
        before = 6
        for n, (name, output, total) in enumerate((("var/lib/dpkg/status", REAL["debian-12"], 3),
                                                   ("var/lib/apt/lists", UP_TO_DATE, 0),
                                                   ("var/cache/apt/pkgcache.bin", REAL["debian-12"], 3)), 2):
            with self.subTest(name):
                self.output(output)
                self.touch(name)
                self.now += 60
                first, after = self.check()
                self.assertEqual((first["checking"], first["total"]), (True, before))  # the previous count meanwhile
                self.assertEqual((after["checking"], after["total"]), (False, total))
                self.assertEqual(len(self.calls()), n)
                before = total
        os.remove(self.path("var/cache/apt/pkgcache.bin"))  # apt-get clean
        self.now += 60
        self.check()
        self.assertEqual(len(self.calls()), 5)

    def test_at_most_once_a_minute(self):
        self.check()
        self.touch("var/lib/dpkg/status")
        self.now += 59
        self.assertFalse(self.check()[0]["checking"])
        self.assertEqual(len(self.calls()), 1)
        self.now += 1
        self.assertTrue(self.check()[0]["checking"])
        self.assertEqual(len(self.calls()), 2)

    def test_a_failure_keeps_the_previous_count(self):
        self.check()
        self.output("Calculating upgrade...\n")
        self.touch("var/lib/dpkg/status")
        self.now += 60
        with self.assertLogs(self.logger, "WARNING") as logged:
            after = self.check()[1]
        self.assertEqual((after["state"], after["total"], after["security"]), ("ok", 6, 6))
        self.assertEqual(logged.output, ["WARNING:test_system_status:Pending updates: unexpected output from apt-get: "
                                         "no summary line"])
        self.now += 599
        self.check()
        self.assertEqual(len(self.calls()), 2)  # nothing changed: the next try waits 10 minutes
        self.now += 1
        self.output(REAL["debian-12"])
        self.assertEqual(self.check()[1]["total"], 3)
        self.assertEqual(len(self.calls()), 3)

    def test_a_failure_without_a_previous_count(self):
        self.output("", code=100, stderr="W: Some warning\nE: The package lists or status file could not be parsed or "
                                         "opened.\n")
        with self.assertLogs(self.logger, "WARNING"):
            after = self.check()[1]
        self.assertEqual(after, {"state": "error", "checking": False, "reason": "apt-get failed: E: The package lists "
                                 "or status file could not be parsed or opened."})
        self.touch("var/lib/apt/lists")  # apt update ended: a new try a minute after the last one
        self.now += 60
        with mock.patch.object(self.logger, "warning") as warned:
            self.assertEqual(self.check()[1]["state"], "error")
        warned.assert_not_called()  # the same failure is logged once
        self.assertEqual(len(self.calls()), 2)

    def test_exit_code_without_a_message(self):
        self.output("", code=1)
        with self.assertLogs(self.logger, "WARNING"):
            self.assertEqual(self.check()[1]["reason"], "apt-get failed: exit code 1")

    def test_no_answer_in_time(self):
        with open(os.path.join(self.bin, "sleep"), "w") as f:
            f.write("30")
        t = time.monotonic()
        with mock.patch.object(system_status, "TIMEOUT", 1), self.assertLogs(self.logger, "WARNING"):
            after = self.check()[1]
        self.assertLess(time.monotonic() - t, 10)  # apt-get was stopped
        self.assertEqual(after, {"state": "error", "checking": False,
                                 "reason": "apt-get gave no answer within 1 seconds"})

    def test_an_error_of_the_check_itself_is_caught(self):
        with mock.patch.object(system_status, "parse", side_effect=TypeError("bug")), \
                self.assertLogs(self.logger, "WARNING"):
            after = self.check()[1]
        self.assertEqual(after["reason"], "the check failed: TypeError('bug')")
        self.assertFalse(self.updates.running)

    def test_without_apt(self):
        """Another system: no pill, nothing run."""
        with mock.patch.object(system_status, "PATH", self.path("empty")):
            self.assertEqual(self.updates.status(), {"state": "no-apt", "checking": False})
        self.assertFalse(self.updates.running)
        self.assertEqual(self.calls(), [])

    def test_turned_off(self):
        updates = PendingUpdates(self.logger, enabled=False, root=self.root)
        self.assertEqual(updates.status(), {"state": "off", "checking": False})
        self.assertFalse(updates.running)
        self.assertEqual(self.calls(), [])

    def test_without_package_lists(self):
        """apt would find nothing to upgrade, which says nothing: apt-get is not run."""
        os.remove(self.path("var/lib/apt/lists/deb.debian.org_debian_dists_bookworm_main_binary-amd64_Packages.lz4"))
        self.write("var/lib/apt/lists/lock")
        self.assertEqual(self.check()[1], {"state": "no-lists", "checking": False})
        self.assertEqual(self.calls(), [])
        shutil.rmtree(self.path("var/lib/apt"))
        self.now += 60
        self.assertEqual(self.check()[1], {"state": "no-lists", "checking": False})

    def test_the_date_of_the_package_lists(self):
        """The latest of the lists folder, its partial folder and the stamps of apt-daily and update-notifier."""
        now = int(time.time())
        self.date("var/lib/apt/lists", now - 20 * 86400)
        self.date("var/lib/apt/lists/partial", now - 9 * 86400)
        after = self.check()[1]
        self.assertEqual((after["lists"], after["lists_days"], after["lists_old"]), (now - 9 * 86400, 9, True))
        self.write("var/lib/apt/periodic/update-success-stamp")
        self.date("var/lib/apt/periodic/update-success-stamp", now - 2 * 86400 - 60)
        after = self.updates.status()
        self.assertEqual((after["lists_days"], after["lists_old"]), (2, False))
        self.write("var/lib/apt/periodic/update-stamp")  # apt-daily, today
        self.assertEqual(self.updates.status()["lists_days"], 0)


class Setting(unittest.TestCase):
    def test_updates_check(self):
        logger = logging.getLogger("test_system_status")
        for value, expected in ((True, True), (False, False)):
            self.assertEqual(system_status.updates_check({"UPDATES_CHECK": value}, logger), expected)
        self.assertTrue(system_status.updates_check({}, logger))
        for value in ("False", 0, 1, None, "no"):
            with self.subTest(value), self.assertLogs(logger, "WARNING") as logged:
                self.assertTrue(system_status.updates_check({"UPDATES_CHECK": value}, logger))
            self.assertIn("UPDATES_CHECK = %r is invalid" % (value,), logged.output[0])


@unittest.skipUnless(shutil.which("apt-get", path=system_status.PATH), "apt-get is not installed")
class RealApt(unittest.TestCase):
    def test_the_real_apt_get_gives_numbers(self):
        """The automatic tests run on Debian and Ubuntu: apt-get -s on the real system gives a count (or says that
        apt has no lists). 2 to 10 seconds."""
        updates = PendingUpdates(logging.getLogger("test_system_status"))
        self.assertEqual(updates.status(), {"state": "checking", "checking": True})
        end = time.monotonic() + 120
        while updates.running and time.monotonic() < end:
            time.sleep(0.1)
        after = updates.status()
        self.assertIn(after["state"], ("ok", "no-lists"), after)
        if after["state"] == "ok":
            self.assertIsInstance(after["total"], int)
            self.assertTrue(0 <= after["security"] <= after["total"], after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
