# `send` operation order

This is the operation-order reference for the single implicit console session.
The [runtime guide](BUILTIN_RUNTIME.md) describes language behavior and response notices; [requirements and environments](REQUIREMENTS.md) describes dependency inputs, activation, and host resolution.

A call accepts at most one complete `r`, `python`, or `sql` cell.
Submit cells sequentially and collect an unfinished evaluation before submitting another cell.
A control-only interrupt may overlap a pending call, including requirement preparation for restart.
`requirements.action="get"` may also overlap active evaluation or resolution; it reads a committed server snapshot and consumes no output.
An empty `stdin` string contributes no bytes; the table refers to nonempty input.

## Server readiness

After launch configuration and applicable local native-policy validation, MCP initialization, tool discovery, and pings can complete while runtime discovery, initial environment preparation, and default worker startup run in the background.
The tool schema and descriptions come from captured configuration and do not change when that work finishes.
An advertised language is not proof that its runtime is installed.

One startup owner prepares the default environment and launches its real worker through transport readiness.
The built-in worker then initializes enabled R and Python runtimes on its serialized interpreter thread, without waiting for submitted code.
Transport readiness comes first so startup hooks can use resolver callbacks and input.
Explicit and independently prepared Python selections start through the native facade; unresolved R-side selection keeps its existing rules.
SQL bridge setup accompanies runtime initialization; managed DuckDB connections and queries remain lazy.
Custom workers retain their existing lazy launch contract.
Startup and all early cells share the ordinary generation, admission, evaluation, and retirement machinery.
Default dependency preparation and a worker process are started even if the client submits no code.
The first evaluation waits behind shared interpreter initialization and is accepted only once.

A structurally valid early cell occupies the existing evaluation slot immediately, including a cell with explicit requirements.
Its observation deadline starts when the call enters the server and includes waiting for shared startup.
Expiry returns `[running; poll with an empty send]`; `timeout_ms=0` requests immediate observation.
The accepted cell continues in the background and is executed at most once.
Collect it with an empty `send`; a second cell is rejected until the first completion has been delivered.
An empty poll without an accepted cell waits for startup within its budget and returns `[worker starting]` if it expires.
Requirement inspection likewise returns `[worker starting]` without a manifest until discovery and startup settle.

Early code-free stdin is buffered for the current generation and delivered once when its worker is registered.
A cell's bundled stdin remains withheld until its requirements are prepared.
Startup hooks can produce output, plots, errors, and input requests without a submitted cell.
They use the ordinary output tape, input notices, stdin delivery, and polling path.
An empty poll can return `[idle]` while interpreter startup continues after transport readiness; it does not certify interpreter completion.
Restart discards old-generation buffered input.
A standalone requirements call that exhausts its budget before readiness returns `[worker starting]` without accepting a preparation; submit that declaration again after startup.
An interrupt can signal startup before a process is registered and between resolver phases.
A restart after discovery uses the existing retirement and replacement protocol, including while the default worker is launching.
Before discovery completes, a control requiring configuration can exhaust its budget without applying control; a bundled cell is explicitly reported as not run.

MCP cancellation before cell admission leaves no accepted cell.
For a call admitted while initial startup is pending, cancellation after admission releases only that call's response wait: the cell remains accepted, including before execution begins, and continues through ordinary empty polling.
If execution has begun, its evaluation and effects remain active.
Calls admitted after readiness retain the existing response-delivery cancellation behavior.
A cancelled request may have crossed admission before its caller observed cancellation; an empty poll discovers the retained state safely.
Neither cancellation nor timeout cancels shared startup.
Closing the MCP connection cancels discovery, preparation, and worker launch, then joins the existing ownership and retirement protocol.
After MCP input closes, delivery and the exact diagnostic of an outstanding response are unspecified.

A failed runtime discovery is retained: subsequent `send` calls report the same failure rather than retrying setup, and tool discovery remains available.
The failure uses the ordinary bounded tool-error response, including for requirement inspection.
Recording metadata is unavailable after discovery fails, so buffered early tool records are discarded and subsequent recording is disabled.
Correct the execution-host setup and start a new MCP server session to retry.
Default environment and worker startup failures retain the ordinary later-cell retry boundary; they do not trigger a new retry loop.
Failure preparing an accepted initial declaration ends that startup attempt and withholds the cell; a later cell owns any retry.

Explicit early requirements are prepared before the accepted cell or its bundled stdin reaches execution.
If the declaration is already admitted when discovery finishes, the startup owner uses it to select the initial candidate before preparing pending defaults.
Declarations arriving after default preparation begins use the same preparation transaction after shared startup settles.
An unchanged declaration reuses the default candidate.
A changed replacement can retire an unused prewarmed candidate after successful resolution, without requiring an explicit restart merely because it was prewarmed.
Effective additions to an unused candidate also use prestart preparation and retirement, preserving its captured environment.
Interpreter initialization alone does not make that candidate used.
Replacement serializes bootstrap resolver callbacks with preparation and confirms old-worker retirement before launching the replacement.
Configured startup hooks may run once in each new worker generation, including a replacement before the first cell.
Once user code or stdin has reached the worker, the ordinary live-change and explicit-restart rules apply.
This includes idle stdin queued for a later read.
Resolution failure preserves the committed environment and candidate; uncertain retirement blocks replacement.

## Validation before actions

Request decoding and structural checks precede interruption, preparation, stdin enqueue, and evaluation.
These checks reject incompatible `get` fields, payloads with `reset`, replacement actions with interrupt, unknown fields, wrong field types, multiple code fields, disabled languages, standalone preparation with nonempty stdin, and interrupt plus requirements without a cell.
Checks requiring discovered runtime capabilities complete before execution and requirement preparation; an early accepted cell can report such a validation error through its ordinary polling result.

[SSH targets](SSH.md) discover capability on the execution host in the same background preparation phase.
Managed targets use the preparation ordering below; bare targets allow only `requirements.action="get"` at execution and reject supplied preparation before control, stdin, or evaluation side effects.

Local and SSH sans-R sessions managed through uv on the execution host expose Python and DuckDB extension requirements and the ordinary action/version/cutoff fields; R requirements remain unavailable.
They support standalone preparation and preparation with a Python or SQL cell before first worker startup, and explicit restart preparation with or without a cell.
An idle running worker also accepts effective new Python-distribution and DuckDB-extension additions with `action: "add"`, including both in one call; already-retained declarations remain no-ops.
Automatic missing-import resolution starts only when a managed Python cell reaches that import and uses the running evaluation's resolver exchange.
Changes to a declared distribution, interpreter constraints, publication cutoffs, and changed `set` or `reset` declarations require explicit restart.
The server prepares the complete Python candidate and retained extensions before one live activation when both change.
Live validation, host preparation, and compatibility failures or cancellation leave the worker and committed declaration unchanged; same-call code and input are not sent.
An activation failure withholds the cell and requires restart before further requirement changes.
Restart resolves the cumulative candidate and inspects its embedding configuration before retirement.
Failure before retirement preserves the current worker, retained requirements, and queued input; same-call code and input are sent only after successful replacement.
Combining interrupt with requirements is rejected before signaling or queuing stdin, including retained requirements.
The retirement/replacement failure semantics below still apply.

Requirement-content errors normally also reject the call before those actions.
For interrupt plus a cell, reporting those errors is deferred until after interrupt delivery, stdin enqueue, the 100-millisecond grace, and settlement of the previous evaluation.
If that evaluation remains active, the call instead reports that the new cell was not run.
Requirement compatibility and resolver errors can arise later during preparation.

## Operations

`timeout_ms` defaults to 60,000 milliseconds and provides one observation deadline measured from call entry.
Shared initial startup consumes this budget; evaluation does not receive a fresh timeout after readiness.
Explicit preparation after startup and inline control still finish before observation, as shown below, and can extend the complete call beyond the deadline.
It does not cancel evaluation, worker startup, or resolution.
SSH connection setup and worker bootstrap have separate 30-second setup deadlines and remain cancellable by session shutdown.
Runtime discovery after the preparation handshake and remote dependency preparation have no setup deadline; an interrupt or cancellation targets the active remote resolver processes.
An automatic replacement attempt after worker failure shares the same evaluation wait.
The table describes operation order after shared initial startup settles; early accepted cells follow the readiness rules above.
A conflicting operation or generation change can reject admission before the remaining steps.

| Call                                                          | Order after structural checks                                                                                                                                                                                                                        | Generation receiving stdin                                                    | What can remain after failure                                                                                                                                                                                                                           | When `timeout_ms` applies                                                                                                                          |
| ------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| Cell, optionally with requirements and stdin                  | Validate declared requirements; prepare additions; admit the evaluation with queued stdin; start the worker if needed; deliver stdin before the evaluation command.                                                                                  | The generation accepting the cell.                                            | Preparation may change host caches or live state before it fails. The cell is not run if its preconditions fail. Changes made by a running cell remain.                                                                                                 | After evaluation admission, including lazy worker startup and automatic resolution during the cell. Explicit preparation precedes the wait.        |
| Requirements without code or control                          | Validate and prepare additions. Before the first worker starts, retain them without starting it; an idle worker can apply supported live changes.                                                                                                    | None; nonempty stdin is rejected.                                             | Live preparation can commit some changes before a later step fails; see below. A stopped worker with new additions returns `[restart required]`.                                                                                                        | No preparation deadline.                                                                                                                           |
| Interrupt with a cell, optionally with requirements and stdin | Signal the active resolver first, otherwise the worker; acknowledge delivery; queue stdin; wait 100 milliseconds; settle the previous evaluation. Only if it has stopped, report deferred requirement errors, prepare additions, and admit the cell. | The interrupted generation, which must remain current through cell admission. | An acknowledged interrupt and queued input remain effective if a later step fails. An active previous evaluation prevents the new cell from running; it is not queued for later.                                                                        | After admission of the new cell. If the previous evaluation remains active, return its available state and the cell-not-run error after the grace. |
| Interrupt without a cell, optionally with stdin               | Signal the active resolver first, otherwise the worker; acknowledge delivery; queue stdin; wait 100 milliseconds; observe the current operation. Requirements are not accepted.                                                                      | The interrupted generation.                                                   | Interrupt and input may already have affected the operation. An interrupt delivery failure prevents subsequent stdin enqueue.                                                                                                                           | After the grace if the call can attach to an evaluation; otherwise it returns current status. `0` requests immediate observation after the grace.  |
| Restart, optionally with requirements, stdin, and a cell      | Validate, resolve, and commit any declared additions or replacement; retire the worker; start its replacement, preparing pending defaults if needed. Only after replacement succeeds, queue stdin and admit any cell.                                | Only the replacement generation. Old unread input is discarded.               | Failure resolving declared additions before retirement leaves the current worker and retained configuration unchanged, but may change host caches. Retirement or replacement failure does not roll back the environment or restore old in-memory state. | Only after admission of a following cell. Resolution, retirement, and replacement startup have no `timeout_ms` deadline.                           |
| Empty poll                                                    | Attach to the current evaluation and collect output, or return idle output immediately. Do not start an initial or stopped worker.                                                                                                                   | None.                                                                         | No code or requirements are submitted.                                                                                                                                                                                                                  | From attachment to an evaluation. An idle poll returns immediately.                                                                                |
| Stdin without code or control                                 | Queue input and observe the active evaluation, or start an initial/stopped worker if needed, queue input, and collect idle output.                                                                                                                   | The generation accepting the input.                                           | Queued bytes can be consumed even if subsequent observation fails.                                                                                                                                                                                      | From attachment to an active evaluation. With no evaluation, startup and input submission have no `timeout_ms` deadline.                           |

The preparation rows describe `add`, the default action.
For `set` and `reset`, a changed declaration with a live worker that has accepted user execution requires explicit restart and is rejected before resolution or mutation otherwise.
An unused prewarmed default candidate instead permits resolution followed by confirmed retirement and replacement.
Without a live worker, standalone replacement resolves and retains the complete candidate without starting a worker.
Omitted `set` fields are empty; `reset` restores startup defaults.
An unchanged replacement skips preparation, while an explicit restart still takes effect.
`get` waits for initial startup within its observation budget, then bypasses evaluation and preparation admission, reads the committed snapshot, and returns without collecting output.
It rejects code, stdin, control, and requirement payloads.
See [requirements actions](REQUIREMENTS.md#inspecting-and-replacing-requirements).

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
The built-in server prepares its initial environment and worker in the background through these same operations.
An early accepted cell's observation budget includes shared startup and any deferred explicit preparation before execution.
After initial readiness, explicit requirements and restart finish before observing a following cell with the remaining budget.
Code-free stdin during initial startup uses the bounded readiness wait above; starting an initial or stopped worker afterward retains the existing unbounded input-submission step.
See [retained environments](REQUIREMENTS.md#retained-environments).

## Output and retained files

Requirement inspection returns the complete manifest in structured content; large manifests use a short text notice instead of a truncated declaration.
Every complete result shares an 8 KiB rendered UTF-8 text budget, including preparation, old-worker output, replacement-cell output, input and lifecycle notices, and failures.
Oversized output returns a bounded beginning and latest tail; images use a separate allowance.
A poll consumes its newly observed interval, including omitted text, without replaying the middle in later responses.
Retained raw cell logs are flushed at response cuts and remain accessible during evaluation.
Their paths are relative to the server's launch directory when its `.agents/console` exists, or absolute under its Console home directory (`~/.agents/console` by default, or `MCP_CONSOLE_HOME`) otherwise.
For remote and container targets, both locations are on the controller.
Reading these logs requires a filesystem tool that can access that location and does not change polling state.
See [output and errors](BUILTIN_RUNTIME.md#output-and-notices) for preview, retention, and loss reporting.
