"""Source adapters: raw transcripts are the source of truth.

Two hard-won rules encoded here:

1. **Parse raw transcripts, not derived indexes.** Calibrating from a derived
   prose index undercounted conversations by 15-27% because the index was stale.
   See .valholl/articles/derived-calibration.md.
2. **Fail soft on every field and every record.** The JSONL format is
   explicitly not a stable API and can change on any release, so a missing or
   renamed key is a counted parse failure, never an exception.
   See .valholl/articles/unstable-jsonl-format.md.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

# Directories whose sessions are programmatic rather than conversational.
TOOL_CWD_MARKERS = (
    "/.local/state/", "/.cache/", "/Library/Caches/", "/.thlibo/",
    "\\AppData\\Local\\", "/AppData/Local/",
)
# Entrypoints that mark an SDK/programmatic invocation.
TOOL_ENTRYPOINTS = {"sdk-cli", "sdk", "sdk-ts", "sdk-py"}

HUMAN = "human"
TOOL_INVOKED = "tool-invoked"
SUBAGENT = "subagent"


@dataclass
class ParsedSession:
    """Everything one transcript yields. Fields are optional by design."""

    session_id: str
    source: str
    text: str = ""
    provenance: str = HUMAN
    parent_id: str | None = None
    cwd: str | None = None
    branch: str | None = None
    model: str | None = None
    title: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_s: float | None = None
    user_turns: int = 0
    assistant_turns: int = 0
    tool_uses: int = 0
    tool_results: int = 0
    tokens: int | None = None
    files: set[str] = field(default_factory=set)
    tools: dict[str, int] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)

    @property
    def words(self) -> int:
        return len(self.text.split())

    def note_failure(self, category: str) -> None:
        self.failures[category] = self.failures.get(category, 0) + 1


def _iso(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _seconds_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        a = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (b - a).total_seconds()


def iter_json_lines(path: Path, start_offset: int = 0) -> Iterator[tuple[dict, int]]:
    """Yield ``(record, offset_after)`` pairs, skipping unparsable lines.

    A trailing partial line (the writer is still appending) is not consumed:
    the offset returned for the last complete line is what gets persisted, so
    the partial record is re-read next pass rather than lost or half-parsed.
    """
    with path.open("rb") as fh:
        fh.seek(start_offset)
        offset = start_offset
        for raw in fh:
            if not raw.endswith(b"\n"):
                break  # incomplete final line; leave offset before it
            offset += len(raw)
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line.decode("utf-8", errors="replace"))
            except (ValueError, UnicodeDecodeError):
                yield ({"__parse_error__": "malformed_json"}, offset)
                continue
            if not isinstance(rec, dict):
                yield ({"__parse_error__": "not_an_object"}, offset)
                continue
            yield (rec, offset)


def classify(sess: ParsedSession, *, entrypoint: str | None = None,
             is_subagent: bool = False) -> str:
    """Provenance is structural, never length-based.

    92% of one real corpus was tool-invoked `claude -p` calls. Pooling them with
    conversations distorted every statistic by ~40x.
    See .valholl/articles/provenance-classification.md.
    """
    if is_subagent:
        return SUBAGENT
    if entrypoint and entrypoint.lower() in TOOL_ENTRYPOINTS:
        return TOOL_INVOKED
    cwd = sess.cwd or ""
    if any(marker in cwd for marker in TOOL_CWD_MARKERS):
        return TOOL_INVOKED
    if sess.user_turns == 0:
        return TOOL_INVOKED
    if sess.user_turns <= 1 and sess.duration_s is not None and sess.duration_s < 2.0:
        return TOOL_INVOKED
    return HUMAN


# -- Claude Code -----------------------------------------------------------

_FILE_TOOLS = {"Read", "Edit", "Write", "NotebookEdit", "MultiEdit"}


def parse_claude(path: Path, start_offset: int = 0) -> tuple[ParsedSession, int]:
    """Parse a Claude Code transcript JSONL."""
    is_subagent = path.stem.startswith("agent-") or "subagents" in path.parts
    sess = ParsedSession(session_id=path.stem, source="claude")
    parts: list[str] = []
    entrypoint: str | None = None
    first_ts: str | None = None
    last_ts: str | None = None
    offset = start_offset
    tokens = 0

    for rec, offset in iter_json_lines(path, start_offset):
        if "__parse_error__" in rec:
            sess.note_failure(rec["__parse_error__"])
            continue

        # A subagent transcript carries its PARENT's sessionId. Trusting it
        # would collapse every subagent onto the parent row and silently drop
        # the subagent's prose, so the filename stem stays authoritative here.
        if not is_subagent:
            sess.session_id = rec.get("sessionId") or sess.session_id
        elif sess.parent_id is None:
            sess.parent_id = rec.get("sessionId")
        sess.cwd = sess.cwd or rec.get("cwd")
        sess.branch = sess.branch or rec.get("gitBranch")
        entrypoint = entrypoint or rec.get("entrypoint")
        if is_subagent:
            sess.parent_id = _claude_parent_id(path) or sess.parent_id

        ts = _iso(rec.get("timestamp"))
        if ts:
            first_ts = first_ts or ts
            last_ts = ts

        rtype = rec.get("type")
        message = rec.get("message")
        if not isinstance(message, dict):
            if rtype in ("user", "assistant"):
                sess.note_failure("missing_message")
            continue

        sess.model = sess.model or message.get("model")
        usage = message.get("usage")
        if isinstance(usage, dict):
            for key in ("input_tokens", "output_tokens"):
                v = usage.get(key)
                if isinstance(v, int):
                    tokens += v

        content = message.get("content")
        if isinstance(content, str):
            blocks: list[Any] = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            blocks = content
        else:
            sess.note_failure("unreadable_content")
            continue

        if rtype == "user":
            sess.user_turns += 1
        elif rtype == "assistant":
            sess.assistant_turns += 1

        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    label = "USER" if rtype == "user" else "ASSISTANT"
                    parts.append(f"[{label}] {text}")
            elif btype == "tool_use":
                sess.tool_uses += 1
                name = block.get("name")
                if isinstance(name, str):
                    sess.tools[name] = sess.tools.get(name, 0) + 1
                    if name in _FILE_TOOLS:
                        inp = block.get("input")
                        if isinstance(inp, dict):
                            fp = inp.get("file_path") or inp.get("notebook_path")
                            if isinstance(fp, str) and fp:
                                sess.files.add(fp)
            elif btype == "tool_result":
                sess.tool_results += 1
            # thinking / unknown block types are intentionally ignored

    sess.text = "\n\n".join(parts)
    sess.started_at, sess.ended_at = first_ts, last_ts
    sess.duration_s = _seconds_between(first_ts, last_ts)
    sess.tokens = tokens or None
    sess.provenance = classify(sess, entrypoint=entrypoint, is_subagent=is_subagent)
    return sess, offset


def _claude_parent_id(path: Path) -> str | None:
    """``.../<parent-session-id>/subagents/agent-x.jsonl`` -> parent id."""
    parts = path.parts
    if "subagents" in parts:
        i = parts.index("subagents")
        if i >= 1:
            candidate = parts[i - 1]
            if candidate not in ("workflows",):
                return candidate
    return None


# -- Codex -----------------------------------------------------------------


def parse_codex(path: Path, start_offset: int = 0) -> tuple[ParsedSession, int]:
    """Parse a Codex rollout JSONL."""
    sess = ParsedSession(session_id=path.stem, source="codex")
    parts: list[str] = []
    first_ts: str | None = None
    last_ts: str | None = None
    offset = start_offset
    tokens = 0

    for rec, offset in iter_json_lines(path, start_offset):
        if "__parse_error__" in rec:
            sess.note_failure(rec["__parse_error__"])
            continue

        ts = _iso(rec.get("timestamp"))
        if ts:
            first_ts = first_ts or ts
            last_ts = ts

        rtype = rec.get("type")
        if rtype == "turn_context":
            sess.cwd = sess.cwd or rec.get("cwd")
            sess.model = sess.model or rec.get("model")
            sess.branch = sess.branch or rec.get("git_branch")
            continue

        payload = rec.get("payload")
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type")

        if ptype == "user_message":
            text = payload.get("message")
            if isinstance(text, str) and text.strip():
                sess.user_turns += 1
                parts.append(f"[USER] {text}")
        elif ptype == "agent_message":
            text = payload.get("message")
            if isinstance(text, str) and text.strip():
                sess.assistant_turns += 1
                parts.append(f"[ASSISTANT] {text}")
        elif ptype == "token_count":
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                v = payload.get(key)
                if isinstance(v, int):
                    tokens = max(tokens, v) if key == "total_tokens" else tokens + v
        elif isinstance(ptype, str) and ptype.startswith("exec"):
            sess.tool_uses += 1
            sess.tools["exec"] = sess.tools.get("exec", 0) + 1

    # Codex session ids live in the filename: rollout-<ts>-<uuid>.jsonl
    stem = path.stem
    if stem.startswith("rollout-"):
        tail = stem.split("-", 2)[-1]
        sess.session_id = tail[tail.find("T") + 1:].lstrip("0123456789-") or tail

    sess.text = "\n\n".join(parts)
    sess.started_at, sess.ended_at = first_ts, last_ts
    sess.duration_s = _seconds_between(first_ts, last_ts)
    sess.tokens = tokens or None
    sess.provenance = classify(sess)
    return sess, offset


PARSERS = {"claude": parse_claude, "codex": parse_codex}


def discover(root: Path, source: str) -> Iterator[Path]:
    """Yield transcript paths for ``source`` under ``root``, sorted for determinism."""
    if not root.is_dir():
        return
    pattern = "*.jsonl" if source == "claude" else "rollout-*.jsonl"
    for path in sorted(root.rglob(pattern)):
        if source == "claude" and path.name == "journal.jsonl":
            continue  # workflow event logs are not conversations
        yield path
