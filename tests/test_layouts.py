"""Where the layout chosen by each account is kept (core/layouts.py): the file never stops the dashboard.

The panel is not imported here: the threads of these tests are real system threads, not eventlet's.

Run from the repository root: python tests/test_layouts.py
(works in throwaway folders: nothing on the machine is touched)."""
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import layouts  # noqa: E402


class LayoutsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "layouts.json")
        self.choices = layouts.Choices(self.path)

    def write(self, data):
        with open(self.path, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode())

    def content(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def leftovers(self):
        """Files of the folder other than the choices: temporary files left behind."""
        return sorted(set(os.listdir(self.dir)) - {"layouts.json"})


class Read(LayoutsTest):
    def test_no_file_gives_the_default(self):
        self.assertEqual(self.choices.get("tester"), "classic")
        self.assertEqual(layouts.Choices(self.path, "overview").get("tester"), "overview")
        self.assertFalse(os.path.exists(self.path))  # reading creates nothing

    def test_each_account_its_choice(self):
        self.write('{"tester": "overview", "friend": "classic"}')
        choices = layouts.Choices(self.path, "overview")
        self.assertEqual([choices.get(u) for u in ("tester", "friend", "other")], ["overview", "classic", "overview"])

    def test_a_broken_file_gives_the_default_and_is_logged(self):
        """Whatever the file holds, reading it never raises: the dashboard shows, with the default layout."""
        cases = {"half written": '{"tester": "over', "empty": "", "a list": '["overview"]', "a string": '"overview"',
                 "null": "null", "not UTF-8": b'{"tester": "\xff\xfe"}', "nested too deep": "[" * 100000 + "]" * 100000}
        for name, data in cases.items():
            with self.subTest(name):
                self.write(data)
                with self.assertLogs("core.layouts", logging.WARNING) as logs:
                    self.assertEqual(self.choices.get("tester"), "classic")
                self.assertIn("cannot be read", logs.output[0])

    def test_a_folder_in_its_place_gives_the_default(self):
        """Unreadable: as root, a file without rights could still be read; a folder cannot, by anyone."""
        os.mkdir(self.path)
        with self.assertLogs("core.layouts", logging.WARNING):
            self.assertEqual(self.choices.get("tester"), "classic")

    def test_an_object_gives_its_layout(self):
        """A later version may keep more for an account, as an object with the layout under "layout": this version
        reads that layout, and gives the default when it has none, or one it does not know."""
        cases = [({"layout": "overview", "overview": {"order": ["cpu", "ram"]}}, "overview"),
                 ({"layout": "classic", "overview": {}}, "classic"),
                 ({"layout": "overview"}, "overview"),
                 ({"overview": {"order": ["cpu"]}}, "classic"),
                 ({}, "classic"),
                 ({"layout": "fancy"}, "classic"),
                 ({"layout": "Overview"}, "classic"),
                 ({"layout": None}, "classic"),
                 ({"layout": ["overview"]}, "classic"),
                 ({"layout": {"layout": "overview"}}, "classic")]
        for value, expected in cases:
            with self.subTest(value):
                self.write(json.dumps({"tester": value, "friend": "overview"}))
                self.assertEqual(self.choices.get("tester"), expected)
                self.assertEqual(self.choices.get("friend"), "overview")  # the others as they were

    def test_a_name_this_version_does_not_know_gives_the_default(self):
        for value in ("fancy", "", "Overview", 3, None, ["overview"], {"overview": 1}):
            with self.subTest(value):
                self.write(json.dumps({"tester": value}))
                self.assertEqual(self.choices.get("tester"), "classic")


class Write(LayoutsTest):
    def test_a_choice_is_kept_with_the_others(self):
        self.choices.set("tester", "overview")
        self.choices.set("friend", "classic")
        self.choices.set("tester", "classic")
        self.assertEqual(self.content(), {"tester": "classic", "friend": "classic"})
        self.assertEqual(self.leftovers(), [])

    def test_only_known_layouts_are_kept(self):
        self.choices.set("tester", "overview")
        for value in ("fancy", "", None, 3, ["overview"]):
            with self.subTest(value):
                with self.assertRaises(ValueError):
                    self.choices.set("tester", value)
        self.assertEqual(self.content(), {"tester": "overview"})

    def test_readable_by_the_panel_only(self):
        """0600, whatever the umask, and even when the file it replaces was readable by others."""
        self.write('{"friend": "overview"}')
        os.chmod(self.path, 0o644)
        old = os.umask(0)
        try:
            self.choices.set("tester", "overview")
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(self.content(), {"friend": "overview", "tester": "overview"})

    def test_a_reader_never_sees_half_a_file(self):
        """The new file is complete before it takes the old one's name: a page shown meanwhile reads the old one."""
        self.choices.set("tester", "classic")
        seen = []
        real_replace = os.replace

        def replace(src, dst):
            with open(src) as f:  # the new file, complete; the page reads the old one
                seen.append((self.choices.get("tester"), json.load(f)))
            real_replace(src, dst)
        with mock.patch.object(layouts.os, "replace", side_effect=replace):
            self.choices.set("tester", "overview")
        self.assertEqual(seen, [("classic", {"tester": "overview"})])
        self.assertEqual(self.choices.get("tester"), "overview")

    def test_a_failed_write_leaves_the_old_file_and_nothing_else(self):
        self.choices.set("tester", "classic")
        for error in (OSError(28, "No space left on device"), KeyboardInterrupt()):
            with self.subTest(type(error).__name__):
                with mock.patch.object(layouts.json, "dump", side_effect=error):
                    with self.assertRaises(type(error)):
                        self.choices.set("tester", "overview")
                self.assertEqual(self.content(), {"tester": "classic"})
                self.assertEqual(self.leftovers(), [])

    def test_a_folder_that_cannot_be_written_raises_oserror(self):
        """A missing folder: OSError for root too (the route answers 503, see tests/test_layouts_page.py)."""
        choices = layouts.Choices(os.path.join(self.dir, "missing", "layouts.json"))
        with self.assertRaises(OSError):
            choices.set("tester", "overview")
        self.assertEqual(choices.get("tester"), "classic")

    def test_a_broken_file_is_written_again(self):
        self.write('{"tester": "over')
        with self.assertLogs("core.layouts", logging.WARNING):
            self.choices.set("friend", "overview")
        self.assertEqual(self.content(), {"friend": "overview"})

    def test_choices_made_at_the_same_moment_are_all_kept(self):
        """Each write reads the file, adds its choice and writes it again: without the lock, two writes at the same
        moment would both start from the same file and one choice would be lost. The rename is slowed down, so that
        the writes overlap."""
        users = ["user%d" % i for i in range(16)]
        start = threading.Barrier(len(users))
        real_replace = os.replace

        def slow_replace(src, dst):
            time.sleep(0.02)
            real_replace(src, dst)

        def choose(user):
            start.wait()
            self.choices.set(user, "overview")
        with mock.patch.object(layouts.os, "replace", side_effect=slow_replace):
            threads = [threading.Thread(target=choose, args=(u,)) for u in users]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(self.content(), dict.fromkeys(users, "overview"))
        self.assertEqual(self.leftovers(), [])


class Configured(unittest.TestCase):
    """DASHBOARD_LAYOUT in swizzin.cfg: the layout of the accounts that chose none."""

    def test_classic_unless_swizzin_cfg_says_otherwise(self):
        self.assertEqual(layouts.configured({}), "classic")
        self.assertEqual(layouts.configured({"DASHBOARD_LAYOUT": "overview"}), "overview")

    def test_a_name_that_is_not_a_layout_falls_back_to_classic(self):
        for value in ("Overview", "fancy", "", None, ["overview"]):
            with self.subTest(value):
                with self.assertLogs("core.layouts", logging.WARNING) as logs:
                    self.assertEqual(layouts.configured({"DASHBOARD_LAYOUT": value}), "classic")
                self.assertIn("DASHBOARD_LAYOUT", logs.output[0])

    def test_the_default_of_config_py(self):
        sys.modules.pop("core.config", None)
        from core.config import Config
        self.assertEqual(Config.DASHBOARD_LAYOUT, "classic")


if __name__ == "__main__":
    unittest.main(verbosity=2)
