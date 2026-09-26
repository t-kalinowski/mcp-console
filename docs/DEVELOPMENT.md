# Development workflow

Run development commands from the repository root.
Use `scripts/test SELECTOR` for the red/green loop and `scripts/check` as the ordinary final local gate.
Fast validation is the default; `--quick` remains an alias for it.
Use `scripts/check --full` only when explicitly requested or when the changed area warrants exhaustive local validation, such as release preparation or changes spanning runtime, lifecycle, and packaging boundaries.
CI explicitly runs full core checks, all capability-applicable transcripts, and installation checks as the comprehensive merge gate.
Report the commands and scope actually validated; a default pass is not exhaustive validation.

| Command                                    | Scope                                                                                               |
| ------------------------------------------ | --------------------------------------------------------------------------------------------------- |
| `scripts/check` or `scripts/check --quick` | Companion staging, focused core checks, release build, and the smoke transcript profile             |
| `scripts/check --full`                     | Companion staging, full core checks, all capability-applicable transcripts, and installation checks |
| `scripts/test` or `scripts/test --quick`   | The explicit smoke selection in [`tests/boundaries/_profiles.py`](../tests/boundaries/_profiles.py) |
| `scripts/test --full`                      | All capability-applicable transcript cases and execution modes                                      |
| `scripts/test SELECTOR`                    | The requested case or suite in its declared execution modes, including with `--quick` or `--full`   |

The smoke profile selects existing cases and snapshots by exact case name.
It covers MCP and CLI admission, real R/Python/SQL execution, persistent and mixed runtime state, interactive input, interruption, recording with an image, and native sandbox policy.
It omits remote providers, installation, stress, and broad environment matrices by selection, without changing their assertions or capability requirements.
Use `scripts/test --list` to inspect the smoke selection and `scripts/test --full --list` to discover the whole suite.
Only `--full` without a case, suite, or `--locate` selector audits orphan snapshots globally; `scripts/test --full --update` can remove them after a successful complete update.
Smoke and focused updates preserve unselected snapshots.

Default core checks validate extracted runtime sources, architecture, Rust formatting, Clippy, and Rust tests.
`scripts/check-core --full` additionally runs release, staging, transcript-runner, workflow, formatter, development-tool, test-client, and architecture-checker self-tests; `scripts/check-core` and its `--quick` alias run the default core selection.
When changing repository tooling, run its owning test directly, for example `tests/transcript_runner.py`, `python3 tests/workflow.py`, or `python3 tests/staging.py`.
Installation checks remain last in the full gate because they replace and hide the shared `target` directory.

## Resume from a small checkpoint

For work that spans validation, review, or context changes, initialize a [task checkpoint](templates/task-checkpoint.md) and replace its placeholders:

```sh
mkdir -p .dev-workflow
cp docs/templates/task-checkpoint.md .dev-workflow/task.md
```

Use this copy command only when starting a new checkpoint.
This file is ignored by Git; keep task progress there and durable recipes in the owning documentation.
Record the task objective and scope, or link to an issue or PR that supplies them.
The checkpoint is a handoff note, not a saved worktree; use `not recorded` for facts that are unavailable.
Update it after a meaningful validation or review result and before handing off work.
When changing branches or stack layers, update the branch, intended base, working revision and state, and next action together.

Resume with a bounded sequence:

1. Read the checkpoint and `git status --short --branch`.
   Compare `git rev-parse HEAD` with the saved working revision, and resolve the saved base with `git rev-parse 'BASE^{commit}'` (replace `BASE` with the saved reference).
   If Git metadata is unavailable or the saved branch, revisions, or state differ, reconstruct the checkpoint from the task and current checkout before relying on it.
   If either the saved or current worktree is dirty, inspect `git diff`, `git diff --cached`, and the untracked files listed by status, then refresh the checkpoint and next action from those edits.
   Matching `dirty` labels do not establish that the edits are the same.
2. Read the relevant development route and owning contract, then use scoped `rg -n` searches or `scripts/test --locate SELECTOR` to find the implementation and public case.
3. Follow the recorded next action.
   After failed validation, read the relevant phase log around the failure before rerunning the focused command.
   For an unfinished run, read the latest completion record and return to its original terminal or task using the handle saved with the evidence.
   The record has no process identity, and the lock diagnostic may be stale; neither identifies a live run.
   If the run remains unfinished and its execution context is unavailable, record the blocker until its owner can establish that the run and its children have stopped.
   After passed validation or a review-only handoff, continue the recorded work without inventing a failed check to rerun.
   Expand the search or log range when the evidence requires it.

Record the runnable repository command, including its arguments and relevant environment overrides.
For `scripts/check`, `scripts/check-core`, and `scripts/test`, the completion record's `command` array starts with the workflow mode: prefix that first element with `scripts/` and preserve and shell-quote the remaining arguments.
For example, `["test", "cli/test_config_overrides"]` means `scripts/test cli/test_config_overrides`.
Copy the recorded revision, worktree status at admission, result, exact failing selectors, and log paths when a completion record exists.
For commands without a record, such as `scripts/format` or `python3 tests/workflow.py`, capture the command, revision and worktree status before starting, then retain the observed result and a log or terminal reference manually.
Keep missing metadata as `not recorded`, including null revision or worktree status in a completion record; do not fill it from the later checkout state.
A passing focused command does not establish a passing full gate.
A result for an earlier revision, any dirty tree, or an unidentified checkout is historical evidence; rerun the required validation on the current clean revision before claiming it passed there.
The recorded `clean` or `dirty` label and porcelain status do not identify the contents of uncommitted changes.
A null completion status means unfinished; it does not prove the process is still running.
Keep the checkpoint short by linking evidence instead of copying output.

Record the stopping condition from the user's request, such as review completion or handoff, local validation, push and PR publication, or hosted CI completion, including any requested review follow-up.
Honor requests to push and return.
Waiting for hosted CI is an explicit part of the task only when requested; do not start a watcher by default.
Report the hosted state actually observed and distinguish it from local validation.

## Inspect local preparation

Run `scripts/preflight` for a local inventory, or `scripts/preflight --json` to retain structured output.
It uses the project's required Python 3.11 or later.
It reports the checkout and revision, the release executable used by transcript tests and any separate executable on `PATH`, the companion pin and staged revision, Python/R selections, Cargo and Rustup metadata, the companion toolchain, and cache paths.
Required command probes report missing or failing tools separately from optional Docker, SBX, external SSH, and host-test capability skips.
Optional skips do not fail the command; missing or failing required tools, including timed-out version probes, give exit status 1.
Metadata command failures, such as a missing active Rustup toolchain or invalid uv configuration, are retained in `probe_errors` and also give exit status 1 while preserving the remaining inventory.
An independently installed Cargo need not use Rustup's active toolchain.

The report reuses `scripts/stage-sandbox-runner --describe` and the existing test capability probes.
It does not clone, resolve dependencies, build, install, or provision services.
Configured provider probes may contact their existing daemon or SSH host; SBX discovery uses its normal serialization lock.
These probes retain the test harness's behavior: negative availability is a skip, while exceptional probe failures remain errors.
Artifact presence, the staged target, and the recorded revision are inventory facts.
Exit status 0 means the required command probes succeeded; it does not certify build readiness.
Staging and Cargo validate the target and build inputs, while runtime verification and public tests check the resulting bundle.
Malformed existing staging records are errors; inspect the generated record before removing it and rerunning staging.
The command inventory covers Git, uv, Cargo, Rustup, R, and Rscript; operating-system build dependencies and SDK setup remain in [RELEASE.md](../RELEASE.md).

To prepare a checkout for direct development, run:

```sh
scripts/stage-sandbox-runner
scripts/with-checkout cargo build --release --target-dir target
```

For a source installation, use `scripts/with-checkout uv tool install --reinstall .`; its packaging backend stages and builds the companion and application.
The companion's selected source checkout owns its Rust toolchain; Console uses the active toolchain in this checkout.
The pinned companion source and Cargo output are shared across worktrees under `${XDG_CACHE_HOME:-$HOME/.cache}/mcp-console/sandbox/<repository>/<commit>/source`.
Staging still invokes Cargo to check build inputs, including changed compiler flags.
Use `MCP_CONSOLE_SANDBOX_SOURCE` for an explicit clean checkout at the pin.
Each Console checkout keeps its own application Cargo output and wheel staging as described under [checkout ownership](#checkout-ownership).

## Find the public test

Use `scripts/test --full --list` to discover selectors and `scripts/test --locate SELECTOR` to find source lines and the primary snapshot before building.
These routes are starting points; read the relevant contract and case before changing behavior.

| Task                   | Owning source                                                  | Public check                                                                  | Selected snapshot update                                                                                    |
| ---------------------- | -------------------------------------------------------------- | ----------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Output previews        | `src/worker_client/output/`                                    | `scripts/test client_server/output/test_previews`                             | `scripts/test --update client_server/output/test_previews`                                                  |
| Delivery recovery      | `src/worker_client/output/`, `src/server_transport.rs`         | `scripts/test client_server/output/test_recovery`                             | `scripts/test --update client_server/output/test_recovery`                                                  |
| Configuration layering | `src/config.rs`, `src/config/`                                 | `scripts/test cli/test_config_overrides`                                      | `scripts/test --update cli/test_config_overrides`                                                           |
| Console file locations | `src/console_paths.rs`, `src/settings.rs`, `src/transcript.rs` | `scripts/test cli/test_config_overrides client_server/recording/test_journal` | `scripts/test --update cli/test_config_overrides client_server/recording/test_journal`                      |
| Fixture serialization  | `tests/support/snapshots.py`, `tests/transcript_runner.py`     | `tests/transcript_runner.py`                                                  | For handshake changes: `scripts/test --update client_server/server/test_tools::initializes_and_lists_tools` |
| Validation ownership   | `checkout_workflow.py`, `build_backend.py`                     | `python3 tests/workflow.py`                                                   | No transcript snapshots                                                                                     |

For MCP admission changes, use the public server with a custom worker and resolver sentinels:

```sh
scripts/test \
  client_server/server/test_tools::validates_send_arguments \
  client_server/server/test_tools::validates_standalone_requirement_arguments \
  client_server/server/test_tools::invalid_send_has_no_external_effects \
  client_server/server/test_tools::bounds_argument_decoding_errors
```

These checks reject malformed requests before startup and with a live worker without preparing a package environment.
Rejected requests contain real R, Python, and SQL source; the live-worker probes execute Python cells in the fixture and record every evaluation.
Resolver capability discovery is distinct from dependency preparation.
For preparation/lifecycle changes, also run the real-runtime sequence and the existing causal startup and custom-worker restart cases:

```sh
scripts/test \
  client_server/requirements/test_r::prepares_with_empty_stdin_then_restarts \
  client_server/requirements/test_custom_workers::standalone_preparation_before_worker_startup_is_causal_and_idempotent \
  client_server/requirements/test_custom_workers::custom_worker_restart_prepares_r_and_duckdb_requirements
```

`prepares_with_empty_stdin_then_restarts` owns the successful preparation, repeated preparation with empty stdin, and preparation-plus-restart sequence formerly in `validates_send_arguments`.
It uses small R packages, verifies their availability in the managed library, and checks live-state preservation and reset in direct and sandboxed execution.
The smoke selection and its real R/Python/SQL, persistent-state, mixed-language recording, and native-sandbox executions remain unchanged.

For automatic R package discovery and live activation, start with the reached-package case, then run its suite:

```sh
scripts/test client_server/requirements/test_r_automatic::resolves_reached_r_packages_at_runtime
scripts/test client_server/requirements/test_r_automatic
```

The local-package cases retain the real MCP server, worker, R evaluator, and package loader.
They install immutable fixture packages once per case process for its sequential direct and sandbox executions.
Each execution creates fresh library views, resolver records, checkpoints, and runtime state; only requested packages become visible through activation.
The recording resolver still uses real `ir` for the base environment.
The suite also keeps real-resolver coverage of automatic installation, errors, and restart.

For real `ir` resolution, installation, default-library selection, and explicit preparation before and after startup, also run:

```sh
scripts/test \
  client_server/requirements/test_r::prepares_and_uses_cran_packages \
  client_server/requirements/test_r::prepares_initial_r_requirements \
  client_server/requirements/test_r::prepares_r_requirements_after_worker_startup \
  client_server/requirements/test_r::evaluates_with_default_managed_r
```

A case selector narrows a suite further, for example:

```sh
scripts/test --locate cli/test_config_overrides
scripts/test cli/test_config_overrides::layers_project_then_cli_in_order
```

## Choose the review boundary

Before a change crosses modules or triggers broad snapshot regeneration, write down:

```text
Observable behavior (or behavior preserved by an internal refactor):
Owning modules:
Public acceptance cases:
Expected snapshot changes and affected platforms:
Intended PR base:
```

Use one observable behavior per PR and identify the parent branch of each stack layer before implementation spreads across them.
This is a planning aid, not an approval requirement.
Keep task-specific answers in temporary notes; durable contracts belong in the owning documentation.

Inspect the actual diff against that base early, before regenerating unrelated snapshots:

```sh
scripts/review-diff BASE
git diff --merge-base BASE -- src/ tests/boundaries/
```

`BASE` names the intended parent; changes are measured from its merge base with `HEAD`.
Parent-only commits made after this layer forked are excluded.
The report includes tracked staged and unstaged changes; stage intended new files before measuring.
It reports production, tooling, tests, documentation, and generated snapshots separately, with binary-file counts outside line totals.
Production means `src/`, `python/mcp_console/`, `r/R/`, and `r/src/`; generated snapshots mean `tests/snapshots/`.
These path groups measure review volume, not semantic complexity or whether a change is mechanical.
Renames count as deletion and addition so moves do not hide review work.
Use `--json` for a reusable report, and review the full diff before opening the PR.
For a stack, measure each layer against its intended parent rather than accumulating every earlier layer against `main`.

## Validation ladder

1. For a behavior change, add a public acceptance or regression case and confirm it fails for the intended reason.
   For an internal refactor, establish the existing public suite's baseline.
2. Implement the change and rerun the focused case or suite until it passes.
   Failures print an exact rerun command; completion records retain the selector and full log.
   Full-update reruns retain `--full` and nondefault concurrency; focused reruns omit redundant profile flags.
   Every rerun retains a nondefault timeout.
   A failed full snapshot update retains full-update scope so orphan cleanup remains available; other failures narrow the rerun to the failed case.
3. Regenerate only the snapshots affected by an intentional behavior change with `scripts/test --update SELECTOR`, then rerun that selection without `--update`.
   A broader interface change may require a full update; inspect every resulting difference.
   Shared fixture changes also require the affected snapshots on other platforms; a local capability skip does not validate them.
4. Run `scripts/format`, inspect every formatter's result, and review `git diff` and `git diff --check`.
   Check embedded program indentation after formatting.
   `scripts/format --strict` reports failure after attempting every formatter; the [authoring recipe](../tests/boundaries/AUTHORING.md) explains embedded program conventions.
5. Run `scripts/check` as the ordinary final local gate, plus the owning focused tests for changed tooling.
   Use `scripts/check --full` only when explicitly requested or when the changed area warrants exhaustive local validation.
   Keep completion records with the tested revision and log paths, and report exactly which validation ran.
   Use a failed phase's focused command for diagnosis; repeat checks when changes or unresolved failures require them.
   CI supplies comprehensive validation before merge; opening a PR does not require a full local gate.

## Which commands mutate build state?

| Command                                            | State it can change                                                                   |
| -------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `scripts/test --help`, `--list`, `--locate`        | No compilation or bundle changes; uv may prepare the script environment               |
| `scripts/stage-sandbox-runner`                     | Shared companion source/build cache, checkout-local staged manifest, and `wheel-data` |
| `scripts/check-core`                               | Cargo debug build data and test fixture state                                         |
| `scripts/check`, `scripts/test SELECTOR`           | Cargo debug/release build data and test fixture state                                 |
| `scripts/test --update SELECTOR`                   | The preceding state plus selected snapshots                                           |
| `scripts/format`                                   | Source and documentation formatting, including snapshot formatting                    |
| `scripts/check --full`, `python3 tests/install.py` | Build and package state; installation checks temporarily rename `target`              |
| `uv run` or source installation                    | May build the local package and change its environment and bundle                     |

## Checkout ownership

`scripts/check`, `scripts/check-core`, `scripts/test`, `scripts/stage-sandbox-runner`, `tests/install.py`, and the Python packaging backend share `.dev-workflow/checkout.lock`.
The lock lives outside `target` and remains held until the owning command finishes.
A conflicting command exits with the lock path and last recorded owner's PID and command.
The file lock decides admission; the separately published diagnostic may be empty or stale during ownership handoff.
Retry after that owner finishes; do not delete a lock file to bypass ownership.
Sequential nested commands inherit the same ownership, including packaging invoked through `uv`.
Do not start concurrent children under an inherited owner.
Nested commands must wait for their children before returning; background mutators that outlive a nested command are outside this synchronous workflow contract.
For `scripts/check`, `scripts/check-core`, `scripts/test`, and `scripts/with-checkout`, cancellation sends `SIGTERM`, allows up to five seconds for the phase to exit, and then kills any remaining members of its owned process group before releasing ownership.
Cleanup defers cancellation signals until retirement finishes, including when cleanup follows a normal exit.
Nested validation commands share that group so escalation also reaches their children.
Commands that deliberately detach into a new session remain responsible for their own cleanup.
The outer group owner retires remaining members before releasing ownership.
This requires the owner to remain alive: `SIGKILL`, an owner crash, or a system failure can release its locks while children survive.
Recovery after abrupt owner death is outside this cooperative workflow contract; stop surviving commands before starting another mutator.
Direct staging, installation, and packaging entry points provide cooperative admission locks; their caller owns cancellation.
Use `scripts/with-checkout` when invoking those commands with the wrapper's cancellation semantics.

Use the same ownership for direct build commands:

```sh
scripts/with-checkout cargo build --release --target-dir target
scripts/with-checkout uv tool install --reinstall .
scripts/with-checkout scripts/stage-sandbox-runner
```

Direct Cargo and Maturin builds still require `scripts/stage-sandbox-runner` first.
The Python packaging backend performs staging for source installations.
Commands invoked outside these entry points cannot be serialized by the wrapper.
Keep separate Console checkouts' mutable build outputs separate; sharing download caches does not authorize sharing application `target` or wheel staging.
The companion has separate source ownership at `<source-checkout>.stage.lock`, outside its Git checkout and Cargo output.
That ownership covers fetch, build, and artifact copying, including when `MCP_CONSOLE_SANDBOX_SOURCE` selects the same source from different Console worktrees.
A concurrent stage waits for that owner, then checks Cargo freshness and stages the runner into its own checkout.
The same cooperative owner-lifetime limits apply to source ownership.

## Concurrent worktrees

`scripts/test --help`, `--list`, and `--locate` run before ownership or compilation; invalid test arguments also fail before building.
Help and syntax-only validation use Python's standard library before invoking `uv`.
Listing, location lookup, and semantic selector validation may prepare the script's dependency environment.
Checks and transcript runs in separate worktrees can run concurrently.
Each checkout owns its mutable build output, while staging serializes access to the shared pinned companion source and Cargo output.
An explicitly configured `XDG_CACHE_HOME` must be absolute for companion staging.
Transcript worker concurrency remains controlled by `scripts/test --jobs`.

## Completion records

`scripts/check`, `scripts/check-core`, and execution through `scripts/test` print the path to `.dev-workflow/runs/<run>/result.json` on completion, including failures.
Each record contains the checkout, command, Git revision and worktree status at admission, exit status, elapsed time, failing transcript selectors, and a log and timing for each phase that ran.
Transcript phases also link `case_timings` to a `case-timings.jsonl` file beside the record.
Each completed execution appends its selector, execution mode, status, and elapsed seconds, including snapshot comparison.
These durations overlap across parallel cases and exclude case-process startup; do not sum them as wall time.
An execution killed before its `finally` block can run has no timing record; the phase log and exit status remain authoritative for failures and cancellation.
Direct runner calls can select an existing output directory with `MCP_CONSOLE_TEST_TIMINGS=/absolute/path/cases.jsonl`; records append to that file.
Revision and worktree status are null for a source tree without Git metadata or when cancellation interrupts metadata collection.
A dirty worktree is recorded explicitly; its result is not evidence for an unchanged clean revision.
The overall exit status uses the shell convention `128 + signal` for a phase killed by a signal; the phase retains its negative subprocess status.
Once work and retirement finish, finalization freezes that status and blocks further cancellation through record publication and process exit.
A signal received during the final save or record-path announcement does not change the completed result.
Nested runs reference their parent's record and retain their own phase details.
Records are updated after each phase, so an unfinished run has a null exit status.
Each validation phase writes stdout and stderr directly to its log, preserving complete errors and tracebacks.
The terminal reports the phase, log path, completion status, failing selectors, and rerun commands on stderr.
After a failed phase exits, its full log is also printed so CI logs retain the diagnostics.
Read or tail the advertised log for detailed progress; nested phases advertise their own logs in the enclosing phase's log.
`scripts/with-checkout` only adds ownership and command lifetime management: it inherits stdin, stdout, and stderr and creates no validation record.
A forcibly killed owner may leave an unfinished record; a record is complete only when its exit status is present.

These files are ignored by Git and survive installation checks that rename `target`.
They may be removed when their evidence is no longer needed and no command is active.
