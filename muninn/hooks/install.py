"""Idempotently wire the ``muninn-hook session-end`` command into settings.json.

This module — unlike ``muninn/hooks/cli.py`` — is allowed to import freely; it
runs from the CLI (``muninn install-hooks``), never from inside the
SessionEnd hook itself, so the 1.5-second budget does not apply here.

The one rule that matters more than any other in this file: ``settings.json``
is the user's real, live configuration, already carrying hooks and settings
this tool knows nothing about (CwdChanged handlers, PreToolUse matchers, model
overrides, ...). Every write here is read -> merge -> atomic replace, and nets
out preserving every key this tool did not put there — never a blind
overwrite. See docs/specs/003-background-indexer.md, Step 3.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


def default_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def hook_command(*, python_exe: str | None = None) -> str:
    """The exact command string written into settings.json.

    Resolving to an absolute interpreter-free console-script path (rather
    than ``python -m muninn.hooks.cli``) means the hook keeps working even if
    the invoking shell's PATH/venv differs from whatever environment installed
    muninn — Claude Code invokes hook commands via its own shell, which may
    not have this project's virtualenv activated.
    """
    exe = shutil.which("muninn-hook")
    if exe:
        return f"{exe} session-end"
    # Fall back to a module invocation with an explicit interpreter path if
    # the console script is not on PATH (e.g. running from a source checkout
    # without an editable install) -- still absolute, still robust to PATH
    # differences in the hook's invoking shell.
    py = python_exe or sys.executable
    return f"{py} -m muninn.hooks.cli session-end"


def _read_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    return json.loads(text)


def _has_muninn_entry(settings: dict[str, Any], command: str) -> bool:
    session_end = settings.get("hooks", {}).get("SessionEnd", [])
    for matcher_block in session_end:
        for hook in matcher_block.get("hooks", []):
            if hook.get("type") == "command" and hook.get("command") == command:
                return True
    return False


def _merge(settings: dict[str, Any], command: str) -> dict[str, Any]:
    """Return a new dict: ``settings`` with the muninn SessionEnd hook added.

    Never mutates ``settings`` in place, so a caller doing a dry-run
    (``--check``) can diff before/after without the read having side effects.
    Every other top-level key, every other SessionEnd matcher block, is
    copied through untouched -- this is the one property the guardrails
    demand above all else.
    """
    out = dict(settings)
    hooks = dict(out.get("hooks", {}))
    session_end = list(hooks.get("SessionEnd", []))

    if _has_muninn_entry(out, command):
        return out  # already wired; re-running must not duplicate

    # async is deliberately never set: SessionEnd does not support it (see
    # session-lifecycle-facts.md), and setting it anyway would be silently
    # ignored at best or a source of confusion at worst.
    session_end.append({"hooks": [{"type": "command", "command": command}]})
    hooks["SessionEnd"] = session_end
    out["hooks"] = hooks
    return out


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".muninn-settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class InstallResult:
    def __init__(self, *, changed: bool, already_installed: bool,
                command: str, settings_path: Path, backup_path: Path | None) -> None:
        self.changed = changed
        self.already_installed = already_installed
        self.command = command
        self.settings_path = settings_path
        self.backup_path = backup_path


def install(*, settings_path: Path | None = None, command: str | None = None,
           check_only: bool = False) -> InstallResult:
    """Read -> merge -> atomic write. ``check_only=True`` never writes.

    Backs up to ``settings.json.muninn-bak`` before the FIRST write only —
    a second run that is already idempotent (no change needed) must not
    overwrite that backup with a copy of the already-migrated file, or the
    backup would stop being useful as "what the file looked like before
    muninn ever touched it."
    """
    path = settings_path or default_settings_path()
    cmd = command or hook_command()

    settings = _read_settings(path)
    already = _has_muninn_entry(settings, cmd)
    merged = _merge(settings, cmd)
    changed = merged != settings

    if check_only or not changed:
        return InstallResult(changed=False, already_installed=already,
                             command=cmd, settings_path=path, backup_path=None)

    backup_path = path.with_name(path.name + ".muninn-bak")
    if path.exists() and not backup_path.exists():
        shutil.copy2(path, backup_path)
    elif not path.exists():
        backup_path = None

    _atomic_write(path, merged)
    return InstallResult(changed=True, already_installed=already,
                         command=cmd, settings_path=path, backup_path=backup_path)
