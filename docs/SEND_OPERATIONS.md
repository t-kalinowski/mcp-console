# `send` operation order

`send` operates on one implicit session and accepts at most one complete `r`, `python`, or `sql` cell.
Submit cells sequentially and collect unfinished results before another cell.
A control-only interrupt may overlap pending work; `requirements.action="get"` may inspect the last commit during evaluation or resolution without consuming output.
If another send owns evaluation polling or response delivery, the interrupt returns `[running; poll with an empty send]` without consuming that send's output.
Otherwise, it observes and collects the interrupted evaluation normally.

[Runtime behavior](BUILTIN_RUNTIME.md) and [requirements](REQUIREMENTS.md) explain the operations themselves.
This page defines their order and partial effects.

## Server readiness

MCP initialization, tool discovery, and pings do not wait for runtime discovery, default preparation, or built-in worker startup.
The interface comes from captured configuration, not installed-runtime availability.
One shared startup owner prepares and prelaunches the real worker.
After transport readiness connects resolver, input, and output services, the built-in worker initializes enabled R and Python on its serialized thread without waiting for code.
Explicit or host-resolved Python selections start through the native facade; unresolved R-side selection retains its compatibility rules.
SQL bridges accompany runtime setup; enabled SQL opens its managed connection during bootstrap when its optional provider is installed.
First-query work remains lazy.
Custom workers remain lazy.

A structurally valid early cell immediately reserves the evaluation slot.
Its single observation deadline starts at call entry, including shared startup and deferred preparation.
Expiry returns `[running; poll with an empty send]`; the cell stays accepted and runs at most once.
Its evaluation frame waits behind interpreter bootstrap; timeout and polling never replay it.
An empty poll or `get` without an accepted cell can instead return `[worker starting]`.
`timeout_ms=0` observes immediately; it does not skip validation or create another worker.

Early standalone requirements that time out before readiness have **not** been accepted and must be submitted again.
Early code-free stdin is buffered for its generation; a cell's bundled stdin waits for its requirements.
Startup hooks can emit output, plots, errors, and managed input requests without a submitted cell.
An empty poll can return `[idle]` while interpreters initialize after transport readiness; it does not certify initialization completion.
Before discovery finishes, control needing configuration can time out without applying; any bundled cell is explicitly reported as not run.

Requirements already admitted when discovery finishes can select the initial candidate.
Later early declarations use the same preparation transaction after shared startup.
Changed requirements may replace an **unused** prewarmed worker after successful preparation and confirmed retirement, without an explicit restart.
Initialization alone does not make that worker used; user code or nonempty stdin ends this exception.
Replacement serializes bootstrap callbacks with preparation and confirms old-worker retirement before launching the successor.
Configured hooks can run once in each new generation, including a replacement before the first cell.
Once user code or nonempty stdin has reached it, normal live/restart rules apply.
Prewarming must not change the user's effective declaration semantics.

Cancellation before admission leaves no accepted cell.
Cancellation after early admission releases the caller's wait, not the accepted cell or shared startup; poll to discover retained state.
Timeout likewise cancels nothing.
Closing MCP input cancels startup and follows normal retirement; outstanding response delivery after closure is unspecified.

Discovery or initial preparation failure is retained by ordinary sends; tool discovery remains usable with the same schema.
After correcting setup, an explicit `control="restart"` retries that failed readiness attempt using the server's captured configuration and current filesystem/tool availability.
Concurrent restart callers share an in-flight retry; request cancellation and timeout leave it running, while connection closure cancels and retires it.
If setup still fails, the retry reports and retains its new diagnostic.
Previously rejected cells and their bundled stdin are not replayed.
A retry that succeeds accepts its environment once; joining restart callers do not replace that worker or discard state accepted meanwhile.
After configuration is accepted, restart uses the retained environment and ordinary worker replacement.
Same-call code, stdin, and requirements proceed only after readiness succeeds and retain their ordinary admission rules.
Retries require confirmed cleanup of the failed preparation attempt.
After discovery, a failed built-in worker prelaunch is reported once by the next idle poll, cell, or requirements inspection, with its diagnostics.
The declaration remains available and ordinary worker recovery still applies.
The first failure response includes captured startup diagnostics, including for requirements-only calls and restart.
Early calls and their results remain recorded if discovery fails; unavailable metadata stays unknown.
Later environment/worker-start failures follow the ordinary later-cell retry boundary, not an automatic retry loop.

## Validation before actions

Decoding and structural checks precede side effects: unknown fields/types, multiple or configured-disabled languages, incompatible get/reset payloads, set/reset with interrupt, standalone preparation with nonempty stdin, and interrupt-plus-requirements without a cell are rejected first.
Runtime-capability checks precede execution/preparation, though an early accepted cell can report such an error through its polling result.

Bare workers reject mutations before control, stdin, or evaluation.
Python-only sessions reject **all** interrupt-plus-requirements combinations before signaling or queuing input, including retained requirements.

Requirement-content errors normally precede effects too.
The exception is a supported interrupt-plus-cell: signal delivery, input enqueue, grace, and settlement of the old evaluation precede deferred content validation.
If the old evaluation remains active, the new cell is not run or queued.

## Operations

The following order applies after shared startup.
Admission can still fail on a conflicting operation or generation transition.

| Call                                        | Ordered effects                                                                                                                                                                                       |
| ------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Cell, optionally requirements/stdin         | Validate and prepare requirements → reserve the cell → start worker if needed → queue/deliver input before evaluation. Failed preconditions withhold the cell.                                        |
| Requirements only                           | Validate → prepare/retain supported changes. No worker launch solely for prestart preparation; nonempty stdin is invalid.                                                                             |
| Interrupt + cell                            | Signal active resolver or worker → acknowledge delivery → queue stdin → wait 100 ms → settle prior evaluation → validate deferred requirements → prepare → admit new cell only if prior work stopped. |
| Interrupt without cell                      | Signal → acknowledge → queue stdin → wait 100 ms → observe. Requirements are invalid.                                                                                                                 |
| Restart, optionally requirements/stdin/cell | Validate and resolve candidate → commit → retire old worker → start replacement → send input and code only to the ready replacement.                                                                  |
| Empty poll                                  | Collect the current evaluation or idle output; do not start an initial/stopped worker.                                                                                                                |
| Stdin only                                  | Queue for the current generation and observe; after shared startup, start an initial/stopped worker if needed. Empty text queues no bytes.                                                            |
| Requirements `get`                          | Wait for initial readiness within budget → report an unconsumed prelaunch failure, otherwise read the committed snapshot without evaluation admission or output collection.                           |

Control and optional cell admission share one lifecycle boundary.
Interrupt's generation must remain current; restart's input/code can reach only its replacement.
An already waiting read may consume same-generation stdin before a new cell, including while interrupted work unwinds.
Enqueue is not a receipt of consumption.

Changed set/reset on a worker that has accepted user execution requires restart; unchanged replacement skips preparation, not an explicitly requested restart.
Get rejects code, stdin (including empty), control, and payloads.
See [requirements actions](REQUIREMENTS.md#inspecting-and-replacing-requirements).

## Wait timing

`timeout_ms` defaults to **60,000 ms** and is one observation deadline from call entry.
It does not cancel evaluation, startup, or resolution.
Shared initial startup consumes the budget; evaluation gets no fresh deadline afterward.
Automatic import/package resolution and one failure-replacement attempt belong to the evaluation's wait.

After startup, explicit preparation, interrupt/grace, restart retirement/startup, and input submission without an evaluation finish before observation and can make the whole call exceed its deadline.
Standalone preparation has no `timeout_ms` execution limit.
With a running evaluation, polling/input/control observe using the remaining budget.
Idle polls return immediately.

Transport setup and retirement have their own deadlines, not a total resolver installation deadline.
Interrupt targets active resolver work; connection closure cancels and retires it.
See [sandbox lifetime limits](SANDBOX.md#supported-hosts-and-lifetime-limits).

## Preparation and failure

Standalone success returns `[prepared]`; bundled preparation has no such marker.
Exact repeats perform no resolver/worker preparation.
Packages are not attached, imported, or loaded by preparation.

Before startup or restart, complete candidates commit only after successful host preparation.
Failure then preserves the current worker/declaration and withholds same-call input/code.
Once retirement begins, replacement failure cannot restore live state or roll back the accepted environment.

Live preparation has separate activation receipts: a Python success can remain if a later R update fails.
Arbitrary site hooks and host cache/build effects are not rolled back.
Some activation failures preserve the worker for state recovery but require restart for further changes; infrastructure failure stops it.
See [live preparation](REQUIREMENTS.md#live-preparation).

An acknowledged interrupt and queued input remain effective even when later preparation or cell admission fails.
Signal-delivery failure prevents later input enqueue.
No failed cell is automatically replayed.

## Output and retained files

A controlled send combines prior output, lifecycle notices, and new-cell output under one delivery owner and one 8 KiB text budget; images have separate limits.
Polling consumes its observed interval, including omitted text.
Get returns the complete manifest in structured content, not a truncated declaration.

[Raw cell logs](RECORDING.md) are flushed at response cuts and can be read while evaluation continues.
Paths refer to the local Console server.
Reading a file does not move the polling cursor; resubmitting code is not output retrieval.
[Architecture](ARCHITECTURE.md#output-and-delivery) covers response recovery and the limits of delivery guarantees.
