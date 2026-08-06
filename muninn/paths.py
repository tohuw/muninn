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
from typing import Mapping


def state_dir(env: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    """Muninn's own state directory, resolved against ``env``/``home``.

    Parameterised rather than inlined at module scope so ``agent_install`` can
    ask the one question an installer has to answer: *where would this resolve
    for a process that does not inherit my shell?* Answering that by
    re-implementing the rule there would put two copies of it in the tree, and
    the copy a reader finds first is then the one they trust — the mistake
    ``muninn/agent_install.py`` already refuses to make about corvidae's
    hardening. Callers with no such question keep using :data:`STATE_DIR`.

    An empty-but-set variable falls back to the default instead of resolving to
    ``Path("")``, matching ``raven.state_dir``'s ``.strip()`` handling. The two
    functions resolve different directories on purpose (see that docstring), but
    they must not disagree about what "set" means.
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    if sys.platform == "win32":
        local = (env.get("LOCALAPPDATA") or "").strip()
        return (Path(local) if local else home / "AppData/Local") / "Muninn"
    xdg = (env.get("XDG_STATE_HOME") or "").strip()
    return (Path(xdg) if xdg else home / ".local/state") / "muninn"


STATE_DIR = state_dir()
DB_PATH = STATE_DIR / "muninn.db"
QUEUE_DIR = STATE_DIR / "queue"


def default_roots(env: Mapping[str, str] | None = None,
                  home: Path | None = None) -> dict[str, Path]:
    """Where transcripts live, per source. ``$CODEX_HOME`` is honored.

    Takes the same optional ``env``/``home`` as :func:`state_dir`, for the same
    reason: an installed login agent reads transcripts too, and "which corpus
    will the service actually ingest" is half of the question
    ``muninn install-agent`` refuses on.
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    codex_home = (env.get("CODEX_HOME") or "").strip()
    return {
        "claude": home / ".claude" / "projects",
        "codex": (Path(codex_home) if codex_home else home / ".codex") / "sessions",
    }
