from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from muninn import app_bundle


class ApplicationBundleTest(unittest.TestCase):
    def test_install_builds_a_managed_user_application(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(app_bundle.sys, "platform", "darwin"), \
                patch.object(app_bundle.Path, "home", return_value=Path(tmp)):
            bundle = app_bundle.install()
            self.assertEqual(bundle, Path(tmp) / "Applications" / "Muninn.app")
            with (bundle / "Contents" / "Info.plist").open("rb") as stream:
                self.assertEqual(plistlib.load(stream)["CFBundleIdentifier"], app_bundle.BUNDLE_ID)
            launcher = bundle / "Contents" / "MacOS" / "Muninn"
            self.assertTrue(launcher.stat().st_mode & 0o111)
            self.assertIn("muninn.cli serve", launcher.read_text())
            self.assertTrue(app_bundle.uninstall())
            self.assertFalse(bundle.exists())

    def test_install_refuses_an_unrelated_application(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(app_bundle.sys, "platform", "darwin"), \
                patch.object(app_bundle.Path, "home", return_value=Path(tmp)):
            info = Path(tmp) / "Applications" / "Muninn.app" / "Contents" / "Info.plist"
            info.parent.mkdir(parents=True)
            info.write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.example.other"}))
            with self.assertRaisesRegex(RuntimeError, "unrelated application"):
                app_bundle.install()
