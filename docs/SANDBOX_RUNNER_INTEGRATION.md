# Standalone runner integration record

This is a blocked migration, prepared as a draft for review.
The thin frontend and pin are implemented, but the retained public suite finds runner compatibility failures.
Do not merge or replace the failing expectations with newly accepted output.
The required fixes belong in the private runner; Console must not regain native supervision.

## Revisions and baseline

Work started from current `origin/main`, `c3027d71a86f837804ff3234dd1fab5c9103ff40`, with a clean tree.
The previous runner pin was `3ee7d3190983b482b312ddfc3201c464179a1245`.
The new exact implementation pin is `ee88fa4f7744fdfb0e99dd0e928ff43d7171814e` from `mcp-console/sandbox-runner/rust-v0.150.1`.
Protocol 2 and Rust 1.95.0 remain pinned.
The original runner repository was read only; the build and executable tests used a separate checkout at the exact commit inside this worktree's ignored `.sandbox-runner-source` directory.

Before implementation edits, the baseline record captured all 417 discovered transcript cases, SHA-256 hashes of 510 boundary-test and snapshot files, the source revision and runner pin, a complete local `scripts/check`, and the installed-runner acceptance tests.
All recorded file hashes were unchanged at the end of the baseline.
Local evidence is under `/tmp/mcp-console-supervisor-integration`; this document records the durable results and reproductions.

| Validation                                      | Baseline result                                                                                                                                                                                                                    |
| ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| macOS 26.6.2, build 25G83, arm64; Darwin 25.6.0 | `scripts/check` passed: runtime-source validation, Python suites (12 release, 25 transcript-runner, 5 client, 4 architecture), formatting, Clippy, 47 Rust tests, and 407 applicable transcript cases. Ten cases were unavailable. |
| Installed public binary/private runner pair     | `python3 tests/sandbox_installation.py target/debug/mcp-console target/libexec/mcp-console-sandbox`: seven tests, five passed and two Linux-only skips.                                                                            |
| Current-main hosted macOS                       | Passed, including core, wheel smoke, R package, and transcripts.                                                                                                                                                                   |
| Current-main hosted Ubuntu 24.04                | Passed, including core, wheel smoke, R package, and transcripts.                                                                                                                                                                   |
| Local Linux Console baseline                    | Not performed.                                                                                                                                                                                                                     |

Both hosted results are for the exact baseline head in [Actions run 34365637066](https://github.com/t-kalinowski/mcp-console/actions/runs/34365637066).
The run was still pending when the baseline was captured and completed successfully during integration.
A local Linux VM was inspected, but no Console or runner validation was performed there; it was stopped and the prior Docker context restored.

## Executable contract inspected

The pin was selected after reading `mcp-console-sandbox/PROTOCOL.md`, `LIFECYCLE.md`, `src/main.rs`, `bootstrap.rs`, `config.rs`, `launch.rs`, `native.rs`, `storage.rs`, `signals.rs`, and the platform implementations and executable contract tests.
The pin's `cargo +1.95.0 test --locked -p codex-mcp-console-sandbox --test bootstrap_contract` passed all 54 contracts on macOS.
Its locked release build for `aarch64-apple-darwin` and Console artifact staging also passed.
Those runner tests do not establish that the Console acceptance suite passes.

Console uses the executable's `--config-env NAME -- COMMAND [ARG]...` interface.
Only the small fixed policy and lifecycle object is serialized into the selected environment value; the target's command, cwd, and environment use ordinary launch inputs.
The runner consumes and removes the selected variable.
The separate inherited-descriptor interface remains covered by the unchanged installation tests, but Console no longer writes a bootstrap pipe.
There is no new Console payload limit or single environment value containing the entire target environment.
The retained 96 KiB environment case passes; maximum host exec limits were not exhaustively tested.

## Before and after layouts

Before, on macOS:

```text
caller or MCP server
└─ mcp-console sandbox                    Console launcher, terminal owner
   ├─ mcp-console sandbox-manager         Console descendant and directory owner
   └─ private runner                      waitable process-group root
      └─ mcp-console sandbox-target → target / relay
         └─ worker and descendants
```

The Console launcher had a manager recovery monitor and a direct-root exit waiter.
It owned a setup pipe and control socket, kept the direct root waitable during cleanup, and recovered manager failure.

Before, on Linux:

```text
caller or MCP server
└─ mcp-console sandbox                    Console subreaper and recovery owner
   └─ forked Console manager              Console subreaper and directory owner
      └─ private runner / native helper
         └─ bubblewrap / namespace init
            └─ mcp-console sandbox-target → target / relay
               └─ worker and descendants
```

After, on both supported hosts:

```text
caller or MCP server
└─ mcp-console sandbox → private runner   exec preserves frontend PID and caller
   └─ native enforcement and target       owned entirely by the private runner
      └─ relay → worker, or standalone command descendants
```

On macOS the native stage execs the target in its own PID as the dedicated process-group leader; there is no waiting native adapter between the supervisor and target.
On Linux the runner owns the native helper, namespace init, host subreaper and pidfd cleanup.
The server still owns one ordinary child, its standard streams, logical worker-generation retirement, and restart admission.
It has no private directory, native startup state, process tree, or manager channel.

## Production deletions and retained policy

Sixteen production files are deleted: `sandbox/child.rs`, `command.rs`, `linux.rs`, `linux/process.rs`, `process_group.rs`, `supervision.rs`, and all ten files under `sandbox/supervision/`.
This removes native descendant tracking, manager and monitor recovery, temporary-directory guards, fork-and-continue startup, root waiters, sandbox job control, bootstrap writing, and the hidden target signal wrapper.
The `sandbox-manager` and `sandbox-target` CLI variants and dispatch paths are deleted.
Sandbox-only descriptor and direct-child polling helpers are deleted; ordinary server child observation and descriptor sanitation remain.

The production diff removes 4,025 lines and adds 64, a net deletion of 3,961 lines (`git diff --numstat c3027d71 -- src`).
All rules in `src/sandbox/policy_extensions.sbpl` and installation verification are unchanged; only the policy comment linking to its audit document is updated.
Filesystem host reads, network restriction, full mutation of private temporary data, host-terminal restrictions, and all named macOS compatibility exceptions remain selected by Console.
The runner now creates and owns `sandbox-XXXXXX/data`, exported as `TMPDIR`.
The former Console supervision and Linux ownership documents are replaced by [sandbox integration](SANDBOX.md); protocol and architecture documents retain the ordinary server and relay responsibilities.

## Changed tests and observation mechanisms

No retained transcript expectation is rewritten and no behavior assertion is relaxed.
Historical transcript role labels such as “manager” are preserved where they still identify the cleanup owner, now the runner at the original frontend PID.
The following fixture changes are distinct from changes to supported guarantees:

| File or fixture                                                      | Observation change                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| -------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tests/boundaries/cli/_harness.py`                                   | Find the runner's native child by direct-child identity and its dedicated group, allowing the existing inherited foreground peer. The target and native root now share a PID; the cleanup owner and frontend now share a PID. Deduplicate identical exit watches and exclude the supervisor from its own prerequisite cleanup set. Keep strict exit-before-cleanup-barrier assertions. Fixture teardown removes the runner container around `data` after stopping test processes. |
| `cli/sandbox/test_supervision.py`                                    | Three topology assertions now require target PID = native root/group leader. Signal delivery, terminal ownership, descendant exit, and stream assertions are unchanged.                                                                                                                                                                                                                                                                                                           |
| `client_server/_harness.py`                                          | Discover fixture markers under `sandbox-*/data`, watch the new container/data directories, and identify the macOS stopped-worker relay as the group leader directly below the runner. Linux namespace-init parent assertions remain.                                                                                                                                                                                                                                              |
| `client_server/lifecycle/test_startup.py`                            | Four no-private-storage-before-start assertions use the runner's directory naming.                                                                                                                                                                                                                                                                                                                                                                                                |
| `client_server/recording/test_journal.py`                            | The no-private-storage-before-start assertion uses the runner's directory naming.                                                                                                                                                                                                                                                                                                                                                                                                 |
| `client_server/sandbox/test_crashes.py`                              | Locate the supervisor as the server's direct child instead of deriving it from the relay's process group or scanning for the deleted manager CLI. Preserve exact process identities and caller-death cleanup assertions; fixture teardown removes the container around `data`.                                                                                                                                                                                                    |
| `client_server/sandbox/test_relay.py`                                | The explicit wrapper is the native root itself, with the relay still verified as its direct child.                                                                                                                                                                                                                                                                                                                                                                                |
| `client_server/sandbox/test_replacement.py` and `tests/fixtures/zod` | Worker and relay remain distinct; the relay can now equal its group leader. Preserve descendant cleanup, group separation, stdin and replacement assertions.                                                                                                                                                                                                                                                                                                                      |
| `client_server/sandbox/test_retirement.py`                           | Identify the supervisor separately from the relay group leader, identify the native root as the relay, and update the accepted-shutdown fixture's macOS group-leader identity. The legacy manager-recovery fixture resolves the same supervisor directly.                                                                                                                                                                                                                         |
| `client_server/sandbox/test_startup.py`                              | Resolve server → runner → native root at the startup checkpoint; add the native fixture include path. Existing error text and target-gating assertions remain.                                                                                                                                                                                                                                                                                                                    |
| `cli/sandbox/test_crashes.py`                                        | Use runner private-storage names and include paths. Before parent capture, no native root or storage exists yet, so watch the supervisor alone and assert no storage. Caller-loss, target-not-run, cancellation, and exact diagnostics remain required. Fixture teardown removes the container around `data`.                                                                                                                                                                     |
| `client_server/sandbox/test_supervision.py`                          | Fixture teardown removes the runner container around the recorded `data` directory after stopping test processes. Public cleanup assertions remain unchanged.                                                                                                                                                                                                                                                                                                                     |
| `native/runner_interposer.h` (new)                                   | Test-only `execvp` interposition reinserts the observation library across Console's production loader-variable removal. It creates no replacement executable or helper process and leaves production sanitation unchanged.                                                                                                                                                                                                                                                        |
| `native/manager_start_interposer.c`                                  | Gate the actual runner's successful native-readiness `recv` instead of the deleted manager entrypoint.                                                                                                                                                                                                                                                                                                                                                                            |
| `native/setup_write_interposer.c`                                    | Observe the actual runner's framed native setup `send` instead of the deleted Console setup-pipe `write`; preserve partial-write and cancellation checkpoints.                                                                                                                                                                                                                                                                                                                    |
| `native/root_waiter_start_interposer.c`                              | Gate the runner's parent capture instead of the deleted root-waiter startup.                                                                                                                                                                                                                                                                                                                                                                                                      |
| `native/manager_observation_interposer.c`                            | Observe the actual runner's `proc_listchildpids` calls instead of the deleted manager. The detached-child observation checkpoint remains causal.                                                                                                                                                                                                                                                                                                                                  |
| `tests/architecture.py`                                              | Point existing forbidden-dependency fixture edits at the surviving `sandbox/runner.rs`; boundary assertions remain unchanged.                                                                                                                                                                                                                                                                                                                                                     |

One new public case, `cli/sandbox/test_execution::frontend_exec_preserves_pid_and_standard_streams`, verifies that the original frontend PID becomes the verified runner, consumes the configuration variable, preserves all 256 byte values on stdin/stdout/stderr, and returns target status 23.
It failed against the baseline executable because that PID still named `mcp-console`, then passed after integration.
Only its new snapshot was generated with `scripts/test --update cli/sandbox/test_execution::frontend_exec_preserves_pid_and_standard_streams`.
No existing snapshot was regenerated.

## Changed guarantees and legacy recovery tests

Independent recovery after supervisor death is intentionally removed.
SIGKILL, supervisor crash, or an unresponsive supervisor no longer guarantees descendant cleanup, directory removal, or terminal restoration.
Native policy remains enforced on surviving sandboxed processes.
Configured caller death while the runner lives is a retained guarantee, including startup and detached descendants already observed by the runner.

The following original cases specifically require the removed recovery topology and are retained, with their original expectations, as explicit migration failures in this draft:

- `cli/sandbox/test_crashes::launcher_crash_retires_the_sandbox_lifetime`;
- `cli/sandbox/test_crashes::manager_crash_retires_the_sandbox_lifetime`;
- `client_server/sandbox/test_crashes::manager_crash_retires_the_worker_generation`;
- `client_server/sandbox/test_startup::manager_failure_before_readiness_keeps_custom_relay_gated`;
- `client_server/sandbox/test_retirement::restart_waits_for_owned_launcher_manager_recovery`; and
- only the launcher-crash, manager-crash, and stopped-manager scenarios within Linux `cli/sandbox/test_linux::retires_descendants_after_exit_and_supervisor_loss`.

These require explicit retirement or replacement once the migration can pass its retained contracts; they are not capabilities to rebuild in Console or request from the runner.
The Linux combined case's command-exit, owned-SIGTERM, and owner-exit scenarios must remain.
The caller-death cases in `cli/sandbox/test_crashes`, the MCP server-crash case, and cancellation during setup must also remain.
No cases are skipped or disabled to obtain a passing report.

Other visible changes are runner-owned temporary-path shape, strict directory-removal errors instead of best-effort deletion, storage retention on unproven retirement, and runner-prefixed startup errors.
On Linux, `TMPDIR` now identifies a mutable `data` child rather than the bind-mount root, so that directory and its metadata entries can be replaced.
These source-derived differences are recorded separately from the intentionally removed supervisor-death guarantee.
Directory-removal failure status and the expanded Linux temporary-directory mutation were not validated against the old contract and require compatibility review.
Temporary paths are incidental test data; the exact error-text differences are unresolved compatibility failures, not accepted snapshot changes.
The documented pre-existing Darwin boundary for unobserved descendants that detach and orphan before discovery remains distinct from cleanup of an owned group or an already observed descendant.

## Runner-only follow-up

1. Preserve the native root's waitable identity through retirement and close the owned process group as well as retiring observed detached identities.
   The retained partial-sideband replacement case failed its descendant-group cleanup assertion.
   A paired public CLI probe also found a live detached child after runner status 23 and directory removal; five baseline runs cleaned up all children, while one of five integrated runs leaked a live `Ss` child.
   The latter probe alone does not prove discovery of that detached child.
   A second paired probe kept the child inside the target group: all five baseline runs retired it, while three of five integrated runs returned status 23 with the child still live (`S`) and private storage removed.
   This reproduces the missing group cleanup without relying on detached-child discovery.
   Source inspection shows `root.try_wait()` reaps the native root before retirement and the Darwin tracker has no original-group cleanup backstop; these are the runner-side points to correct and verify against retained Console cases.
2. Keep cancellation ordered with native setup writes and retain the setup channel until the native reader has been retired.
   The partial-frame cancellation case observes `mcp-console-sandbox: failed to fill whole buffer` where cancellation previously produced empty stderr.
   The before-release caller-death case sees the same native error in place of its caller-loss diagnostic.
   Console cannot suppress or translate these errors after exec without restoring a waiting adapter.
3. Resolve startup diagnostic compatibility in the runner interface.
   The private-storage failure case expects ``failed to create temporary directory `<sandbox temp>`: Not a directory (os error 20)`` but receives `mcp-console-sandbox: create private storage: Not a directory (os error 20)`.
   Caller loss during parent capture likewise changes its exact diagnostic.
   The stable MCP startup error and retry path remain; their inherited stderr contract does not.

No runner repository edits or follow-up implementation are included in this task.
Advance the pin only after the runner-only fixes have executable regressions and the retained Console suite passes on both supported hosts.

## Final validation

The final `scripts/check` passed runtime-source validation, all four Python core suites, Rust formatting, Clippy, and all 47 Rust tests, then failed in the transcript suite on retained startup diagnostics and a legacy manager-recovery expectation.
Its fail-fast cancellations are not passing validation.

To expose the remaining outcomes, each of the 418 discovered cases was then invoked independently through `scripts/test CASE`, with the same CPU-based concurrency as the ordinary runner.
That pass produced 393 passes, 15 failures, and ten capability skips.
Two failures were remaining topology assertions (stopped-worker ancestry and accepted-shutdown group identity); both passed their unchanged behavior and snapshot assertions after the fixture corrections listed above.
After those corrections the inventory was 395 passed, 13 failed, and ten skipped.
A final fixture-cleanup recheck reproduced the earlier intermittent `owned_root_exit_waits_for_cleanup` failure.
The conservative final inventory is therefore **394 passed, 14 failed, ten skipped**.
It combines the full per-case pass with focused reruns, rather than claiming one successful full-suite invocation.
The legacy launcher-crash case reached the existing 600-second case deadline while a surviving target retained stderr; the test supervisor interrupted it and its fixture cleaned up the processes.

The fourteen failures comprise the five macOS supervisor-recovery cases listed above and these nine retained contracts:

| Retained case                                                                                       | Failure                                                                                                                          |
| --------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `cli/sandbox/test_signals::owned_root_exit_waits_for_cleanup`                                       | Supervisor exit precedes the observed descendant-cleanup barrier. Intermittent; reproduced in the final recheck.                 |
| `cli/sandbox/test_crashes::owner_loss_before_exit_watch_cleans_startup`                             | Caller-loss diagnostic changed to `parent_pid is not the current parent`.                                                        |
| `cli/sandbox/test_crashes::owner_loss_before_target_release_cancels_startup`                        | Native setup EOF diagnostic replaces the caller-loss diagnostic.                                                                 |
| `cli/sandbox/test_crashes::cancels_owned_launch_during_setup`                                       | Cancellation unexpectedly emits the native setup EOF diagnostic. Later scenarios after the failing assertion were not validated. |
| `client_server/sandbox/test_startup::sandbox_setup_failure_is_reported_and_retryable`               | Private-storage setup diagnostic changed.                                                                                        |
| `client_server/sandbox/test_replacement::restarts_after_worker_exit_with_partial_sideband`          | Descendant group survives retirement.                                                                                            |
| `client_server/sandbox/test_retirement::restart_does_not_report_never_ready_worker_as_stopped`      | Detached startup descendant survives retirement.                                                                                 |
| `client_server/sandbox/test_shutdown::shutdown_cancels_partial_sideband_frame`                      | Descendant group survives server shutdown.                                                                                       |
| `client_server/sandbox/test_shutdown::restart_drains_readable_frame_before_abandoning_partial_tail` | Descendant group survives retirement.                                                                                            |

The strict cleanup barrier failure in `cli/sandbox/test_signals::owned_root_exit_waits_for_cleanup` appeared in both an initial focused run and the final recheck; its intervening pass does not erase that failure.
Both same-group and detached-child paired probes are reported above, including live process states rather than zombie-only observations.

Passing retained cases include file/network isolation, host-terminal denial, processx PTYs, offline uv installation, R/Python/SQL runtime workflows, binary and regular-file stdin, inherited descriptor and signal-state handling, terminal interruption with and without a foreground peer, owned SIGTERM before setup, configured caller/server death, observed detached-descendant cleanup, and ordinary restart/shutdown cases outside the failures above.
The new frontend exec case passed after its baseline red result.
The unchanged installed-binary tests passed again: five applicable tests and two Linux-only skips.
All 425 baseline snapshot files match their recorded hashes; only the new frontend-exec case adds a snapshot.
All existing policy rules match the baseline; only their documentation-link comment changed.
Formatting and `git diff --check` passed.

Linux integration execution, installed-wheel rehearsal, R package checks on the changed tree, x86_64 host execution, and hosted validation of the new pin have not been performed locally.
