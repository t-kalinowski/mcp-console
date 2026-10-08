# Architecture

The shared runtime coordinator supports independent R and Python startup on [Windows](WINDOWS.md) for local sandboxed or unsandboxed sessions, including managed dependency resolution through the shared `resolve` subcommand with `ir` and `uv` materializing environments on the host.
Windows uses native pipe/event/process primitives, and resolvers and Python inspection enter kill-on-close Jobs while suspended, before executing code; cancellation and normal completion require confirmed empty Jobs.
These Jobs own trusted host preparation and inspection processes, not evaluated user code, and are not sandboxes.
Windows SQL is deferred.

Console separates session management from code execution.
The server owns what survives a worker; the worker owns live language state.
A relay connects them, and the native runner enforces the execution boundary.
See the [glossary](GLOSSARY.md) for terms used below.

## Process layout

Default local execution:

```text
MCP client
  │ MCP over stdio
  ▼
server ───── native runner → resolve → dependencies   resolver sandbox
  │
  ▼
sandbox frontend → private native runner            same PID on Unix
  │
  ▼
relay ───── worker                                  sandboxed workload
            └─ R, Python, SQL
```

The frontend verifies the installed companion and passes immutable launch configuration.
Unix replaces the frontend with the runner; Windows waits for its exit and uses native owner handles.
Native enforcement, private storage, and descendant supervision belong to that runner, not to Console's relay.

All Console processes run on the local host.
The MCP client and its shell and filesystem tools should run on that same host.
For remote work, run the client and Console together in the chosen environment; deployment and its outer lifecycle belong to the client or deployment tooling.

`serve --no-sandbox` skips native enforcement.
Direct host execution has no runner-provided descendant cleanup.
See [sandbox limits](SANDBOX.md#supported-hosts-and-lifetime-limits).

## Ownership

| Component         | Owns                                                                                  | Does not own                                                  |
| ----------------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| Server            | Session, admission, generations, retained requirements, response delivery, recordings | Interpreter execution or native supervision                   |
| Preparation owner | Execution-host discovery, resolver processes, confirmed resolver cleanup              | Accepted session manifest or worker activation                |
| Native runner     | Its enforcement and resource-retirement contract                                      | MCP operations or language state                              |
| Relay             | Worker descriptors, stream translation, signals, direct-worker shutdown and reaping   | Session policy, response budgets, or process-tree enforcement |
| Worker            | Interpreter state, cell evaluation, input, semantic output, environment activation    | Durable session state or MCP response ownership               |

The server-relay transport is [JSONL](RELAY_PROTOCOL.md).
The relay-worker [sideband](WORKER_PROTOCOL.md) carries commands and semantic events separately from interactive stdin and direct stdout/stderr.
Each producer preserves its own order; observation order does not reconstruct chronology across streams.

## Startup and runtime ownership

The server captures launch configuration and constructs tool presentation before runtime discovery.
One connection-owned background task discovers capabilities, prepares defaults, and prelaunches the built-in worker through transport readiness.
The worker then initializes enabled R and Python on its serialized interpreter thread, after input, resolver, and output services are connected.
MCP initialization, tool discovery, and pings do not wait for it.
Failed initial discovery/preparation can be retried only by explicit restart, under that same connection owner and after confirmed preparation cleanup.
That confirmation includes closing the preparation connection and reaping its child; a completed resolver operation alone cannot authorize another attempt.
The owner retains its captured initializer, replaces only the failed readiness attempt, and shares the new attempt among concurrent restart callers.
Cells capture readiness at admission, so replacing a failed attempt cannot revive a rejected cell.
Accepted configuration and post-acceptance worker recovery retain their existing ownership.
Custom workers remain lazy.
The captured public `languages` selection governs presentation and source-argument admission only; it is not forwarded into worker runtime configuration.
Hidden source keys are rejected before same-call control, preparation, or stdin effects.
Configured language fields stay visible even when a runtime is unavailable; execution validates discovered capabilities.
SQL provider routing uses actual R capability independently of public visibility, so hidden interpreters can implement SQL.

Early cells reserve the ordinary evaluation slot while startup finishes.
There is no cell queue.
A call's observation deadline includes that wait; timeout or request cancellation does not cancel admitted evaluation or shared startup.
Connection closure cancels startup through the existing preparation and worker owners and waits for their cleanup contract.
The preparation close barrier joins its owner thread after protocol closure and child reaping, so the server cannot exit with resolver cleanup still in flight.
If the owner failed, closing retains its protocol error instead of replacing it with a generic stopped-owner error.
A failed close handshake kills the preparation child before joining I/O and reaping, without starting a second exit allowance.
It joins owned shutdown before retiring relay I/O so the relay can stop and reap its direct worker.
Retirement cancels an in-flight Python bootstrap inspection without reporting that cancellation as a Python setup error.

An unused worker can be replaced during preparation, but an incomplete native R initialization cannot authorize another startup attempt.
User input or evaluation consumes the unused-worker replacement exception.
Preparation reserves an ordered bootstrap-callback barrier before acquiring the environment.
Once a callback is deferred, later output, images, and input events from that worker sideband wait behind it; independent stdout, stderr, and retirement observations remain responsive.
Deferred events use a private temporary spool capped at 16 MiB and removed when its last handle closes; the dispatcher resumes them in order before accepting later sideband events.
Exceeding that limit or a spool I/O failure fails the worker boundary and releases the spool; retirement also releases it.
Failed preparation resumes the current bootstrap; successful replacement confirms old-worker retirement before starting its successor.
The accepted first cell retains its admission and is never replayed.

Worker readiness is not interpreter initialization.
One coordinator initializes enabled interpreters and runs cells on a single owning thread.
After runtime attachment, bootstrap owns a graphics scope without marking user code active; configured Console startup output and plots use the ordinary output tape.
Native R startup precedes Console's plot device and uses R's native graphics device.
Enabled SQL opens its managed connection during bootstrap when its optional provider is installed; first-query work remains lazy.
Custom workers retain lazy initialization, and an absent provider can be prepared on later SQL demand.
An explicit or host-resolved Python selection can start without R.
Unresolved R-side selection hints use R's compatibility adapter when installed; its absence does not prevent bare R use.
Background selection also permits an ordinary absent-interpreter discovery result, preserving R without treating selection errors as absence.
Later R cells, Python's R bridge, and R-owned SQL enter R through the same facade.
Failed or interrupted native R initialization requires an explicit worker restart; it is never retried in place.
Console owns CPython bootstrap and services; reticulate supplies R selection compatibility and object conversion.
Attaching the bridge must use the running interpreter identity, not select or initialize a second Python.
Host inspection remains isolated; after setup, conversion paths and NumPy metadata describe the live interpreter without preparing or importing optional packages.
NumPy metadata comes from an already loaded module or a matching installed distribution; a shadowing workspace module/package is treated as absent.
Missing or unusable optional distribution metadata is also treated as absent.

The coordinator owns command dispatch, cell bookkeeping, input, and completion.
Language adapters own their runtime-specific event, graphics, error, and unwind boundaries.
Signal handlers only mark native state and wake waiters; they never enter an interpreter.
Keep R affinity, GIL ownership, and reentrant callbacks on the current thread model.
Do not split evaluators across threads without a new ownership design.
[Runtime limitations](BUILTIN_RUNTIME.md#current-limitations) include the remaining late-R-startup environment constraint.

SQL routes to an R DBI or Python DB-API provider.
R capability selects the default managed provider independently of initialization order; without R, Python owns the managed DuckDB connection.
An optional captured startup source runs on its owning interpreter after helpers and runtime setup, before cell dispatch; it selects a native connection without managed warmup.
The worker retains failed startup admission, and the server refuses automatic replay after a configured launch; only explicit restart authorizes another attempt after confirmed retirement.
Explicitly selected connections remain user-owned.
The [runtime guide](BUILTIN_RUNTIME.md) owns connection and interoperability rules.

## Generations and operations

Every evaluation, stdin write, resolver callback, control target, and environment commit belongs to the generation that admitted it.
Work from an old generation must never reach or commit into its replacement.

A normal cell is admitted once, prepared if needed, and dispatched through the relay.
A controlled send retains admission across control and reservation of its optional following cell.
Same-call stdin and code after restart belong only to the replacement.
After interrupt, they cannot silently migrate to a different generation.
See [`send` ordering](SEND_OPERATIONS.md) for validation and failures.

Restart resolves a changed candidate before retiring the old worker.
Resolution failure preserves the old worker; failure after an accepted manifest commit does not roll that commit back.
Successful replacement starts fresh interpreters and database state with the server's retained requirements.

An established worker failure permits one automatic replacement attempt for the call.
The failed cell and stdin are **not replayed**.
Explicit and failure-driven replacement both respect retirement barriers.
Native R startup reports its admission and completion through the generation-owned worker protocol.
A failed or interrupted R initialization withholds automatic replacement until explicit restart authorizes another attempt.
R's own surviving profile errors remain ordinary diagnostics.

## Preparation and activation

Local dependency resolution runs in a separate native resolver sandbox on macOS and Linux.
Its cache and download policy is independent of the worker policy; see [resolver configuration](RESOLVER.md).
The hidden `resolve` command owns local preparation over a private JSONL connection.
Its client and child run from the same Console executable, so their schema is unversioned.

The server owns the accepted manifest and candidate transactions.
The preparation owner returns a result only after resolver cleanup.
For live changes, the worker checks and activates a provisional environment, then publishes acceptance.
The server commits only the matching candidate from the current generation.
An accepted activation survives a later import or cell error; an unactivated stale candidate does not.
Unsafe partial activation can require restart.

Explicit preparation and worker-originated requests share environment-change ownership, preventing a stale preparation result from overwriting a newer manifest.
Automatic R loads and Python imports request packages only when execution reaches them; cells are not scanned or rerun.

Transactions protect accepted environments; the resolver sandbox bounds preparation permissions.
Its default host reads and Console-specific writable caches still require trusted dependencies and inputs.
[Requirements](REQUIREMENTS.md) defines supported changes and the trust boundary.

## Retirement and cancellation

Each worker launch owns one retained retirement operation.
Lifecycle reserves its original budgets while holding admission, then releases that mutex before command writes, preparation cancellation, process observation or task joins.
Restart, EOF, startup failure and failed-worker recovery observe the same request and terminal cleanup/I/O result.
They send Shutdown once and join every owned transport task once; a late startup registration retires through its launch's owner before startup completion permits connection shutdown to finish.
Publishing a failed-worker transition checks that its generation still owns it.
If restart takes over during failed-worker retirement, the retiring caller keeps the captured process outcome in the old generation's output region for response settlement.

The operation keeps normal command/barrier failure separate from physical cleanup and the dispatcher outcome.
Confirmed physical cleanup and joined tasks can supersede a failed normal barrier; failed native cleanup still blocks replacement.
Launcher reaping and successful I/O settlement also supersede that barrier when launcher-status or temporary-storage cleanup reports an independent error; the cleanup error remains authoritative.
Process and worker consumers read separate cleanup and I/O views of that retained result, avoiding duplicate diagnostics within one shutdown response.
Restart and EOF check the retiring launch's retained I/O result even when an initial launch failed before readiness, a failed evaluation has already stopped the logical worker or physical cleanup fails.
Failed-worker replacement also requires launcher reaping and confirmed retirement of its owned temporary storage.
Available output is drained even when cleanup fails.
The existing worker, relay, launcher and force-stop allowances are captured once.
Relay drain eligibility uses when the local dispatcher processes ShutdownStarted.
Connection closure passes its original worker deadline into startup cancellation.

The relay bounds shutdown and reaps its direct worker.
Its stream draining must not wait forever for descendants retaining descriptors or for a blocked output consumer.
It does not infer process-tree membership from a process group.

The server's sole command writer owns bootstrap and JSONL serialization for one generation.
Normal retirement queues ordered Shutdown and allows the relay its grace period.
Forced transport retirement closes command admission and independently aborts pending writes or an idle queue wait, then joins the writer.
It does not use stdout closure to decide whether stdin can be retired, and it never inserts a control into a partial frame.
Retirement settles outstanding control receipts; cancelling a call's observation does not redirect its queued interrupt.
Owned output readers preserve their bounded available-output drain even when launcher cleanup fails; joining I/O does not confirm native cleanup.
Worker-client native adapters own endpoint setup and separate output wakeup from command cancellation.
The shared process and generation owners retain exit observation, I/O joins, and retirement decisions.
The preparation owner bounds exit observation after forced termination and reaps only an observed exited child.
If termination fails, it reports unconfirmed retirement and stops diagnostic collection even when the child keeps stderr open.
It retains the child and exit observer in a background reaping owner and retries termination during connection closure, without erasing the original retirement failure.

The server integrates a local native launcher as an ordinary child; successful managed exit is the cleanup barrier.
Unconfirmed retirement blocks replacement.
Worker, relay, and native retirement allowances have different owners; none is a universal end-to-end cleanup deadline.

Connection closure that refuses the next preparation stage is separate from control of a completed operation.
It permits a quiet exit only after the refused stage's cleanup is confirmed.
Preparation retains the operation's terminal result, control cause and cleanup confirmation.
The subprocess owner captures its cause when collection finishes; a later control acknowledgment cannot replace an independent setup failure.
When an interrupted subprocess exits unsuccessfully, its formatted materializer error retains that captured cause and its complete diagnostic.
Unix interruption confirms the leader is stopped or exited before delivering SIGINT, then resumes a stopped leader; a successful signal call alone cannot distinguish a live process from an unreaped zombie.
Preparation that consumes control after successful collection retains that cause before publishing its terminal result.
For multistage preparation, only a subprocess report matching the operation's final result supplies its control cause; cleanup confirmation still includes every stage.
Errors closing the preparation connection remain visible.
Cancellation of a preparation operation does not suppress an independent failure of the preparation connection's close handshake.

Interrupt targets the active resolver, otherwise the current worker.
It is not retried against a replacement.
Each materializer invocation owns its child, non-reaping exit observer, stdin writer, and stdout/stderr readers.
Success, cancellation, registration failure, and process or I/O failure share one retirement path.
Retirement cancels stdin independently and joins every I/O task, including when native process cleanup fails.
Output collection preserves bytes already read plus a finite snapshot of queued bytes per stream after process retirement; it does not wait for inherited descriptors to close or accept an endless final producer.
Failed process cleanup cancels and joins exit observation without signalling or reaping the remaining child.
The original operation failure and cleanup failures remain separate until diagnostic formatting.
Resolver cleanup confirmation combines the native process owner's result with settled observer/I/O tasks; it does not confirm preparation transport or worker retirement.
Unix signals the owned process group and reaps its leader after observation settles; it does not provide a separate empty-group receipt, and escaped descendants remain outside that scope.
Windows retains suspended creation and kill-on-close Job ownership, requires a confirmed empty Job, and shares its existing retirement allowance with exit observation.
On connection closure, the server closes admission, cancels preparation, retires owned execution resources, settles accepted responses, and bounds blocked MCP delivery.
Native runner death has no independent recovery guarantee.

## Output and delivery

The server owns an ordered output tape across generations.
It selects finite output cuts for responses, retains bounded text beginnings and tails, admits images separately, and adds lifecycle notices.
The final text budget is 8 KiB including notices.
Raw-file retention and inline omission are separate; collection stays bounded even when recording fails.
Startup diagnostics use this tape, including preparation and launcher stderr on Unix.
Each diagnostic producer retains its own incomplete UTF-8 scalar until more bytes arrive or that producer closes.
Diagnostic ingestion also preserves pending UTF-8 bytes from the worker's direct streams.
Closing one diagnostic producer does not flush another producer's pending terminal update; response cuts and shutdown finish the shared terminal projection.

One recoverable response remains owned until local delivery or cancellation settles it.
Controlled sends can combine earlier output with a following cell; a failed delivery restores the whole combined region, not just its last part.
A poll already waiting for startup waits for an intervening response's delivery within its remaining observation budget before claiming that evaluation's output.
Expiry reports pending delivery; expiry and cancellation leave the output unclaimed.
This is not exactly-once client observation: cancellation can race with bytes already visible to the client.
A journaled result likewise records assembly, not receipt.

## Recording, cell output, and image artifacts

The controller records calls and assembled output independently of the private protocols.
The journal is authoritative; Markdown and Quarto are projections, not worker checkpoints.
Paths, formats, failure behavior, and rendering safety are covered in [recordings](RECORDING.md).
Startup and idle output can be recorded before a tool call; discovery fills in pending recording metadata without replacing the session owner.
Buffered calls and results retain their original timestamps and precede the discovery event when startup output has already materialized the recording.
Discovery failure retains pending calls and their results alongside the startup failure, with unavailable metadata left unknown.

## Where to look in source

| Concern                                       | Entry point                                                                                                                     |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| MCP and shared startup                        | [`src/server.rs`](../src/server.rs), [`src/server/startup.rs`](../src/server/startup.rs)                                        |
| Session operations and generations            | [`src/worker_client.rs`](../src/worker_client.rs) and its modules                                                               |
| Relay transport and direct-worker supervision | [`src/worker_relay.rs`](../src/worker_relay.rs)                                                                                 |
| Language coordination                         | [`src/worker/coordinator.rs`](../src/worker/coordinator.rs), [`src/python.rs`](../src/python.rs), [`src/sql.rs`](../src/sql.rs) |
| Host preparation                              | [`src/resolver/preparation.rs`](../src/resolver/preparation.rs)                                                                 |
| Local launch ownership                        | [`src/worker_client/process.rs`](../src/worker_client/process.rs), [`src/sandbox.rs`](../src/sandbox.rs)                        |
| Recording                                     | [`src/transcript.rs`](../src/transcript.rs)                                                                                     |

Follow these owners into their modules rather than maintaining a parallel file inventory in prose.
Public evidence is organized by the [tested process boundaries](../tests/boundaries/README.md).
