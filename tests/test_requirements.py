"""Test availability of required packages."""

import unittest
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement

_REQUIREMENTS_PATH = Path(__file__).parent.with_name("requirements.txt")


class TestRequirements(unittest.TestCase):
    """Test availability of required packages."""

    def test_requirements(self):
        """Test that each required package is installed, in a version the file allows."""
        for line in _REQUIREMENTS_PATH.read_text().splitlines():
            line = line.split("#")[0].strip()
            if not line:
                continue
            requirement = Requirement(line)
            with self.subTest(requirement=line):
                version = metadata.version(requirement.name)
                self.assertTrue(requirement.specifier.contains(version, prereleases=True),
                                "%s %s is installed" % (requirement.name, version))


if __name__ == "__main__":
    unittest.main()
