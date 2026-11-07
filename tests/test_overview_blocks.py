"""The list of the blocks of the Overview layout (core/overview_blocks.py): ids, zones, templates, scripts, styles.
What the page makes of it is tested in tests/test_layouts_page.py.

The panel is not imported here. Run from the repository root: python tests/test_overview_blocks.py"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import overview_blocks  # noqa: E402
from core.overview_blocks import BLOCKS, ZONES  # noqa: E402

KEYS = {"id", "zone", "template", "label"}
OPTIONAL = {"script", "stylesheet"}


def template(block):
    with open(os.path.join(ROOT, "templates", block["template"]), encoding="utf-8") as f:
        return f.read()


class Blocks(unittest.TestCase):
    def test_each_block_says_what_it_is(self):
        for block in BLOCKS:
            with self.subTest(block.get("id")):
                self.assertLessEqual(KEYS, set(block))
                self.assertLessEqual(set(block), KEYS | OPTIONAL)  # a misspelt key would be left out in silence
                for value in block.values():
                    self.assertIsInstance(value, str)
                    self.assertEqual(value, value.strip())
                    self.assertTrue(value)

    def test_each_block_has_an_id_of_its_own(self):
        """The id names a block for good (the browser keeps the open sections of Details by it)."""
        ids = [block["id"] for block in BLOCKS]
        self.assertEqual(sorted({i for i in ids if ids.count(i) > 1}), [])
        for i in ids:
            self.assertRegex(i, r"^[a-z][a-z0-9-]*$")

    def test_each_block_in_a_known_zone(self):
        for block in BLOCKS:
            self.assertIn(block["zone"], ZONES, block["id"])
        self.assertEqual([b["id"] for z in ZONES for b in overview_blocks.zone(z)],
                         [b["id"] for z in ZONES for b in BLOCKS if b["zone"] == z])

    def test_the_labels_of_a_zone_differ(self):
        for z in ZONES:
            labels = [block["label"] for block in overview_blocks.zone(z)]
            self.assertEqual(len(labels), len(set(labels)), z)

    def test_each_template_exists_and_each_one_is_listed(self):
        templates = [block["template"] for block in BLOCKS]
        for name in templates:
            self.assertRegex(name, r"^overview/[\w-]+\.html$")
            self.assertTrue(os.path.isfile(os.path.join(ROOT, "templates", name)), name)
        self.assertEqual(len(templates), len(set(templates)))
        folder = sorted("overview/" + f for f in os.listdir(os.path.join(ROOT, "templates", "overview")))
        self.assertEqual(folder, sorted(templates))  # none that the page would never show

    def test_a_block_of_details_is_a_section_named_by_its_id(self):
        """In the two columns of Details, each block is a <details> section whose data-section is its id: the
        browser keeps the open ones by it (static/js/overview.js)."""
        for block in overview_blocks.zone("left") + overview_blocks.zone("right"):
            with self.subTest(block["id"]):
                text = template(block)
                self.assertEqual(re.findall(r'data-section="([^"]*)"', text), [block["id"]])
                self.assertIn('<details class="card ov-section" data-section="%s">' % block["id"], text)

    def test_the_scripts_and_stylesheets_exist_once(self):
        for files, kind in ((overview_blocks.scripts(), ".js"), (overview_blocks.stylesheets(), ".css")):
            with self.subTest(kind):
                self.assertEqual(len(files), len(set(files)))
                for name in files:
                    self.assertTrue(name.endswith(kind), name)
                    self.assertTrue(os.path.isfile(os.path.join(ROOT, "static", name)), name)
        self.assertEqual(overview_blocks.scripts()[:len(overview_blocks.SCRIPTS)], overview_blocks.SCRIPTS)
        self.assertEqual(overview_blocks.stylesheets()[:len(overview_blocks.STYLESHEETS)], overview_blocks.STYLESHEETS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
