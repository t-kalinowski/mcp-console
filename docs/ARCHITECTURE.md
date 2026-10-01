# Architecture

Console separates session management from code execution.
The server owns what survives a worker; the worker owns live language state.
A relay connects them, and a native runner or compute provider enforces the execution boundary.
See the [glossary](GLOSSARY.md) for terms used below.

## Process layout

Default local execution:

```text
MCP client
  │ MCP over stdio
  ▼
server ───── resolve ───── dependency resolvers       trusted host
  │
  ▼
sandbox frontend → private native runner            same PID
  │
  ▼
relay ───── worker                                  sandboxed workload
            └─ R, Python, SQL
```

The frontend verifies the installed companion and execs it with immutable launch configuration.
Native enforcement, private storage, and descendant supervision belong to that runner, not to Console's relay.

[SSH](SSH.md) places the launcher, relay, worker, and preparation owner on the remote host.
[Docker](DOCKER.md) uses a captured image and a fresh container for each generation.
[Docker Sandbox](DOCKER_SANDBOX.md) uses an owned microVM from a prepared template, with provider-managed enforcement and no inner native runner.
Admission, requirements, output, and recordings remain on the controller.

`serve --no-sandbox` skips native enforcement at the selected target; it does not remove a Docker container or microVM boundary.
Direct host execution has no runner-provided descendant cleanup.
See [sandbox limits](SANDBOX.md#supported-hosts-and-lifetime-limits).

## Ownership

| Component                     | Owns                                                                                  | Does not own                                                  |
| ----------------------------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| Server                        | Session, admission, generations, retained requirements, response delivery, recordings | Interpreter execution or native supervision                   |
| Preparation owner             | Execution-host discovery, resolver processes, confirmed resolver cleanup              | Accepted session manifest or worker activation                |
| Native runner / compute owner | Its enforcement and resource-retirement contract                                      | MCP operations or language state                              |
| Relay                         | Worker descriptors, stream translation, signals, direct-worker shutdown and reaping   | Session policy, response budgets, or process-tree enforcement |
| Worker                        | Interpreter state, cell evaluation, input, semantic output, environment activation    | Durable session state or MCP response ownership               |

The server-relay transport is [JSONL](RELAY_PROTOCOL.md).
The relay-worker [sideband](WORKER_PROTOCOL.md) carries commands and semantic events separately from interactive stdin and direct stdout/stderr.
Each producer preserves its own order; observation order does not reconstruct chronology across streams.

## Startup and runtime ownership

The server captures launch configuration and constructs tool presentation before runtime discovery.
One connection-owned background task discovers capabilities, prepares defaults, and prelaunches the built-in worker through transport readiness.
The worker then initializes enabled R and Python on its serialized interpreter thread, after input, resolver, and output services are connected.
MCP initialization, tool discovery, and pings do not wait for it.
Custom workers remain lazy.
Configured language fields stay visible even when a runtime is unavailable; execution validates discovered capabilities.

Early cells reserve the ordinary evaluation slot while startup finishes.
There is no cell queue.
A call's observation deadline includes that wait; timeout or request cancellation does not cancel admitted evaluation or shared startup.
Connection closure cancels startup through the existing preparation/provider owners and waits for their cleanup contract.
It joins owned shutdown before retiring relay I/O so the relay can stop and reap its direct worker.

Initialization alone does not consume the unused-worker replacement exception.
Preparation reserves an ordered bootstrap-callback barrier before acquiring the environment.
Once a callback is deferred, later output, images, and input events from that worker sideband wait behind it; independent stdout, stderr, and retirement observations remain responsive.
Failed preparation resumes the current bootstrap; successful replacement confirms old-worker retirement before starting its successor.
The accepted first cell retains its admission and is never replayed.

Worker readiness is not interpreter initialization.
One coordinator initializes enabled interpreters and runs cells on a single owning thread.
Bootstrap owns a graphics scope without marking user code active; startup output and plots use the ordinary output tape.
Managed SQL connections and first-query work remain lazy.
An explicit or host-resolved Python selection can start without R.
Unresolved R-side selection hints use R's compatibility adapter when installed; its absence does not prevent bare R use.
Background selection also permits an ordinary absent-interpreter discovery result, preserving R without treating selection errors as absence.
Later R cells, Python's R bridge, and R-owned SQL retry incomplete initialization through the same facade.
Console owns CPython bootstrap and services; reticulate supplies R selection compatibility and object conversion.
Attaching the bridge must use the running interpreter identity, not select or initialize a second Python.

The coordinator owns command dispatch, cell bookkeeping, input, and completion.
Language adapters own their runtime-specific event, graphics, error, and unwind boundaries.
Signal handlers only mark native state and wake waiters; they never enter an interpreter.
Keep R affinity, GIL ownership, and reentrant callbacks on the current thread model.
Do not split evaluators across threads without a new ownership design.
[Runtime limitations](BUILTIN_RUNTIME.md#current-limitations) include the remaining late-R-startup environment constraint.

SQL routes to an R DBI or Python DB-API provider.
R capability selects the default managed provider independently of initialization order; without R, DuckDB is created lazily in Python.
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

## Preparation and activation

Dependency resolution runs outside the worker sandbox, on the execution host.
Local sessions use the hidden `resolve` command; SSH has a remote preparation connection.
Prepared Docker/SBX targets use preinstalled environments and never invoke controller or target dependency resolvers.

The server owns the accepted manifest and candidate transactions.
The preparation owner returns a result only after resolver cleanup.
For live changes, the worker checks and activates a provisional environment, then publishes acceptance.
The server commits only the matching candidate from the current generation.
An accepted activation survives a later import or cell error; an unactivated stale candidate does not.
Unsafe partial activation can require restart.

Explicit preparation and worker-originated requests share environment-change ownership, preventing a stale preparation result from overwriting a newer manifest.
Automatic R loads and Python imports request packages only when execution reaches them; cells are not scanned or rerun.

These are transaction boundaries, **not a resolver sandbox**.
Accepted package builds and worker-writable resolver inputs can execute with host permissions.
[Requirements](REQUIREMENTS.md) defines supported changes and the trust boundary.

## Retirement and cancellation

The relay bounds shutdown and reaps its direct worker.
Its stream draining must not wait forever for descendants retaining descriptors or for a blocked output consumer.
It does not infer process-tree membership from a process group.

The server integrates a local native launcher as an ordinary child; successful managed exit is the cleanup barrier.
SSH and compute generations carry explicit retirement receipts: transport exit alone does not prove remote processes, containers, or microVMs are gone.
Unconfirmed retirement blocks replacement.
Provider setup, command, and retirement allowances have different owners; none is a universal end-to-end cleanup deadline.

Interrupt targets the active resolver, otherwise the current worker.
It is not retried against a replacement.
On connection closure, the server closes admission, cancels preparation, retires owned execution resources, settles accepted responses, and bounds blocked MCP delivery.
Native runner death has no independent recovery guarantee.

## Output and delivery

The server owns an ordered output tape across generations.
It selects finite output cuts for responses, retains bounded text beginnings and tails, admits images separately, and adds lifecycle notices.
The final text budget is 8 KiB including notices.
Raw-file retention and inline omission are separate; collection stays bounded even when recording fails.

One recoverable response remains owned until local delivery or cancellation settles it.
Controlled sends can combine earlier output with a following cell; a failed delivery restores the whole combined region, not just its last part.
This is not exactly-once client observation: cancellation can race with bytes already visible to the client.
A journaled result likewise records assembly, not receipt.

## Recording, cell output, and image artifacts

The controller records calls and assembled output independently of the private protocols.
The journal is authoritative; Markdown and Quarto are projections, not worker checkpoints.
Paths, formats, failure behavior, and rendering safety are covered in [recordings](RECORDING.md).

## Where to look in source

| Concern                                       | Entry point                                                                                                                     |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| MCP and shared startup                        | [`src/server.rs`](../src/server.rs), [`src/server/startup.rs`](../src/server/startup.rs)                                        |
| Session operations and generations            | [`src/worker_client.rs`](../src/worker_client.rs) and its modules                                                               |
| Relay transport and direct-worker supervision | [`src/worker_relay.rs`](../src/worker_relay.rs)                                                                                 |
| Language coordination                         | [`src/worker/coordinator.rs`](../src/worker/coordinator.rs), [`src/python.rs`](../src/python.rs), [`src/sql.rs`](../src/sql.rs) |
| Host preparation                              | [`src/resolver/preparation.rs`](../src/resolver/preparation.rs)                                                                 |
| Target ownership                              | [`src/target_session.rs`](../src/target_session.rs), [`src/sandbox.rs`](../src/sandbox.rs)                                      |
| Recording                                     | [`src/transcript.rs`](../src/transcript.rs)                                                                                     |

Follow these owners into their modules rather than maintaining a parallel file inventory in prose.
Public evidence is organized by the [tested process boundaries](../tests/boundaries/README.md).
