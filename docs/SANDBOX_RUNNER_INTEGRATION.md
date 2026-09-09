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

The final pin is the exact implementation commit `fe1b9e4e89458ba8812bfb0a65fb0a1ad84e71d5` on `mcp-console/sandbox-runner/rust-v0.150.1`.
The inspected branch tip `64a207d82525e6a22843971e8c64c4a02e358583` adds handoff documentation only.
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

The actual protocol, lifecycle documentation, main/configuration parsing, launch, native setup, signal handling, storage, platform implementations, and executable contract tests were read at the pin.
The macOS release build and all **58 executable bootstrap contracts** passed with Rust 1.95.0 on `aarch64-apple-darwin`.
The Linux release runner and bubblewrap build passed on `x86_64-unknown-linux-gnu`.

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

The production diff removes 4,028 lines and adds 85, a net deletion of 3,943 lines (`git diff --numstat 26a5f3c3 -- src`).
All rules in `src/sandbox/policy_extensions.sbpl` and installation verification are unchanged; only the policy comment linking to its audit document is updated.
Filesystem host reads, network restriction, full mutation of private temporary data, host-terminal restrictions, and all named macOS compatibility exceptions remain selected by Console.
The runner now creates and owns `sandbox-XXXXXX/data`, exported as `TMPDIR`.
The former Console supervision and Linux ownership documents are replaced by [sandbox integration](SANDBOX.md); protocol and architecture documents retain the ordinary server and relay responsibilities.

The server's ordinary child integration also fixes a startup-cancellation race: the I/O join could SIGKILL the runner before the shutdown thread requested retirement.
The retained never-ready-worker regression failed with an observed, live detached child; native-call tracing recorded the server sending signal 9 directly to the runner.
The join now requests SIGTERM retirement, waits for the existing child grace period, and escalates only if that wait fails.
It does not inspect or manage descendants.

## Fixture and test inventory

Retained runtime transcripts and behavior assertions are unchanged except for the three macOS startup diagnostics and one Linux prerequisite diagnostic listed below.
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
SIGKILL, supervisor crash, or an unresponsive supervisor no longer guarantees descendant cleanup, directory removal, or terminal restoration.
Surviving processes retain native sandbox policy.
Configured caller death while the runner lives remains required, including startup cancellation and cleanup of observed detached descendants.

These five recovery-only cases and their snapshots are removed:

- `cli/sandbox/test_crashes::launcher_crash_retires_the_sandbox_lifetime`;
- `cli/sandbox/test_crashes::manager_crash_retires_the_sandbox_lifetime`;
- `client_server/sandbox/test_crashes::manager_crash_retires_the_worker_generation`;
- `client_server/sandbox/test_startup::manager_failure_before_readiness_keeps_custom_relay_gated`; and
- `client_server/sandbox/test_retirement::restart_waits_for_owned_launcher_manager_recovery`.

Linux `cli/sandbox/test_linux::retires_descendants_after_exit_and_supervisor_loss` becomes `retires_descendants_after_exit_and_caller_loss`.
Only its launcher-crash, manager-crash, and stopped-manager scenarios are removed.
Command exit (23), owned SIGTERM (0), and caller death remain, with the same descendant and private-directory cleanup assertions.
The three retained scenario records compare equal to their main snapshots after parsing.
Unused recovery-only helpers and `fixtures/startup_marker_relay` are deleted.
No caller-death, isolation, cancellation, stdin, signal, restart, or retained descendant-cleanup case is skipped to obtain a passing result.

Other accepted boundaries are runner-owned temporary-path shape, strict directory-removal errors instead of best-effort deletion, storage retention when retirement is unproven, and native startup diagnostic wording.
On Linux, the writable `data` child can replace itself and its metadata entries; the old bind-mount root could not.
Directory-removal failure and expanded Linux directory mutation were not separately compared against the baseline.
The pre-existing Darwin limits for unobserved detached orphans and non-atomic identity-check-and-signal delivery remain documented, without an added recovery service.

## Final validation

The final suite discovers 419 public cases, compared with 423 on updated main: one frontend-exec case is added and five recovery-only cases are removed.
The Linux lifetime case is renamed without changing its three retained scenarios.

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

The final snapshot inventory contains 427 files: 421 are byte-identical to current main, four have the listed diagnostic changes, six old paths are removed, and two paths are added (the frontend-exec case and the renamed Linux lifetime case).
Formatting-only regeneration is restored to baseline bytes after checking parsed equality.
The two-pipe sideband, isolation, cancellation, stdin, signals, restart, caller-death cleanup, and observed-descendant barriers retain their assertions.

Not performed for this revision: hosted PR CI, a full local Linux baseline, Linux runner executable-contract tests, wheel rehearsal, and R-package acceptance.
The full Linux suite's host prerequisites and separate container result must not be reported as an ordinary hosted-CI pass.
