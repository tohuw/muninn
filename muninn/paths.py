"""Filesystem locations Muninn's own state lives at.

Split out of ``cli.py`` for one reason: the ``SessionEnd`` hook
(``muninn/hooks/cli.py``) must resolve ``STATE_DIR``/``QUEUE_DIR`` without
importing anything that pulls in ``sqlite3`` or ``muninn.store``. ``cli.py``,
``ingest.py`` and ``queue.py`` all import this module, so it has to stay free
of any heavy or store-touching import forever, not just today -- see
.valholl/articles/session-lifecycle-facts.md, "A hook may only enqueue, never
index." (SessionEnd shares a 1.5s budget across every SessionEnd hook and does
not support ``async: true``; opening the archive here would blow that budget.)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.platform == "win32":
    STATE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Muninn"
else:
    STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "muninn"

DB_PATH = STATE_DIR / "muninn.db"
QUEUE_DIR = STATE_DIR / "queue"


def default_roots() -> dict[str, Path]:
    """Where transcripts live, per source. ``$CODEX_HOME`` is honored."""
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return {
        "claude": Path.home() / ".claude" / "projects",
        "codex": codex_home / "sessions",
    }
