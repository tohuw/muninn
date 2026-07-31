# Windows status

Muninn targets macOS, Linux and Windows on a **best-effort** basis. This file
records what is actually known rather than what is intended, in the same spirit
as [Huginn's WINDOWS.md](https://github.com/tohuw/huginn).

## Never tested on a real Windows machine

Everything below comes from the GitHub Actions `windows-latest` runner. No part
of Muninn has been exercised on real Windows hardware by a human. Treat Windows
support as plausible, not proven.

## Known limitation: subprocess and thread-fan-out tests

Four tests are skipped on Windows, marked `@requires_subprocess`:

| Test | Property |
|---|---|
| `test_queue.py::test_many_threads_enqueue_no_job_lost_or_corrupted` | concurrent enqueues survive |
| `test_query.py::test_cli_json_flag_emits_a_json_array` | `--json` shape via the real CLI |
| `test_exports.py::test_float_update_time_digests_identically_across_processes` | digest stability across processes |
| `test_indexer.py::test_n_concurrent_imports_yield_one_imported_rest_duplicate` | import lock serializes |

On that runner these do not *fail* — they **wedge**, hanging in
`subprocess.communicate()` or `threading.Thread.start()` until the job timeout
kills the entire run. One hung test therefore hides every other result, which is
strictly worse than a red test.

Seven attempted fixes eliminated every cause in Muninn's own code before this
conclusion was reached. Each found a real defect worth fixing on its own:

1. An unbounded `sys.stdin.read()` in the hook — a genuine production hazard
   given the 1.5-second `SessionEnd` budget. Fixed.
2. A `select()` bound that raised `UnsupportedOperation: fileno` on an in-memory
   stdin. Fixed.
3. A test `sys.path` entry pointing *inside* the package rather than at the repo
   root. Fixed.
4. A `/dev/null` transcript path, meaningless on Windows. Fixed.
5. Fifty simultaneous thread starts where twelve prove the same property. Fixed.
6. A subprocess test that replaced `sys.stdin` while the inherited OS-level
   stdin stayed open. Removed.
7. An import-purity check that spawned an interpreter at all — now done by
   static `ast` analysis, which is both platform-independent and stricter, since
   a `sys.modules` probe silently passes if something else already imported the
   module. Rewritten.

The behaviour survived all seven. Every skipped property has equivalent
in-process coverage that runs on all three platforms, so what is lost on Windows
is the belt-and-braces process-level check, not the invariant.

## Known limitation: chmod cannot make a directory unwritable

`os.chmod(dir, 0o500)` does not prevent writes on Windows — the CI runner writes
into such a directory happily. Two queue tests that assert `enqueue()` *returns
None* when the queue is unwritable are therefore POSIX-only.

The half that matters everywhere still runs there: `enqueue()` must **never
raise**, because a failing `SessionEnd` hook must not disrupt a session no matter
what state the queue directory is in. Only the return-value assertion is skipped,
and only because the precondition cannot be established.

## Untested on Windows generally

- The background indexer's watcher (`watchfiles` on Windows file locking).
- `install-hooks` writing to `%USERPROFILE%\.claude\settings.json`.
- Rewrite detection when a transcript is held open by another process — Windows
  disallows deleting or renaming open files, and the atomic
  temp-file-plus-replace pattern needs care there.
- WSL transcript discovery.

## If you run Muninn on Windows

Bug reports are welcome and will be treated as new information rather than as
regressions. Start with `muninn doctor`, which reports index lag, queue depth and
parse-failure rates — the three things most likely to reveal a platform problem.
