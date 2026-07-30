#!/usr/bin/env python3
"""Muninn corpus survey — anonymous, statistics-only calibration data collector.

WHAT THIS IS
------------
Muninn is an agent-history search/archive console. To pick sane defaults
(enrichment gates, chunk sizes, retention urgency, provenance heuristics) we need
to know what real agent-transcript corpora look like on real developer machines.
This script measures your local corpus and writes a JSON report of *statistics
only*.

THE PRIVACY GUARANTEE
---------------------
The report contains no prose and no identifiers. Specifically it never contains:

  * message text, prompts, replies, thinking blocks, tool inputs or tool outputs
  * file contents, session titles, git branch names, error message strings
  * paths, usernames, hostnames, repository names, URLs, environment values

Path-derived signals (e.g. "how many distinct projects, how skewed is the
distribution") are emitted only as a salted SHA-256 prefix (8 hex chars) plus
structural facts such as path depth and whether the directory is under a
state/cache directory. The salt is random per run and is never written to the
report, so hashes cannot be reversed or correlated across runs.

Everything emitted is a count, a length, a duration, a month bucket ("2026-07"),
an age-in-days bucket, a model identifier, an enum-like classification, or a
salted hash prefix.

Model identifiers (e.g. "claude-sonnet-5", "gpt-5-codex") are emitted because
they are useful and not sensitive; anything resembling an ARN, an account id, a
custom endpoint or a URL is replaced with the literal "custom/redacted".

The `--self-test` flag proves the above: it builds a synthetic corpus containing
planted secret strings, surveys it, and asserts that none of those strings (and
no salt) appear anywhere in the serialized output, while also asserting that the
statistics are computed correctly.

OTHER PROPERTIES
----------------
  * Read-only. Nothing under a transcript source is ever written, moved or
    deleted. The only file written is the report itself, in the current
    directory.
  * Stdlib only, Python 3.10+. Runs as `python3 corpus-survey.py`.
  * Deterministic: the same corpus yields the same statistics. The only run-to-run
    differences are the anonymization salt, the run timestamp, and age-in-days.
  * Bounded memory: transcripts are streamed line by line; nothing but per-session
    scalars is retained.
  * Never crashes on permission errors, unreadable files, malformed JSON or
    truncated lines — those are counted as categorized parse failures.

You are encouraged to read the report before sharing it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

SCHEMA_VERSION = 1
SCRIPT_VERSION = "1.0.0"

SOURCES: tuple[str, ...] = ("claude", "codex")
CLASSES: tuple[str, ...] = ("human", "tool-invoked", "subagent")

# Directory markers that indicate a programmatic caller rather than a human
# working directory. Compared against a lowercased, forward-slash-normalized cwd.
STATE_DIR_MARKERS: tuple[str, ...] = (
    "/.local/state/",
    "/.cache/",
    "/library/caches/",
    "appdata/local/",
    "appdata/roaming/",
    "/.thlibo/",
)

# Provenance rule names (enum-like; emitted as counts).
RULE_SUBAGENT_PATH = "subagent_path"
RULE_NESTED_PATH = "nested_transcript_path"
RULE_SIDECHAIN_FLAG = "sidechain_flag"
RULE_ENTRYPOINT_SDK_CLI = "entrypoint_sdk_cli"
RULE_ORIGINATOR_NON_INTERACTIVE = "originator_non_interactive"
RULE_CWD_STATE_DIR = "cwd_state_dir"
RULE_ZERO_USER_TURNS = "zero_user_turns"
RULE_SINGLE_TURN_FAST = "single_user_turn_under_2s"
RULE_DEFAULT_HUMAN = "default_human"

# Parse-failure reason categories (enum-like; never message text).
FAIL_JSON_DECODE = "json_decode_error"
FAIL_LINE_NOT_OBJECT = "line_not_json_object"
FAIL_EMPTY_FILE = "empty_file"
FAIL_UNREADABLE_FILE = "unreadable_file"
FAIL_READ_ERROR = "read_error_midfile"
FAIL_NO_TIMESTAMP = "no_usable_timestamp"
FAIL_NO_ENTRIES = "no_recognized_entries"

# Codex originators that are not an interactive human session.
NON_INTERACTIVE_ORIGINATORS: tuple[str, ...] = (
    "codex-exec",
    "codex_exec",
    "codex-mcp",
    "codex_mcp",
    "codex-sdk",
    "exec",
    "mcp",
    "sdk",
)

CHUNK_TARGET_WORDS = 400
CHUNK_STRIDE_WORDS = 320
COVERAGE_TARGET_PCT = 85.0
AGE_BUCKET_DAYS = 10

PERCENTILES: tuple[int, ...] = (10, 25, 50, 75, 90, 95, 99)

MODEL_SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@\-\[\]]{0,63}$")
LONG_DIGIT_RUN_RE = re.compile(r"\d{10,}")
MODEL_GEO_PREFIXES = ("us-gov.", "us.", "eu.", "apac.", "global.")
MODEL_PROVIDER_PREFIXES = (
    "anthropic.",
    "anthropic/",
    "openai/",
    "bedrock/",
    "vertex_ai/",
    "vertex/",
    "azure/",
    "litellm/",
    "openrouter/",
)
MODEL_ALWAYS_ALLOWED = ("<synthetic>",)
MODEL_REDACTED = "custom/redacted"


# --------------------------------------------------------------------------- #
# Small numeric helpers
# --------------------------------------------------------------------------- #


def percentile(sorted_values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile of an already-sorted sequence (deterministic)."""
    if not sorted_values:
        return None
    rank = math.ceil(pct / 100.0 * len(sorted_values))
    index = min(max(rank - 1, 0), len(sorted_values) - 1)
    return sorted_values[index]


def _round_number(value: float | None) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if float(value).is_integer():
        return int(value)
    return round(float(value), 3)


def summarize(values: Iterable[float]) -> dict[str, Any]:
    """Distribution summary: count, sum, min, p10..p99, max, mean."""
    ordered = sorted(values)
    out: dict[str, Any] = {"count": len(ordered)}
    if not ordered:
        out["sum"] = 0
        out["min"] = None
        for pct in PERCENTILES:
            out[f"p{pct}" if pct != 50 else "median"] = None
        out["max"] = None
        out["mean"] = None
        return out
    total = sum(ordered)
    out["sum"] = _round_number(total)
    out["min"] = _round_number(ordered[0])
    for pct in PERCENTILES:
        key = "median" if pct == 50 else f"p{pct}"
        out[key] = _round_number(percentile(ordered, pct))
    out["max"] = _round_number(ordered[-1])
    out["mean"] = _round_number(total / len(ordered))
    return out


def estimate_chunks(words: int, target: int = CHUNK_TARGET_WORDS,
                    stride: int = CHUNK_STRIDE_WORDS) -> int:
    """Number of overlapping windows of `target` words advancing by `stride`."""
    if words <= 0:
        return 0
    if words <= target:
        return 1
    return 1 + math.ceil((words - target) / stride)


def derive_enrichment_gate(word_counts: Sequence[int],
                           target_coverage_pct: float = COVERAGE_TARGET_PCT) -> dict[str, Any]:
    """Smallest set of longest sessions whose words cover target_coverage_pct."""
    descending = sorted(word_counts, reverse=True)
    total_words = sum(descending)
    if not descending or total_words == 0:
        return {
            "target_coverage_pct": target_coverage_pct,
            "threshold_words": 0,
            "sessions": 0,
            "conversations_total": len(descending),
            "coverage_pct": 0.0,
            "share_of_conversations_pct": 0.0,
        }
    running = 0
    for position, words in enumerate(descending, start=1):
        running += words
        if running / total_words * 100.0 >= target_coverage_pct:
            return {
                "target_coverage_pct": target_coverage_pct,
                "threshold_words": words,
                "sessions": position,
                "conversations_total": len(descending),
                "coverage_pct": round(running / total_words * 100.0, 2),
                "share_of_conversations_pct": round(position / len(descending) * 100.0, 2),
            }
    return {
        "target_coverage_pct": target_coverage_pct,
        "threshold_words": descending[-1],
        "sessions": len(descending),
        "conversations_total": len(descending),
        "coverage_pct": 100.0,
        "share_of_conversations_pct": 100.0,
    }


# --------------------------------------------------------------------------- #
# Anonymization
# --------------------------------------------------------------------------- #


def normalize_path_for_hash(raw_path: str) -> str:
    """Case- and separator-normalized path string, for stable hashing only."""
    text = raw_path.replace("\\", "/").rstrip("/")
    if os.name == "nt":
        text = text.lower()
    return text


def hash_path(raw_path: str, salt: str) -> str:
    """Stable, salted, non-reversible 8-hex-char tag for a path. Never the path."""
    normalized = normalize_path_for_hash(raw_path)
    digest = hashlib.sha256(f"{salt}\x00{normalized}".encode("utf-8")).hexdigest()
    return digest[:8]


def path_depth(raw_path: str) -> int:
    """Number of non-empty components in a path (structural, not the path)."""
    normalized = normalize_path_for_hash(raw_path)
    return len([part for part in normalized.split("/") if part and part != "."])


def is_under_state_dir(raw_path: str) -> bool:
    lowered = raw_path.replace("\\", "/").lower()
    if not lowered.endswith("/"):
        lowered += "/"
    return any(marker in lowered for marker in STATE_DIR_MARKERS)


def normalize_model_identifier(raw_model: Any) -> str | None:
    """Keep plain model names; redact ARNs, account ids, endpoints, oddities."""
    if not isinstance(raw_model, str):
        return None
    model = raw_model.strip()
    if not model:
        return None
    if model in MODEL_ALWAYS_ALLOWED:
        return model
    lowered = model.lower()
    if "arn:" in lowered or "://" in lowered or "@" in lowered:
        return MODEL_REDACTED
    if LONG_DIGIT_RUN_RE.search(model):
        return MODEL_REDACTED
    # Strip routing/provider prefixes repeatedly: real Bedrock ids look like
    # "us.anthropic.claude-sonnet-5" (geo prefix then provider prefix).
    for _ in range(4):
        for prefix in MODEL_GEO_PREFIXES + MODEL_PROVIDER_PREFIXES:
            if lowered.startswith(prefix):
                model = model[len(prefix):]
                lowered = model.lower()
                break
        else:
            break
    if not model:
        return MODEL_REDACTED
    if "/" in model or not MODEL_SAFE_RE.match(model):
        return MODEL_REDACTED
    return model


# --------------------------------------------------------------------------- #
# Per-session measurement record
# --------------------------------------------------------------------------- #


@dataclass
class SessionStat:
    """Scalars measured for one transcript file. No text is ever retained."""

    source: str
    provenance_class: str = "human"
    rules_fired: tuple[str, ...] = ()
    user_turns: int = 0
    assistant_turns: int = 0
    prose_words: int = 0
    prose_bytes: int = 0
    tool_use_count: int = 0
    tool_result_count: int = 0
    thinking_count: int = 0
    raw_bytes: int = 0
    duration_seconds: float | None = None
    start_month: str | None = None
    age_days: float | None = None
    models: tuple[str, ...] = ()
    tokens: dict[str, int] = field(default_factory=dict)
    project_hash: str | None = None
    cwd_depth: int | None = None
    cwd_under_state_dir: bool = False


@dataclass
class SessionDraft:
    """Mutable accumulator used while streaming a single transcript file."""

    source: str
    raw_bytes: int
    is_nested_path: bool = False
    is_subagent_path: bool = False
    user_turns: int = 0
    assistant_turns: int = 0
    prose_words: int = 0
    prose_bytes: int = 0
    tool_use_count: int = 0
    tool_result_count: int = 0
    thinking_count: int = 0
    saw_sidechain_true: bool = False
    saw_sidechain_false: bool = False
    saw_sdk_cli_entrypoint: bool = False
    non_interactive_originator: bool = False
    first_ts: dt.datetime | None = None
    last_ts: dt.datetime | None = None
    models: set[str] = field(default_factory=set)
    tokens: dict[str, int] = field(default_factory=dict)
    cwd: str | None = None
    project_key: str | None = None
    entries_seen: int = 0

    def note_text(self, text: str) -> None:
        self.prose_words += len(text.split())
        self.prose_bytes += len(text.encode("utf-8", errors="replace"))

    def note_timestamp(self, moment: dt.datetime | None) -> None:
        if moment is None:
            return
        if self.first_ts is None or moment < self.first_ts:
            self.first_ts = moment
        if self.last_ts is None or moment > self.last_ts:
            self.last_ts = moment

    def add_tokens(self, key: str, amount: Any) -> None:
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            return
        if amount <= 0:
            return
        self.tokens[key] = self.tokens.get(key, 0) + int(amount)

    def set_tokens(self, key: str, amount: Any) -> None:
        """For cumulative counters (Codex) keep the maximum observed value."""
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            return
        if amount <= 0:
            return
        self.tokens[key] = max(self.tokens.get(key, 0), int(amount))


# --------------------------------------------------------------------------- #
# Provenance classification
# --------------------------------------------------------------------------- #


def classify_provenance(draft: SessionDraft,
                        duration_seconds: float | None) -> tuple[str, tuple[str, ...]]:
    """Return (class, rules_fired). Structural signals only, never body length."""
    rules: list[str] = []

    if draft.is_subagent_path:
        rules.append(RULE_SUBAGENT_PATH)
    if draft.is_nested_path and not draft.is_subagent_path:
        rules.append(RULE_NESTED_PATH)
    if draft.saw_sidechain_true and not draft.saw_sidechain_false:
        rules.append(RULE_SIDECHAIN_FLAG)
    if draft.saw_sdk_cli_entrypoint:
        rules.append(RULE_ENTRYPOINT_SDK_CLI)
    if draft.non_interactive_originator:
        rules.append(RULE_ORIGINATOR_NON_INTERACTIVE)
    if draft.cwd and is_under_state_dir(draft.cwd):
        rules.append(RULE_CWD_STATE_DIR)
    if draft.user_turns == 0:
        rules.append(RULE_ZERO_USER_TURNS)
    if draft.user_turns == 1 and duration_seconds is not None and duration_seconds < 2.0:
        rules.append(RULE_SINGLE_TURN_FAST)

    subagent_rules = (RULE_SUBAGENT_PATH, RULE_NESTED_PATH, RULE_SIDECHAIN_FLAG)
    tool_rules = (
        RULE_ENTRYPOINT_SDK_CLI,
        RULE_ORIGINATOR_NON_INTERACTIVE,
        RULE_CWD_STATE_DIR,
        RULE_ZERO_USER_TURNS,
        RULE_SINGLE_TURN_FAST,
    )
    if any(rule in rules for rule in subagent_rules):
        return "subagent", tuple(rules)
    if any(rule in rules for rule in tool_rules):
        return "tool-invoked", tuple(rules)
    rules.append(RULE_DEFAULT_HUMAN)
    return "human", tuple(rules)


def finalize(draft: SessionDraft, salt: str, now: dt.datetime,
             failures: dict[str, int]) -> SessionStat | None:
    """Turn a streamed draft into an immutable, anonymized SessionStat."""
    if draft.entries_seen == 0:
        failures[FAIL_NO_ENTRIES] = failures.get(FAIL_NO_ENTRIES, 0) + 1
        return None

    duration: float | None = None
    if draft.first_ts is not None and draft.last_ts is not None:
        duration = max(0.0, (draft.last_ts - draft.first_ts).total_seconds())
    else:
        failures[FAIL_NO_TIMESTAMP] = failures.get(FAIL_NO_TIMESTAMP, 0) + 1

    provenance_class, rules = classify_provenance(draft, duration)

    start_month = draft.first_ts.strftime("%Y-%m") if draft.first_ts else None
    age_days: float | None = None
    if draft.first_ts is not None:
        age_days = max(0.0, (now - draft.first_ts).total_seconds() / 86400.0)

    project_hash = hash_path(draft.project_key, salt) if draft.project_key else None

    return SessionStat(
        source=draft.source,
        provenance_class=provenance_class,
        rules_fired=rules,
        user_turns=draft.user_turns,
        assistant_turns=draft.assistant_turns,
        prose_words=draft.prose_words,
        prose_bytes=draft.prose_bytes,
        tool_use_count=draft.tool_use_count,
        tool_result_count=draft.tool_result_count,
        thinking_count=draft.thinking_count,
        raw_bytes=draft.raw_bytes,
        duration_seconds=duration,
        start_month=start_month,
        age_days=age_days,
        models=tuple(sorted(draft.models)),
        tokens=dict(draft.tokens),
        project_hash=project_hash,
        cwd_depth=path_depth(draft.cwd) if draft.cwd else None,
        cwd_under_state_dir=bool(draft.cwd and is_under_state_dir(draft.cwd)),
    )


# --------------------------------------------------------------------------- #
# Shared JSONL streaming
# --------------------------------------------------------------------------- #


def parse_timestamp(raw: Any) -> dt.datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)


def stream_json_objects(path: Path, failures: dict[str, int]) -> Iterator[dict[str, Any]]:
    """Yield JSON objects from a .jsonl file, one line at a time.

    Never raises. Unreadable files and malformed lines are counted as
    categorized parse failures.
    """
    try:
        handle = path.open("r", encoding="utf-8", errors="replace", newline=None)
    except OSError:
        failures[FAIL_UNREADABLE_FILE] = failures.get(FAIL_UNREADABLE_FILE, 0) + 1
        return
    try:
        with handle:
            while True:
                try:
                    line = handle.readline()
                except (OSError, UnicodeError):
                    failures[FAIL_READ_ERROR] = failures.get(FAIL_READ_ERROR, 0) + 1
                    return
                if not line:
                    return
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except (json.JSONDecodeError, ValueError, RecursionError):
                    failures[FAIL_JSON_DECODE] = failures.get(FAIL_JSON_DECODE, 0) + 1
                    continue
                if not isinstance(obj, dict):
                    failures[FAIL_LINE_NOT_OBJECT] = failures.get(FAIL_LINE_NOT_OBJECT, 0) + 1
                    continue
                yield obj
    except OSError:
        failures[FAIL_READ_ERROR] = failures.get(FAIL_READ_ERROR, 0) + 1


def file_size(path: Path, failures: dict[str, int]) -> int | None:
    try:
        size = path.stat().st_size
    except OSError:
        failures[FAIL_UNREADABLE_FILE] = failures.get(FAIL_UNREADABLE_FILE, 0) + 1
        return None
    if size == 0:
        failures[FAIL_EMPTY_FILE] = failures.get(FAIL_EMPTY_FILE, 0) + 1
        return None
    return size


# --------------------------------------------------------------------------- #
# Claude Code transcripts
# --------------------------------------------------------------------------- #


def _claude_content_items(message: Any) -> Iterator[tuple[str, str]]:
    """Yield (kind, text) pairs. kind is one of text/thinking/tool_use/tool_result."""
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if isinstance(content, str):
        yield ("text", content)
        return
    if not isinstance(content, list):
        return
    for item in content:
        if isinstance(item, str):
            yield ("text", item)
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            yield ("text", item.get("text") if isinstance(item.get("text"), str) else "")
        elif kind == "thinking":
            yield ("thinking", "")
        elif kind == "tool_use":
            yield ("tool_use", "")
        elif kind == "tool_result":
            yield ("tool_result", "")


def _claude_note_usage(draft: SessionDraft, message: Any) -> None:
    if not isinstance(message, dict):
        return
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return
    for key in ("input_tokens", "output_tokens",
                "cache_read_input_tokens", "cache_creation_input_tokens"):
        draft.add_tokens(key, usage.get(key))


def survey_claude_file(path: Path, project_key: str, is_nested: bool,
                       is_subagent: bool, salt: str, now: dt.datetime,
                       failures: dict[str, int]) -> SessionStat | None:
    size = file_size(path, failures)
    if size is None:
        return None

    draft = SessionDraft(source="claude", raw_bytes=size,
                         is_nested_path=is_nested, is_subagent_path=is_subagent,
                         project_key=project_key)

    for entry in stream_json_objects(path, failures):
        entry_type = entry.get("type")
        if entry_type in ("user", "assistant", "system"):
            draft.entries_seen += 1
        elif entry_type is not None:
            draft.entries_seen += 1

        if isinstance(entry.get("cwd"), str) and entry["cwd"] and draft.cwd is None:
            draft.cwd = entry["cwd"]
        if entry.get("entrypoint") == "sdk-cli":
            draft.saw_sdk_cli_entrypoint = True
        sidechain = entry.get("isSidechain")
        if sidechain is True:
            draft.saw_sidechain_true = True
        elif sidechain is False:
            draft.saw_sidechain_false = True

        # Wall-clock span uses every timestamped entry, not just prose turns.
        draft.note_timestamp(parse_timestamp(entry.get("timestamp")))

        if entry_type not in ("user", "assistant"):
            continue

        message = entry.get("message")
        if entry_type == "assistant":
            _claude_note_usage(draft, message)
            model = normalize_model_identifier(
                message.get("model") if isinstance(message, dict) else None)
            if model:
                draft.models.add(model)

        is_meta = bool(entry.get("isMeta")) or bool(entry.get("isCompactSummary"))
        has_prose = False
        for kind, text in _claude_content_items(message):
            if kind == "text":
                if not is_meta and text:
                    has_prose = True
                    draft.note_text(text)
            elif kind == "thinking":
                draft.thinking_count += 1
            elif kind == "tool_use":
                draft.tool_use_count += 1
            elif kind == "tool_result":
                draft.tool_result_count += 1

        if has_prose:
            if entry_type == "user":
                draft.user_turns += 1
            else:
                draft.assistant_turns += 1

    return finalize(draft, salt, now, failures)


def discover_claude_files(projects_dir: Path) -> list[tuple[Path, str, bool, bool]]:
    """Return sorted (path, project_key, is_nested, is_subagent) tuples."""
    discovered: list[tuple[Path, str, bool, bool]] = []
    try:
        project_dirs = sorted(
            (child for child in projects_dir.iterdir() if child.is_dir()),
            key=lambda child: child.name)
    except OSError:
        return discovered

    for project_dir in project_dirs:
        project_key = project_dir.name
        try:
            candidates = sorted(project_dir.rglob("*.jsonl"),
                                key=lambda child: str(child))
        except OSError:
            continue
        for candidate in candidates:
            try:
                relative_parts = candidate.relative_to(project_dir).parts
            except ValueError:
                continue
            is_nested = len(relative_parts) > 1
            is_subagent = "subagents" in relative_parts[:-1] or candidate.name.startswith("agent-")
            discovered.append((candidate, project_key, is_nested, is_subagent))
    return discovered


# --------------------------------------------------------------------------- #
# Codex rollouts
# --------------------------------------------------------------------------- #

CODEX_TOOL_CALL_TYPES = (
    "function_call",
    "custom_tool_call",
    "local_shell_call",
    "web_search_call",
    "computer_call",
)
CODEX_TOOL_OUTPUT_TYPES = (
    "function_call_output",
    "custom_tool_call_output",
    "local_shell_call_output",
    "web_search_call_output",
    "computer_call_output",
)
CODEX_INJECTED_PREFIXES = ("<", "## Memory", "# Memory")


def _codex_is_injected(text: str) -> bool:
    stripped = text.lstrip()
    return any(stripped.startswith(prefix) for prefix in CODEX_INJECTED_PREFIXES)


def _codex_note_token_count(draft: SessionDraft, info: Any) -> None:
    if not isinstance(info, dict):
        return
    totals = info.get("total_token_usage")
    if not isinstance(totals, dict):
        return
    for source_key, target_key in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("cached_input_tokens", "cache_read_input_tokens"),
        ("cache_write_input_tokens", "cache_creation_input_tokens"),
        ("reasoning_output_tokens", "reasoning_output_tokens"),
    ):
        draft.set_tokens(target_key, totals.get(source_key))


def survey_codex_file(path: Path, salt: str, now: dt.datetime,
                      failures: dict[str, int]) -> SessionStat | None:
    size = file_size(path, failures)
    if size is None:
        return None

    draft = SessionDraft(source="codex", raw_bytes=size)

    for entry in stream_json_objects(path, failures):
        entry_type = entry.get("type")
        if entry_type is None:
            continue
        draft.entries_seen += 1
        draft.note_timestamp(parse_timestamp(entry.get("timestamp")))

        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        payload_type = payload.get("type")

        if entry_type == "session_meta":
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd and draft.cwd is None:
                draft.cwd = cwd
                draft.project_key = cwd
            originator = payload.get("originator")
            if isinstance(originator, str):
                lowered = originator.strip().lower()
                if lowered in NON_INTERACTIVE_ORIGINATORS:
                    draft.non_interactive_originator = True
            model = normalize_model_identifier(payload.get("model"))
            if model:
                draft.models.add(model)
            continue

        if entry_type == "turn_context":
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd and draft.cwd is None:
                draft.cwd = cwd
                draft.project_key = cwd
            model = normalize_model_identifier(payload.get("model"))
            if model:
                draft.models.add(model)
            continue

        if entry_type == "event_msg":
            if payload_type == "user_message":
                text = payload.get("message")
                if isinstance(text, str) and text and not _codex_is_injected(text):
                    draft.user_turns += 1
                    draft.note_text(text)
            elif payload_type == "agent_message":
                text = payload.get("message")
                if isinstance(text, str) and text:
                    draft.assistant_turns += 1
                    draft.note_text(text)
            elif payload_type == "token_count":
                _codex_note_token_count(draft, payload.get("info"))
            continue

        if entry_type == "response_item":
            if payload_type in CODEX_TOOL_CALL_TYPES:
                draft.tool_use_count += 1
            elif payload_type in CODEX_TOOL_OUTPUT_TYPES:
                draft.tool_result_count += 1
            elif payload_type == "reasoning":
                draft.thinking_count += 1
            continue

    return finalize(draft, salt, now, failures)


def discover_codex_files(sessions_dir: Path) -> list[Path]:
    try:
        return sorted(sessions_dir.rglob("rollout-*.jsonl"), key=lambda child: str(child))
    except OSError:
        return []


# --------------------------------------------------------------------------- #
# Source discovery
# --------------------------------------------------------------------------- #


@dataclass
class SourceLocation:
    """Where a source's transcripts live. The path itself is never emitted."""

    name: str
    root: Path | None
    transcripts_dir: Path | None
    location_kind: str  # enum: cli-override | env-override | home | appdata | localappdata | absent


def _first_existing(candidates: Sequence[tuple[Path, str]]) -> tuple[Path | None, str]:
    for path, kind in candidates:
        try:
            if path.is_dir():
                return path, kind
        except OSError:
            continue
    return None, "absent"


def locate_claude(override: Path | None) -> SourceLocation:
    if override is not None:
        root = override
        kind = "cli-override"
    else:
        candidates: list[tuple[Path, str]] = []
        env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        if env_dir:
            candidates.append((Path(env_dir), "env-override"))
        candidates.append((Path.home() / ".claude", "home"))
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append((Path(appdata) / "claude", "appdata"))
        localappdata = os.environ.get("LOCALAPPDATA")
        if localappdata:
            candidates.append((Path(localappdata) / "claude", "localappdata"))
        root, kind = _first_existing(candidates)
    if root is None:
        return SourceLocation("claude", None, None, "absent")
    projects = root / "projects"
    try:
        has_projects = projects.is_dir()
    except OSError:
        has_projects = False
    return SourceLocation("claude", root, projects if has_projects else None, kind)


def locate_codex(override: Path | None) -> SourceLocation:
    if override is not None:
        root = override
        kind = "cli-override"
    else:
        candidates: list[tuple[Path, str]] = []
        env_dir = os.environ.get("CODEX_HOME")
        if env_dir:
            candidates.append((Path(env_dir), "env-override"))
        candidates.append((Path.home() / ".codex", "home"))
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append((Path(appdata) / "codex", "appdata"))
        localappdata = os.environ.get("LOCALAPPDATA")
        if localappdata:
            candidates.append((Path(localappdata) / "codex", "localappdata"))
        root, kind = _first_existing(candidates)
    if root is None:
        return SourceLocation("codex", None, None, "absent")
    sessions = root / "sessions"
    try:
        has_sessions = sessions.is_dir()
    except OSError:
        has_sessions = False
    return SourceLocation("codex", root, sessions if has_sessions else None, kind)


def survey_index_dirs(home: Path) -> dict[str, Any]:
    """Presence + file counts for optional prose indexes. No paths emitted."""
    result: dict[str, Any] = {}
    for label, relative in (("claudex_index", ".claudex/index"),
                            ("codexdex_index", ".codexdex/index")):
        directory = home / relative
        try:
            present = directory.is_dir()
        except OSError:
            present = False
        entry: dict[str, Any] = {"present": present}
        if present:
            try:
                entry["file_count"] = sum(
                    1 for child in directory.iterdir() if child.is_file())
            except OSError:
                entry["file_count"] = None
        result[label] = entry
    return result


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #

METRIC_FIELDS: tuple[str, ...] = (
    "prose_words",
    "user_turns",
    "assistant_turns",
    "duration_seconds",
    "tool_use_count",
    "tool_result_count",
    "raw_bytes",
    "prose_bytes",
    "thinking_count",
)


class Aggregator:
    """Accumulates per-session scalars into aggregates and distributions."""

    def __init__(self) -> None:
        self.metrics: dict[tuple[str, str], dict[str, list[float]]] = {}
        self.session_counts: dict[tuple[str, str], int] = {}
        self.months: dict[tuple[str, str], dict[str, int]] = {}
        self.rules: dict[str, dict[str, int]] = {}
        self.models: dict[str, dict[str, int]] = {}
        self.tokens: dict[tuple[str, str], dict[str, int]] = {}
        self.projects: dict[str, dict[str, int]] = {}
        self.project_facts: dict[str, dict[str, Any]] = {}
        self.cwd_depths: dict[str, list[float]] = {}
        self.cwd_state_dir_counts: dict[str, int] = {}
        self.cwd_known_counts: dict[str, int] = {}
        self.age_buckets: dict[str, dict[str, int]] = {}
        self.max_age_days: dict[str, float] = {}
        self.min_age_days: dict[str, float] = {}
        self.human_words: dict[str, list[int]] = {}

    def add(self, stat: SessionStat) -> None:
        source = stat.source
        key = (source, stat.provenance_class)
        self.session_counts[key] = self.session_counts.get(key, 0) + 1

        bucket = self.metrics.setdefault(key, {name: [] for name in METRIC_FIELDS})
        for name in METRIC_FIELDS:
            value = getattr(stat, name)
            if value is not None:
                bucket[name].append(float(value))

        if stat.start_month:
            self.months.setdefault(key, {})
            self.months[key][stat.start_month] = self.months[key].get(stat.start_month, 0) + 1

        source_rules = self.rules.setdefault(source, {})
        for rule in stat.rules_fired:
            source_rules[rule] = source_rules.get(rule, 0) + 1

        source_models = self.models.setdefault(source, {})
        for model in stat.models:
            source_models[model] = source_models.get(model, 0) + 1

        token_bucket = self.tokens.setdefault(key, {})
        for token_key, amount in stat.tokens.items():
            token_bucket[token_key] = token_bucket.get(token_key, 0) + amount

        if stat.project_hash:
            source_projects = self.projects.setdefault(source, {})
            source_projects[stat.project_hash] = source_projects.get(stat.project_hash, 0) + 1
            facts = self.project_facts.setdefault(source, {})
            if stat.project_hash not in facts:
                facts[stat.project_hash] = {
                    "path_depth": stat.cwd_depth,
                    "under_state_or_cache_dir": stat.cwd_under_state_dir,
                }

        if stat.cwd_depth is not None:
            self.cwd_depths.setdefault(source, []).append(float(stat.cwd_depth))
            self.cwd_known_counts[source] = self.cwd_known_counts.get(source, 0) + 1
            if stat.cwd_under_state_dir:
                self.cwd_state_dir_counts[source] = self.cwd_state_dir_counts.get(source, 0) + 1

        if stat.age_days is not None:
            index = int(stat.age_days // AGE_BUCKET_DAYS)
            label = f"{index * AGE_BUCKET_DAYS}-{index * AGE_BUCKET_DAYS + AGE_BUCKET_DAYS - 1}"
            source_ages = self.age_buckets.setdefault(source, {})
            source_ages[label] = source_ages.get(label, 0) + 1
            previous_max = self.max_age_days.get(source)
            if previous_max is None or stat.age_days > previous_max:
                self.max_age_days[source] = stat.age_days
            previous_min = self.min_age_days.get(source)
            if previous_min is None or stat.age_days < previous_min:
                self.min_age_days[source] = stat.age_days

        if stat.provenance_class == "human":
            self.human_words.setdefault(source, []).append(stat.prose_words)

    # -- derived views ----------------------------------------------------- #

    def sources_seen(self) -> list[str]:
        seen = {source for source, _ in self.session_counts}
        return [source for source in SOURCES if source in seen] + sorted(
            source for source in seen if source not in SOURCES)

    def classes_for(self, source: str) -> list[str]:
        present = {klass for src, klass in self.session_counts if src == source}
        return [klass for klass in CLASSES if klass in present] + sorted(
            klass for klass in present if klass not in CLASSES)

    def total_sessions(self, source: str | None = None) -> int:
        return sum(count for (src, _), count in self.session_counts.items()
                   if source is None or src == source)

    def distributions(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for source in self.sources_seen():
            per_class: dict[str, Any] = {}
            for klass in self.classes_for(source):
                bucket = self.metrics.get((source, klass), {})
                per_class[klass] = {
                    "sessions": self.session_counts.get((source, klass), 0),
                    **{name: summarize(bucket.get(name, [])) for name in METRIC_FIELDS},
                }
            out[source] = per_class
        return out

    def months_histogram(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for source in self.sources_seen():
            per_class: dict[str, Any] = {}
            for klass in self.classes_for(source):
                counts = self.months.get((source, klass), {})
                if counts:
                    per_class[klass] = dict(sorted(counts.items()))
            out[source] = per_class
        return out

    def token_totals(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for source in self.sources_seen():
            per_class: dict[str, Any] = {}
            for klass in self.classes_for(source):
                totals = self.tokens.get((source, klass), {})
                if totals:
                    per_class[klass] = dict(sorted(totals.items()))
            out[source] = per_class
        return out

    def project_summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for source in self.sources_seen():
            counts = self.projects.get(source, {})
            if not counts:
                out[source] = {"distinct_projects": 0,
                               "sessions_per_project": summarize([]),
                               "top_projects": []}
                continue
            total = sum(counts.values())
            ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            facts = self.project_facts.get(source, {})
            top = [
                {
                    "project_hash": project_hash,
                    "sessions": sessions,
                    "share_of_sessions_pct": round(sessions / total * 100.0, 2),
                    "path_depth": facts.get(project_hash, {}).get("path_depth"),
                    "under_state_or_cache_dir": facts.get(project_hash, {}).get(
                        "under_state_or_cache_dir"),
                }
                for project_hash, sessions in ordered[:5]
            ]
            out[source] = {
                "distinct_projects": len(counts),
                "sessions_per_project": summarize(counts.values()),
                "top_projects": top,
            }
        return out

    def cwd_structure(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for source in self.sources_seen():
            known = self.cwd_known_counts.get(source, 0)
            out[source] = {
                "sessions_with_known_cwd": known,
                "path_depth": summarize(self.cwd_depths.get(source, [])),
                "under_state_or_cache_dir_sessions": self.cwd_state_dir_counts.get(source, 0),
                "under_state_or_cache_dir_pct": (
                    round(self.cwd_state_dir_counts.get(source, 0) / known * 100.0, 2)
                    if known else None),
            }
        return out

    def retention(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for source in self.sources_seen():
            buckets = self.age_buckets.get(source, {})
            ordered = dict(sorted(buckets.items(),
                                  key=lambda item: int(item[0].split("-")[0])))
            months = sorted(
                month
                for (src, _), counts in self.months.items() if src == source
                for month in counts)
            out[source] = {
                "age_days_buckets": ordered,
                "bucket_width_days": AGE_BUCKET_DAYS,
                "max_age_days": _round_number(self.max_age_days.get(source)),
                "min_age_days": _round_number(self.min_age_days.get(source)),
                "oldest_month": months[0] if months else None,
                "newest_month": months[-1] if months else None,
                "distinct_months": len(set(months)),
            }
        return out

    def derived_calibration(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "chunk_target_words": CHUNK_TARGET_WORDS,
            "chunk_stride_words": CHUNK_STRIDE_WORDS,
            "coverage_target_pct": COVERAGE_TARGET_PCT,
            "scope": "human-provenance sessions only",
            "sources": {},
        }
        for source in self.sources_seen():
            words = self.human_words.get(source, [])
            if not words:
                out["sources"][source] = {
                    "conversations": 0,
                    "total_words": 0,
                    "enrichment_gate": derive_enrichment_gate([]),
                    "estimated_chunks_all_conversations": 0,
                    "estimated_chunks_above_gate": 0,
                }
                continue
            gate = derive_enrichment_gate(words)
            threshold = gate["threshold_words"]
            out["sources"][source] = {
                "conversations": len(words),
                "total_words": sum(words),
                "enrichment_gate": gate,
                "estimated_chunks_all_conversations": sum(
                    estimate_chunks(count) for count in words),
                "estimated_chunks_above_gate": sum(
                    estimate_chunks(count) for count in words if count >= threshold),
            }
        return out


# --------------------------------------------------------------------------- #
# Anomalies (plain language, numbers and hashes only)
# --------------------------------------------------------------------------- #


def build_anomalies(aggregator: Aggregator, source_reports: dict[str, Any]) -> list[str]:
    anomalies: list[str] = []

    if aggregator.total_sessions() == 0:
        anomalies.append("No transcripts were found; the report contains no statistics.")
        return anomalies

    for source in aggregator.sources_seen():
        total = aggregator.total_sessions(source)
        if total == 0:
            continue
        tool_invoked = aggregator.session_counts.get((source, "tool-invoked"), 0)
        subagent = aggregator.session_counts.get((source, "subagent"), 0)
        human = aggregator.session_counts.get((source, "human"), 0)

        if tool_invoked and tool_invoked / total * 100.0 >= 25.0:
            anomalies.append(
                f"{source}: {tool_invoked / total * 100.0:.0f}% of sessions are tool-invoked "
                f"({tool_invoked:,} of {total:,}); pooling them with conversations would "
                f"distort every rate and length statistic.")
        if subagent and subagent / total * 100.0 >= 25.0:
            anomalies.append(
                f"{source}: {subagent / total * 100.0:.0f}% of sessions are subagent "
                f"transcripts ({subagent:,} of {total:,}).")
        if human == 0:
            anomalies.append(
                f"{source}: zero sessions classified as human out of {total:,}; "
                f"the provenance heuristics may be mis-tuned for this corpus.")

        projects = aggregator.projects.get(source, {})
        if projects:
            top_hash, top_count = max(projects.items(), key=lambda item: (item[1], item[0]))
            share = top_count / sum(projects.values()) * 100.0
            if share >= 40.0:
                facts = aggregator.project_facts.get(source, {}).get(top_hash, {})
                where = ("under a state/cache directory"
                         if facts.get("under_state_or_cache_dir") else "in a normal directory")
                anomalies.append(
                    f"{source}: the largest single project hash {top_hash} accounts for "
                    f"{share:.0f}% of sessions ({top_count:,}) and is {where}.")

        retention = source_reports.get(source, {}).get("retention_hint")
        if retention is not None:
            anomalies.append(retention)

        human_months = aggregator.months.get((source, "human"), {})
        if human_months and len(human_months) < 3:
            anomalies.append(
                f"{source}: only {len(human_months)} month(s) of human conversation history; "
                f"derived thresholds from this corpus are provisional.")

        failures = source_reports.get(source, {}).get("parse_failures", {})
        failure_total = sum(value for value in failures.values() if isinstance(value, int))
        files = source_reports.get(source, {}).get("files_discovered", 0) or 0
        if failure_total and files and failure_total > max(10, files * 0.05):
            anomalies.append(
                f"{source}: {failure_total:,} parse failures across {files:,} files "
                f"(see parse_failures for the reason categories).")

    for source in aggregator.sources_seen():
        max_age = aggregator.max_age_days.get(source)
        if max_age is not None and max_age < 40:
            anomalies.append(
                f"{source}: the oldest transcript is only {max_age:.0f} days old, which is "
                f"evidence that history is being swept or rotated.")

    return anomalies


# --------------------------------------------------------------------------- #
# Survey driver
# --------------------------------------------------------------------------- #


class Progress:
    """Minimal stderr progress reporter."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def note(self, message: str) -> None:
        if self.enabled:
            print(message, file=sys.stderr, flush=True)

    def step(self, label: str, done: int, total: int, every: int = 250) -> None:
        if not self.enabled:
            return
        if done % every == 0 or done == total:
            print(f"\r  {label}: {done:,}/{total:,} files", end="", file=sys.stderr, flush=True)
            if done == total:
                print("", file=sys.stderr, flush=True)


def run_survey(claude_override: Path | None, codex_override: Path | None,
               home: Path, progress: Progress,
               now: dt.datetime | None = None) -> dict[str, Any]:
    """Survey the corpus and return the report dict. Reads only; writes nothing."""
    salt = secrets.token_hex(16)
    started = now or dt.datetime.now(dt.timezone.utc)

    aggregator = Aggregator()
    source_reports: dict[str, Any] = {}

    claude = locate_claude(claude_override)
    codex = locate_codex(codex_override)

    # -- Claude Code ------------------------------------------------------- #
    failures: dict[str, int] = {}
    claude_files: list[tuple[Path, str, bool, bool]] = []
    if claude.transcripts_dir is not None:
        claude_files = discover_claude_files(claude.transcripts_dir)
    progress.note(f"claude: {len(claude_files):,} transcript files discovered")
    parsed = 0
    for index, (path, project_key, is_nested, is_subagent) in enumerate(claude_files, start=1):
        try:
            stat = survey_claude_file(path, project_key, is_nested, is_subagent,
                                      salt, started, failures)
        except Exception:  # never crash on one bad file
            failures[FAIL_READ_ERROR] = failures.get(FAIL_READ_ERROR, 0) + 1
            stat = None
        if stat is not None:
            aggregator.add(stat)
            parsed += 1
        progress.step("claude", index, len(claude_files))
    source_reports["claude"] = {
        "present": claude.transcripts_dir is not None,
        "location_kind": claude.location_kind,
        "files_discovered": len(claude_files),
        "sessions_measured": parsed,
        "files_skipped": len(claude_files) - parsed,
        "parse_failures": dict(sorted(failures.items())),
    }

    # -- Codex ------------------------------------------------------------- #
    failures = {}
    codex_files: list[Path] = []
    if codex.transcripts_dir is not None:
        codex_files = discover_codex_files(codex.transcripts_dir)
    progress.note(f"codex: {len(codex_files):,} rollout files discovered")
    parsed = 0
    for index, path in enumerate(codex_files, start=1):
        try:
            stat = survey_codex_file(path, salt, started, failures)
        except Exception:
            failures[FAIL_READ_ERROR] = failures.get(FAIL_READ_ERROR, 0) + 1
            stat = None
        if stat is not None:
            aggregator.add(stat)
            parsed += 1
        progress.step("codex", index, len(codex_files), every=25)
    source_reports["codex"] = {
        "present": codex.transcripts_dir is not None,
        "location_kind": codex.location_kind,
        "files_discovered": len(codex_files),
        "sessions_measured": parsed,
        "files_skipped": len(codex_files) - parsed,
        "parse_failures": dict(sorted(failures.items())),
    }

    # -- Retention hints (used by anomalies) -------------------------------- #
    for source in aggregator.sources_seen():
        max_age = aggregator.max_age_days.get(source)
        if max_age is not None and max_age < 15:
            source_reports.setdefault(source, {})["retention_hint"] = (
                f"{source}: no transcript older than {max_age:.0f} days was found — "
                f"history appears to be short-lived on this machine.")

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "script_version": SCRIPT_VERSION,
        "generated_at_utc": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "python_version": "{}.{}.{}".format(*sys.version_info[:3]),
        "platform": sys.platform if sys.platform in ("darwin", "linux", "win32") else "other",
        "privacy": {
            "contains_transcript_text": False,
            "contains_paths": False,
            "contains_identifiers": False,
            "path_anonymization": "sha256(random-per-run salt + normalized path)[:8]",
            "salt_emitted": False,
            "notes": "Counts, lengths, month buckets, age buckets, model names, "
                     "enum classifications and salted hash prefixes only.",
        },
        "sources": {name: {key: value
                           for key, value in data.items() if key != "retention_hint"}
                    for name, data in source_reports.items()},
        "prose_indexes": survey_index_dirs(home),
        "sessions": {
            "total": aggregator.total_sessions(),
            "by_source": {source: aggregator.total_sessions(source)
                          for source in aggregator.sources_seen()},
            "by_source_and_class": {
                source: {klass: aggregator.session_counts.get((source, klass), 0)
                         for klass in aggregator.classes_for(source)}
                for source in aggregator.sources_seen()
            },
        },
        "distributions": aggregator.distributions(),
        "sessions_per_month": aggregator.months_histogram(),
        "provenance_rules_fired": {source: dict(sorted(rules.items()))
                                   for source, rules in sorted(aggregator.rules.items())},
        "model_usage_sessions": {source: dict(sorted(models.items(),
                                                     key=lambda item: (-item[1], item[0])))
                                 for source, models in sorted(aggregator.models.items())},
        "projects": aggregator.project_summary(),
        "cwd_structure": aggregator.cwd_structure(),
        "token_usage_totals": aggregator.token_totals(),
        "parse_failures": {name: data.get("parse_failures", {})
                           for name, data in source_reports.items()},
        "retention": aggregator.retention(),
        "derived_calibration": aggregator.derived_calibration(),
    }
    report["anomalies"] = build_anomalies(aggregator, source_reports)
    return report


# --------------------------------------------------------------------------- #
# Human-readable summary
# --------------------------------------------------------------------------- #


def _fmt(value: Any, width: int = 0) -> str:
    if value is None:
        return "-".rjust(width)
    if isinstance(value, float):
        return f"{value:,.0f}".rjust(width)
    if isinstance(value, int):
        return f"{value:,}".rjust(width)
    return str(value).rjust(width)


def render_summary(report: dict[str, Any], out_path: Path | None) -> str:
    lines: list[str] = []
    add = lines.append

    add("=" * 78)
    add("MUNINN CORPUS SURVEY — statistics only, no transcript content")
    add("=" * 78)
    add(f"generated {report['generated_at_utc']}  platform {report['platform']}  "
        f"python {report['python_version']}  script v{report['script_version']}")
    add("")

    add("## Sources")
    for name, data in report["sources"].items():
        state = "found" if data["present"] else "not found"
        add(f"  {name:8} {state:10} files {_fmt(data['files_discovered'], 7)}"
            f"  measured {_fmt(data['sessions_measured'], 7)}"
            f"  skipped {_fmt(data['files_skipped'], 6)}  ({data['location_kind']})")
    for label, data in report.get("prose_indexes", {}).items():
        if data.get("present"):
            add(f"  {label:20} present, {_fmt(data.get('file_count'))} files")
    add("")

    total = report["sessions"]["total"]
    if total == 0:
        add("No transcripts were found. Nothing to report — this is still useful")
        add("information for calibration, so the report file is valid and worth sending.")
        add("")
    else:
        add("## Provenance breakdown")
        add(f"  {'source':8} {'class':14} {'sessions':>9} {'prose words':>14} "
            f"{'median words':>13} {'median turns':>13}")
        for source, per_class in report["distributions"].items():
            for klass, data in per_class.items():
                words = data["prose_words"]
                turns = data["user_turns"]
                add(f"  {source:8} {klass:14} {_fmt(data['sessions'], 9)} "
                    f"{_fmt(words['sum'], 14)} {_fmt(words['median'], 13)} "
                    f"{_fmt(turns['median'], 13)}")
        add("")

        add("## Human conversations — derived calibration")
        calibration = report["derived_calibration"]
        for source, data in calibration["sources"].items():
            if not data["conversations"]:
                add(f"  [{source}] no human conversations detected")
                continue
            gate = data["enrichment_gate"]
            add(f"  [{source}] {data['conversations']:,} conversations, "
                f"{data['total_words']:,} prose words")
            add(f"     enrich gate  >= {gate['threshold_words']:,} words -> "
                f"{gate['sessions']:,} sessions "
                f"({gate['share_of_conversations_pct']}% of conversations, "
                f"{gate['coverage_pct']}% of text)")
            add(f"     est. chunks  {data['estimated_chunks_all_conversations']:,} all / "
                f"{data['estimated_chunks_above_gate']:,} above gate "
                f"(@{calibration['chunk_target_words']}w target, "
                f"{calibration['chunk_stride_words']}w stride)")
        add("")

        add("## Retention evidence")
        for source, data in report["retention"].items():
            add(f"  {source:8} oldest {_fmt(data['max_age_days'], 6)} days"
                f"  months {data['oldest_month'] or '-'} .. {data['newest_month'] or '-'}"
                f"  ({data['distinct_months']} distinct)")
        add("")

        add("## Models (sessions using each)")
        for source, models in report["model_usage_sessions"].items():
            top = list(models.items())[:6]
            rendered = ", ".join(f"{name} {count:,}" for name, count in top)
            add(f"  {source:8} {rendered or '-'}")
        add("")

        add("## Projects (salted hashes only)")
        for source, data in report["projects"].items():
            per_project = data["sessions_per_project"]
            add(f"  {source:8} {data['distinct_projects']:,} distinct  "
                f"median {_fmt(per_project['median'])} sessions/project  "
                f"max {_fmt(per_project['max'])}")
        add("")

        failures = {source: sum(v for v in data.values() if isinstance(v, int))
                    for source, data in report["parse_failures"].items()}
        add("## Parse failures (by category, counts only)")
        for source, data in report["parse_failures"].items():
            detail = ", ".join(f"{key} {value:,}" for key, value in data.items())
            add(f"  {source:8} total {failures[source]:,}"
                f"{('  [' + detail + ']') if detail else ''}")
        add("")

    if report["anomalies"]:
        add("## Anomalies")
        for anomaly in report["anomalies"]:
            add(f"  [!] {anomaly}")
        add("")

    add("-" * 78)
    add("PRIVACY: this report contains no message text, no prompts, no file contents,")
    add("no paths, no branch names, no usernames, no hostnames and no URLs. Paths appear")
    add("only as salted, non-reversible 8-character hashes; the salt is not saved.")
    if out_path is not None:
        add("")
        add(f"REPORT WRITTEN TO: {out_path}")
        add("Please skim this file before sharing it. It is plain JSON and short enough")
        add("to read. If anything in it looks sensitive, do not send it — tell us instead.")
    else:
        add("")
        add("--print-only was used: no file was written.")
    add("-" * 78)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Self-test — this is the proof of the privacy guarantee
# --------------------------------------------------------------------------- #

SELF_TEST_SECRETS: tuple[str, ...] = (
    "SECRETPROSEONE",
    "flamingo",
    "mandolin",
    "ravine",
    "quokka",
    "SECRETPROSETWO",
    "SECRETPROSETHREE",
    "SECRETTHINKING",
    "SECRETTOOLRESULT",
    "SECRETTOOLINPUT",
    "SECRETCODEXPROMPT",
    "SECRETAGENTREPLY",
    "SECRETSUBAGENT",
    "zzsecretproject",
    "zzcodexproject",
    "zzbranchname",
    "zzsecretfile",
    "zztoolcache",
    "sk-zzsecrettoken123",
    "arn:aws:bedrock",
    "123456789012",
    "feature/",
    "hostname-zzbox",
)


def _write_jsonl(path: Path, records: Sequence[Any], extra_lines: Sequence[str] = ()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
        for line in extra_lines:
            handle.write(line + "\n")


def _build_synthetic_corpus(root: Path) -> tuple[Path, Path]:
    """Create a tiny corpus with planted secrets. Returns (claude_dir, codex_dir)."""
    claude_dir = root / "claude-home"
    codex_dir = root / "codex-home"

    project = claude_dir / "projects" / "-tmp-zzsecretproject"
    session_id = "aaaaaaaa-1111-2222-3333-444444444444"
    human_records: list[Any] = [
        {"type": "mode", "mode": "normal", "sessionId": session_id},
        {
            "type": "user",
            "isSidechain": False,
            "entrypoint": "cli",
            "cwd": "/tmp/zzsecretproject/deep/nested",
            "gitBranch": "feature/zzbranchname",
            "timestamp": "2026-01-01T00:00:00.000Z",
            "message": {"role": "user",
                        "content": "SECRETPROSEONE flamingo mandolin ravine quokka"},
        },
        {
            "type": "assistant",
            "isSidechain": False,
            "entrypoint": "cli",
            "cwd": "/tmp/zzsecretproject/deep/nested",
            "timestamp": "2026-01-01T00:10:00.000Z",
            "message": {
                "role": "assistant",
                "model": "arn:aws:bedrock:us-east-1:123456789012:inference-profile/zz",
                "usage": {"input_tokens": 10, "output_tokens": 5,
                          "cache_read_input_tokens": 2,
                          "cache_creation_input_tokens": 1},
                "content": [
                    {"type": "thinking", "thinking": "SECRETTHINKING about zzsecretfile"},
                    {"type": "text", "text": "SECRETPROSETWO alpha beta"},
                    {"type": "tool_use", "name": "Bash",
                     "input": {"command": "cat /tmp/zzsecretfile SECRETTOOLINPUT"}},
                ],
            },
        },
        {
            "type": "user",
            "isSidechain": False,
            "cwd": "/tmp/zzsecretproject/deep/nested",
            "timestamp": "2026-01-01T00:11:00.000Z",
            "message": {"role": "user",
                        "content": [{"type": "tool_result",
                                     "content": "SECRETTOOLRESULT sk-zzsecrettoken123"}]},
        },
        {
            "type": "user",
            "isSidechain": False,
            "cwd": "/tmp/zzsecretproject/deep/nested",
            "timestamp": "2026-01-01T00:20:00.000Z",
            "message": {"role": "user", "content": "SECRETPROSETHREE gamma"},
        },
        {
            "type": "assistant",
            "isSidechain": False,
            "cwd": "/tmp/zzsecretproject/deep/nested",
            "timestamp": "2026-01-01T00:30:00.000Z",
            "message": {
                "role": "assistant",
                "model": "us.anthropic.claude-sonnet-5",
                "usage": {"input_tokens": 20, "output_tokens": 7},
                "content": [{"type": "text", "text": "delta epsilon zeta eta"}],
            },
        },
        {
            "type": "system",
            "timestamp": "2026-01-01T00:31:00.000Z",
            "content": "system note about hostname-zzbox",
        },
    ]
    _write_jsonl(project / f"{session_id}.jsonl", human_records,
                 extra_lines=['{"type": "user", "message": ', "", "not json at all", "[1,2,3]"])

    subagent_records: list[Any] = [
        {
            "type": "user",
            "isSidechain": True,
            "cwd": "/tmp/zzsecretproject",
            "timestamp": "2026-01-02T00:00:00.000Z",
            "message": {"role": "user", "content": "SECRETSUBAGENT task one two three"},
        },
        {
            "type": "assistant",
            "isSidechain": True,
            "cwd": "/tmp/zzsecretproject",
            "timestamp": "2026-01-02T00:05:00.000Z",
            "message": {"role": "assistant", "model": "claude-haiku-4-5",
                        "content": [{"type": "text", "text": "four five"},
                                    {"type": "tool_use", "name": "Read", "input": {}}]},
        },
    ]
    _write_jsonl(project / session_id / "subagents" / "agent-abc123.jsonl", subagent_records)

    tool_project = claude_dir / "projects" / "-tmp--local-state-zztoolcache"
    tool_records: list[Any] = [
        {
            "type": "user",
            "isSidechain": False,
            "entrypoint": "sdk-cli",
            "cwd": "/tmp/.local/state/zztoolcache",
            "timestamp": "2026-01-03T00:00:00.000Z",
            "message": {"role": "user", "content": "summarize this zzsecretproject blurb"},
        },
        {
            "type": "assistant",
            "isSidechain": False,
            "entrypoint": "sdk-cli",
            "cwd": "/tmp/.local/state/zztoolcache",
            "timestamp": "2026-01-03T00:00:01.000Z",
            "message": {"role": "assistant", "model": "claude-sonnet-5",
                        "usage": {"input_tokens": 3, "output_tokens": 1},
                        "content": [{"type": "text", "text": "blurb"}]},
        },
    ]
    _write_jsonl(tool_project / "bbbbbbbb-1111-2222-3333-444444444444.jsonl", tool_records)

    # An empty file and a garbage-only file, to exercise failure categories.
    (project / "empty.jsonl").write_text("", encoding="utf-8")
    (project / "garbage.jsonl").write_text("}}}not json\n{{{\n", encoding="utf-8")

    codex_records: list[Any] = [
        {
            "timestamp": "2026-02-03T01:00:00.000Z",
            "type": "session_meta",
            "payload": {"session_id": "cccccccc-1111-2222-3333-444444444444",
                        "cwd": "/tmp/zzcodexproject",
                        "originator": "codex-tui",
                        "cli_version": "0.146.0",
                        "base_instructions": {"text": "SECRETCODEXPROMPT preamble"}},
        },
        {
            "timestamp": "2026-02-03T01:00:01.000Z",
            "type": "turn_context",
            "payload": {"cwd": "/tmp/zzcodexproject", "model": "gpt-5-codex"},
        },
        {
            "timestamp": "2026-02-03T01:00:02.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message",
                        "message": "SECRETCODEXPROMPT sigma tau upsilon"},
        },
        {
            "timestamp": "2026-02-03T01:00:03.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message",
                        "message": "<environment_context>/tmp/zzcodexproject</environment_context>"},
        },
        {
            "timestamp": "2026-02-03T01:00:04.000Z",
            "type": "response_item",
            "payload": {"type": "reasoning", "encrypted_content": "zzencrypted"},
        },
        {
            "timestamp": "2026-02-03T01:00:05.000Z",
            "type": "response_item",
            "payload": {"type": "function_call", "name": "shell",
                        "arguments": "{\"cmd\": \"cat /tmp/zzsecretfile\"}"},
        },
        {
            "timestamp": "2026-02-03T01:00:06.000Z",
            "type": "response_item",
            "payload": {"type": "function_call_output", "output": "SECRETTOOLRESULT zz"},
        },
        {
            "timestamp": "2026-02-03T01:05:00.000Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "SECRETAGENTREPLY phi chi"},
        },
        {
            "timestamp": "2026-02-03T01:09:00.000Z",
            "type": "event_msg",
            "payload": {"type": "token_count",
                        "info": {"total_token_usage": {"input_tokens": 100,
                                                       "output_tokens": 20,
                                                       "cached_input_tokens": 40,
                                                       "reasoning_output_tokens": 9}}},
        },
        {
            "timestamp": "2026-02-03T01:10:00.000Z",
            "type": "event_msg",
            "payload": {"type": "task_complete",
                        "last_agent_message": "SECRETAGENTREPLY done"},
        },
    ]
    _write_jsonl(
        codex_dir / "sessions" / "2026" / "02" / "03"
        / "rollout-2026-02-03T01-00-00-cccccccc-1111-2222-3333-444444444444.jsonl",
        codex_records)

    return claude_dir, codex_dir


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_self_test() -> int:
    """Build a synthetic corpus, survey it, and assert privacy + correctness."""
    print("self-test: building synthetic corpus...")
    temp_root = Path(tempfile.mkdtemp(prefix="muninn-survey-selftest-"))
    try:
        claude_dir, codex_dir = _build_synthetic_corpus(temp_root)

        # -- percentile / helper unit checks ------------------------------- #
        values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        _assert(percentile(values, 50) == 5, "median of 1..10 should be 5 (nearest-rank)")
        _assert(percentile(values, 100) == 10, "p100 should be max")
        _assert(percentile(values, 10) == 1, "p10 of 1..10 should be 1")
        _assert(percentile([], 50) is None, "percentile of empty is None")
        summary = summarize([1, 2, 3, 4])
        _assert(summary["count"] == 4 and summary["sum"] == 10, "summarize count/sum")
        _assert(summary["min"] == 1 and summary["max"] == 4, "summarize min/max")
        _assert(summary["mean"] == 2.5, "summarize mean")
        _assert(estimate_chunks(0) == 0, "no words -> no chunks")
        _assert(estimate_chunks(400) == 1, "400 words -> 1 chunk")
        _assert(estimate_chunks(401) == 2, "401 words -> 2 chunks")
        _assert(estimate_chunks(1040) == 3, "1040 words -> 3 chunks")
        gate = derive_enrichment_gate([1000, 100, 50, 50], 85.0)
        _assert(gate["sessions"] == 2 and gate["threshold_words"] == 100,
                f"gate should pick 2 sessions at 100 words, got {gate}")
        _assert(normalize_model_identifier(
            "arn:aws:bedrock:us-east-1:123456789012:x") == MODEL_REDACTED,
            "ARNs must be redacted")
        _assert(normalize_model_identifier("us.anthropic.claude-sonnet-5")
                == "claude-sonnet-5", "geo/provider prefixes stripped")
        _assert(normalize_model_identifier("https://internal.example/v1")
                == MODEL_REDACTED, "endpoints must be redacted")
        _assert(normalize_model_identifier("gpt-5-codex") == "gpt-5-codex",
                "plain model names preserved")
        _assert(is_under_state_dir("/home/x/.local/state/tool/cache"),
                "state dir detection")
        _assert(not is_under_state_dir("/home/x/Projects/repo"),
                "normal dirs are not state dirs")
        _assert(path_depth("/a/b/c") == 3, "path depth counts components")

        print("self-test: surveying synthetic corpus...")
        report = run_survey(claude_dir, codex_dir, temp_root, Progress(False))
        serialized = json.dumps(report, indent=2, sort_keys=True)

        # -- PRIVACY ASSERTIONS ------------------------------------------- #
        haystack = serialized.lower()
        for secret in SELF_TEST_SECRETS:
            _assert(secret.lower() not in haystack,
                    f"PRIVACY FAILURE: planted secret {secret!r} leaked into the report")
        for forbidden in ("/tmp", "/users/", "/home/", "http://", "https://",
                          "c:\\", "\\users\\", "appdata\\"):
            _assert(forbidden not in haystack,
                    f"PRIVACY FAILURE: forbidden substring {forbidden!r} in the report")
        # No POSIX- or Windows-path-shaped token anywhere in the serialized report.
        posix_path = re.search(r"(?:/[A-Za-z0-9._~-]+){2,}", serialized)
        _assert(posix_path is None,
                f"PRIVACY FAILURE: path-shaped token in the report: {posix_path}")
        windows_path = re.search(r"[A-Za-z]:\\\\|(?:\\\\[A-Za-z0-9._~-]+){2,}", serialized)
        _assert(windows_path is None,
                f"PRIVACY FAILURE: Windows-path-shaped token in the report: {windows_path}")
        _assert("salt" not in json.dumps(report.get("privacy", {})).lower()
                or report["privacy"]["salt_emitted"] is False,
                "salt must not be emitted")
        # The salt is fresh per run; assert no 32-hex-char blob appears anywhere.
        _assert(not re.search(r"\b[0-9a-f]{32}\b", serialized),
                "PRIVACY FAILURE: a 32-hex blob (possible salt) appears in the report")
        for project_entry in report["projects"].values():
            for top in project_entry["top_projects"]:
                _assert(re.fullmatch(r"[0-9a-f]{8}", top["project_hash"]) is not None,
                        "project hashes must be exactly 8 hex chars")

        # -- CORRECTNESS ASSERTIONS --------------------------------------- #
        counts = report["sessions"]["by_source_and_class"]
        _assert(counts["claude"]["human"] == 1,
                f"expected 1 human claude session, got {counts['claude']}")
        _assert(counts["claude"]["tool-invoked"] == 1,
                f"expected 1 tool-invoked claude session, got {counts['claude']}")
        _assert(counts["claude"]["subagent"] == 1,
                f"expected 1 subagent claude session, got {counts['claude']}")
        _assert(counts["codex"]["human"] == 1,
                f"expected 1 human codex session, got {counts['codex']}")
        _assert(report["sessions"]["total"] == 4,
                f"expected 4 sessions total, got {report['sessions']['total']}")

        human = report["distributions"]["claude"]["human"]
        _assert(human["prose_words"]["sum"] == 14,
                f"claude human prose words should be 14, got {human['prose_words']['sum']}")
        _assert(human["user_turns"]["sum"] == 2,
                f"claude human user turns should be 2, got {human['user_turns']['sum']}")
        _assert(human["assistant_turns"]["sum"] == 2,
                f"claude human assistant turns should be 2, got {human['assistant_turns']['sum']}")
        _assert(human["tool_use_count"]["sum"] == 1, "claude human tool_use should be 1")
        _assert(human["tool_result_count"]["sum"] == 1, "claude human tool_result should be 1")
        _assert(human["thinking_count"]["sum"] == 1, "claude human thinking should be 1")
        _assert(human["duration_seconds"]["max"] == 1860,
                f"claude human duration should be 1860s, got {human['duration_seconds']['max']}")
        # 46 + 25 + 22 + 22 bytes of natural-language text; tool and thinking
        # blocks contribute nothing.
        _assert(human["prose_bytes"]["sum"] == 115,
                f"claude human prose bytes should be 115, got {human['prose_bytes']['sum']}")
        _assert(human["raw_bytes"]["sum"] > 0, "raw bytes must be measured")

        codex_human = report["distributions"]["codex"]["human"]
        # 4 words of user prose + 3 of agent prose; the injected
        # <environment_context> message and task_complete echo are excluded.
        _assert(codex_human["prose_words"]["sum"] == 7,
                f"codex prose words should be 7, got {codex_human['prose_words']['sum']}")
        _assert(codex_human["user_turns"]["sum"] == 1,
                "injected <environment_context> must not count as a user turn")
        _assert(codex_human["assistant_turns"]["sum"] == 1, "codex assistant turns should be 1")
        _assert(codex_human["tool_use_count"]["sum"] == 1, "codex tool_use should be 1")
        _assert(codex_human["tool_result_count"]["sum"] == 1, "codex tool_result should be 1")
        _assert(codex_human["thinking_count"]["sum"] == 1, "codex reasoning should be 1")
        _assert(codex_human["duration_seconds"]["max"] == 600,
                f"codex duration should be 600s, got {codex_human['duration_seconds']['max']}")

        tokens = report["token_usage_totals"]["claude"]["human"]
        _assert(tokens["input_tokens"] == 30 and tokens["output_tokens"] == 12,
                f"claude token totals wrong: {tokens}")
        _assert(tokens["cache_read_input_tokens"] == 2, "cache read tokens summed")
        codex_tokens = report["token_usage_totals"]["codex"]["human"]
        _assert(codex_tokens["input_tokens"] == 100 and codex_tokens["output_tokens"] == 20,
                f"codex token totals wrong: {codex_tokens}")

        models = report["model_usage_sessions"]["claude"]
        _assert(models.get(MODEL_REDACTED) == 1,
                f"the ARN model must be counted as {MODEL_REDACTED}: {models}")
        _assert(models.get("claude-sonnet-5") == 2,
                f"claude-sonnet-5 should be seen in 2 sessions: {models}")
        _assert(report["model_usage_sessions"]["codex"].get("gpt-5-codex") == 1,
                "codex model should be gpt-5-codex")

        rules = report["provenance_rules_fired"]["claude"]
        _assert(rules.get(RULE_SUBAGENT_PATH) == 1, f"subagent_path rule: {rules}")
        _assert(rules.get(RULE_ENTRYPOINT_SDK_CLI) == 1, f"sdk-cli rule: {rules}")
        _assert(rules.get(RULE_CWD_STATE_DIR) == 1, f"state dir rule: {rules}")
        _assert(rules.get(RULE_SINGLE_TURN_FAST) == 1, f"single-turn-fast rule: {rules}")
        _assert(rules.get(RULE_DEFAULT_HUMAN) == 1, f"default_human rule: {rules}")

        months = report["sessions_per_month"]["claude"]["human"]
        _assert(months == {"2026-01": 1}, f"month histogram wrong: {months}")

        projects = report["projects"]["claude"]
        _assert(projects["distinct_projects"] == 2,
                f"expected 2 distinct claude projects, got {projects}")

        failures = report["parse_failures"]["claude"]
        _assert(failures.get(FAIL_JSON_DECODE, 0) >= 3,
                f"malformed lines should be counted: {failures}")
        _assert(failures.get(FAIL_LINE_NOT_OBJECT, 0) >= 1,
                f"non-object JSON lines should be counted: {failures}")
        _assert(failures.get(FAIL_EMPTY_FILE, 0) == 1,
                f"empty file should be counted: {failures}")
        _assert(failures.get(FAIL_NO_ENTRIES, 0) == 1,
                f"garbage-only file should count as no recognized entries: {failures}")

        retention = report["retention"]["claude"]
        _assert(retention["max_age_days"] is not None and retention["max_age_days"] > 0,
                "session age must be measured")
        _assert(retention["oldest_month"] == "2026-01", "oldest month wrong")

        calibration = report["derived_calibration"]["sources"]["claude"]
        _assert(calibration["conversations"] == 1, "one human conversation")
        _assert(calibration["total_words"] == 14, "calibration scoped to human prose")
        _assert(calibration["estimated_chunks_all_conversations"] == 1, "one short chunk")

        cwd_structure = report["cwd_structure"]["claude"]
        _assert(cwd_structure["under_state_or_cache_dir_sessions"] == 1,
                f"one state-dir session expected: {cwd_structure}")
        _assert(cwd_structure["path_depth"]["max"] == 4,
                f"deepest synthetic cwd has depth 4: {cwd_structure['path_depth']}")

        _assert(isinstance(report["anomalies"], list), "anomalies must be a list")
        summary_text = render_summary(report, None)
        for secret in SELF_TEST_SECRETS:
            _assert(secret.lower() not in summary_text.lower(),
                    f"PRIVACY FAILURE: {secret!r} leaked into the printed summary")

        print("self-test: privacy assertions passed "
              f"({len(SELF_TEST_SECRETS)} planted secrets, none present)")
        print("self-test: correctness assertions passed "
              "(counts, distributions, provenance rules, tokens, models, failures)")
        print("self-test: PASS")
        return 0
    except AssertionError as error:
        print(f"self-test: FAIL — {error}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
        if temp_root.exists():
            print(f"self-test: warning — could not remove {temp_root}", file=sys.stderr)
        else:
            print("self-test: temporary corpus removed")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

EPILOG = """\
privacy properties
------------------
  * The report contains ONLY counts, lengths, durations, month buckets, age
    buckets, model identifiers, enum-like classifications and salted hash
    prefixes.
  * It contains NO message text, prompts, replies, thinking blocks, tool inputs
    or outputs, file contents, session titles, git branch names, error strings,
    paths, usernames, hostnames, repository names or URLs.
  * Paths are never emitted. Path-derived signals appear as
    sha256(random-per-run-salt + path)[:8] plus structural facts such as depth.
    The salt is discarded when the run ends, so hashes cannot be reversed.
  * Read-only: nothing under a transcript directory is written, moved or deleted.
    The only file written is the report.
  * Run --self-test to see the guarantee verified against planted secrets.

You are encouraged to open the report and read it before sharing it.
"""


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corpus-survey.py",
        description=("Survey a local AI-agent transcript corpus and emit an anonymous, "
                     "statistics-only JSON report for Muninn calibration. "
                     "No transcript content ever leaves your machine."),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--out", metavar="PATH", type=Path, default=None,
                        help="report destination "
                             "(default ./muninn-corpus-survey-<utc-timestamp>.json)")
    parser.add_argument("--claude-dir", metavar="PATH", type=Path, default=None,
                        help="override the Claude Code home directory (expects ./projects)")
    parser.add_argument("--codex-dir", metavar="PATH", type=Path, default=None,
                        help="override the Codex home directory (expects ./sessions); "
                             "$CODEX_HOME is honored by default")
    parser.add_argument("--quiet", action="store_true",
                        help="suppress progress output on stderr")
    parser.add_argument("--print-only", action="store_true",
                        help="compute and print the summary, write no file")
    parser.add_argument("--self-test", action="store_true",
                        help="verify the privacy guarantee and the statistics against a "
                             "synthetic corpus in a temporary directory, then exit")
    return parser


def default_out_path(now: dt.datetime) -> Path:
    return Path.cwd() / f"muninn-corpus-survey-{now.strftime('%Y%m%dT%H%M%SZ')}.json"


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.self_test:
        return run_self_test()

    progress = Progress(not args.quiet)
    started = dt.datetime.now(dt.timezone.utc)
    progress.note("muninn corpus survey — read-only, statistics only")

    report = run_survey(args.claude_dir, args.codex_dir, Path.home(), progress, started)

    out_path: Path | None = None
    if not args.print_only:
        out_path = args.out if args.out is not None else default_out_path(started)
        payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(payload, encoding="utf-8")
        except OSError as error:
            print(f"error: could not write report ({type(error).__name__})", file=sys.stderr)
            return 2

    print(render_summary(report, out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
