# Development validation

Run development commands from the repository root.
`scripts/check` runs companion staging, core checks, release transcript tests, and installation checks in that order.
Installation checks run last because they replace and hide the shared `target` directory.

## Checkout ownership

`scripts/check`, `scripts/check-core`, `scripts/test`, `scripts/stage-sandbox-runner`, `tests/install.py`, and the Python packaging backend share `.dev-workflow/checkout.lock`.
The lock lives outside `target` and remains held until the owning command finishes.
A conflicting command exits with the active owner's PID, command, and lock path.
Retry after that owner finishes; do not delete a lock file to bypass ownership.
Sequential nested commands inherit the same ownership, including packaging invoked through `uv`.
Do not start concurrent children under an inherited owner.
Nested commands must wait for their children before returning; background mutators that outlive a nested command are outside this synchronous workflow contract.
For `scripts/check`, `scripts/check-core`, `scripts/test`, and `scripts/with-checkout`, cancellation sends `SIGTERM`, allows up to five seconds for the phase to exit, and then kills any remaining members of its owned process group before releasing ownership.
Cleanup defers cancellation signals until retirement finishes, including when cleanup follows a normal exit.
Nested validation commands share that group so escalation also reaches their children.
Commands that deliberately detach into a new session remain responsible for their own cleanup.
The outer group owner retires remaining members before releasing ownership.
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
Changing the budget does not change case assertions, deadlines, or transcript worker concurrency.

## Completion records

`scripts/check`, `scripts/check-core`, and execution through `scripts/test` print the path to `.dev-workflow/runs/<run>/result.json` on completion, including failures.
Each record contains the checkout, command, Git revision and worktree status at admission, exit status, elapsed time, failing transcript selectors, and a log and timing for each phase that ran.
Revision and worktree status are null for a source tree without Git metadata.
A dirty worktree is recorded explicitly; its result is not evidence for an unchanged clean revision.
The overall exit status uses the shell convention `128 + signal` for a phase killed by a signal; the phase retains its negative subprocess status.
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
