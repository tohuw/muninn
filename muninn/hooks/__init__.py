"""The SessionEnd hook and its installer.

Everything under this package must stay importable without pulling in
``sqlite3`` or ``muninn.store`` -- see ``muninn/hooks/cli.py`` for why.
"""
from __future__ import annotations
