# Spec 008 — Plugin contract and policy chokepoint

**Status:** ready to implement. Blocks both Cisco plugins.
**Owner of design:** planned by Opus, implemented by Sonnet
**Read first:** `.valholl/articles/model-policy-chokepoint.md` — normative for the
policy half. Also `.valholl/articles/lessons-for-huginn.md` #1 and #6, which are
the two mistakes this spec exists to avoid repeating.

## Why

Muninn has no extension surface. Anything that wants to add an embedding
provider, a text provider, or a history source has to fork. That blocks the whole
distribution model: a Cisco-internal build needs to add Bedrock and Neo-Cortex
*without* Cisco code entering the public repo.

Two upstream mistakes must not be repeated:

1. **Exact-match API versions** (Huginn #38). An exact `api_version ==` comparison
   silently disables every plugin on a routine bump. Muninn declares
   **compatibility ranges** and fails **loudly**.
2. **A purely additive registry** (Huginn #41). If plugins can only add, no
   deployment can say "only these models." Policies must **intersect**.

## Scope

**In:** `muninn/plugins.py`, `muninn/policy.py`, three capability protocols,
entry-point discovery with per-plugin failure isolation, `doctor` reporting,
tests.

**Out:** any concrete provider. This spec ships **no** Bedrock, no MLX, no
Neo-Cortex. It is the socket, not the plug.

## Files

| File | Action |
|---|---|
| `muninn/plugins.py` | **new** — protocols, `PluginSpec`, discovery |
| `muninn/policy.py` | **new** — `ModelPolicy`, `resolve()`, `check()` |
| `muninn/cli.py` | `doctor` reports plugins + policies + load errors |
| `tests/test_plugins.py` | **new** |
| `tests/test_policy.py` | **new** |

## The contract

```python
API_VERSION = 1
ENTRY_POINT_GROUP = "muninn.plugins"
POLICY_ENTRY_POINT_GROUP = "muninn.policy"


class EmbeddingProvider(Protocol):
    name: str
    model: str
    dim: int
    def available(self) -> str | None: ...
    def embed(self, texts: Sequence[str]) -> Any: ...   # (n, dim) float32, L2-normalized


class TextProvider(Protocol):
    """Text generation, for enrichment and briefs."""
    name: str
    model: str
    def available(self) -> str | None: ...
    def generate(self, prompt: str, *, max_tokens: int = 2048,
                 timeout: float = 60.0) -> str: ...


class HistorySource(Protocol):
    """Contributes sessions Muninn cannot discover locally."""
    name: str
    def available(self) -> str | None: ...
    def fetch(self, context: "SourceContext") -> Iterable["ParsedSession"]: ...


@dataclass(frozen=True)
class PluginSpec:
    name: str
    version: str
    api_version: int = API_VERSION
    min_api: int = 1
    max_api: int = API_VERSION
    embedders: tuple[EmbeddingProvider, ...] = ()
    text_providers: tuple[TextProvider, ...] = ()
    history_sources: tuple[HistorySource, ...] = ()
```

Rules, each a test:

- **`available()` must not do I/O.** No network call, no credential lookup, no
  model load. It is called during discovery; a slow probe there is a hang. It
  returns `None` when usable, else a human-readable reason.
- **Compatibility is a range.** A plugin loads when its
  `[min_api, max_api]` overlaps core's `API_VERSION`. A mismatch is a
  `PluginLoadError` surfaced in `doctor`, never a silent skip.
- **Names match** `^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$`, are unique across all
  plugins, and `claude`/`codex` are reserved.
- **Failures isolate per entry point.** One broken plugin must not prevent others
  loading. Collect `PluginLoadError(entry_point, error_class, detail)` and carry
  on.
- **Only the exception class name** is exposed in API output. A message can embed
  transcript text or credentials, per the same rule the ledger follows.
- **Discovery is cached** (`lru_cache`), so a change requires a restart. Document
  that in `doctor` output so the behaviour is not surprising.

`fetch()` returns `ParsedSession` objects reusing `muninn.sources.ParsedSession`,
and `SourceContext` gives a source its own namespace so contributed session ids
cannot collide with local ones: `plugin:<plugin>.<source>:<external_id>`.

## The policy chokepoint

```python
@dataclass(frozen=True)
class ModelPolicy:
    name: str
    allow: tuple[str, ...]          # regex allowlist of model ids
    require_provider: str | None    # None = any provider
    reason: str                     # shown verbatim on refusal

class PolicyRefused(RuntimeError): ...

def resolve() -> tuple[ModelPolicy, ...]   # from POLICY_ENTRY_POINT_GROUP
def check(model: str, provider: str) -> None   # raise PolicyRefused, or return
def effective_allowed(candidates: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]
```

Semantics that are not negotiable:

- **Intersection, never union.** With policies A and B loaded, a call is
  permitted only if **every** policy permits it. A contributor may narrow, never
  widen.
- **Fail closed.** No policy matching a `(model, provider)` pair ⇒ refuse. The
  refusal message includes each refusing policy's `reason` verbatim.
- **Config cannot widen.** If a config or flag names a model the policy forbids,
  the policy wins. Test this explicitly.
- **A failure to *discover* a policy narrows, never widens.** Added after the
  first implementation shipped a fail-open here; see
  `.valholl/articles/model-policy-chokepoint.md`, "Discovery is the attack
  surface, not just loading." `resolve()` must **not** call
  `entry_points(group=...)`, which deduplicates by normalised distribution name
  with first-on-`sys.path` winning and so lets a metadata-only directory mask a
  real policy distribution into invisibility. Walk `distributions()` and filter
  each one's `entry_points` by group instead. Two distributions contributing a
  policy under one normalised name is the shadowing signal and is reported in
  `doctor`.
- **The default is permissive but real.** With no policy plugins installed, a
  single built-in `ModelPolicy(name="default", allow=(".*",), ...)` applies, so
  the code path is exercised in normal use rather than only in restricted builds.
  A permissive default that is *absent* would mean restricted builds run
  different code from everyone else.
- **Every model call routes through `check()`.** That is the entire point;
  a provider that calls out without checking is a defect.

## Acceptance criteria

`tests/test_policy.py`:

1. **Intersection** — policy A allows `{a,b}`, B allows `{b,c}`; only `b` passes.
2. **Fail closed** — a model matching no policy is refused.
3. **Reason surfaced** — the refusal message contains each refusing policy's
   `reason` string verbatim.
4. **Config cannot widen** — a requested model forbidden by policy is refused
   even when explicitly configured.
5. **`require_provider`** — right model id via the wrong provider is refused.
6. **Permissive default applies** with no policy plugins installed, and is a real
   policy object rather than a bypass branch.
7. **Regex anchoring** — `allow=("^us\\.anthropic\\.",)` does not permit
   `evil-us.anthropic.foo`. Assert the anchoring behaviour you implement, and
   document it.

Criteria 15-18 were added after review found four fail-opens in the first
implementation. Note that criteria 1-7 are all satisfiable by monkeypatching
`entry_points` — which is what the original tests did, and why 15 was invisible
to a green suite. **A mock at the discovery seam cannot test discovery.**

15. **A shadowed distribution's policy still binds** — build real `.dist-info`
    directories on disk: a genuine policy distribution, plus a metadata-only
    distribution with the same name (no `entry_points.txt`) earlier on
    `sys.path`. The policy must still be enforced. Use a subprocess with
    `PYTHONPATH` rather than mutating `sys.path` in-process, because
    `importlib.metadata` caches its per-path-entry scan. Also assert two
    same-named distributions are reported, and that no policy installed still
    allows everything.
16. **`allow` is validated at construction** — `ModelPolicy(allow="string")` is
    rejected (the missing comma makes the permit check iterate *characters*, and
    `.` matches anything, so the policy permits nearly everything while
    `bool(allow)` stays truthy). Non-`str` elements are rejected too.
17. **A malformed pattern refuses rather than raising** — `allow=("[unclosed",)`
    fails at construction, not mid-check, and a policy that fails validation
    becomes the refuse-everything path. `re.error` must never reach a caller
    that catches only `PolicyRefused`.
18. **`SystemExit` at policy import fails closed** — neither `SystemExit` nor
    `KeyboardInterrupt` inherits from `Exception`, so a stray `sys.exit()` in a
    policy module propagated out of `check()`. Same test for
    `plugins.discover_plugins()`, which had the identical gap.

`tests/test_plugins.py`:

8. **A well-formed fake plugin loads** and its capabilities appear in the
   registry.
9. **Range compatibility** — `min_api=1, max_api=2` loads against
   `API_VERSION=1`; `min_api=2` does **not**, and produces a `PluginLoadError`
   that `doctor` shows.
10. **Failure isolation** — one entry point that raises on load does not prevent a
    healthy sibling from loading.
11. **Error text is a class name only** — assert the surfaced detail contains no
    spaces and matches `^[A-Za-z_][A-Za-z0-9_]*$`.
12. **Name validation** — an invalid name, a duplicate across plugins, and a
    reserved name (`claude`, `codex`) are each rejected with a load error.
13. **`available()` is not called with I/O** — a fake provider whose
    `available()` opens a socket fails the test; assert discovery does not invoke
    anything beyond constructing the spec. (Implement by asserting `available()`
    is not called during `discover_plugins()` at all.)
14. **Namespacing** — a contributed session id is prefixed
    `plugin:<plugin>.<source>:<id>` and cannot collide with a local session id.

Plus: existing contract tests pass unmodified; ruff clean; **no new
dependencies** — `importlib.metadata` is stdlib.

## Definition of done

```sh
uv run python -m unittest discover tests -v
uv run ruff check muninn tests tools
uv run muninn --db /tmp/muninn-008.db doctor   # plugins + policy sections, no errors
uv run muninn --db /tmp/muninn-008.db index    # real-corpus regression
```

Commit; do not push.

## Known limitation (found by the first real implementation)

`HistorySource.fetch()` is synchronous and receives only a `SourceContext`, so a
source has **no route to record that a session it previously contributed has
vanished upstream**. Contributing is expressible; absence is not.

That matters because the correct response to a vanished remote session is not
deletion — the archived prose may be the only surviving copy — but
`source_present = 0`, exactly as the local sweep does. See
`.valholl/articles/archive-of-record.md`.

The first real implementation had to put eviction in a non-protocol
`poll(store, context)` method, which works and is tested but which nothing in
core calls. Tracked as [#1](https://github.com/tohuw/muninn/issues/1); the
likely fix is a `reconcile()` method returning the keys a source still vouches
for, so core decides what absence means rather than trusting each plugin author
to know the rule.

## Guardrails

- **Do not ship a concrete provider.** No boto3, no MLX, no Neo-Cortex.
- **Do not use exact-match versioning.** Ranges, and loud failures.
- **Do not let policies union.** Intersection is the whole design.
- **Do not call `available()` during discovery.**
- **Do not put exception messages in surfaced output.**
- **Do not modify** `tests/test_losslessness.py`, `tests/test_ledger.py`,
  `tests/test_query.py`, `tests/test_indexer.py`, `tests/test_queue.py`,
  `tests/test_exports.py`, `tests/test_version.py`.
- Windows CI note: this repo pins `shell: bash`; avoid subprocess/thread fan-out
  in new tests (see `WINDOWS.md`).
