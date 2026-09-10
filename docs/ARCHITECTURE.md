# Implemented architecture

**Status:** Current implementation

This document describes the structure and ownership implemented in the current source.
It explains how the pieces fit together without defining their wire fields or the built-in console's operating rules.
See the [relay protocol](RELAY_PROTOCOL.md) and [worker protocol](WORKER_PROTOCOL.md) for exact transports, the [built-in runtime guide](BUILTIN_RUNTIME.md) for console behavior, and [requirements and environments](REQUIREMENTS.md) for dependency management and its trust boundary.
The material under `design-sketches/` is future or exploratory design, not evidence for this document.

## Process layout

For default sandboxed execution, the public sandbox frontend execs the private runner in the same PID:

```text
MCP client
    │ MCP JSON-RPC over stdio
    ▼
mcp-console server                         host
    ├── generation, relay lifetime, and operation owner
    ├── retained environments, output, and recording
    ├──── host resolvers                    R, Python, and DuckDB setup
    │
    │ ordinary child stdin/stdout; inherited stderr
    ▼
mcp-console sandbox → private runner        same PID, host supervisor
    │ immutable launch-time policy and lifecycle configuration
    │ native enforcement and target release
    ▼
worker relay                               sandbox
    │ worker sideband plus standard streams
    ▼
worker                                     same sandbox
    └── built-in R, Python, and DuckDB runtime
```

On macOS, the runner's native child applies Seatbelt and execs the relay in place.
That relay leads the target process group; an explicit relay wrapper can instead lead the group and launch the relay below it.
Linux retains native namespace-init and bubblewrap helper processes.
Those native processes and all sandbox descendant supervision belong to the runner executable.

A standalone `mcp-console sandbox -- COMMAND [ARG]...` uses the same frontend-to-runner exec, with the requested command in place of the relay and worker.
No waiting Console adapter remains.
The runner sees the original caller as its direct parent and inherits the original standard streams.
The [sandbox integration](SANDBOX.md) defines Console policy defaults, installation verification, supported hosts, and lifetime limits.
The [migration record](SANDBOX_RUNNER_INTEGRATION.md) records the baseline, validation, and changed lifetime guarantees.

With `serve --no-sandbox`, the server launches the relay directly with host permissions and the host temporary-directory environment.
R, Python, and DuckDB dependency resolution always runs in separate host processes; [requirements and environments](REQUIREMENTS.md) defines its trust boundary.

## Communication boundaries

### MCP client and server

The client and server exchange MCP JSON-RPC over the server's standard input and output.
The server registers only the `send` tool.
The MCP adapter in `src/server.rs` decodes arguments, applies language filtering, and translates responses into MCP text and image content.
The session coordinator in `src/worker_client.rs` validates requirements and interprets every `send` combination, including standalone preparation.
One `send` can poll, provide stdin, prepare requirements, evaluate a cell, interrupt, restart, or combine compatible parts under one ordered operation.
[`TOOL_DESCRIPTIONS.md`](TOOL_DESCRIPTIONS.md) gives editorial guidance, and the [canonical handshake snapshot](../tests/snapshots/client_server/server/test_tools/initializes_and_lists_tools.yaml) records the registered descriptions; `src/server.rs` and the actual `tools/list` result are authoritative.

This is the only public protocol boundary.
The client does not communicate directly with a relay, worker, or resolver.

### Sandbox frontend and private runner

The private `mcp-console-sandbox` executable contains the extracted native sandbox implementation and is pinned by source revision in `sandbox-runner.json`.
The Python packaging backend prepares the pinned source in a dedicated checkout under `target`, strips the distributed runner, and records companion digests before invoking the application's Cargo build.
Cargo consumes that prepared bundle; source installations invoke the runner's Cargo build on each run and reuse its normal build intermediates.
macOS and Linux wheels install a relocatable bundle with `bin/mcp-console`, `libexec/mcp-console-sandbox`, and notices under `share/licenses/mcp-console`.
Linux bundles also include `libexec/bwrap` and its license.
The frontend resolves these companions relative to its canonical executable path and verifies their SHA-256 digests with a bounded buffer on every sandbox launch.
Missing or modified companions are an installation error.
The main executable has no embedded runner payload, extraction step, or runtime runner cache.
The runtime does not access the source checkout or download the runner.

The frontend supplies immutable environment configuration through the runner's explicit `--config-env` interface.
Only policy and lifecycle choices enter this JSON object.
Command arguments, cwd, environment, and standard streams remain ordinary process-launch inputs.
The runner consumes the selected configuration variable and owns all subsequent native setup, target signal restoration, descendant retirement, and private storage.
Console has no setup pipe, target wrapper, manager socket, recovery monitor, or sandbox-owned path state.

### Server and relay

The server sends commands to the relay's standard input and receives JSONL events from the relay's standard output.
Relay standard error is inherited separately and is not part of that protocol.
The server's ordinary child-exit observer also wakes the relay reader.
On launcher exit, that reader drains already-queued bytes and ends the generation even if an unsupervised descendant retains stdout.
The server joins the reader and dispatcher before replacement; a reaped launcher permits replacement while any cleanup failure is still reported.
The transport is private and keeps worker connections and direct-worker supervision in the relay, inside the sandbox by default.
[`RELAY_PROTOCOL.md`](RELAY_PROTOCOL.md) defines its commands, events, framing, and retirement behavior.

### Relay and worker

The relay launches the worker with standard input, standard output, standard error, and a dedicated sideband.
Complete cells and other worker-protocol messages travel over the sideband, interactive input uses worker fd 0, and direct worker output remains on fd 1 and fd 2.
Direct-worker signals and supervision remain relay responsibilities.
[`WORKER_PROTOCOL.md`](WORKER_PROTOCOL.md) defines the launch descriptors, framing, messages, ordering rules, closure behavior, and custom-worker contract.

## Responsibility boundaries

### Server

The server owns the logical console session and all state that must survive a worker process:

- MCP tool admission and validation;
- worker lifecycle and generation ownership;
- relay-generation launch and retirement through an ordinary child process;
- retained R, Python, and DuckDB requirements;
- host resolver launch, interruption, cancellation, and result commits;
- evaluation, preparation, stdin, inline control, restart, and replacement admission;
- the pending output tape, output budgets, response boundaries, and MCP response assembly;
- response-delivery ownership; and
- transcript recording and image artifacts.

These responsibilities remain on the host side of the sandbox boundary.
The server does not execute submitted cells or ask the relay to interpret MCP calls.
It configures the child's piped standard input and output and inherited standard error, closes unrelated inherited descriptors before exec, and knows normal child exit and signaling, but no private directory, startup gate, sandbox root, manager, or manager monitor.
At generation retirement, it first requests graceful shutdown through the relay protocol and waits through the applicable relay deadline.
In sandboxed mode, it then sends `SIGTERM` to the launcher to request managed retirement and uses a hard launcher kill only as the final fail-safe.
On normal and owned-retirement paths, the server treats successful managed launcher exit as the synchronous cleanup barrier before reaping.
Cancellation before worker readiness follows the same runner-retirement request and grace period, including when the startup I/O join reaches the child first.
With `--no-sandbox`, the server owns and reaps the relay directly; no runner supplies descendant cleanup.
After the relay deadline, the server accepts termination from its own successful `SIGTERM` request as completed direct retirement.

### Sandbox frontend and runner

The frontend selects application policy and verifies the installation, then execs the runner.
It never writes protocol data to stdout or waits for a target process.
The runner captures configured caller identity, applies native enforcement, releases the target, restores its inherited signal state, and owns descendant and directory retirement.
A successful runner exit is the server's synchronous cleanup barrier.
Supervisor death has no independent recovery guarantee.
The server retains only ordinary child signaling, exit observation, and reaping around this boundary.

### Relay

The relay is a thin ordered transport and worker supervisor.
It owns the worker's local descriptors, translates applicable relay commands to worker-sideband messages, forwards worker observations, delivers signals, bounds shutdown, drains streams, and reaps the direct worker.
Two anonymous pipes carry the sideband in opposite directions, separately from stdin, stdout, and stderr.
Their nonblocking reads and writes use readiness waits with explicit cancellation wakeups.
Each producer encodes its observations as JSONL frames before enqueueing them, and one relay writer emits those frames in queue order.
Its FIFO bounds admitted encoded payload bytes and event count, including the write in progress, and reserves space for supervisor events.
It accepts an oversized frame alone on the ordinary budget and pauses output readers until capacity is available.
This preserves each producer's order without reconstructing chronology across the independent sideband, stdout, and stderr transports.

The relay does not own the logical session, retained requirements, evaluation admission, server pending-output budgets, response assembly, or MCP delivery.
It exits with the worker lifetime it supervises.
In sandboxed mode, remaining descendants, including those retaining worker streams, are retired by the sandbox launcher after the target exits or retirement is requested.
Its cancellable local transports share a 100-millisecond allowance for additional nonblocking reads during retirement.
They attempt to queue complete buffered sideband frames but may abandon incomplete frames and further descendant output, so draining does not depend on those descendants becoming quiet or closing their descriptors.
After direct-worker retirement, the relay gives pipe and socket output one shared second to flush, starting before local I/O joins.
That deadline also wakes readers waiting for queue space; abandoned output fails the transport.
Blocked downstream pipe or socket output can therefore fail retirement without delaying worker shutdown; [the relay protocol](RELAY_PROTOCOL.md#retirement-and-failure) defines delivery and descriptor limits.

The internal `worker-relay` command uses the same stream protocol when launched directly without a sandbox or below another process wrapper.
Such a direct invocation owns only its direct worker; it supplies no sandbox policy or descendant-cleanup guarantee.
`serve --no-sandbox` selects this direct launch while retaining the relay protocol and direct-worker shutdown behavior.
A replacement sandbox launcher must provide the process-lifetime contract described in [sandbox integration](SANDBOX.md), including cleanup before successful owned retirement.
Any future sandbox-specific control plane ends at that launcher, without reaching the relay or changing its protocol.

### Worker

The worker owns language-runtime state and implements the worker protocol.
It reports readiness, accepts complete cells and supported preparation operations, consumes interactive stdin, publishes console events and images, and reports completion or failure through the sideband.

The built-in worker's `worker::core` owns shared sideband state, deferred operation messages, resolver exchanges, output publication, and shutdown and failure state.
The `worker::embedded_r` backend owns interpreter initialization, event handling, interactive input, interrupts, and language dispatch, including suppression of R resolution during SQL callbacks.

The built-in worker embeds R on its main thread.
On Linux, it re-executes before R initialization with the selected `R_HOME/lib` first in `LD_LIBRARY_PATH`, preserving inherited library paths and its sideband endpoint.
This lets native R packages resolve R's shared libraries even when that R installation is absent from the system linker cache.
Its language adapters provide persistent Python and SQL within that worker process.
The SQL router uses a DBI provider in embedded R or a DB-API provider in CPython.
The R provider owns a managed DuckDB connection by default and can retain a user-selected DBI connection; the Python provider retains a user-selected DB-API connection without converting it or its result rows through reticulate.
Its private R environment bridge conditionally wraps `base::library` and runs R's unchanged `base::loadNamespace` body in a private lexical environment that intercepts its retry restart; it applies accepted managed libraries and reports activation outcomes.
The Rust Python facade loads, retains, and initializes the selected file-backed `libpython`, or attaches its own handle if CPython was already initialized.
It embeds and installs the private evaluator and DB-API adapter through that CPython API; reticulate attaches to the interpreter and continues to own object conversion, Python-cell evaluation dispatch, its manifest, event handling, and interrupts.
Its private Python runtime conditionally appends a last-chance import finder, while the R Python bridge owns the reticulate manifest and the callback into the existing managed-Python resolver.
Bare sessions leave both resolution adapters disabled.
Their user-visible behavior belongs in the [built-in runtime guide](BUILTIN_RUNTIME.md), while the sideband contract remains independent of the interpreter implementation.

## Worker generations

Generation ownership belongs to the server.
The server captures the current generation when it admits an evaluation, stdin write, resolver callback, preparation, interrupt target, or retained-environment commit.
The operation can affect only the lifecycle generation that accepted it.

An explicit restart advances admission to a new generation before the old relay and worker finish retirement.
Work still completing for the retiring generation is either settled for that generation or discarded according to its existing lifecycle contract; it cannot be forwarded to the replacement or commit state on its behalf.
The replacement receives new worker transports and fresh language-runtime state, while the server supplies the retained environment selected for it.

A controlled `send` keeps one admission boundary from control through reservation of its optional new cell.
After restart, same-call stdin and code belong only to the replacement generation.
After interrupt, the server verifies that the interrupted generation is still current before it reserves the new cell.
Another lifecycle transition cannot enter between successful replacement or interruption and that reservation.

An unexpected worker or transport failure also retires that relay and worker before replacement.
The server reports the failed operation and does not replay its cell or stdin against the new worker.

## Lifecycle

### Server and worker startup

The built-in server first captures a stable host resolver configuration and detects its capability without installing an environment.
It prefers `ir` on `PATH`, otherwise selects `uv` on `PATH` or an explicit `uv` path, and can obtain `uv` from reticulate when only `ir` or an ambient R installation is available.
It retains the selected bootstrap as pending setup and accepts MCP input before invoking it or resolving the default R, DuckDB, and managed Python environments.
An operation that first needs an environment resolves the defaults through the normal generation-owned resolver lifecycle and commits the complete candidate only after all preparation succeeds.
For an ordinary cell, this happens after evaluation admission, so the client can poll or interrupt preparation.
Explicit requirements remain preconditions of evaluation and combine their additions with the pending defaults.
If no resolver bootstrap is available, it accepts MCP input with an empty retained environment and a fixed bare capability that disables later dynamic resolution.
The worker itself starts lazily when an operation first needs it; preparing retained requirements can happen without launching a worker.
An explicit restart starts its replacement eagerly, including when the session had not started a worker before.

For each worker start, the server first constructs the relay target independently of sandboxing.
The built-in target is the current executable's `worker-relay` command followed by the worker command line; a configured relay is followed directly by the same worker command line.
By default, the server then constructs an ordinary current-executable command for `sandbox --exit-with-parent <server-pid> -- <relay-target>`; with `--no-sandbox`, it uses the relay target directly.
It applies the retained environment and configures piped input and output plus inherited error in either mode.
In sandboxed mode, the frontend execs the runner with that environment and those streams; the runner establishes native enforcement and descendant observation before releasing the relay.
The relay creates the worker sideband and standard streams, launches the worker, and forwards its startup events.
The server admits the worker only after the required readiness exchange succeeds.
If sandbox setup fails before relay readiness, the launcher writes the detailed infrastructure error to inherited standard error and exits; the server reports a stable relay-startup failure from the closed transport.

### Evaluation

The server admits one cell against the current generation, starts a worker if needed, and registers the operation before sending it through the relay.
Worker sideband output and direct fd output are published to the server-owned output tape as the relay observes them.
The server waits, polls, or completes the MCP response without moving response ownership into the relay or worker.

When a code-bearing `send` declares requirements, the server treats them as preconditions of that evaluation.
One exclusive environment transition covers requirement-delta calculation, host resolution, live preparation or a pre-start retained-environment commit, and reservation and launch of the evaluation in the same generation.
No other send or environment-changing operation can enter that boundary, and a failed or superseded transition cannot dispatch the cell.
The server releases the environment transition after launch; the active evaluation continues to own stdin, waiting, output cuts, response delivery, and restart handoff.

The [send operation-order reference](SEND_OPERATIONS.md) defines user-visible ordering, validation, and wait timing for all call combinations.

### Controlled send

Control, stdin, interrupt grace, requirement preparation, and reservation of the optional new cell form one lifecycle operation.

Interrupt routes to an active resolver first, otherwise to the worker, and preserves ownership of the interrupted evaluation's response through handoff.
Restart resolves and commits declared requirements before retirement; a resolution failure leaves the old worker in place, while later replacement failure does not roll back the retained commit.
After successful replacement startup, admission remains reserved through same-call stdin and cell dispatch, so another generation cannot receive them.

### Worker-originated R resolution

Automatic R resolution is a callback from the running built-in worker, not an idle preparation operation.
The `library()` wrapper and the managed `loadNamespace()` retry handler issue callbacks only when evaluation reaches a missing package load; the worker does not inspect the cell in advance.
The relay only translates the callback messages and preserves their transport order.

The server atomically assigns environment-change ownership to either an idle runtime R callback or explicit environment preparation.
An admitted callback keeps that ownership through activation or failure, so preparation cannot resolve and later commit a stale retained-environment snapshot.
If the callback already owns the transition, preparation returns a nonfatal tool error; if preparation reserved it first, an otherwise idle callback receives an ordinary host failure.
A runtime R callback sent after live preparation begins is a protocol failure.

For a request, the server verifies the worker generation and validates the supplied plain package names.
It serializes access to the retained environment and host resolver, merges the names into the complete retained R requirement set, and returns the existing managed environment without invoking `ir` when that set is unchanged.
Otherwise it resolves the complete candidate on the host and prepares every retained DuckDB extension for that candidate library.
The server rechecks the generation and returns the candidate path without committing it.

The worker applies the candidate through its managed `.libPaths()` bridge, then reports either activation or activation failure.
On `RActivated`, the server matches the exact uncommitted candidate and commits it only when the reporting generation is still current and ready.
That commit updates both the retained R environment and the DuckDB R-library target history.
The original R package operation resumes after the receipt, so later namespace or cell failure does not roll back a successfully accepted environment.

On `RActivationFailed`, the server discards the candidate and marks requirement changes as restart-required for the same current generation.
An unchanged restart or shutdown cancels an active resolver.
A restart that adds requirements serializes behind active environment resolution before replacing the worker; generation checks prevent any unactivated old candidate from committing into the replacement.
Ordinary resolver and activation errors leave an otherwise healthy worker available; transport, protocol, or bridge-infrastructure failure follows the existing worker-failure lifecycle.

### Worker-originated Python resolution

Automatic Python resolution is also a callback from an active built-in worker.
The private finder runs only after Python's existing import finders have failed, so available standard-library, local, and installed modules do not enter this path.
It also yields without a callback for optional-dependency misses reached while the default NumPy or pandas package is initializing, so importing those available defaults does not change the managed environment.
It derives one bare distribution from the top-level import through a curated mapping or a conservative same-name fallback; the server validates that name through the existing managed-Python requirement validator.

The Python finder calls a process-lifetime R closure through reticulate.
That closure adds the distribution to reticulate's additive manifest and materializes it through the same helper used by explicit live Python preparation.
The worker then uses the existing synchronous `ResolvePython` request; the relay only forwards that message and its reply.

The server resolves a complete managed-Python candidate on the host and returns it provisionally.
Reticulate checks compatibility with the live interpreter and activates the environment without replacing Python or the worker.
Its active manifest binding reports `PythonActivated`, and the server commits only a matching candidate owned by the current generation.
The worker emits that report before it invalidates import caches and resumes the original import through Python's current meta-path finders.
An automatic request records a differently named import and distribution on its provisional candidate, and the server renders that mapping as a bounded bracketed notice only when it commits the matching activation.
The cell is not replayed.

A successful activation remains retained if the inferred distribution does not contain the requested module or later cell code fails.
An ordinary pre-activation failure restores the earlier reticulate manifest and leaves the worker usable.
Restart, shutdown, and generation checks discard unactivated candidates owned by an old worker.

The finder uses a reentrancy guard while the R callback runs.
It also records the worker PID and configuring Python thread; a missing import reached from a fork child or another thread fails without calling R, reticulate, the sideband, or a host resolver.
These checks keep R callbacks on the embedded-R thread and prevent nested resolver waits.

### Interruption

An interrupt targets the active host resolver when one is registered; otherwise it targets the live worker through its relay.
It stays associated with that resolver or worker and is not retried against a replacement.
Resolver interruption and lifecycle cancellation are tracked as typed outcomes for the affected operation.

### Explicit restart

When restart includes requirements, the server first resolves the candidate retained environment outside the sandbox.
A resolution failure leaves the existing worker generation in place.
After resolution succeeds, or immediately for an unchanged restart, the server closes admission to the old generation, settles any active response ownership, retires and reaps the relay and worker, then starts the replacement from the retained environment.
For `send(control = "restart")`, that transaction continues under the same admission boundary through same-call stdin enqueue and reservation of the optional cell against the ready replacement.
Exact requirement commit behavior is documented in [requirements and environments](REQUIREMENTS.md).

### Failure replacement

When an established worker fails during an evaluation, the server retires its relay and worker, retains the observed failure and output, and makes one automatic replacement attempt for that call.
The failed cell is not run again.
A successful replacement starts with fresh in-memory state and the retained environment; the [built-in runtime guide](BUILTIN_RUNTIME.md) owns the exact notices and polling behavior.

### Server shutdown

Closing MCP input begins shutdown of the implicit console session.
The server stops accepting generation work, requests bounded retirement of the active relay and worker, cancels an active host resolver, joins the remaining relay I/O tasks, and reaps owned processes.
The protocol documents define the exact closure and retirement order.

## Output ownership

The server owns one ordered pending-output tape across worker lifetimes.
The relay publishes observations to it, but neither the relay nor worker decides which MCP call receives them.
The server assigns output to an evaluation, poll, restart, controlled send, or later idle response; applies pending-output limits; preserves image order; adds lifecycle notices; and assembles MCP content.
Before MCP projection, it compacts single-line carriage-return and backspace redraws within each consecutive run of text from one worker output stream in that delivered segment.

A controlled send produces one MCP response.
When a completed or interrupted evaluation precedes a new cell, the server transfers the prior response region into the new evaluation's prelude instead of acknowledging it separately.
The resulting delivery owner covers prior-operation output, restart lifecycle notices when present, new-cell output, and the final combined state marker in that order.
If MCP response delivery is cancelled or its write fails, the complete combined response returns to its delivery owner and can be delivered exactly once.

Each relay producer preserves its own order.
The ordered event stream gives the server one observation order, but it does not establish chronology between independent worker sideband, stdout, and stderr transports.
The [relay protocol](RELAY_PROTOCOL.md) owns that ordering guarantee, and the [built-in runtime guide](BUILTIN_RUNTIME.md) describes the resulting console behavior.

## Recording, cell output, and image artifacts

Recording is a server responsibility and does not add messages to either private protocol.
On the first `send` call, the server creates a private run directory under `.mcp-console/sessions/` in its working directory.
It appends tool calls and assembled results to `internal/events.jsonl`.
The initial `session_started` event records whether dynamic environment resolution is available, and the Quarto projection derives its managed defaults from that capability.
Each `tool_result` is appended before the MCP transport attempts the corresponding response write.
It records server assembly, not whether the transport write succeeded or the client received the response.

The run directory also contains `transcript.md` and `transcript.qmd` projections.
For each event, the server flushes the authoritative JSONL record first.
It then appends and flushes the corresponding Markdown fragment without rewriting earlier bytes.
When a call submits source or declares R or Python requirements, the server updates QMD-only in-memory state, regenerates the complete document from that state, and atomically replaces the prior file.
It does not reread or parse prior result events from the journal during live projection.
Both documents are emitted in Yamark-formatted form without rewriting submitted code or result content through embedded formatters.
The Markdown document presents R, Python, and SQL source as syntax-highlighted code fences, stdin and result text as literal text fences, call options as JSON, and artifacts through relative links.
Fences expand when literal content contains backticks.
It is a chronological call ledger: a timed-out cell, later polls, and eventual results remain separate calls because the journal does not infer evaluation-level grouping.
The executable Quarto document contains the source from calls with exactly one submitted R, Python, or SQL field in call order; it omits stdin, options, results, errors, polls, and artifacts.
It includes qualifying source from rejected calls and failed evaluations.
Its `ir` front matter declares the managed built-in R and Python requirements followed by cumulative explicit declarations from recorded calls.
Bare sessions omit both managed defaults and rejected requirement payloads.
It does not declare a Python version, so `ir render transcript.qmd` uses reticulate's default managed Python selection.
The declarations are submitted inputs, not a lockfile or an exact record of successful retained and automatically inferred requirements.
Rendering executes the captured client-authored cells in order in a fresh Quarto/knitr runtime outside the MCP Console worker sandbox and exports their new output.
Rendering does not reconstruct session control, stdin, recorded results, or artifacts.
SQL chunks require a DBI connection supplied by the document user.

From the recording directory, render the source projection with:

```sh
uv tool run --from r-lib-ir ir render transcript.qmd
```

When `ir` is installed on `PATH`, `ir render transcript.qmd` is equivalent.

Each admitted evaluation also owns `outputs/call-NNNNNN.log` beneath the run directory.
The server attaches that file to the ordered output tape at the same boundary as the worker operation, appends console text and direct stdout and stderr before pending-output admission can discard it, and detaches it at the evaluation's completion or restart cut.
Response cuts flush the active file, so output already returned by `send` is also visible through ordinary file reads while the evaluation remains active.
The file is limited to 1 GiB; later worker output is still drained and counted after the limit or a file failure.

At file completion, a `cell_output` journal event records its initiating call, relative path, retained bytes, bytes omitted from inline responses, bytes not retained in the file (`discarded_bytes`), and retention limit.
These counts describe separate projections: text not retained in the file may still have been delivered inline.
The Markdown projection links to the file when either projection omitted text.
The source-only Quarto projection ignores cell output events.

Images remain ordinary MCP image content for the client.
For recording, the server decodes retained image data into files under the run's `artifacts/` directory and records artifact identifiers and relative paths in the JSONL result instead of duplicating the encoded payload there.
Artifact events appear as links in the live Markdown projection as soon as their files are recorded, even when no later poll collects them; result image blocks remain inline in result-content order.
A journal or artifact failure disables further recording and reports a server diagnostic without stopping the console or worker.
A cell output failure stops only that file, reports the loss in the console response, and leaves the journal and projections available when they can still be written.
A Markdown or Quarto creation, append, or regeneration failure disables both derived projections; the journal and artifacts continue, and the server reports the failure once.
This includes a server working directory that cannot be represented as UTF-8 because its exact path cannot be emitted as the QMD execution root.

## Platform support

The relay, built-in worker, and managed resolvers support macOS and Linux.
Both platforms support default sandboxed execution and explicit `serve --no-sandbox`.
Descriptor sanitation uses `close_range(CLOSE_RANGE_CLOEXEC)` when available.
On kernels without that syscall or flag, the forked child enumerates `/proc/self/fd` with `getdents64` and marks descriptors close-on-exec with `fcntl`.
This path requires mounted procfs, uses no allocation after fork, covers descriptors above a lowered descriptor limit and those opened by other parent threads, and preserves Rust's spawn-error pipe until exec.
Other sanitation errors fail the spawn.
The server uses blocking `poll` on Linux and `kqueue` on macOS for startup input-closure observation.
CI runs core checks and the applicable transcript cases on both platforms.
Linux sandboxing requires procfs, permitted native namespace operations, and the selected policy enforcement capabilities.
The runner uses native namespace lifetime and its direct child wait; host subreapers, process-tree enumeration, and namespace-PID discovery are unnecessary.
Fresh procfs and pidfds are optional; [Linux compatibility](LINUX_COMPATIBILITY.md) records the tested capabilities and failure boundaries.
Windows has no working execution stack.
