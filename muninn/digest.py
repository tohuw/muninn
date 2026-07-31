"""Content digests that give an import source a stable identity.

See .valholl/articles/import-ledger-schema.md ("Digest scheme"). Two schemes,
deliberately kept separate:

- ``digest_items`` / ``digest_file`` identify an *export's content* — a pure
  function of (item_id, updated_at) pairs, so re-importing the same export is
  detected by identity rather than inferred from counters.
- ``digest_tree`` identifies a *live transcript tree as observed at a moment*.
  Live trees are append-only and grow constantly; there is no stable
  (item_id -> updated_at) mapping for append-only JSONL, and pretending
  otherwise would fabricate an identity that does not exist. Its purpose is
  attributing a scan, not detecting duplicate imports.

All three return a digest prefixed with its scheme name, so a future scheme
change cannot silently collide with an old one.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable

_CHUNK_SIZE = 1024 * 1024  # 1 MiB


def digest_items(source_kind: str, pairs: Iterable[tuple[str, str]]) -> str:
    """``items-sha256:<hex>`` over source_kind then sorted "id\\tupdated_at" lines.

    ``updated_at`` must be the source's own raw representation, stringified
    verbatim — never a converted one. claude.ai gives an ISO string; ChatGPT
    gives an epoch float. Converting float -> ISO inside the digest boundary
    would make the digest depend on sub-second float formatting and therefore
    on the Python version, which breaks the "same content, same digest"
    guarantee the whole duplicate-detection scheme depends on.

    ``source_kind`` is included in the preimage because both vendors ship a
    top-level JSON array in a file named ``conversations.json``; only the
    presence of ``mapping`` (ChatGPT) versus ``chat_messages``/``uuid``
    (claude.ai) tells them apart, so without a discriminator two genuinely
    different exports could collide on digest.
    """
    lines = sorted(f"{item_id}\t{updated_at}" for item_id, updated_at in pairs)
    preimage = source_kind + "\n" + "\n".join(lines)
    h = hashlib.sha256(preimage.encode("utf-8"))
    return f"items-sha256:{h.hexdigest()}"


def digest_file(path: Path) -> str:
    """``file-sha256:<hex>``, streamed in chunks so large exports do not need
    to fit in memory. Recorded alongside ``digest_items``: it is nearly free
    and it catches upstream schema changes that leave the (id, updated_at)
    pairs identical, and it is the only usable identity when ``updated_at`` is
    absent altogether.
    """
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while True:
            chunk = fh.read(_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return f"file-sha256:{h.hexdigest()}"


def digest_tree(root: Path, paths: Iterable[Path]) -> str:
    """``tree-sha256:<hex>`` over sorted "relpath\\tsize\\tmtime_ns" lines.

    ``relpath`` is POSIX-style relative to ``root`` so the digest is stable
    across platforms and across moving the tree to a new location. ``mtime_ns``
    comes from ``stat().st_mtime_ns`` — the integer nanosecond field, never the
    float ``st_mtime`` — because float mtimes are not reproducible across
    filesystems (precision varies) and would make the digest flap for a tree
    that has not actually changed.
    """
    root = Path(root)
    lines = []
    for path in paths:
        st = path.stat()
        rel = path.relative_to(root).as_posix()
        lines.append(f"{rel}\t{st.st_size}\t{st.st_mtime_ns}")
    preimage = "\n".join(sorted(lines))
    h = hashlib.sha256(preimage.encode("utf-8"))
    return f"tree-sha256:{h.hexdigest()}"
