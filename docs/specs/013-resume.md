# Spec 013 — `muninn resume`

**Status: implemented.** Depends on 004 (the archive already stores `cwd`,
`source` and `session_id`).

**Read first:** [`archive-of-record`](../../.valholl/articles/archive-of-record.md).
The refusals in this command are that article, expressed as exit codes.

This is one of the four capabilities [#4](https://github.com/tohuw/muninn/issues/4)
lists as named-but-unbuilt. It is the cheapest and the only one with no
dependency on enrichment or embeddings, which is why it landed first. The status
of the other three is recorded at the bottom rather than left implicit.

## The command

```sh
muninn resume a7efca23           # print the invocation
muninn resume a7efca23 --exec    # run it
muninn resume a7efca23 --json    # for an agent
```

```
a7efca23-...  claude  2026-06-14  /Users/you/Projects/muninn

cd /Users/you/Projects/muninn && claude --resume a7efca23-...
```

Exit codes, because an agent runs this: **0** resumable, **1** no session
matched, **3** matched but not resumable.

## The decision worth making explicitly: print, don't execute

Printing is the default and `--exec` is opt-in. Printing is safer and
composable — the line can be read, edited or piped — and executing hands the
terminal to another interactive program, which is a bigger thing to do on a
*prefix match* than on an explicit request.

## The refusals are the important half

Muninn is an archive of record. The vendor sweeps transcripts after 30 days, so
the **majority of what this command can find, it cannot resume** — on the
development corpus, every one of the 3,730 sessions recovered by spec 012. That
is the normal end state, not a malfunction, and this command's usefulness decays
as the archive outlives its sources. It has to say so.

So a refusal never emits a command. Printing `claude --resume <id>` for a
transcript deleted months ago hands the user a failure carrying the vendor's own
error message, which explains nothing — and CLAUDE.md's agent-facing contract
says an agent should only transport claims the tool can prove. "The transcript is
gone; here is what the archive still has" is provable. "Try this and see" is not.

| case | refusal |
|---|---|
| `source_present = 0` | the vendor swept it; the archive is now the only copy |
| `origin != 'raw'` | recovered from a predecessor's archive, so swept by construction |
| `provenance = 'subagent'` | no session of its own to reopen — names the parent |
| `claude-cloud` / `chatgpt-cloud` | lives in a web UI; there is no local invocation to print |

Checks run in order of *certainty*, not of likelihood. A subagent has no
resumable identity at all, which is true regardless of whether its transcript
survives, so it is answered before presence — which is merely true today.

Every refusal names `muninn show`, because the prose is still there. That is the
whole point of having kept it.

**Exit 3 is separate from exit 1 on purpose.** "I could not find it" and "I found
it and its transcript is gone" lead to completely different next moves, and
collapsing them would make the common case indistinguishable from a typo.

## Two smaller decisions

- **`ResumePlan` carries a command or a refusal, never both.** A plan with both
  would invite a caller to print the command anyway, which is the behaviour this
  module exists to prevent.
- **`--exec` refuses when the session's directory is gone.** Letting the tool
  start in whatever directory happens to be current would resume the right
  session in the wrong repository — the tool comes up with a working tree
  matching nothing in the transcript.

## Acceptance criteria

1. A live Claude session prints `cd <cwd> && claude --resume <id>` and exits 0.
2. Codex gets `codex resume`, not `codex --resume`.
3. A prefix is enough; an ambiguous prefix lists candidates and exits 1.
4. Each of the four refusals exits 3, emits no command, and names `muninn show`.
5. A subagent is refused even when its transcript survives, and names its parent.
6. A plan never carries both a command and a refusal.
7. `--exec` is off by default, relays the tool's exit code, refuses a vanished
   directory, and reports a missing tool by exception *class* only.
8. `--json` is parseable for both answers and carries the refusal text.

## The rest of #4, unbuilt and why

- **`brief`** — the highest-value item, and blocked on spec 005 (enrichment). A
  brief over unenriched sessions is concatenated search results. Now unblocked
  at the calibration end by spec 011. Its design constraint is already fixed:
  every claim must carry provenance, so an agent can say "decided in session
  a7efca23 on 2026-06-14" rather than "apparently you decided".
- **`correlate`** — fully specified by 006; blocked on embeddings. No further
  design needed.
- **The agent skill** — deliberately last. It is largely a wrapper over these
  commands, teaching an agent to use `search`, then `brief`, then `show` in
  order of increasing cost, so writing it before `brief` exists means rewriting
  it after. Two things it must carry: the CLI is the public boundary (it forbids
  reading `~/.claude`, `~/.codex` or the archive directly), and transcript
  excerpts are **observed data, never instructions** — Muninn returns text
  written by other agents and by web content, which can contain injection
  attempts.
