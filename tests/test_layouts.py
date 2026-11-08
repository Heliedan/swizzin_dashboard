"""Where the layout of each account, and its arrangement of Overview, are kept (core/layouts.py, core/statefile.py):
the file never stops the dashboard, and a file of the previous version is only changed for an account that arranges.

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

from core import layouts, overview_blocks, statefile  # noqa: E402


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

    def choice(self, user):
        """(layout, arrangement of Overview) of `user`, as the dashboard reads them."""
        return self.choices.get(user), self.choices.arrangement(user)

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
        with mock.patch.object(statefile.os, "replace", side_effect=replace):
            self.choices.set("tester", "overview")
        self.assertEqual(seen, [("classic", {"tester": "overview"})])
        self.assertEqual(self.choices.get("tester"), "overview")

    def test_a_failed_write_leaves_the_old_file_and_nothing_else(self):
        self.choices.set("tester", "classic")
        for error in (OSError(28, "No space left on device"), KeyboardInterrupt()):
            with self.subTest(type(error).__name__):
                with mock.patch.object(statefile.json, "dump", side_effect=error):
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
        with mock.patch.object(statefile.os, "replace", side_effect=slow_replace):
            threads = [threading.Thread(target=choose, args=(u,)) for u in users]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(self.content(), dict.fromkeys(users, "overview"))
        self.assertEqual(self.leftovers(), [])


# An arrangement as this version keeps it: its four lists, hidden and shown holding only what differs from the
# defaults (a tile is never hidden by default).
ARRANGED = {"order": ["tile-ram", "tile-cpu", "services", "bandwidth", "disks", "cpu", "ram"], "hidden": ["tile-ram"],
            "shown": [], "other_column": ["disks"]}
BY_DEFAULT = [block["id"] for block in overview_blocks.BLOCKS if block.get("hidden")]  # hidden by default
SHOWN = [block["id"] for block in overview_blocks.BLOCKS if not block.get("hidden")]  # shown by default
WIDE = [block["id"] for block in overview_blocks.zone("wide")]  # across the whole width of Details


def hidden(block_id, arrangement=None):
    """Whether the page hides `block_id` with `arrangement`: hidden by it, or hidden by default and not shown by it."""
    arrangement = arrangement or {}
    return block_id in arrangement.get("hidden", []) or (
        block_id in BY_DEFAULT and block_id not in arrangement.get("shown", []))


def ids(zones):
    """{zone: [(id, hidden), ...]}: what zones() gives, by id."""
    return {zone: [(block["id"], hidden) for block, hidden in items] for zone, items in zones.items()}


class ReadArrangement(LayoutsTest):
    """The arrangement of Overview of an account: an object under "overview", read as far as this version can."""

    def test_a_file_of_the_previous_version_reads_as_before(self):
        text = '{"tester": "overview", "friend": "classic"}'
        self.write(text)
        self.assertEqual(self.choice("tester"), ("overview", None))
        self.assertEqual(self.choice("friend"), ("classic", None))
        self.assertEqual(self.choice("other"), ("classic", None))
        with open(self.path) as f:
            self.assertEqual(f.read(), text)  # reading changes nothing

    def test_an_arrangement_is_read_with_its_layout(self):
        for layout, expected in (("overview", "overview"), ("classic", "classic"), (None, "classic"),
                                 ("fancy", "classic")):
            with self.subTest(layout):
                value = {"overview": ARRANGED}
                if layout:
                    value["layout"] = layout
                self.write(json.dumps({"tester": value}))
                self.assertEqual(self.choice("tester"), (expected, ARRANGED))
                self.assertEqual(self.choices.get("tester"), expected)

    def test_what_this_version_cannot_read_is_left_out(self):
        """Keys and ids it does not know (a block a later version added, or one it removed), ids given twice, items
        that are not ids, a list that is not one, and a block of another zone than Details in other_column."""
        value = {"order": ["tile-ram", "future-block", "tile-ram", 3, None, ["cpu"], {"id": "cpu"}, "cpu", "",
                           "Tile-cpu"],
                 "hidden": "ram", "other_column": ["disks", "tile-cpu", "services", "disks", "cpu"],
                 "collapsed": ["cpu"], "theme": "dark"}
        self.write(json.dumps({"tester": {"layout": "overview", "overview": value, "colour": "teal"}}))
        self.assertEqual(self.choice("tester"),
                         ("overview", {"order": ["tile-ram", "cpu"], "hidden": [], "shown": [],
                                       "other_column": ["disks", "cpu"]}))

    def test_no_more_than_the_limit_is_read(self):
        junk = ["junk%d" % i for i in range(layouts.LIMIT - 2)]
        value = {"order": ["tile-disk"] + junk + ["tile-ram", "tile-net"] + ["x"] * 10000,  # tile-net: the 65th
                 "hidden": junk + ["x", "y", "cpu"]}  # cpu: the 65th
        self.write(json.dumps({"tester": {"overview": value}}))
        self.assertEqual(self.choice("tester")[1],
                         {"order": ["tile-disk", "tile-ram"], "hidden": [], "shown": [], "other_column": []})

    def test_an_arrangement_of_another_meaning_is_not_read(self):
        """A later version that changes what the lists mean marks its arrangement with "v": this one leaves it."""
        for version, read in ((1, True), (1.0, True), (2, False), ("1", False), (True, False), (None, False)):
            with self.subTest(version):
                self.write(json.dumps({"tester": {"layout": "overview", "overview": dict(ARRANGED, v=version)}}))
                self.assertEqual(self.choice("tester"), ("overview", ARRANGED if read else None))

    def test_the_default_arrangement_is_none(self):
        """Nothing hidden, nothing in the other column, and the blocks in the order of the list: the page is the
        default one, even when only some blocks are listed (those of another account, say)."""
        every = [block["id"] for block in overview_blocks.BLOCKS]
        for value in ({"order": every}, {"order": ["tile-cpu", "tile-disk", "cpu", "network"]}, {"order": []}, {},
                      {"hidden": [], "other_column": []}, {"hidden": ["nothing-known"]},
                      {"hidden": BY_DEFAULT, "shown": SHOWN}):  # each block as it is by default: nothing changed
            with self.subTest(value):
                self.write(json.dumps({"tester": {"overview": value}}))
                self.assertEqual(self.choice("tester"), ("classic", None))

    def test_whatever_the_value_the_dashboard_shows(self):
        for value in ("overview", 3, None, [], ["overview"], {"overview": "cpu"}, {"overview": ["cpu"]},
                      {"overview": None}, {"overview": {"order": "cpu"}}, {"overview": {"order": {"cpu": 1}}},
                      {"layout": 3, "overview": 3}):
            with self.subTest(value):
                self.write(json.dumps({"tester": value}))
                layout, arrangement = self.choice("tester")
                self.assertIn(layout, layouts.LAYOUTS)
                self.assertIsNone(arrangement)

    def test_a_broken_file_gives_no_arrangement_and_is_logged(self):
        self.write('{"tester": {"overview": {"order": ["tile-ram"')
        with self.assertLogs("core.layouts", logging.WARNING) as logs:
            self.assertIsNone(self.choices.arrangement("tester"))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("cannot be read", logs.output[0])
        self.assertIn("Overview shows its blocks in their default order", logs.output[0])


class DefaultsAndShown(LayoutsTest):
    """The blocks hidden by default ("hidden" in core/overview_blocks.py) and the list "shown" of an arrangement: it
    says only what the account changed, so that an arrangement keeps its meaning when a default changes."""

    def test_the_list_has_blocks_hidden_by_default_and_blocks_shown(self):
        self.assertTrue(BY_DEFAULT)
        self.assertTrue(SHOWN)

    def test_a_block_hidden_by_default_shown_again(self):
        """Shown by the account: in "shown", and on the page; the others hidden by default stay hidden."""
        first = BY_DEFAULT[0]
        arrangement = layouts.checked({"shown": [first]})
        self.assertEqual(arrangement, {"order": [], "hidden": [], "shown": [first], "other_column": []})
        self.choices.set("tester", "overview")
        self.choices.arrange("tester", arrangement)
        self.assertEqual(self.content(), {"tester": {"layout": "overview", "overview": arrangement}})
        self.assertEqual(self.choice("tester"), ("overview", arrangement))
        flags = {i: flag for items in ids(layouts.zones(arrangement)).values() for i, flag in items}
        self.assertEqual({i for i, flag in flags.items() if flag}, set(BY_DEFAULT) - {first})
        self.choices.arrange("tester", None)  # Reset to default: hidden again, the plain name back
        self.assertEqual(self.content(), {"tester": "overview"})
        flags = {i: flag for items in ids(layouts.zones(self.choices.arrangement("tester"))).values()
                 for i, flag in items}
        self.assertEqual({i for i, flag in flags.items() if flag}, set(BY_DEFAULT))

    def test_what_changes_nothing_is_left_out(self):
        """A block hidden by default in hidden, or shown by default in shown: the same page, so it is not kept."""
        value = {"order": ["tile-cpu"], "hidden": BY_DEFAULT + ["tile-ram"], "shown": SHOWN, "other_column": []}
        self.assertEqual(layouts.read(value), {"order": ["tile-cpu"], "hidden": ["tile-ram"], "shown": [],
                                               "other_column": []})
        self.assertEqual(layouts.checked(dict(value, shown=[])), layouts.read(value))

    def test_each_block_left_in_its_place_is_the_default(self):
        """Customize sends every block of the page, zone by zone (static/js/arrange.js): an arrangement that leaves each
        one in its place, hiding and showing only what the defaults do, is the default one, whether it is read from
        the file (written by an earlier version, whose defaults differed) or sent; the account keeps its plain name.
        Even when the blocks hidden by default are listed as hidden, and without the blocks a page does not have."""
        page = [b["id"] for zone in overview_blocks.ZONES for b in overview_blocks.zone(zone)]
        for value in ({"order": page}, {"order": page, "hidden": BY_DEFAULT, "shown": [], "other_column": []},
                      {"order": [i for i in page if i not in ("quota", "system-status")], "hidden": BY_DEFAULT[::-1]}):
            with self.subTest(value=value):
                self.assertIsNone(layouts.read(value))
                self.assertIsNone(layouts.checked(value))
        self.assertIsNotNone(layouts.read({"order": page[::-1]}))  # in another order: an arrangement

    def test_a_block_both_hidden_and_shown_is_read_hidden(self):
        both = [BY_DEFAULT[0], "tile-ram"]
        arrangement = layouts.read({"hidden": both, "shown": both + [BY_DEFAULT[-1]]})
        flags = {i: flag for items in ids(layouts.zones(arrangement)).values() for i, flag in items}
        self.assertTrue(flags[BY_DEFAULT[0]] and flags["tile-ram"])
        self.assertEqual(flags[BY_DEFAULT[-1]], len(BY_DEFAULT) == 1)  # shown, unless it is the same block

    def test_an_arrangement_of_the_previous_version(self):
        """Written before "shown" and the blocks hidden by default (#30): it said what the account hid. Read now, the
        blocks hidden by default are hidden too (the account never chose to show them), what it hid stays hidden, a
        section it put in the other column that now takes the whole width of Details stays there, and the file is not
        written again."""
        details = [b["id"] for b in overview_blocks.BLOCKS if b["zone"] in ("left", "right", "wide")]
        old = {"order": ["tile-ram", "tile-cpu"] + details, "hidden": [BY_DEFAULT[0], "tile-disk"],
               "other_column": ["disks"] + WIDE}
        text = json.dumps({"tester": {"layout": "overview", "overview": old}, "friend": "classic"})
        self.write(text)
        arrangement = self.choices.arrangement("tester")
        self.assertEqual(arrangement, {"order": old["order"], "hidden": ["tile-disk"], "shown": [],
                                       "other_column": ["disks"]})
        zones = ids(layouts.zones(arrangement))
        flags = {i: flag for items in zones.values() for i, flag in items}
        self.assertEqual({i for i, flag in flags.items() if flag}, set(BY_DEFAULT) | {"tile-disk"})
        self.assertEqual([i for i, _ in zones["wide"]], [i for i in old["order"] if i in WIDE])
        self.assertEqual([i for i, _ in zones["left"]][-1], "disks")
        with open(self.path) as f:
            self.assertEqual(f.read(), text)  # reading changes nothing

    def test_a_file_of_the_previous_version_without_arrangement(self):
        """An account with its plain name: the page of everyone, the blocks hidden by default hidden."""
        self.write('{"tester": "overview"}')
        self.assertEqual(self.choice("tester"), ("overview", None))
        flags = {i: flag for items in ids(layouts.zones(None)).values() for i, flag in items}
        self.assertEqual({i for i, flag in flags.items() if flag}, set(BY_DEFAULT))

    def test_what_this_version_writes_keeps_the_lists_of_the_previous_one(self):
        """The three lists of the previous version keep their meaning (what the account hid, its order, its columns),
        and "shown" is one more key: that version leaves out the keys it does not know, so it shows what the account
        arranged there, but for the blocks hidden by default here, which it shows as it showed every block."""
        self.choices.set("tester", "overview")
        self.choices.arrange("tester", layouts.checked(dict(ARRANGED, shown=BY_DEFAULT[:1])))
        kept = self.content()["tester"]["overview"]
        self.assertEqual(sorted(kept), ["hidden", "order", "other_column", "shown"])
        self.assertEqual((kept["hidden"], kept["other_column"]), (ARRANGED["hidden"], ARRANGED["other_column"]))
        self.assertEqual(kept["shown"], BY_DEFAULT[:1])


class Zones(unittest.TestCase):
    """zones(): the blocks of each zone of the page, in the order of an arrangement."""

    def test_without_arrangement_the_order_of_the_list(self):
        """Each block in its zone, in the order of the list, hidden when it is hidden by default."""
        self.assertEqual(ids(layouts.zones(None)),
                         {zone: [(block["id"], bool(block.get("hidden"))) for block in overview_blocks.zone(zone)]
                          for zone in overview_blocks.ZONES})
        self.assertEqual(list(layouts.zones(None)), list(overview_blocks.ZONES))

    def test_an_arrangement(self):
        """In its order, the hidden ones marked; a section in the other column at its place in the order; a block it
        does not list after those it lists, in the order of the list."""
        zones = ids(layouts.zones(ARRANGED))
        self.assertEqual(zones["tiles"][:2], [("tile-ram", True), ("tile-cpu", False)])
        self.assertEqual(zones["tiles"][2:], [(b["id"], False) for b in overview_blocks.zone("tiles")
                                              if b["id"] not in ("tile-ram", "tile-cpu")])
        self.assertEqual(zones["row"][:2], [("services", False), ("bandwidth", False)])
        self.assertEqual(zones["left"][:3], [(i, hidden(i, ARRANGED)) for i in ("disks", "cpu", "ram")])
        self.assertNotIn("disks", [i for i, _ in zones["right"]])
        self.assertEqual(zones["glance"], [(b["id"], hidden(b["id"])) for b in overview_blocks.zone("glance")])
        every = sorted(i for items in zones.values() for i, _ in items)
        self.assertEqual(every, sorted(block["id"] for block in overview_blocks.BLOCKS))  # each block once

    def test_a_block_added_to_the_list_is_arranged_like_the_others(self):
        """Nothing to write for a new block: its id is read, kept and placed as the others are."""
        extra = {"id": "extra", "zone": "right", "template": "overview/extra.html", "label": "Extra"}
        with mock.patch.object(overview_blocks, "BLOCKS", overview_blocks.BLOCKS + [extra]):
            arrangement = layouts.read({"order": ["extra", "cpu"], "hidden": ["extra"], "other_column": ["extra"]})
            self.assertEqual(arrangement, {"order": ["extra", "cpu"], "hidden": ["extra"], "shown": [],
                                           "other_column": ["extra"]})
            self.assertEqual(ids(layouts.zones(arrangement))["left"][:2], [("extra", True), ("cpu", hidden("cpu"))])
            self.assertEqual(layouts.checked({"order": ["extra"], "other_column": ["extra"]}),
                             {"order": ["extra"], "hidden": [], "shown": [], "other_column": ["extra"]})
        self.assertIsNone(layouts.read({"order": ["extra", "cpu"]}))  # once it is gone, it is left out


class Checked(unittest.TestCase):
    """checked(): what POST /layout/arrange accepts."""

    def test_an_arrangement(self):
        self.assertEqual(layouts.checked(ARRANGED), ARRANGED)
        self.assertEqual(layouts.checked({"hidden": ["tile-cpu"]}),
                         {"order": [], "hidden": ["tile-cpu"], "shown": [], "other_column": []})
        self.assertEqual(layouts.checked({"other_column": ["disks"]}),
                         {"order": [], "hidden": [], "shown": [], "other_column": ["disks"]})
        self.assertEqual(layouts.checked({"shown": BY_DEFAULT}),
                         {"order": [], "hidden": [], "shown": BY_DEFAULT, "other_column": []})

    def test_the_default_arrangement_is_none(self):
        every = [block["id"] for block in overview_blocks.BLOCKS]
        for value in ({"order": every}, {"order": every[:3]}, {"order": []}, {"hidden": []}, {"shown": []},
                      {"hidden": BY_DEFAULT}, {"shown": SHOWN}, {"hidden": BY_DEFAULT, "shown": SHOWN}):
            with self.subTest(value):
                self.assertIsNone(layouts.checked(value))

    def test_anything_else_is_refused(self):
        too_many = ["cpu"] * (layouts.LIMIT + 1)
        cases = [None, [], "cpu", 3, {}, {"reset": True}, {"order": ["cpu"], "reset": True}, {"order": ["cpu"], "x": 1},
                 {"order": "cpu"}, {"order": None}, {"order": {"cpu": 1}}, {"order": ["future-block"]},
                 {"order": ["cpu", "cpu"]}, {"order": ["cpu", 3]}, {"order": [None]}, {"order": [""]},
                 {"order": ["CPU"]}, {"hidden": ["ram", "ram"]}, {"hidden": too_many},
                 {"order": [b["id"] for b in overview_blocks.BLOCKS] + ["x%d" % i for i in range(layouts.LIMIT)]},
                 {"other_column": ["tile-cpu"]}, {"other_column": ["services"]}, {"other_column": ["system-status"]},
                 {"other_column": ["disks", "disks"]}, {"other_column": WIDE[:1]}, {"shown": "cpu"},
                 {"shown": ["future-block"]}, {"shown": ["cpu", "cpu"]}, {"shown": [3]}, {"shown": too_many},
                 {"hidden": ["cpu"], "shown": ["cpu"]}, {"hidden": ["tile-ram"], "shown": ["tile-ram", "cpu"]}]
        for value in cases:
            with self.subTest(value):
                with self.assertRaises(ValueError):
                    layouts.checked(value)


class WriteArrangement(LayoutsTest):
    def stamp(self):
        st = os.stat(self.path)
        with open(self.path, "rb") as f:
            return f.read(), st.st_ino, st.st_mtime_ns

    def test_only_the_account_that_arranges_changes(self):
        """A file of the previous version: the other accounts keep their name, as it was written."""
        self.write('{"tester": "overview", "friend": "classic", "third": "overview"}')
        self.choices.arrange("tester", ARRANGED)
        self.assertEqual(self.content(), {"tester": {"layout": "overview", "overview": ARRANGED},
                                          "friend": "classic", "third": "overview"})
        self.assertEqual(self.choice("tester"), ("overview", ARRANGED))
        self.assertEqual(self.choice("friend"), ("classic", None))
        self.assertEqual(self.leftovers(), [])

    def test_an_account_without_a_layout_keeps_the_default(self):
        """It arranges the Overview it has by default (DASHBOARD_LAYOUT): it still has no layout of its own."""
        choices = layouts.Choices(self.path, "overview")
        choices.arrange("friend", ARRANGED)
        self.assertEqual(self.content(), {"friend": {"overview": ARRANGED}})
        self.assertEqual((choices.get("friend"), choices.arrangement("friend")), ("overview", ARRANGED))
        choices.default = "classic"  # swizzin.cfg changed: the account follows it
        self.assertEqual((choices.get("friend"), choices.arrangement("friend")), ("classic", ARRANGED))

    def test_classic_keeps_the_arrangement_for_later(self):
        self.choices.set("tester", "overview")
        self.choices.arrange("tester", ARRANGED)
        self.choices.set("tester", "classic")
        self.assertEqual(self.content(), {"tester": {"layout": "classic", "overview": ARRANGED}})
        self.assertEqual(self.choice("tester"), ("classic", ARRANGED))
        self.choices.set("tester", "overview")
        self.assertEqual(self.choice("tester"), ("overview", ARRANGED))

    def test_reset_gives_the_name_back(self):
        self.write('{"tester": "overview", "friend": "classic"}')
        self.choices.arrange("tester", ARRANGED)
        self.choices.arrange("tester", None)
        self.assertEqual(self.content(), {"tester": "overview", "friend": "classic"})
        self.choices.arrange("third", ARRANGED)
        self.choices.arrange("third", None)
        self.assertEqual(self.content(), {"tester": "overview", "friend": "classic"})  # no choice: no entry

    def test_a_change_that_changes_nothing_writes_nothing(self):
        """A file of the previous version is never written again without a reason: not by choosing the layout
        shown, nor by a reset or a default arrangement where there is none, nor by the same arrangement again."""
        self.write('{"tester": "overview", "friend": "classic"}')
        before = self.stamp()
        with mock.patch.object(layouts.Choices, "write", side_effect=AssertionError("written")):
            self.choices.set("tester", "overview")
            self.choices.arrange("tester", None)
            self.choices.arrange("other", None)
        self.assertEqual(self.stamp(), before)
        self.choices.arrange("friend", ARRANGED)
        before = self.stamp()
        with mock.patch.object(layouts.Choices, "write", side_effect=AssertionError("written")):
            self.choices.arrange("friend", json.loads(json.dumps(ARRANGED)))
            self.choices.set("friend", "classic")
        self.assertEqual(self.stamp(), before)

    def test_no_file_and_nothing_to_keep_creates_no_file(self):
        self.choices.arrange("tester", None)
        self.assertFalse(os.path.exists(self.path))

    def test_what_a_later_version_added_is_kept(self):
        """Its own keys for the account, and a layout this version does not know; its arrangement of another meaning
        is replaced by the one made here."""
        self.write(json.dumps({"tester": {"layout": "fancy", "theme": "dark", "overview": dict(ARRANGED, v=2)}}))
        self.assertEqual(self.choice("tester"), ("classic", None))
        self.choices.arrange("tester", ARRANGED)
        self.assertEqual(self.content(), {"tester": {"layout": "fancy", "theme": "dark", "overview": ARRANGED}})
        self.choices.arrange("tester", None)
        self.assertEqual(self.content(), {"tester": {"layout": "fancy", "theme": "dark"}})
        self.write('{"tester": "fancy"}')
        self.choices.arrange("tester", ARRANGED)
        self.assertEqual(self.content(), {"tester": {"layout": "fancy", "overview": ARRANGED}})
        self.choices.arrange("tester", None)
        self.assertEqual(self.content(), {"tester": "fancy"})

    def test_a_value_that_is_not_one_is_replaced(self):
        for value in (3, None, [], ["overview"]):
            with self.subTest(value):
                self.write(json.dumps({"tester": value, "friend": "classic"}))
                self.choices.arrange("tester", ARRANGED)
                self.assertEqual(self.content(), {"tester": {"overview": ARRANGED}, "friend": "classic"})

    def test_a_failed_write_leaves_the_old_file(self):
        self.choices.set("tester", "overview")
        with mock.patch.object(statefile.json, "dump", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                self.choices.arrange("tester", ARRANGED)
        self.assertEqual(self.content(), {"tester": "overview"})
        self.assertEqual(self.leftovers(), [])

    def test_arrangements_and_choices_at_the_same_moment_are_all_kept(self):
        """Each account arranges and chooses its layout at the same moment as the others: nothing is lost."""
        users = ["user%d" % i for i in range(12)]
        start = threading.Barrier(len(users) * 2)
        real_replace = os.replace

        def slow_replace(src, dst):
            time.sleep(0.01)
            real_replace(src, dst)

        def arrange(user):
            start.wait()
            self.choices.arrange(user, ARRANGED)

        def choose(user):
            start.wait()
            self.choices.set(user, "classic")
        with mock.patch.object(statefile.os, "replace", side_effect=slow_replace):
            threads = [threading.Thread(target=f, args=(u,)) for u in users for f in (arrange, choose)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(self.content(), {u: {"layout": "classic", "overview": ARRANGED} for u in users})
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
