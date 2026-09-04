# Implemented architecture

**Status:** Current implementation

This document describes the structure and ownership implemented in the current source.
It explains how the pieces fit together without defining their wire fields or the built-in console's operating rules.
See the [relay protocol](RELAY_PROTOCOL.md) and [worker protocol](WORKER_PROTOCOL.md) for exact transports, the [built-in runtime guide](BUILTIN_RUNTIME.md) for console behavior, and [requirements and environments](REQUIREMENTS.md) for dependency management and its trust boundary.
The material under `design-sketches/` is future or exploratory design, not evidence for this document.

## Process layout

MCP Console has three runtime communication boundaries, one launcher-private manager channel for startup and lifetime ownership of each sandbox, and one host-only resolver path:

```text
MCP client
    │ MCP JSON-RPC over stdio
    ▼
mcp-console server                         host, outside the sandbox
    ├── generation, relay lifetime, and operation owner
    ├── retained environments and output
    ├── transcript and image artifacts
    │
    ├──── host resolver processes          outside the sandbox
    │     R, Python, and DuckDB setup
    │
    │ relay stdin and stdout; inherited stderr
    ▼
mcp-console sandbox launcher               host, outside the sandbox
    ├── direct-root status and manager monitor
    ├──── sandbox manager                  host, outside the sandbox
    │     primary lifetime observer and private-directory owner
    │
    │ private startup gate
    ▼
sandbox-exec root / hidden wrapper         macOS sandbox
    │ closes the gate and execs after manager readiness
    │ private JSONL over inherited fd 0 and 1
    ▼
worker relay                               macOS sandbox
    │ worker sideband plus fd 0, 1, and 2
    ▼
worker                                     same sandbox and process group
    └── built-in R, Python, and DuckDB runtime
```

The direct development command uses the same launcher implementation without a relay or worker protocol:

```text
mcp-console sandbox launcher               host, outside the sandbox
    ├── direct-root status, terminal, and manager monitor
    ├──── sandbox manager                  host, outside the sandbox
    │     primary lifetime observer for this command
    ▼
sandbox-exec root                          macOS sandbox
    └── command and observed descendants
```

The server is the MCP stdio process.
For each worker lifetime, it constructs either the built-in relay command line or a configured custom relay command line, then starts the current executable as `mcp-console sandbox` in hidden parent-owned mode with that relay as the target.
The launcher is the server's direct child and the sole host-side sandbox owner.
It retains the `sandbox-exec` root and manager waitably, while the root first runs a hidden wrapper blocked on a private release channel.
After the manager reports readiness and manager-failure recovery is installed, the launcher releases the wrapper into the relay.
The server's piped launcher input and output and inherited error stream pass through to that relay without a data proxy.
After transferring standard input to the sandbox root, the owned launcher replaces its own copy with `/dev/null` so relay closure remains observable to the server's writer.
By default the relay is the sandbox root and process-group leader, and the worker inherits that group.
The relay also works below a wrapper process and does not inspect or manage the surrounding process group.
Submitted R, Python, and SQL cells run in the worker, not in the server or a host resolver.

For a direct `mcp-console sandbox` invocation, the launcher retains its direct `sandbox-exec` child and starts the same primary manager while a root-only waiter supplies exit and signal wakeups.
The sandboxed child first runs a hidden wrapper blocked on a private release channel; the launcher releases it into the requested command only after the manager reports readiness and failure monitoring is installed.
It has no MCP, relay, worker, resolver, recording, or retained-session responsibilities.

R, Python, and DuckDB dependency resolution follows a separate path.
The server launches resolver subprocesses on the host, outside the worker sandbox, because they need normal installation, cache, and network access.
Resolver inputs are restricted and may execute trusted installation or build code with server permissions; [requirements and environments](REQUIREMENTS.md) owns the accepted syntax and security policy.

## Communication boundaries

### MCP client and server

The client and server exchange MCP JSON-RPC over the server's standard input and output.
The server registers only the `send` tool, validates calls, and turns server-owned responses into MCP text and image content.
One `send` can poll, provide stdin, prepare requirements, evaluate a cell, interrupt, restart, or combine compatible parts under one ordered operation.
[`TOOL_DESCRIPTIONS.md`](TOOL_DESCRIPTIONS.md) is a human-readable mirror of the registered descriptions; `src/server.rs` and the actual `tools/list` result are authoritative.

This is the only public protocol boundary.
The client does not communicate directly with a relay, worker, or resolver.

### Sandbox launcher and sandbox manager

The launcher starts one manager per invocation of `mcp-console sandbox` and is the sole host-side owner of that sandbox lifetime.
One parent-owned invocation runs each worker generation, which may evaluate multiple cells before restart or replacement; an ordinary invocation runs one direct command.
The launcher first creates the gated root, then starts the manager with the root PID, cleanup timeout, and private temporary-directory path as native command arguments.
The manager derives the owner PID from its parent and uses a private inherited Unix socket to report one-byte readiness and retain lifetime ownership.
Before reporting readiness, the manager validates the root's exact identity and direct-child relationship, installs root and descendant tracking plus control-socket observation, and adopts the directory.
The launcher retains its directory-creation guard until readiness and preserves it if manager adoption is ambiguous.
After receiving readiness, the launcher installs manager-failure recovery, relinquishes that guard, and then releases the root's startup gate.
The manager becomes the sole directory-cleanup owner when the duplicate guard is relinquished.
The adopted guard preserves on unexpected unwind and is armed for removal only after the manager proves cleanup.
If the manager later fails while the root remains live, launcher-side fallback handles process cleanup only and does not remove the directory.
After readiness, the private stream carries no further messages.
The launcher holds it open only as the live-sandbox ownership token, and EOF requests retirement whether the launcher closes it deliberately or exits.
The manager decides whether the root must be stopped, completes observed-descendant and process-group cleanup, and removes the directory only after complete success.
Successful manager process exit is the primary cleanup barrier; the launcher retains the direct root waitably through manager exit and any fallback cleanup, then reaps it.

This is a private lifetime-management boundary rather than part of the relay protocol or public interface.

### Server and relay

The server sends commands to the relay's standard input and receives JSONL events from the relay's standard output.
Relay standard error is inherited separately and is not part of that protocol.
The transport is private and currently exists to keep worker connections and direct-worker supervision inside the sandbox.
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
It configures the launcher's piped standard input and output and inherited standard error, closes unrelated inherited descriptors before launcher exec, and knows normal child exit and signaling, but no private directory, startup gate, sandbox root, manager, or manager monitor.
At generation retirement, it first requests graceful shutdown through the relay protocol and waits through the applicable relay deadline.
It then sends `SIGTERM` to the launcher to request managed retirement and uses a hard launcher kill only as the final fail-safe.
On normal and owned-retirement paths, successful managed launcher exit is the synchronous cleanup barrier before the server reaps it.

### Sandbox launcher

The sandbox launcher preserves one direct sandbox target's status after natural completion and owns the complete host-side sandbox lifetime.
It inherits only the three documented streams from the server, independently closes every unrelated inherited descriptor before target exec, places the target in a dedicated process group, and retains the direct root as a waitable child.
After cleanup succeeds, natural root completion returns that root's exit status; a handled retirement request in hidden parent-owned mode returns success as the cleanup acknowledgment.
It keeps the root blocked on a private descriptor until the manager reports readiness and failure monitoring is installed, then releases the root into the requested command.
The root waiter uses one `kqueue` for root exit and launcher-addressed signals.
In ordinary mode, the launcher relays `SIGHUP`, `SIGINT`, `SIGQUIT`, and `SIGTERM` addressed to it into the target group.
In hidden parent-owned mode, it validates and watches the exact parent identity before target release; parent exit or launcher-addressed `SIGTERM` requests managed retirement instead.
When its foreground process group has no peer, it transfers controlling-terminal ownership to the target group; when a pipeline peer shares the group, it leaves terminal ownership unchanged.
In ordinary mode, after root exit it restores terminal ownership when it transferred it, drains forwarded signals already pending at that boundary, restores its inherited signal mask, closes the ownership token, waits for manager exit, and reaps the root last.
Startup and recovery failures likewise drain pending forwarded signals after stopping the root and before restoring the inherited mask, so those signals cannot replace the reported error.
It does not own descendant tracking, console state, dependency resolution, recording, relay transport, or worker protocol behavior.
It does not implement stopped/continued job state or general shell-pipeline job control.
The launcher itself never writes to standard output because that stream carries relay JSONL in a worker generation.
In parent-owned mode it relinquishes its copy of the relay input pipe after transferring that stream to the target; it retains standard output until cleanup finishes, so target output closure and launcher cleanup completion share one observable boundary.
If the launcher is killed or crashes, manager-control EOF still requests cleanup, but the server can no longer wait synchronously for that manager.

### Sandbox manager

The sandbox manager owns primary observed-descendant cleanup for one sandbox lifetime.
It records descendants by PID and process start time, validates the exact root identity, and adopts the private temporary-directory path.
It retires only identities its tracker observed, uses the still-pinned root process group as a race backstop, and removes the directory only after successful cleanup.
Its single thread uses one `kqueue` for descendant and root events plus control-socket readability.
It does not own session state, operation admission, relay transport, command exit status, or terminal semantics.

After readiness, owner EOF requests retirement.
Natural root exit first retires the observed lifetime and then applies the process-group backstop; owner EOF with a live root applies the backstop and stops the root before draining observed descendants.
After clean natural-root cleanup, the manager waits for owner EOF before removing the directory and exiting.
A successful manager exit is the primary cleanup barrier for the launcher.
If the manager itself fails while the launcher retains a live, waitable root, the launcher monitor reconstructs the root's current ancestry and performs bounded process cleanup.
That fallback has no directory-cleanup state, so the directory remains if the manager exits before completing its own cleanup and removal.
That fallback cannot recover a descendant that had already detached from the root's ancestry.

### Relay

The relay is a thin ordered transport and worker supervisor.
It owns the worker's local descriptors, translates applicable relay commands to worker-sideband messages, forwards worker observations, delivers signals, bounds shutdown, drains streams, and reaps the direct worker.
Its producers preserve their own order, and one relay writer serializes their observations for the server.
That serialization does not reconstruct chronology across the independent sideband, stdout, and stderr transports.

The relay does not own the logical session, retained requirements, evaluation admission, output budgets, response assembly, or MCP delivery.
It exits with the worker lifetime it supervises.
Remaining descendants, including those retaining worker streams, are retired by the sandbox launcher after the target exits or retirement is requested.
Its cancellable local transports let the relay drain available output and finish without waiting for those descendants to close their descriptors.

The internal `worker-relay` command uses the same stream protocol when launched directly without a sandbox or below another process wrapper.
Such a direct invocation owns only its direct worker; it supplies no sandbox policy or descendant-cleanup guarantee.
`serve` currently always uses the bundled sandbox launcher and exposes no unsandboxed server mode.
An alternative launcher must provide the process-lifetime contract described in [sandbox supervision](SANDBOX_SUPERVISION.md), including cleanup before successful owned retirement.
Any future sandbox-specific control plane ends at that launcher, without reaching the relay or changing its protocol.

### Worker

The worker owns language-runtime state and implements the worker protocol.
It reports readiness, accepts complete cells and supported preparation operations, consumes interactive stdin, publishes console events and images, and reports completion or failure through the sideband.

The built-in worker embeds R on its main thread.
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

The built-in server first selects a stable host resolver configuration.
It prefers `ir` on `PATH`, otherwise uses `uv` on `PATH` to run `ir`, and can obtain `uv` from reticulate when only `ir` or an ambient R installation is available.
With that configuration, it constructs its retained environment before accepting MCP input, resolving the default R and DuckDB environment and managed Python when selected.
If no resolver bootstrap is available, it accepts MCP input with an empty retained environment and a fixed bare capability that disables later dynamic resolution.
The worker itself starts lazily when an operation first needs it; preparing retained requirements can happen without launching a worker.
An explicit restart starts its replacement eagerly, including when the session had not started a worker before.

For each worker start, the server first constructs the relay target independently of sandboxing.
The built-in target is the current executable's `worker-relay` command followed by the worker command line; a configured relay is followed directly by the same worker command line.
The server then constructs an ordinary current-executable command for `sandbox --exit-with-parent <server-pid> -- <relay-target>`, applies the retained environment to it, and configures piped input and output plus inherited error.
The launcher inherits that environment and those streams, creates the gated root, and starts the sandbox manager while retaining both children waitably.
After receiving readiness, the launcher installs manager-failure recovery, relinquishes its duplicate directory guard, and releases the same root process into the configured relay.
Neither built-in nor configured relay code can run before manager observation is active and failure recovery is installed.
Darwin can still miss a later descendant that becomes orphaned before the manager resolves its fork event.
The relay creates the worker sideband and standard streams, launches the worker, and forwards its startup events.
The server admits the worker only after the required readiness exchange succeeds.
If sandbox setup fails before relay readiness, the launcher writes the detailed infrastructure error to inherited standard error and exits; the server reports a stable relay-startup failure from the closed transport.

### Sandbox launcher startup and retirement

The sandbox launcher creates a private temporary directory, configures `sandbox-exec`, closes unrelated nonstandard inherited descriptors, and asks it to run a hidden wrapper with inherited standard streams plus one private release descriptor.
The wrapper blocks on that descriptor before requested command code executes.
The launcher starts that gated root before the manager and retains both children waitably while the manager installs root, descendant, and control-socket observation and adopts the directory.
After receiving readiness, the launcher installs recovery monitoring, relinquishes its duplicate directory guard, and writes one release byte; the same root closes the descriptor and replaces itself with the requested command.
A descendant that later escapes before the manager sees its fork remains outside cleanup.

The hidden `--exit-with-parent <PID>` mode binds the launcher to an owning parent process.
Before creating the sandbox, the launcher verifies that PID is its current parent and captures the parent's PID and start time.
It registers a `kqueue` exit watch, revalidates the identity after registration, and checks it again after manager readiness immediately before releasing the target.

The launcher blocks in the root waiter's `kqueue` until root exit, configured-parent exit, or a supported signal is addressed to the launcher.
Ordinary mode consumes pending launcher signals synchronously and relays them to the target process group.
In owned mode, parent exit and launcher-addressed `SIGTERM` request managed retirement; other supported signals retain their relay behavior.
At root exit, the ordinary launcher restores terminal ownership when it transferred it, drains forwarded signals already pending at that boundary, and restores its inherited signal mask before requesting manager cleanup by closing the ownership token.
When startup or recovery cleanup stops the root instead, it applies the same drain before returning the error.
A signal received after that final drain can then follow its inherited disposition; if it terminates the launcher, the manager completes lifetime cleanup.
The launcher waits for successful manager exit as the cleanup barrier, then reaps the direct root.
It returns the root status after natural completion and status 0 after successfully handling an owned retirement request.
Owned mode keeps launcher signals blocked until manager cleanup and root reaping complete, including after natural root exit, so successful launcher exit remains a synchronous cleanup barrier.
The manager alone decides whether cleanup succeeded and removes the directory; launcher loss after readiness reaches the same EOF retirement path.
If the manager is killed while the launcher remains live, its monitor reconstructs the root's current ancestry and performs bounded cleanup while that root remains pinned.

### Evaluation

The server admits one cell against the current generation, starts a worker if needed, and registers the operation before sending it through the relay.
Worker sideband output and direct fd output are published to the server-owned output tape as the relay observes them.
The server waits, polls, or completes the MCP response without moving response ownership into the relay or worker.

When a code-bearing `send` declares requirements, the server treats them as preconditions of that evaluation.
One exclusive environment transition covers requirement-delta calculation, host resolution, live preparation or a pre-start retained-environment commit, and reservation and launch of the evaluation in the same generation.
No other send or environment-changing operation can enter that boundary, and a failed or superseded transition cannot dispatch the cell.
The server releases the environment transition after launch; the active evaluation continues to own stdin, waiting, output cuts, response delivery, and restart handoff.

Without inline control, the externally observable order is requirement preparation, nonempty stdin enqueue, then evaluation.
The wait timeout begins only after the cell is dispatched.

### Controlled send

Control, stdin, interrupt grace, requirement preparation, and reservation of the optional new cell form one lifecycle operation.

For interrupt, the server first uses the existing resolver-first, otherwise-worker routing and waits for delivery acknowledgement.
It then enqueues nonempty stdin immediately, waits the full 100-millisecond grace period, and settles the previous evaluation's response ownership.
Only after the previous evaluation has stopped does it validate and prepare requirements and reserve the new cell against the still-current generation.
If delivery fails, no later step runs; if the evaluation remains active after the grace, a supplied cell is not dispatched.
A validation or explicit preparation failure also prevents the cell from running, but does not undo the completed interrupt or stdin enqueue.

For restart, declared requirements enter the existing restart transaction.
The server resolves and retains them before it closes the old generation; a failure leaves the worker in place and sends neither stdin nor code.
After successful replacement startup, the server queues nonempty same-call stdin and reserves the cell against that exact replacement before it releases admission.
The old generation's unread stdin is discarded and cannot consume the new bytes.

Control delivery, interrupt grace, restart, and explicit requirement preparation happen before dispatch and do not consume the new cell's wait timeout.

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
A control-only `send(control = "interrupt")` uses the same routing as an interrupt followed by a cell, then applies its stdin enqueue and 100-millisecond settling grace before it returns the current state.
A control-only call that attaches to an evaluation after the grace uses its requested wait timeout; a call whose supplied cell was rejected observes the active evaluation without another wait.
With `timeout_ms = 0`, the call returns the state and output visible immediately after the grace.
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
The serialized event stream gives the server one observation order, but it does not establish chronology between independent worker sideband, stdout, and stderr transports.
The [relay protocol](RELAY_PROTOCOL.md) owns that ordering guarantee, and the [built-in runtime guide](BUILTIN_RUNTIME.md) describes the resulting console behavior.

## Recording and image artifacts

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
It retains source even when another argument later makes the call fail, so it is source material rather than an execution ledger.
Its `ir` front matter declares the managed built-in R and Python requirements followed by cumulative explicit declarations from recorded calls.
Bare sessions omit both managed defaults and rejected requirement payloads.
It does not declare a Python version, so `ir render transcript.qmd` uses reticulate's default managed Python selection.
The declarations are submitted inputs, not a lockfile or an exact record of successful retained and automatically inferred requirements.
Rendering executes the captured client-authored cells in order in a fresh Quarto/knitr runtime outside the MCP Console worker sandbox and exports their new output.
This is intended to reproduce the analysis represented by the Markdown ledger, but it does not replay recorded output or artifacts.
It does not yet reconstruct every MCP Console runtime detail; in particular, SQL chunks require a DBI connection supplied by the document user.

Images remain ordinary MCP image content for the client.
For recording, the server decodes retained image data into files under the run's `artifacts/` directory and records artifact identifiers and relative paths in the JSONL result instead of duplicating the encoded payload there.
Artifact events appear as links in the live Markdown projection as soon as their files are recorded, even when no later poll collects them; result image blocks remain inline in result-content order.
A journal or artifact failure disables further recording and reports a server diagnostic without stopping the console or worker.
A Markdown or Quarto creation, append, or regeneration failure disables both derived projections; the journal and artifacts continue, and the server reports the failure once.
This includes a server working directory that cannot be represented as UTF-8 because its exact path cannot be emitted as the QMD execution root.

## Platform support

The implemented sandbox command, relay, built-in worker, and managed resolvers are supported only on macOS.
The complete CI check runs on macOS.
Linux and Windows have unsupported-platform paths but no working execution stack yet.
