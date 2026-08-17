"""Managed macOS application bundle for the local Muninn checkout.

The login agent is still the supervisor.  This small bundle is a Finder and
Spotlight entry point for the same checkout; it does not become a second
installer or retain any archive state.
"""
from __future__ import annotations

import os
import plistlib
import shlex
import sys
import tempfile
from pathlib import Path

from .agent_install import REPO_ROOT

NAME = "Muninn"
BUNDLE_ID = "is.tohuw.muninn"


def bundle_path() -> Path:
    """The per-user Applications entry, never the system-wide Applications dir."""
    return Path.home() / "Applications" / f"{NAME}.app"


def _managed(bundle: Path) -> bool:
    try:
        with (bundle / "Contents" / "Info.plist").open("rb") as stream:
            return plistlib.load(stream).get("CFBundleIdentifier") == BUNDLE_ID
    except (OSError, plistlib.InvalidFileException):
        return False


def _write(path: Path, contents: bytes, mode: int) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(contents)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def install() -> Path | None:
    """Build the managed bundle on macOS, refusing to overwrite another app."""
    if sys.platform != "darwin":
        return None
    bundle = bundle_path()
    if bundle.is_symlink() or (bundle.exists() and not _managed(bundle)):
        raise RuntimeError(f"refusing to overwrite unrelated application: {bundle}")

    macos_dir = bundle / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "CFBundleName": NAME,
        "CFBundleDisplayName": NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleVersion": "1.0",
        "CFBundleShortVersionString": "1.0",
        "CFBundleExecutable": NAME,
        "LSUIElement": True,
    }
    _write(bundle / "Contents" / "Info.plist", plistlib.dumps(info), 0o644)
    launcher = "#!/bin/sh\ncd " + shlex.quote(str(REPO_ROOT)) + "\nexec " + shlex.join(
        [sys.executable, "-m", "muninn.cli", "serve"]
    ) + "\n"
    _write(macos_dir / NAME, launcher.encode(), 0o755)
    return bundle


def uninstall() -> bool:
    """Remove only the bundle this checkout created."""
    bundle = bundle_path()
    if not _managed(bundle):
        return False
    import shutil
    shutil.rmtree(bundle)
    return True
