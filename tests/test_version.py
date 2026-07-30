"""CalVer policy, enforced rather than documented.

Version is ``YYYY.MM.DD`` with an optional ``.MICRO`` for same-day releases, and
``muninn.__version__`` must equal the version in ``pyproject.toml``. Git tags add
a leading ``v``.
"""
from __future__ import annotations

import datetime as dt
import re
import tomllib
import unittest
from pathlib import Path

import muninn

CALVER = re.compile(r"^(\d{4})\.(0[1-9]|1[0-2])\.(0[1-9]|[12]\d|3[01])(?:\.(0|[1-9]\d*))?$")


class VersionTest(unittest.TestCase):
    def test_version_is_calver(self) -> None:
        m = CALVER.match(muninn.__version__)
        self.assertIsNotNone(m, f"{muninn.__version__!r} is not CalVer")
        assert m is not None
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        dt.date(year, month, day)  # raises on an impossible date

    def test_version_matches_pyproject(self) -> None:
        root = Path(__file__).resolve().parent.parent
        with (root / "pyproject.toml").open("rb") as fh:
            data = tomllib.load(fh)
        self.assertEqual(muninn.__version__, data["project"]["version"])


if __name__ == "__main__":
    unittest.main()
