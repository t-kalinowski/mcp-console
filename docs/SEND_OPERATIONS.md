# `send` operation order

This is the operation-order reference for the single implicit console session.
The [runtime guide](BUILTIN_RUNTIME.md) describes language behavior and response notices; [requirements and environments](REQUIREMENTS.md) describes dependency inputs, activation, and host resolution.

A call accepts at most one complete `r`, `python`, or `sql` cell.
Submit cells sequentially and collect an unfinished evaluation before submitting another cell.
A control-only interrupt may overlap a pending call, including requirement preparation for restart.
An empty `stdin` string contributes no bytes; the table refers to nonempty input.

## Validation before actions

Request decoding and structural checks precede interruption, preparation, stdin enqueue, and evaluation.
These checks reject unknown fields, wrong field types, multiple code fields, disabled languages, unavailable requirements, standalone preparation with nonempty stdin, and interrupt plus requirements without a cell.

[SSH targets](SSH.md) discover capability on the execution host before advertising the schema.
Managed targets use the same preparation ordering below; bare targets omit `requirements` and reject supplied preparation before control, stdin, or evaluation side effects.

Requirement-content errors normally also reject the call before those actions.
For interrupt plus a cell, reporting those errors is deferred until after interrupt delivery, stdin enqueue, the 100-millisecond grace, and settlement of the previous evaluation.
If that evaluation remains active, the call instead reports that the new cell was not run.
Requirement compatibility and resolver errors can arise later during preparation.

## Operations

`timeout_ms` defaults to 60,000 milliseconds and limits observation of an evaluation, not the duration of the whole call.
It starts when evaluation observation begins after admission, or when a poll attaches to an evaluation.
Explicit preparation and control finish before this wait, as shown below.
It does not cancel evaluation, worker startup, or resolution.
SSH discovery and worker bootstrap have separate 30-second setup deadlines and remain cancellable by session shutdown.
Remote dependency preparation has no setup deadline; an interrupt or cancellation targets that operation's remote resolver processes.
The independent SSH controller lease can expire during preparation or evaluation when bidirectional control communication is lost.
`timeout_ms` neither renews that lease nor changes its duration.
Within the lease, a brief SSH interruption can resume the same owner and admitted operation.
Polling preserves already ingested output; new cells, requirements, and restart requests are rejected while either channel reports recovery.
Controls already admitted retain their original stream and generation identity; recovery either reconciles them or reports a bounded terminal failure.
An automatic replacement attempt after worker failure shares the same evaluation wait.
The table assumes the session admits the operation; a conflicting operation or generation change can reject it before the remaining steps.

| Call                                                          | Order after structural checks                                                                                                                                                                                                                        | Generation receiving stdin                                                    | What can remain after failure                                                                                                                                                                                                                           | When `timeout_ms` applies                                                                                                                          |
| ------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| Cell, optionally with requirements and stdin                  | Validate declared requirements; prepare additions; admit the evaluation with queued stdin; start the worker if needed; deliver stdin before the evaluation command.                                                                                  | The generation accepting the cell.                                            | Preparation may change host caches or live state before it fails. The cell is not run if its preconditions fail. Changes made by a running cell remain.                                                                                                 | After evaluation admission, including lazy worker startup and automatic resolution during the cell. Explicit preparation precedes the wait.        |
| Requirements without code or control                          | Validate and prepare additions. Before the first worker starts, retain them without starting it; an idle worker can apply supported live changes.                                                                                                    | None; nonempty stdin is rejected.                                             | Live preparation can commit some changes before a later step fails; see below. A stopped worker with new additions returns `[restart required]`.                                                                                                        | No preparation deadline.                                                                                                                           |
| Interrupt with a cell, optionally with requirements and stdin | Signal the active resolver first, otherwise the worker; acknowledge delivery; queue stdin; wait 100 milliseconds; settle the previous evaluation. Only if it has stopped, report deferred requirement errors, prepare additions, and admit the cell. | The interrupted generation, which must remain current through cell admission. | An acknowledged interrupt and queued input remain effective if a later step fails. An active previous evaluation prevents the new cell from running; it is not queued for later.                                                                        | After admission of the new cell. If the previous evaluation remains active, return its available state and the cell-not-run error after the grace. |
| Interrupt without a cell, optionally with stdin               | Signal the active resolver first, otherwise the worker; acknowledge delivery; queue stdin; wait 100 milliseconds; observe the current operation. Requirements are not accepted.                                                                      | The interrupted generation.                                                   | Interrupt and input may already have affected the operation. An interrupt delivery failure prevents subsequent stdin enqueue.                                                                                                                           | After the grace if the call can attach to an evaluation; otherwise it returns current status. `0` requests immediate observation after the grace.  |
| Restart, optionally with requirements, stdin, and a cell      | Validate, resolve, and commit any declared additions; retire the worker; start its replacement, preparing pending defaults if needed. Only after replacement succeeds, queue stdin and admit any cell.                                               | Only the replacement generation. Old unread input is discarded.               | Failure resolving declared additions before retirement leaves the current worker and retained configuration unchanged, but may change host caches. Retirement or replacement failure does not roll back the environment or restore old in-memory state. | Only after admission of a following cell. Resolution, retirement, and replacement startup have no `timeout_ms` deadline.                           |
| Empty poll                                                    | Attach to the current evaluation and collect output, or return idle output immediately. Do not start an initial or stopped worker.                                                                                                                   | None.                                                                         | No code or requirements are submitted.                                                                                                                                                                                                                  | From attachment to an evaluation. An idle poll returns immediately.                                                                                |
| Stdin without code or control                                 | Queue input and observe the active evaluation, or start an initial/stopped worker if needed, queue input, and collect idle output.                                                                                                                   | The generation accepting the input.                                           | Queued bytes can be consumed even if subsequent observation fails.                                                                                                                                                                                      | From attachment to an active evaluation. With no evaluation, startup and input submission have no `timeout_ms` deadline.                           |

Input ordering guarantees enqueue order, not consumption by a particular read.
An already waiting read can consume same-generation input before the new cell begins, including while an interrupted operation unwinds.
The 100-millisecond interrupt grace gives the previous operation time to accept the signal and consume queued input before a following cell is admitted.
Work accepted by an old generation cannot send its stdin or cell into a concurrent replacement.

## Preparation and failure

Successful additions are retained for later cells and restarts.
Exact repeats perform no resolver or worker preparation.
Standalone success returns `[prepared]`; preparation bundled with a cell adds no such marker.
Packages and extensions become available without being attached, imported, or loaded.

Preparation is not a general rollback boundary.
Before the first worker starts and during restart preparation, the server commits changed retained candidates together after their host resolution succeeds.
Host cache writes and installation or build side effects may survive a failed request.
Live preparation has separate activation points: a Python addition can remain retained if a following R update fails, and a failed R update can leave a changed library path that requires restart.
The [live preparation rules](REQUIREMENTS.md#live-r-preparation) describe these outcomes and infrastructure failures that stop a worker.

A wait that ends with `[running; poll with an empty send]` leaves the operation active.
Poll with another empty `send`; do not resubmit its code.
The built-in server prepares its initial environment on first use through these same operations.
For an ordinary first cell, the evaluation wait includes default environment preparation and worker startup.
Explicit requirements and restart finish initial preparation before any following evaluation wait.
Idle stdin awaits preparation, startup, and input submission without a `timeout_ms` deadline; see [retained environments](REQUIREMENTS.md#retained-environments).
