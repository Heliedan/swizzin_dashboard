"""The JavaScript tests are never skipped in silence by the automatic tests.

tests/test_disk_alert_js.py needs Node.js. On a computer without it, its tests are skipped. The automatic tests
(.github/workflows/tests.yml) set REQUIRE_NODE=1: there, a missing node must fail, not skip them all.

Run from the repository root: python tests/test_node_required.py
(runs tests/test_disk_alert_js.py twice, with node hidden: nothing on the machine is touched)."""
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_TESTS = os.path.join(ROOT, "tests", "test_disk_alert_js.py")


def without_node(**env):
    """The environment of this process, with no node in PATH and without REQUIRE_NODE, plus env."""
    out = {k: v for k, v in os.environ.items() if k != "REQUIRE_NODE"}
    out["PATH"] = os.pathsep.join(d for d in os.environ.get("PATH", "").split(os.pathsep)
                                  if d and not os.path.exists(os.path.join(d, "node")))
    out.update(env)
    return out


def run_js_tests(env):
    return subprocess.run([sys.executable, JS_TESTS], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


class NodeRequired(unittest.TestCase):
    def test_without_node_on_a_computer_the_tests_are_skipped(self):
        r = run_js_tests(without_node())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stderr, r"\nOK \(skipped=[1-9][0-9]*\)\n$")

    def test_without_node_with_require_node_they_fail(self):
        r = run_js_tests(without_node(REQUIRE_NODE="1"))
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("node is not installed, and REQUIRE_NODE=1", r.stderr)
        self.assertNotIn("skipped", r.stderr)

    def test_the_automatic_tests_require_node(self):
        with open(os.path.join(ROOT, ".github", "workflows", "tests.yml"), encoding="utf-8") as f:
            workflow = f.read()
        step = re.search(r"\n      - name: Run the tests\n(.*?)(?=\n      - |\Z)", workflow, re.S)
        self.assertIsNotNone(step, "the step that runs the tests is not found")
        env = re.compile(r'^        env:\n(?:          .*\n)*          REQUIRE_NODE: "1"$', re.M)
        self.assertRegex(step.group(1), env)
        self.assertIn('venv/bin/python "$test"', step.group(1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
