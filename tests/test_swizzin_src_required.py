"""The checks of the uninstall table against Swizzin are never skipped in silence by the automatic tests.

tests/test_removable.py needs a copy of Swizzin (SWIZZIN_SRC). On a computer without it, its tests against Swizzin
are skipped. The automatic tests (.github/workflows/tests.yml) set REQUIRE_SWIZZIN_SRC=1: there, a wrong SWIZZIN_SRC
must fail, not skip them all.

Run from the repository root: python tests/test_swizzin_src_required.py
(runs tests/test_removable.py twice, with SWIZZIN_SRC pointing to a folder that is not Swizzin: nothing on the machine
is touched)."""
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REMOVABLE_TESTS = os.path.join(ROOT, "tests", "test_removable.py")


def wrong_src(**env):
    """The environment of this process, with a SWIZZIN_SRC that is not Swizzin and without REQUIRE_SWIZZIN_SRC."""
    out = {k: v for k, v in os.environ.items() if k != "REQUIRE_SWIZZIN_SRC"}
    out["SWIZZIN_SRC"] = os.path.join(ROOT, "tests")
    out.update(env)
    return out


def run_removable_tests(env):
    return subprocess.run([sys.executable, REMOVABLE_TESTS], cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=300)


class SwizzinSrcRequired(unittest.TestCase):
    def test_with_a_wrong_swizzin_src_on_a_computer_the_tests_are_skipped(self):
        r = run_removable_tests(wrong_src())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stderr, r"\nOK \(skipped=[1-9][0-9]*\)\n$")

    def test_with_a_wrong_swizzin_src_and_require_swizzin_src_they_fail(self):
        r = run_removable_tests(wrong_src(REQUIRE_SWIZZIN_SRC="1"))
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("no copy of Swizzin in SWIZZIN_SRC, and REQUIRE_SWIZZIN_SRC=1", r.stderr)
        self.assertNotIn("skipped", r.stderr)

    def test_the_automatic_tests_require_swizzin_src(self):
        with open(os.path.join(ROOT, ".github", "workflows", "tests.yml"), encoding="utf-8") as f:
            workflow = f.read()
        step = re.search(r"\n      - name: Run the tests\n(.*?)(?=\n      - |\Z)", workflow, re.S)
        self.assertIsNotNone(step, "the step that runs the tests is not found")
        env = re.compile(r'^        env:\n(?:          .*\n)*          REQUIRE_SWIZZIN_SRC: "1"$', re.M)
        self.assertRegex(step.group(1), env)
        self.assertRegex(step.group(1), re.compile(r'^        env:\n(?:          .*\n)*          SWIZZIN_SRC: ', re.M))
        self.assertIn('venv/bin/python "$test"', step.group(1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
