# Windows status

Muninn targets macOS, Linux and Windows on a **best-effort** basis. This file
records what is actually known rather than what is intended, in the same spirit
as [Huginn's WINDOWS.md](https://github.com/tohuw/huginn).

## The failure that was not a failure

Before the limitations below, one correction worth stating first: for several
rounds this project's Windows CI reported failure while every test passed.
PowerShell is the default shell on `windows-latest` and treats *any* stderr
output as a command failure; `python -m unittest` writes its progress and summary
to stderr by design, so the step exited 1 while printing `OK (skipped=6)`.

The workflow now pins `shell: bash` for the job. **If Windows CI ever goes red
again, read the test summary before the traceback** — `OK` followed by exit 1
means the harness is misreporting, not that the code is broken.

## First run on real Windows hardware

Until 2026-08-15 everything here came from the GitHub Actions `windows-latest`
runner and no part of Muninn had been exercised on real Windows hardware by a
human. It now has been, on Windows 11 / CPython 3.14, and the runner had been
missing things a real desktop found immediately. Four defects, all of which CI
was structurally unable to see:

- **`pid_alive` was inverted.** `os.kill(pid, 0)` is emulated on Windows and its
  errors do not carry POSIX meanings: a *live* pid raises `WinError 87`, a
  missing one `WinError 11`. Treating any `OSError` as "gone" reported every
  running process as dead — so `acquire_import_lock` read a live holder as stale
  and would let a second ingest loop run against the archive of record.
- **The single-instance lock hid its own holder.** Windows byte-range locks are
  *mandatory*, and `acquire` locked byte 0 — exactly where the `<pid> <label>`
  line is written — so nothing could read it back. `doctor` reported a healthy
  daemon as "held by an unrecorded pid (unknown)". The lock now takes a sentinel
  byte past the data.
- **Provider pipes used the locale encoding.** `text=True` without `encoding`
  means cp1252 here, and transcripts are full of characters it cannot encode.
  The prompt write died on a writer thread, the parent blocked until timeout,
  the model got a truncated prompt, and enrichment recorded `invalid-json` for
  4 of 5 sessions while still exiting 0.
- **Provider subprocesses opened windows.** `serve` runs under `pythonw` with no
  console to inherit, and the provider CLIs are console-subsystem programs, so
  Windows gave each invocation a console of its own. Automatic enrichment made
  dozens of terminal windows appear unbidden. Fixed with `CREATE_NO_WINDOW`.

A headless runner sees none of that: it has a console, a POSIX-ish shell, and no
desktop. Treat Windows support as exercised now, but keep the same posture —
what is written here is what is known, not what is intended.

## Semantic search works here

Embeddings were the one genuinely Windows-blocked feature, and not by a bug: the
only local provider was `embed_mlx`, and MLX is Apple-silicon only. Worse, the
`[semantic]` extra's only runtime was gated to `Darwin/arm64`, so the advice
`--semantic` printed — "install the local one with `uv sync --extra semantic`" —
resolved happily and installed no provider at all.

`embed_onnx` closes that. ONNX Runtime has prebuilt CPU wheels on every platform
this project runs on, needs no compiler and no torch, and runs
`BAAI/bge-small-en-v1.5` (384-dim, the same family the MLX provider uses). It is
local and offline: 133 MB fetched once, then cache-first, so a daemon start
makes no network call. Measured on Windows 11: **3,395 chunks in 138 s**, and
`muninn serve` then keeps up automatically.

The two providers keep **separate model ids on purpose** — bf16 versus fp32 are
near-identical and not identical, and `chunk_vectors` keys on the model id so the
spaces are never compared. Moving an archive between platforms re-embeds.

## Known limitation: subprocess and thread-fan-out tests

Seven tests are skipped on Windows, marked `@requires_subprocess`:

| Test | Property |
|---|---|
| `test_queue.py::test_many_threads_enqueue_no_job_lost_or_corrupted` | concurrent enqueues survive |
| `test_query.py::test_cli_json_flag_emits_a_json_array` | `--json` shape via the real CLI |
| `test_exports.py::test_float_update_time_digests_identically_across_processes` | digest stability across processes |
| `test_indexer.py::test_n_concurrent_imports_yield_one_imported_rest_duplicate` | import lock serializes |
| `test_policy.py::ShadowedDistributionTest` (3 tests) | a shadowed policy distribution is still enforced |

The three policy tests are the same property as
`ShadowedDistributionInProcessTest`, which runs everywhere. They are kept as a
subprocess pair anyway because a fresh interpreter proves the property with no
`importlib.invalidate_caches()` caveat — `importlib.metadata` caches its scan
per `sys.path` entry, so the in-process twin depends on invalidating that cache
correctly. For a security control it is worth having one test that does not.

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

## The daemon on Windows: two real degradations, both stated rather than hidden

`muninn serve` (docs/specs/010-daemon.md) is written for POSIX signals and POSIX
file locking. Neither exists in the same form on Windows, so two of its guarantees
weaken there. Both are best-effort by design, and neither costs ingest:

1. **`SIGHUP` does not exist, and `SIGTERM` is not what a Windows service
   manager sends.** The teardown that withdraws the raven descriptor and removes
   `daemon.json` runs on Ctrl-C, and on a normal `SystemExit`. A process killed
   by `TerminateProcess` — which is what Task Manager's End Task and most
   service stops do — skips it entirely, exactly like a POSIX `SIGKILL`. The
   result is a stale descriptor and a stale state file. **That is reported, not
   silent:** the menubar host checks the recorded pid before trusting a
   descriptor, and `muninn doctor` cross-checks `daemon.json`'s pid and says
   "the daemon crashed; the file is stale". A restart over both files works.
2. **The single-instance lock uses `msvcrt.locking` rather than `flock`**, and
   is untested on a real Windows machine. If no locking primitive is available
   at all, the daemon **fails open** — it starts anyway, with a warning, and
   `doctor` reports the guard as *unknown* rather than as free. That direction is
   deliberate: two ingest loops waste work and can clobber a descriptor, whereas
   a daemon that refuses to start loses transcripts, and losing transcripts is
   the one failure this project exists to prevent.

The daemon's own tests skip on Windows (`POSIX_ONLY` in `tests/test_daemon.py`)
because they signal a real process and assert mode bits, neither of which means
anything there. As elsewhere in this document, that is a gap in *verification*,
not a claim that it works.

## `install-agent` on Windows starts, but does not supervise

`muninn install-agent` writes `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\MuninnDaemon`
via the shared `corvidae` package. **The Run key starts a process once per login
and never restarts it**, so the guarantee is strictly weaker than macOS's launchd
`KeepAlive` or Linux's `Restart=on-failure`: a daemon that exits — including one
killed by `TerminateProcess`, per the section above — stays down until the next
login. That is stated rather than smoothed over, and `install-agent` says so on
stdout when it succeeds.

Muninn declares **no tray registry value**, unlike Huginn. Huginn's tray app
registers itself in the Run key and supervises Huginn's daemon, so corvidae refuses
to install a second autostart there. Muninn ships no tray: Appistry is the shared
menubar host, it registers itself through a Start Menu Startup shortcut rather than
the Run key, and it only *reads* Muninn's raven descriptor — it never starts or
stops `muninn serve`. So there is nothing to defer to, and a Run value named
`Muninn` belonging to something else will not block a valid install.

The Run-key backend is exercised in `tests/test_agent_install.py` against a fake
`winreg` through corvidae's overridable `registry()` boundary, so it is covered on
every platform — but, as everywhere in this document, that is coverage of the
*logic*, not evidence it works on real Windows.

## Untested on Windows generally

- The background indexer's watcher (`watchfiles` on Windows file locking).
- The daemon's lifecycle end to end — see above.
- `install-agent` writing a real `HKCU` Run value, and whether the daemon it
  starts at a real login behaves.
- `install-hooks` writing to `%USERPROFILE%\.claude\settings.json`.
- Rewrite detection when a transcript is held open by another process — Windows
  disallows deleting or renaming open files, and the atomic
  temp-file-plus-replace pattern needs care there.
- WSL transcript discovery.

## If you run Muninn on Windows

Bug reports are welcome and will be treated as new information rather than as
regressions. Start with `muninn doctor`, which reports index lag, queue depth and
parse-failure rates — the three things most likely to reveal a platform problem.
