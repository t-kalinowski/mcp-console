# Standalone runner integration record

This migration leaves Console responsible for application policy, verified installation, and ordinary child-process integration.
The private runner owns native enforcement, lifetime supervision, and storage.
This record distinguishes preserved behavior, changed fixture observation, and intentionally removed supervisor-death recovery.

## Revisions and baselines

The PR originally started from main `c3027d71a86f837804ff3234dd1fab5c9103ff40` and integrated runner `ee88fa4f7744fdfb0e99dd0e928ff43d7171814e` as a blocked draft.
After the runner follow-up landed, main was baselined again at `26a5f3c35408e2a3ca5ba21485322044f883ca8a`, then merged into the PR.
That main includes the two-pipe relay-worker sideband and its fixture synchronization fixes.
Main advanced again to `abba95d6a12c8ba442bbf986e00eee7ef9f6a634` during validation, adding only a poll-ownership fixture checkpoint and its snapshot.
The changed public case passed against the saved baseline executable, its four incoming files were hashed, and the commit was merged unchanged.
The case then passed on the integrated macOS and Linux builds.
Both main baselines used runner `3ee7d3190983b482b312ddfc3201c464179a1245`.

The subsequent main refresh merges `e37f8ab0af7acc8df3b6e787e648b9e24ce55d4f` in merge commit `0f2710035b56c7b6e5da3edbdf9a17053563a99c`.
It retains main's automatic companion preparation for source installations, streaming verification of the companion bundle, release executable for integration tests, and CPU-only PyTorch wheels on Linux.
Conflicts in `AGENTS.md` and `docs/ARCHITECTURE.md` combine main's packaging behavior with this PR's frontend and runner responsibilities.
No snapshot or integration-specific behavior assertion changes in this merge.

The final pin is the exact implementation commit `b5a1c9f76a9c6ca2909105aecbc78555a26fda01` on `mcp-console/sandbox-runner/rust-v0.150.1`.
It supersedes `fe1b9e4e89458ba8812bfb0a65fb0a1ad84e71d5` after the acceptance-test restoration below, adding owned-stdio process-group isolation and Linux native parent-death links.
Protocol 2 and Rust 1.95.0 remain pinned.
The original runner repository was read only; builds and contract tests used isolated checkouts at the exact pin.

Before the original migration and the runner follow-up, the record captured revision, pin, discovered public cases, snapshot hashes, a complete local `scripts/check`, and installed-runner acceptance results.
All baseline file hashes were verified unchanged after the baseline run.
Local logs and inventories are under `/tmp/mcp-console-supervisor-integration` and `/tmp/mcp-console-supervisor-followup`; the results below are the durable record.

| Baseline                                                    | Result                                                                                                                                                                                    |
| ----------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Original main, macOS 26.6.2 arm64                           | `scripts/check` passed: core checks, 47 Rust tests, 407 applicable transcripts; 10 capability skips. All 425 snapshots unchanged.                                                         |
| Updated main, same macOS host                               | `scripts/check` passed: core checks, 47 Rust tests, 413 applicable transcript cases; 10 cases unavailable. All 431 snapshots unchanged; 520 boundary/snapshot files hashed.               |
| Installed public binary/private runner pair, both baselines | Seven tests: five passed, two Linux-only skips.                                                                                                                                           |
| Original main hosted macOS and Ubuntu 24.04                 | Both passed in [run 34365637066](https://github.com/t-kalinowski/mcp-console/actions/runs/34365637066).                                                                                   |
| Updated main hosted macOS and Ubuntu 24.04                  | Ubuntu passed; the macOS job was cancelled during transcripts, so it has no passing full result. [Run 34395951703](https://github.com/t-kalinowski/mcp-console/actions/runs/34395951703). |
| Local Linux Console baseline                                | Not performed.                                                                                                                                                                            |

The later hosted run for main `abba95d6` was inspected during final validation.
macOS passed; Ubuntu failed before sandbox execution while resolving the host PyTorch environment, after the fixture's 300-second timeout.
That is a separate baseline failure in [run 34400453875](https://github.com/t-kalinowski/mcp-console/actions/runs/34400453875).

Counts refer to public cases, including all applicable execution modes.
The macOS logs have 11 skip records for 10 unavailable cases because the null-fault case reports its direct and sandbox modes separately.

## Executable contract inspected

The actual protocol, lifecycle documentation, main/configuration parsing, launch, native setup, signal handling, storage, platform implementations, and executable contract tests were read at `fe1b9e4e`.
The macOS release build and all **58 executable bootstrap contracts** passed at that revision with Rust 1.95.0 on `aarch64-apple-darwin`.
The Linux release runner and bubblewrap build passed on `x86_64-unknown-linux-gnu`.
The subsequent implementation and test changes at the final pin were read separately; their contract and validation are recorded at the end of this document.

Console uses `--config-env NAME -- COMMAND [ARG]...` and execs in place.
Only fixed policy and lifecycle choices enter the immutable JSON value; argv, cwd, environment, and fd 0/1/2 remain ordinary launch inputs.
The selected variable is consumed and removed from the target environment.
No configuration file, path handoff, pipe writer, or waiting Console adapter is introduced.
The separate inherited-descriptor interface remains covered by installation tests.
Console adds no application-level request-size cap; the 96 KiB environment, binary stdin, and long/multibyte source cases remain public constraints.
The fixed configuration consumes part of the native exec byte budget; exact maximum-size parity with the former handoff was not tested.

The new runner preserves the waitable root through retirement, retires owned-group members and observed detached descendants, orders cancellation before native setup writes, retains a partial setup channel through retirement, and reports Linux procfs prerequisite errors without a panic.
Linux socket syscall restrictions remain unchanged; the two-pipe sideband works within them.

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

The production diff removes 4,049 lines and adds 228, a net deletion of 3,821 lines (`git diff --numstat e37f8ab0 -- src`).
All rules in `src/sandbox/policy_extensions.sbpl` are unchanged; only the policy comment linking to its audit document is updated.
The integration retains current main's companion installation verification.
Filesystem host reads, network restriction, full mutation of private temporary data, host-terminal restrictions, and all named macOS compatibility exceptions remain selected by Console.
The runner now creates and owns `sandbox-XXXXXX/data`, exported as `TMPDIR`.
The former Console supervision and Linux ownership documents are replaced by [sandbox integration](SANDBOX.md); protocol and architecture documents retain the ordinary server and relay responsibilities.

The server's ordinary child integration also fixes a startup-cancellation race: the I/O join could SIGKILL the runner before the shutdown thread requested retirement.
The retained never-ready-worker regression failed with an observed, live detached child; native-call tracing recorded the server sending signal 9 directly to the runner.
The join now requests SIGTERM retirement, waits for the existing child grace period, and escalates only if that wait fails.
It does not inspect or manage descendants.

## Fixture and test inventory

Retained runtime transcripts and behavior assertions are unchanged except for the startup/prerequisite diagnostics and supervisor-loss cases listed below.
Historical transcript role labels such as “manager” remain where they now identify the runner at the original frontend PID.
No snapshot approves a surviving descendant, cancellation failure, changed runtime result, or lost output.

### Process and path observation

Paths in this table are relative to `tests/`.

| File or case                                                | Mechanism changed; behavior retained                                                                                                                                                                                                                                                                                             |
| ----------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `boundaries/cli/_harness.py`                                | Discover the native child directly; target/root and frontend/supervisor now share PIDs. Deduplicate identical exit watches, preserve the strict cleanup barrier, and remove the container around `data` during fixture teardown. The lifetime fixture now waits for exact detached-child discovery before triggering retirement. |
| `cli/sandbox/test_signals`                                  | Four lifetime cases declare the native-fixture capability for that discovery checkpoint. Signal disposition, status, streams, and cleanup assertions remain unchanged.                                                                                                                                                           |
| `cli/sandbox/test_supervision`                              | Three topology assertions use target PID = native root/group leader. Terminal, peer, signal, and cleanup behavior is unchanged.                                                                                                                                                                                                  |
| `client_server/_harness.py`                                 | Watch markers under `sandbox-*/data`; find the stopped macOS relay directly below the runner. Linux namespace-init ancestry assertions remain.                                                                                                                                                                                   |
| `client_server/lifecycle/test_startup`                      | Four no-storage-before-start assertions use runner directory names.                                                                                                                                                                                                                                                              |
| `client_server/recording/test_journal`                      | The no-storage-before-start assertion uses runner directory names.                                                                                                                                                                                                                                                               |
| `client_server/sandbox/test_crashes`                        | Discover the supervisor as the server's direct child, retaining exact identities and server-death cleanup. Fixture teardown removes the container around `data`.                                                                                                                                                                 |
| `client_server/sandbox/test_relay`                          | The explicit wrapper is now the native root; the relay remains its direct child.                                                                                                                                                                                                                                                 |
| `client_server/sandbox/test_replacement` and `fixtures/zod` | Allow the relay to be its group leader while keeping worker and relay distinct. Replacement, stream, group separation, and cleanup assertions remain.                                                                                                                                                                            |
| `client_server/sandbox/test_retirement`                     | Identify the supervisor independently of the relay group. Retain accepted relay shutdown, the five-second unresponsive-relay deadline, and worker/descendant exit barriers.                                                                                                                                                      |
| `cli/sandbox/test_crashes`                                  | Observe runner parent capture, native readiness, and setup writes. Before parent capture, assert no native root or storage. Caller-loss, target-not-run, and cancellation assertions remain.                                                                                                                                     |
| `client_server/sandbox/test_supervision`                    | Extract the existing exact-PID observation fixture into shared support; preserve its processx failure-replacement checkpoint and all transcripts.                                                                                                                                                                                |
| `support/sandbox_observation.py`                            | Reuse native discovery events before tests orphan detached children. Linux needs no discovery gate because the runner owns its PID namespace.                                                                                                                                                                                    |
| `native/runner_interposer.h`                                | Test-only exec interposition restores the observation library across production loader-variable removal; no helper executable or process is added.                                                                                                                                                                               |
| `native/manager_start_interposer.c`                         | Gate the runner's native-readiness receive instead of the deleted manager entry point.                                                                                                                                                                                                                                           |
| `native/setup_write_interposer.c`                           | Observe the runner's framed native setup send instead of Console's former pipe write.                                                                                                                                                                                                                                            |
| `native/root_waiter_start_interposer.c`                     | Gate runner parent capture instead of the deleted root-waiter startup.                                                                                                                                                                                                                                                           |
| `native/manager_observation_interposer.c`                   | Observe the runner's descendant registration instead of the deleted manager.                                                                                                                                                                                                                                                     |
| `architecture.py`                                           | Point forbidden-dependency mutations at the surviving frontend module; keep dependency-direction assertions.                                                                                                                                                                                                                     |

The CLI lifetime checkpoint applies to `pending_signal_at_root_exit_preserves_status`, `owned_sigterm_retires_the_sandbox_lifetime`, `owned_sigterm_retires_when_inherited_ignored`, and `owned_root_exit_waits_for_cleanup` in `cli/sandbox/test_signals`.
Their original assertions and snapshots are unchanged.

The shared discovery checkpoint is added to these six retained client-server cases, separately from the CLI lifetime fixture:

- `sandbox/test_replacement::restarts_after_worker_exit_with_partial_sideband`;
- `sandbox/test_retirement::restart_does_not_report_never_ready_worker_as_stopped`;
- `sandbox/test_shutdown::restart_cancels_partial_sideband_frame`;
- `sandbox/test_shutdown::restart_cancels_reader_after_operation_result`;
- `sandbox/test_shutdown::restart_drains_readable_frame_before_abandoning_partial_tail`; and
- `sandbox/test_shutdown::shutdown_cancels_partial_sideband_frame`.

The initial failures included live `Ss` children orphaned before discovery, confirmed by native-call traces.
Waiting for registration makes these fixtures exercise the supported observed-descendant guarantee deterministically; the children still detach, and the original cleanup assertions remain.
The never-ready case also exposed the Console SIGKILL race described above after discovery was confirmed.
Owned-group retirement does not depend on discovery; the runner's executable regression covers that separate guarantee.

### Accepted diagnostics and new coverage

| Public case                                                                           | Exact expectation change                                                                                                                                                 |
| ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `cli/sandbox/test_crashes::owner_loss_before_exit_watch_cleans_startup`               | Caller capture failure now emits `mcp-console-sandbox: parent_pid is not the current parent`. Target suppression and cleanup are unchanged.                              |
| `cli/sandbox/test_crashes::owner_loss_before_target_release_cancels_startup`          | Successful cancellation now emits empty stderr. Target suppression and cleanup are unchanged.                                                                            |
| `client_server/sandbox/test_startup::sandbox_setup_failure_is_reported_and_retryable` | Stderr now reads `mcp-console-sandbox: create private storage: Not a directory (os error 20)`. The MCP error, no-child assertion, and successful retry remain unchanged. |
| `cli/sandbox/test_linux::rejects_inherited_procfs_before_running_command`             | Stderr now reads `native target setup requires namespace-local procfs`. Exit 1 and target-not-run assertions remain unchanged.                                           |

The new `cli/sandbox/test_execution::frontend_exec_preserves_pid_and_standard_streams` case verifies the actual private executable at the original frontend PID, consumed configuration, all 256 byte values through stdin/stdout/stderr, and target status 23.
It failed against the original baseline because that PID still named the Console frontend.
Only runner-generated snapshots are accepted; formatting-only regeneration is checked for equal parsed values and restored to baseline bytes.

## Changed guarantees and removed cases

Independent recovery after supervisor death is intentionally removed.
The final pin supplies Linux workload termination after native readiness through native parent-death links, but not directory removal, terminal restoration, or complete native-startup coverage after supervisor death.
macOS still has no descendant-termination guarantee after supervisor death, and neither platform recovers from a stopped or hung supervisor.
Surviving processes retain native sandbox policy.
Configured caller death while the runner lives remains required, including startup cancellation and cleanup of observed detached descendants.

These two cases and their snapshots are removed because their assertions require independent cleanup after supervisor death:

- `cli/sandbox/test_crashes::launcher_crash_retires_the_sandbox_lifetime`;
- `cli/sandbox/test_crashes::manager_crash_retires_the_sandbox_lifetime`.

Three MCP cases also covered supported application behavior and are retained:

| Case                                                                                                                                      | Retained assertions and explicit changes                                                                                                                                                                                                                                                                                                                                                                                                   |
| ----------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `client_server/sandbox/test_crashes::manager_crash_retires_the_worker_generation`                                                         | Kill the actual runner, require a failure within the original five-second deadline, then run a replacement with the unchanged `replacement ready` transcript. Keep private-directory retention after the failure. Remove only the descendant cleanup assertion after supervisor death. The failure now includes `worker launcher terminated by signal 9`; it does not claim that native workers stopped.                                   |
| `client_server/sandbox/test_startup::manager_failure_before_readiness_keeps_custom_relay_gated`                                           | Keep target nonexecution, gated-root/runner exit, the exact MCP failure, successful retry, and the marker proving only the replacement executed. Observe the server's direct runner and its native child at the existing readiness checkpoint. Only stderr changes to `mcp-console-sandbox: failed to fill whole buffer`. Restore `fixtures/startup_marker_relay`. The native child exits on setup-channel EOF without a recovery monitor. |
| `client_server/sandbox/test_retirement::restart_waits_for_owned_launcher_manager_recovery` → `restart_reports_stalled_sandbox_supervisor` | Keep the stopped relay, active evaluation, stopped supervisor, ten-second response deadline, server survival, and error response. Require the server to reap its direct launcher. Replace the former status-1 diagnostic with the six-second launcher timeout and signal-9 diagnostic. Remove the descendant cleanup barrier after disabling the sole supervisor; teardown explicitly owns survivors.                                      |

Linux `cli/sandbox/test_linux::retires_descendants_after_exit_and_supervisor_loss` retains its original case and snapshot path at the final pin.
Command exit (23), owned SIGTERM (0), and caller death retain the same descendant and private-directory cleanup assertions and transcript records.
The launcher-crash scenario is restored: all observed native processes and detached descendants must exit within the original deadline after runner SIGKILL, with the original status -9 and empty stderr.
Its sole changed expectation is retained private storage (`temporary_directory_removed: false`); the fixture removes that storage after the assertion.
The former manager-crash injection now addresses the same PID as launcher crash, so it has no separate scenario.
The stopped-manager recovery scenario remains removed because a stopped sole supervisor cannot perform retirement.
The three always-retained scenario records compare equal to their main snapshots after parsing; the launcher-crash record differs only in the explicit storage guarantee.
Unused recovery-only helpers are deleted.
The CLI startup-cancellation case now includes the reader scenario, cancellation, exit code, stdout, and stderr in a failure diagnostic; its assertions and five transcript records are unchanged.
No caller-death, isolation, cancellation, stdin, signal, restart, or retained descendant-cleanup case is skipped to obtain a passing result.

Other accepted boundaries are runner-owned temporary-path shape, strict directory-removal errors instead of best-effort deletion, storage retention when retirement is unproven, and native startup diagnostic wording.
On Linux, the writable `data` child can replace itself and its metadata entries; the old bind-mount root could not.
Directory-removal failure and expanded Linux directory mutation were not separately compared against the baseline.
The pre-existing Darwin limits for unobserved detached orphans and non-atomic identity-check-and-signal delivery remain documented, without an added recovery service.

## Runner-follow-up validation

These local results precede the latest-main refresh and were recorded at PR head `5e4a5ccc85c304feeeedff119b622aa1412a6ca3`.

That earlier suite discovered 419 public cases, compared with 423 on updated main: one frontend-exec case was added and five cases were removed.
The later audit restores the three MCP cases listed above, bringing discovery to 422 cases; only the two CLI recovery cases remain removed.
At that earlier head, the Linux lifetime case was renamed without changing its three retained scenarios; the final pin restores its original name and launcher-crash process assertions.

| Scope                                          | Result                                                                                                                                                                                   |
| ---------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| macOS 26.6.2 arm64                             | `scripts/check` passed: formatting, extracted runtime sources, Clippy, 47 Rust tests, and all 409 applicable public transcript cases; 10 cases unavailable (11 mode-level skip records). |
| macOS installed public/private executable pair | Five passed, two Linux-only skips.                                                                                                                                                       |
| Pinned runner executable contracts, macOS      | All 58 passed in the release build.                                                                                                                                                      |
| Linux core checks                              | Passed, including 47 Rust tests.                                                                                                                                                         |
| Linux public transcripts                       | Per-case inventory: all 383 applicable host cases passed; 35 cases unavailable (37 mode-level skip records). Two aggregate attempts failed as detailed below.                            |
| Linux nested procfs prerequisite               | The unchanged public case passed in an Ubuntu 24.04 container with namespace prerequisites enabled.                                                                                      |
| Linux installed public/private executable pair | All seven passed in the same container environment.                                                                                                                                      |

The Linux development host runs kernel 6.8.0-139-generic on x86_64 and restricts user namespaces with AppArmor.
Its transcript run uses a test-only PATH wrapper to execute the exact built bubblewrap under the host's existing `bwrap` profile.
The nested-procfs fixture needs a second namespace; it runs separately in an Ubuntu container with `SYS_ADMIN`, unconfined AppArmor/seccomp, and unmasked proc paths.
No host profile or sysctl is changed.

The first Linux aggregate attempt passed core checks but stopped in the direct, unsandboxed custom-worker requirements case: the host `.Rprofile` loaded `envir` from libraries deliberately excluded by that fixture.
That case passed with `R_PROFILE_USER=/dev/null`; the final transcript run uses that environment setting without changing production code, fixtures, or expectations.

The next aggregate attempt passed 321 cases before `client_server/sql/test_catalog::interrupts_running_sql_query` failed in sandbox mode: after its 30-second interrupt wait it still returned `\n[running; poll with an empty send]`.
The unchanged case then passed a focused run and all 12 repeated trials; the same case also passed all 12 comparison trials on main `26a5f3c3` with its original runner pin.
Those trials each cover direct and sandbox modes, with six trials running concurrently.
The fixture's file marker precedes the blocking SQL statement, so it does not establish entry into that statement; this is a possible timing window, not an isolated root cause.
The failure remains unexplained, and neither its assertion nor snapshot is changed.
The further aggregate run used the same 12-job default and passed 175 cases before `client_server/r/test_lifecycle::restart_skips_direct_stdin_boundary_callback` failed in sandbox mode: the waiting response lacked `direct callback released`.
That unchanged case passed focused execution in both modes on the integration and on main.
This second failure also remains unexplained.
To finish the inventory without stopping at the first failure, a temporary driver invokes each unchanged public selector, with 12 cases concurrently and the existing case deadlines.
It changes no repository harness, test, execution mode, assertion, or snapshot.
The per-case inventory is reported separately from the failed aggregate attempts.
All 383 applicable host cases passed in that inventory, including both cases that failed in aggregate runs.
Together with the separately passing procfs case, this covers all 384 applicable Linux cases.
This does not establish a passing aggregate run or resolve the two intermittent failures.

The snapshot inventory at that earlier head contains 427 files: 421 are byte-identical to current main, four have the listed diagnostic changes, six old paths are removed, and two paths are added (the frontend-exec case and the renamed Linux lifetime case).
Formatting-only regeneration is restored to baseline bytes after checking parsed equality.
The two-pipe sideband, isolation, cancellation, stdin, signals, restart, caller-death cleanup, and observed-descendant barriers retain their assertions.

At that local validation cutoff, hosted PR CI had not completed.
A full local Linux baseline, Linux runner executable-contract tests, wheel rehearsal, and R-package acceptance were not performed.
The full Linux suite's host prerequisites and separate container result must not be reported as an ordinary hosted-CI pass.

## Hosted CI diagnosis

[Run 34405825302](https://github.com/t-kalinowski/mcp-console/actions/runs/34405825302), for head `5e4a5ccc`, completed on September 9, 2026.
Ubuntu passed, including its aggregate transcript run.
That hosted job also passed wheel smoke and R-package checks.
The macOS job failed in `cli/sandbox/test_crashes::cancels_owned_launch_during_setup`, at the assertion checking the fixture owner's exit code.
The assertion did not include the scenario or actual status in its diagnostic, so the hosted log alone does not identify which cancellation path failed.

The apparent stall was successful work before the result: the macOS private-runner build took 8 minutes 44 seconds, then its transcript step failed after about 10 seconds.
Ubuntu's passing transcript step took 17 minutes 39 seconds; its longest reported case, `client_server/output/test_spools::reports_omitted_bytes_retained_at_the_file_limit`, took 5 minutes 27 seconds.
The logs contain no timeout or deadlock for the failed macOS case.
The latest-main merge leaves that case, its assertions, and the runner implementation pin unchanged.

The unchanged five-scenario selector passed all 13 focused local trials against an isolated copy of the merged release executable and its matching companion bundle: 65 scenarios, about three seconds per trial.
These trials used a temporary Python trace for failure diagnostics without modifying the case or its assertions; no failure was reproduced.
The hosted failure therefore remains unexplained.
The next useful observation is the active reader/cancellation scenario, actual owner status, and captured stdout/stderr from a failing hosted invocation.
There is no evidence here to justify changing cancellation behavior or weakening its test.

## Latest-main validation

The full local `scripts/check` passed on macOS after merge `0f271003`, using the existing isolated runner checkout at the unchanged pin.
It passed all core checks and 47 Rust tests, all 409 applicable public transcript cases against the release executable, and both source/wheel installation tests.
Ten public cases were unavailable on this host, producing 11 mode-level skip records.
The installation checks cover native-flag rebuilds, editable and ordinary source installations, source archives, wheel smoke, relocation, and companion verification across three installed bundles.
All 427 snapshot files remain byte-identical to the preceding PR head.
The merge adds no integration-specific test or guarantee change beyond the inventory above.

Logs and the revision/snapshot audit are under `/tmp/mcp-console-pr266-main-refresh`; the focused CI investigation is under `/tmp/mcp-console-pr266-ci-diagnosis`.
No new local Linux run, runner executable-contract run, release wheel rehearsal, or R-package acceptance run was performed for this refresh.
Hosted checks for the new PR head remain separate from the completed preceding-head results above.

## Restored MCP acceptance coverage

The audit started from PR head `90b7399e471e87b5d47fe086f9c14752128a7a6d`, with the complete local check recorded above and the hosted failures below.
Snapshot hashes, restored baseline case sources, the runner pin, and the current hosted failures were saved before edits under `/tmp/mcp-console-pr266-restored-cases`.
The restored crash test first failed because the server continued using a generation after its launcher was killed; the surviving relay retained stdout.
After connecting child exit to the reader, the test exposed a second failure: the session remained permanently `shutting down` after reporting the reaped launcher.

The existing direct-child exit observer now wakes the relay reader.
The reader drains the bytes already queued when it observes launcher exit, then closes its generation even if an unsupervised descendant retains the pipe.
The server joins that reader and its event dispatcher before replacement; this preserves buffered output and prevents the old generation from publishing afterward.
A reaped launcher permits logical replacement while its cleanup error remains visible.
This adds no process-tree observation, signal propagation, native cleanup, or supervisor process to Console.

The restored startup case retained every behavioral assertion and differed only in its native diagnostic.
The stalled-supervisor case retained bounded MCP failure and direct-child reaping; independent descendant cleanup after the supervisor is disabled remains explicitly unsupported.
At `fe1b9e4e`, only these three restored snapshots were generated for the new expectations, and all 427 pre-existing snapshot files remained byte-identical to the audit baseline.
That stage had 430 snapshot files: against main, 421 were byte-identical, six had the documented diagnostic changes, four old paths were removed, and three paths were added (including the two renamed cases).
The final pin's Linux scenario restoration is recorded separately below.

The hosted audit baseline at `90b7399e` failed on both platforms in [run 34410478115](https://github.com/t-kalinowski/mcp-console/actions/runs/34410478115).
macOS repeated the startup-cancellation exit-code failure; Linux failed `server_relay/requirements/test_resolution::explicit_r_preparation_owns_environment_before_host_resolution` with a relay-stdout closure and launcher status 1.
These are recorded separately from the deterministic restored-test failures above.
The macOS fixture now reports its exact cancellation scenario, status, stdout, and stderr on a status mismatch; its expected behavior and snapshot are unchanged.

| Audit validation at `fe1b9e4e`      | Result                                                                                                                                                                                                                                                                                                         |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| macOS 26.6.2 arm64                  | `scripts/check` passed: core checks, 47 Rust tests, all 412 applicable transcript cases, and both source/wheel installation tests. Ten cases are unavailable (11 mode-level skip records).                                                                                                                     |
| Final Rust and focused macOS checks | Clippy and all 47 Rust tests passed again. All 18 selected transcript cases passed, including the three restored cases, the retained storage assertion, the complete retirement suite, and the five-scenario startup-cancellation case.                                                                        |
| Linux x86_64                        | Core checks, all 47 Rust tests, and the release build passed. All 16 applicable selected transcript cases passed; four macOS-only cases were unavailable. Selection covers launcher retirement/output draining, descriptor inheritance, restart, and the explicit R preparation case that failed in hosted CI. |
| Snapshot audit                      | All 427 pre-audit snapshot files remain byte-identical. The restored startup marker executable also matches main byte-for-byte.                                                                                                                                                                                |

Linux uses the same host prerequisites as the preceding validation: the exact staged helper under the existing AppArmor profile and `R_PROFILE_USER=/dev/null`.
The full Linux transcript suite, Linux installation checks, native runner contracts, separate release rehearsal, and R-package checks were not rerun at that stage.
The full macOS check and focused Linux pass do not explain or resolve the preceding hosted intermittent failures; fresh hosted results remain separate.
One initial aggregate macOS attempt was interrupted while the deterministic restored-test failures were still being fixed; the final aggregate above completed successfully.

## Final runner pin update

After the restoration passed at `fe1b9e4e`, implementation commit `b5a1c9f76a9c6ca2909105aecbc78555a26fda01` was published and pinned.
Its launch parsing and protocol are unchanged.
The launch/native implementation, lifecycle contract, and changed executable fixtures and assertions were read before using it.
The original runner repository remains unmodified by this task.

Owned launches with nonterminal stdin and stdout now place the runner in its own process group, including when stderr is a terminal.
This preserves the original PID, parent, session, and standard-stream descriptions while allowing the runner to clean up after caller-group SIGKILL.
Interactive input/output and unowned launches retain their group behavior; explicit signals to the runner remain supported.
Linux adds parent-death links through the native chain and re-arms namespace-init termination before readiness.
The native helper restores signal delivery needed by its death links; the final target still receives the caller's original signal state.
Post-readiness runner death terminates the tested Linux native chain and detached workload descendants.
Storage deletion after runner death, macOS descendant termination after runner death, and retirement throughout earlier bubblewrap fork/credential startup windows remain unsupported.
The target-release gate still prevents execution of an unreleased target after runner death.

This pin update introduces no Console process layer or fixture-topology change.
It restores the Linux lifetime case's original name and launcher-crash process assertions, using the existing native-child traversal and pidfd exit barriers.
The snapshot is generated only for the explicit storage-retention change and restored scenario; the other three records are unchanged.
It does not restore independent directory cleanup or stopped-supervisor recovery.

The final snapshot inventory contains 430 files: 421 are byte-identical to main, six contain the documented diagnostic changes, and the Linux lifetime snapshot contains the explicit scenario and storage changes.
Three old paths are removed (the two recovery-only CLI cases and the renamed stalled-supervisor case); two new paths are added (the frontend-exec case and the renamed stalled-supervisor case).
Against the pre-audit PR head, 426 snapshot files remain byte-identical; the remaining Linux file returns to its original name with its three existing records unchanged, and the three MCP snapshots are restored.


| Validation at `b5a1c9f76`   | Result                                                                                                                                                                                                                                                                                                       |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| macOS 26.6.2 arm64          | Full `scripts/check` passed: core checks, 47 Rust tests, all 412 applicable public transcript cases, and both source/wheel installation tests. Ten cases were unavailable (11 mode-level skip records).                                                                                                      |
| Linux x86_64                | Release build and all 19 selected public transcript cases passed, with four macOS-only cases unavailable. Selection includes the restored Linux supervisor-loss scenario, isolation, signals, caller death, retirement, descriptor inheritance, restart, and the explicit R preparation case from hosted CI. |
| Runner executable contracts | Release suites passed all 61 tests on macOS and all 62 on Linux, including owned process groups, caller-group death, Linux supervisor death, inherited signals, standard streams, and gated startup.                                                                                                         |
| Snapshot comparison         | 426 pre-audit files are byte-identical; all three existing Linux records are unchanged. The restored launcher-crash record differs from main only in private-storage retention. All 422 public cases are discovered.                                                                                         |

The Linux Console runs use the exact staged helper through the existing AppArmor profile.
The Linux runner contracts use a compiled test-only trampoline that execs `aa-exec -p bwrap -- <exact staged helper>`; it accommodates helper copying in those fixtures without changing the shipped helper or host policy.
An attempted old-executable negative comparison failed installation verification before target launch, so it supplies no runner behavior result.
The restored Linux case then passed both snapshot generation and a separate check against the final pin.
Logs and inventories are under `/tmp/mcp-console-pr266-restored-cases`.

The full Linux Console suite, nested-procfs container case, Linux installed-pair checks, upstream native suites, debug runner suites, separate release rehearsal, and R-package checks were not rerun at this final pin.
The final macOS aggregate ran before the Linux-only scenario was restored; that case is unavailable on macOS and was validated on Linux.
At this validation cutoff, the preceding hosted failures remained unexplained; the subsequent evidence and fixture corrections follow below.

## CI fixture corrections

[Run 34416068195](https://github.com/t-kalinowski/mcp-console/actions/runs/34416068195) at `ded5da6b` completed with Ubuntu passing and macOS failing.
The added status diagnostic identifies the macOS failure as the writable-pipe scenario without cancellation: status 134, empty stdout, and a dyld error loading `setup_write_interposer.dylib` into the arm64e target from an arm64 library.
The preceding cancellation scenarios passed.
The test library was reinjected at the frontend exec boundary and then copied into the target environment by the runner.
Local System Integrity Protection is enabled, and the unchanged `/bin/echo` case did not reproduce that loader error locally.
A separate public sandbox invocation with a Python target did reproduce the leaked loader variable before the fix and passed after it.

The earlier Linux failure in [run 34410478115](https://github.com/t-kalinowski/mcp-console/actions/runs/34410478115) came from an unordered fixture exchange.
Receiving MCP `[prepared]` does not establish that the server has processed the scripted relay's subsequent runtime R callback on its separate stream.
A controlled short write between `r_prepared` and `resolve_r` reproduced the exact CI failure: the relay expected `r_resolved`, received `evaluate`, and exited with status 1.
The server then reported relay-stdout closure and unsuccessful launcher retirement.

| Changed fixture or case                                                                                     | Change and preserved behavior                                                                                                                                                                                                                                                                                                                                                                                     |
| ----------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `server_relay/requirements/test_resolution::explicit_r_preparation_owns_environment_before_host_resolution` | Wait for the existing callback-reply checkpoint before submitting the next cell. `scripted_relay.py` sends a second token on that checkpoint after `r_resolved` and `r_activated`. Keep the busy-callback rejection, explicit-environment ownership, resolver-call count, `[prepared]`, `[done]`, and exact wire transcript assertions.                                                                           |
| `fixtures/native/runner_interposer.h`                                                                       | Remove `DYLD_INSERT_LIBRARIES` in the runner's library constructor, after the interposer has loaded and before target-environment capture. Keep reinjection at the frontend exec boundary. This shared fix applies to startup, parent-capture, setup-write, and descendant-observation fixtures; their process checkpoints, target commands, cancellation, status, streams, and cleanup assertions are unchanged. |

The new callback wait first failed deterministically because its second checkpoint token was absent, then passed with the matching fixture notification.
These corrections change no production code, runner pin, supported guarantee, transcript expectation, or snapshot file.

After the corrections, macOS core checks, all 47 Rust tests, and all 412 applicable public transcript cases passed; ten cases remain unavailable (11 mode-level skip records).
The installation step initially failed because the local invocation supplied a relative `MCP_CONSOLE_SANDBOX_SOURCE`, which resolved inside the installation fixture's temporary source copy.
Rerunning `python3 tests/install.py` with the absolute checkout path passed both installation tests.
The requirements-resolution and restart suites passed all 14 cases on both macOS and Linux with six concurrent cases, preserving both applicable execution modes and all snapshots.
All 430 snapshot files remain byte-identical to `ded5da6b`, and all 73 existing assertions in the edited requirements suite are unchanged.
The full Linux suite, runner executable suites, separate release rehearsal, and R-package acceptance were not rerun for these fixture-only corrections.
Fresh hosted CI is not awaited; the completed hosted results above apply to their recorded heads.
Logs, the controlled reproductions, and the assertion/snapshot audit are under `/tmp/mcp-console-pr266-ci-fix`.
