# Development workflow

Run development commands from the repository root.
`scripts/check` runs companion staging, core checks, release transcript tests, and installation checks in that order.
Installation checks run last because they replace and hide the shared `target` directory.

## Find the public test

Use `scripts/test --list` to discover selectors and `scripts/test --locate SELECTOR` to find source lines and the primary snapshot before building.
These routes are starting points; read the relevant contract and case before changing behavior.

| Task                   | Owning source                                              | Public check                                      | Selected snapshot update                                                                                    |
| ---------------------- | ---------------------------------------------------------- | ------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Output previews        | `src/worker_client/output/`                                | `scripts/test client_server/output/test_previews` | `scripts/test --update client_server/output/test_previews`                                                  |
| Delivery recovery      | `src/worker_client/output/`, `src/server_transport.rs`     | `scripts/test client_server/output/test_recovery` | `scripts/test --update client_server/output/test_recovery`                                                  |
| Configuration layering | `src/config.rs`, `src/config/`                             | `scripts/test cli/test_config_overrides`          | `scripts/test --update cli/test_config_overrides`                                                           |
| Fixture serialization  | `tests/support/snapshots.py`, `tests/transcript_runner.py` | `tests/transcript_runner.py`                      | For handshake changes: `scripts/test --update client_server/server/test_tools::initializes_and_lists_tools` |
| Validation ownership   | `checkout_workflow.py`, `build_backend.py`                 | `python3 tests/workflow.py`                       | No transcript snapshots                                                                                     |

A case selector narrows a suite further, for example:

```sh
scripts/test --locate cli/test_config_overrides
scripts/test cli/test_config_overrides::layers_project_then_cli_in_order
```

## Validation ladder

1. For a behavior change, add a public acceptance or regression case and confirm it fails for the intended reason.
   For an internal refactor, establish the existing public suite's baseline.
2. Implement the change and rerun the focused case or suite until it passes.
   Failures print an exact rerun command; completion records retain the selector and full log.
   Full-update reruns retain nondefault concurrency; every rerun retains a nondefault timeout.
   A failed full snapshot update retains full-update scope so orphan cleanup remains available; other failures narrow the rerun to the failed case.
3. Regenerate only the snapshots affected by an intentional behavior change with `scripts/test --update SELECTOR`, then rerun that selection without `--update`.
   A broader interface change may require a full update; inspect every resulting difference.
   Shared fixture changes also require the affected snapshots on other platforms; a local capability skip does not validate them.
4. Run `scripts/format`, inspect every formatter's result, and review `git diff` and `git diff --check`.
   Check embedded program indentation after formatting.
   `scripts/format --strict` reports failure after attempting every formatter and the fixture checker; the [authoring recipe](../tests/boundaries/AUTHORING.md) explains directives and intentional invalid inputs.
5. Run `scripts/check` before opening the PR.
   Keep its completion record with the tested revision and log paths.
   Use a failed phase's focused command for diagnosis; repeat the full gate when changes or unresolved failures require it.

## Which commands mutate build state?

| Command                                     | State it can change                                                            |
| ------------------------------------------- | ------------------------------------------------------------------------------ |
| `scripts/test --help`, `--list`, `--locate` | No compilation or bundle changes; uv may prepare the script environment        |
| `scripts/stage-sandbox-runner`              | Companion source/build cache under `target`, staged manifest, and `wheel-data` |
| `scripts/check-core`                        | Cargo debug build data and test fixture state                                  |
| `scripts/test SELECTOR`                     | Cargo release build data and test fixture state                                |
| `scripts/test --update SELECTOR`            | The preceding state plus selected snapshots                                    |
| `scripts/format`                            | Source and documentation formatting, including snapshot formatting             |
| `scripts/check`, `python3 tests/install.py` | Build and package state; installation checks temporarily rename `target`       |
| `uv run` or source installation             | May build the local package and change its environment and bundle              |

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
Keep separate checkouts' mutable build outputs separate; sharing download caches does not authorize sharing `target` or wheel staging.

## Host concurrency

`scripts/test --help`, `--list`, and `--locate` run before ownership or compilation; invalid test arguments also fail before building.
Help and syntax-only validation use Python's standard library before invoking `uv`.
Listing, location lookup, and semantic selector validation may prepare the script's dependency environment.
Full checks and transcript runs share a host budget of one active owner by default.
Set `MCP_CONSOLE_CHECK_SLOTS` to a positive integer to select another budget, using the same setting for concurrent callers.
When every slot is occupied, the command exits with `full-check budget is busy` before running a phase.
Nested commands reuse their parent's slot.
Slot locks live in `${XDG_CACHE_HOME:-$HOME/.cache}/mcp-console/checks/`.
An explicitly configured `XDG_CACHE_HOME` must be absolute; relative paths fail before phases run because they would make the budget checkout-local.
Changing the budget does not change case assertions, deadlines, or transcript worker concurrency.

## Completion records

`scripts/check`, `scripts/check-core`, and execution through `scripts/test` print the path to `.dev-workflow/runs/<run>/result.json` on completion, including failures.
Each record contains the checkout, command, Git revision and worktree status at admission, exit status, elapsed time, failing transcript selectors, and a log and timing for each phase that ran.
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
