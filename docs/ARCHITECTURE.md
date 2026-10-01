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
    ├──── mcp-console resolve               host preparation owner
    │       └── resolver process groups     R, Python, and DuckDB setup
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

For supported native selections, standalone `mcp-console sandbox -- COMMAND [ARG]...` uses the same local frontend-to-runner exec, with the requested command in place of the relay and worker.
It rejects resolved compute enforcement.
No waiting Console adapter remains.
The runner sees the original caller as its direct parent and inherits the original standard streams.
The [sandbox integration](SANDBOX.md) defines Console policy defaults, installation verification, supported hosts, and lifetime limits.
The [migration record](SANDBOX_RUNNER_INTEGRATION.md) records the baseline, validation, and changed lifetime guarantees.

With `serve --no-sandbox`, the relay skips the native runner at its selected execution target.
Local and SSH host execution use that account's permissions and temporary-directory environment.
Docker retains its outer container boundary and retirement.
Docker Sandbox compute enforcement retains its outer microVM and provider policy, including with `--no-sandbox`; it does not use an inner native runner.
Available R, Python, and DuckDB dependency resolution runs in separate host processes; [requirements and environments](REQUIREMENTS.md) defines its trust boundary.

For local sessions, the server opens the hidden `mcp-console resolve` command before managed-runtime discovery and keeps that host process for the session.
It exchanges JSON lines on the command's standard streams: the server sends complete requirement manifests and operation controls, and the command returns resolved environment data with a cleanup receipt.
The server still owns the retained manifest, candidate commits, worker generations, and interrupt routing.
The preparation owner captures host resolver choices once and retires each child process group before returning its result.
Custom workers open the command when they first request host preparation.

For a configured SSH target, the process chain is:

```text
local MCP server → local OpenSSH → remote ssh-launch helper
    → public sandbox frontend / private runner → relay → built-in worker
```

The logical session, admission, output handling, and recording remain local.
The remote helper owns only its ordinary launcher child and connection lifetime.
It consumes captured user policy, applies remote application defaults and preflight, and passes its own remote PID as the sandbox owner.
The runner retains enforcement, private storage, and descendant cleanup.
Direct SSH execution skips the sandbox at the same target.
A separate local OpenSSH child connects to the remote `ssh-prepare` owner for capability discovery and dependency operations.
That owner captures trusted resolver settings once and runs the existing resolver functions outside the worker sandbox.
It reports operation completion only after its resolver groups retire.
The local server retains requirements, candidates, and activation decisions; remote execution never enters controller runtime discovery or resolver processes.
See [SSH execution](SSH.md) for configuration and prerequisites.

For a Docker target, the controller resolves an immutable image once and probes a disposable container before runtime readiness.
Every generation then follows:

```text
controller MCP server → local Docker owner → Docker attachment
    → container target launcher → native sandbox (unless disabled) → relay → worker
```

The Docker owner creates the container before attaching, captures its ID, observes controller input closure and attachment exit independently of output backpressure, and confirms removal through the captured daemon endpoint.
Container retirement covers descendants outside the relay's process group.
Only confirmed retirement permits replacement.
The shared target launcher consumes captured policy without YAML discovery and uses its own container-local owner identity.
Docker uses the image's preinstalled runtime, disables dynamic preparation, and never discovers controller interpreters or calls host resolvers.
Image setup is separate from generation lifetime and does not reread a build context on replacement.
See [Docker execution](DOCKER.md).

For `compute.kind: docker_sandbox`, `sandbox.provider` resolves to `compute` independently of the user's `no_sandbox` flag.
The controller captures a prepared digest-qualified template and probes it in a disposable owned VM before runtime readiness:

```text
controller MCP server → local ownership helper → sbx create / exec -i
    → in-VM target launcher → relay → built-in worker
```

SBX owns its microVM runtime, sharing, policy inheritance, and host integrations.
Console owns only its newly created VM through fixed CLI invocations and structured listings; it does not call a private service API or the host Docker Engine control plane.
The common target owner observes input loss, transport failure, and signals independently of output backpressure.
The Sandbox adapter verifies the owned name/UUID, requests forced removal, and requires confirmed absence before replacement.
Unacknowledged creation remains uncertain even after an empty listing.
Each generation uses the captured template reference and discards VM-local changes on retirement; host shares remain.
There is no native policy materialization, native preflight, companion discovery, or controller interpreter/resolver discovery in this path.
Workload environment controls are applied inside the VM; provider policy remains externally managed and can change during the session.
Tool prose describes configured placement and enforcement; transcript metadata retains discovered template, VM, and runtime identities, target directory, shared paths, and controller recording location.
Shared paths can expose controller-owned records and metadata to the worker.
See [Docker Sandbox execution](DOCKER_SANDBOX.md).

Both prepared providers use `target_launch/runtime.rs` for runtime discovery and launch configuration.
The disposable probe runs under target workload environment and policy and returns a bounded typed worker-environment frame after CPython inspection or R validation.
It starts no analysis worker or SQL connection and invokes no preparation session or dependency resolver.
The controller accepts that frame after compatible negotiation, successful validation, and provider-confirmed probe retirement, then retains one immutable descriptor beside the image/template identity.
Runtime capabilities determine operation validation and recordings, while the MCP language schema depends only on captured launch configuration.
Target paths never enter controller runtime validation or library loading.
Sans-R generations use the existing native CPython evaluator and lazy Python DB-API SQL connection.
Native Docker supplies runner-owned storage where enabled; direct Docker and SBX use private storage retained by the existing target launcher through relay retirement.
Spill files and stored secrets live there; preinstalled extension caches and shared paths keep their provider ownership.

## Communication boundaries

### MCP client and server

The client and server exchange MCP JSON-RPC over the server's standard input and output.
The server registers only the `send` tool.
The MCP adapter in `src/server.rs` decodes arguments, applies language filtering, and translates responses into MCP text and image content.
Argument decoding errors pass through the same bounded response renderer before recording and delivery.
The session coordinator in `src/worker_client.rs` validates requirements and interprets every `send` combination, including standalone preparation.
One `send` can poll, provide stdin, prepare requirements, evaluate a cell, interrupt, restart, or combine compatible parts under one ordered operation.
[`TOOL_DESCRIPTIONS.md`](TOOL_DESCRIPTIONS.md) gives editorial guidance, and the [canonical handshake snapshot](../tests/snapshots/client_server/server/test_tools/initializes_and_lists_tools.yaml) records the registered descriptions; `src/server.rs` and the actual `tools/list` result are authoritative.
The [capability-advertising decision](TOOL_DESCRIPTIONS.md#supported-capabilities-and-host-availability) keeps supported, configured capabilities visible even when the execution host lacks a runtime.
Tool construction uses captured configuration, while operation validation uses discovered availability.
`ConsoleServer::new` captures the tool router and starts one background runtime task in `src/server/startup.rs`.
Launch configuration and applicable local native-policy preflight remain synchronous; `initialize`, `tools/list`, and `ping` do not wait for interpreter, resolver, or target discovery.
The task configures the existing client, prepares defaults, and launches the built-in worker through actual transport readiness.
After transport readiness, the worker coordinator initializes enabled R and Python through their existing facades on the serialized interpreter thread.
Native or materialized Python selection remains independent of R; unresolved R selection still enters the compatibility adapter.
SQL bridge installation stays with runtime setup; managed connections and queries remain lazy.
One client-owned readiness result serves all calls; custom workers remain lazy.
Early cells are admitted to the ordinary evaluation slot before readiness, and their one call deadline covers startup and execution observation.
No cell queue or scheduler is added.
Unused default candidates can be replaced through the existing requirements transaction and launcher retirement barrier.
Interpreter startup does not change the user-code/nonempty-input cutoff for this replacement.
Preparation first reserves an ordered dispatcher barrier, before taking the environment lock.
Bootstrap resolver and activation events wait behind that reservation; output, input, and retirement remain observable.
Failed preparation releases the barrier and resumes the existing worker.
Successful preparation joins old-worker retirement before committing and launching a new worker, whose hooks can run again.
The accepted first cell retains its admission across this transaction and is never replayed.

The MCP input owner cancels background preparation on EOF or failed handshake using the active resolver/provider stop handle.
During preparation, a non-consuming pipe/socket observer detects closure even when queued input or a blocked initialization response prevents the protocol reader from reaching EOF.
After owned cleanup and response settling, a blocked protocol write cannot hold the server open.
A cancelled tool request stops only its own wait, not the shared preparation task.
The existing resolver and target owners retain their retirement allowances; a worker SIGTERM grace is not a deadline for microVM retirement.
Preparation failures remain available as bounded tool errors, while failed preparation and unconfirmed retirement retain shutdown diagnostics.
Recording metadata is supplied after successful discovery; early tool records are retained until that metadata is available.
Failed discovery does not create a runtime transcript.

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
At this native boundary, Console has no setup pipe, target wrapper, manager socket, recovery monitor, or sandbox-owned path state.

### Server and relay

The server sends commands to the relay's standard input and receives JSONL events from the relay's standard output.
Relay standard error is inherited separately and is not part of that protocol.
The server's ordinary child-exit observer also wakes the relay reader.
On launcher exit, that reader drains already-queued bytes and ends the generation even if an unsupervised descendant retains stdout.
The server joins the reader and dispatcher before replacement; a reaped local launcher permits replacement while any cleanup failure is still reported.
The selected-target session removes explicit bootstrap and retirement frames around SSH, Docker, and SBX relay bytes.
Each generation retains its own retirement receipt and, for compute targets, its allocated resource name.
The receipt must confirm the selected owner's cleanup before replacement; transport exit alone leaves retirement unconfirmed and blocks another generation.
The remote helper monitors connection loss independently of both forwarding directions, so backpressured output does not hide closure.
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
For local host execution with `--no-sandbox`, the server owns and reaps the relay directly; no runner supplies descendant cleanup.
SSH, Docker, and Docker Sandbox keep their target adapters; compute targets require confirmed container or microVM retirement.
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
`serve --no-sandbox` selects this direct relay launch at the configured target while retaining the relay protocol and direct-worker shutdown behavior.
An outer Docker container or Docker Sandbox microVM still supplies descendant retirement.
A replacement sandbox launcher must provide the process-lifetime contract described in [sandbox integration](SANDBOX.md), including cleanup before successful owned retirement.
Any future sandbox-specific control plane ends at that launcher, without reaching the relay or changing its protocol.

### Worker

The worker owns language-runtime state and implements the worker protocol.
It reports readiness, accepts complete cells and supported preparation operations, consumes interactive stdin, publishes console events and images, and reports completion or failure through the sideband.

The built-in worker's `worker::core` owns shared sideband state, command readiness, deferred operation messages, active cell language, resolver exchanges, output publication, and shutdown and failure state.
Its cell state also suppresses R resolution during SQL callbacks.
The `worker::coordinator` owns one message loop, preparation and cell dispatch, and completion reporting.
It retains Python and SQL adapters alongside an optional `worker::r_integration` boundary for R event waiting, idle callbacks, graphics, and interrupt handling.
The `worker::input` module owns interactive stdin buffering and preserves unfinished input across operations.
The `worker::interrupt` service owns native signal distribution, input wakeup setup, blocking native command waiting, and Python interrupt acknowledgment through startup-supplied state callbacks that do not enter an interpreter.
It starts before either interpreter and attaches R's pending and suspended state when R initializes, transferring queued requests without acknowledging them.
R and reticulate attachment reinstall Console services after their hooks.
These shared services neither access R globals directly nor evaluate R code.
The `worker::embedded_r` adapter supplies the mixed runtime's interrupt-state callbacks and owns R initialization, interrupt checks and deferral, native error boundaries, event handling, graphics, and R console callbacks.
Its REPL latch distinguishes submitted R source from interactive input; shared cell state identifies the enclosing language.
Enabled R initializes during bootstrap; later demand from Python-side R access or an R-owned SQL operation uses the same facade.
R capability selects the default managed SQL provider independently of initialization and bridge attachment.

Command readiness is separate from waiting: when no command is ready, the coordinator uses R's event-aware wait and services its idle callbacks before waiting again.
Without R integration, it uses the native sideband, interrupt, and stdin-closure wait.
R retains the native unwind boundaries for its event and interpreter operations.
Scheduled `later` callbacks do not require another R cell: idle processing publishes their output for the next response, including Python and SQL responses.
Cell dispatch marks the cell active before starting graphics or initializing a runtime, clears the active cell after evaluation, finalizes graphics even after an evaluation error, then finishes managed input before the final idle turn and completion.
Both R and Python cells use those R graphics hooks because Python can call R and create plots; SQL retains its existing exclusion.
Idle event processing retains its own graphics and input cleanup ordering in the R adapter.
The launcher supplies private worker-lifetime storage before either interpreter starts.
R creates its own session directory beneath it; R cleanup cannot remove Python caches or SQL storage.
Direct and sandbox launches retain their existing retirement owners.

The coordinator and both interpreters use the same owning thread.
R's `setup_Rmainloop()` installs R services; it is not the outer Console command loop.
R stack setup, protected objects, event servicing, and the C-owned unwind boundaries remain on that thread.
CPython entry points acquire the GIL and its retained initial thread state is restored for process exit; initialization and external adoption remain distinct ownership states.
No library mutex or requirement-state borrow spans interpreter execution, startup hooks, or reentrant callbacks.
The signal handler only marks native state and wakes descriptors; it never enters R or Python.
Reticulate's event service schedules main-thread pending calls and protects R event processing with `R_ToplevelExec()`.
Its Python background callbacks, including the services used by Positron, enqueue work for the main thread and wait for results.
A future split across evaluator threads would need explicit R affinity and stack ownership, reentrant cross-thread requests, GIL release while waiting, cancellation delivery, and coordinated shutdown.
It cannot assign the existing synchronous evaluators to independent blocking threads unchanged.

When R is available, the built-in worker embeds it on its main thread.
Before entering the native DLL REPL, it validates the entire cell with a cached private R function that calls `suppressWarnings(str2expression(source))` and discards the generated expression vector.
The helper is initialized in R's base environment and evaluated directly under `R_ToplevelExec()`, without invoking task callbacks, adding history, or changing `.Last.value`.
Parser warnings remain owned by the native REPL; validation does not mutate warning options or suspend interrupts.
The helper returns `NULL` for valid source or the parser's diagnostic string for rejected source.
The worker writes that diagnostic directly to the console without invoking `options(error)` or replacing `.Traceback`; parser errors do not propagate to R's top level.
On Linux, loader preparation re-executes before either interpreter initializes, with the captured `R_HOME/lib` first in `LD_LIBRARY_PATH`, preserving inherited library paths and its sideband endpoint.
Late R initialization never re-executes a worker with live Python state.
This lets native R packages resolve R's shared libraries even when that R installation is absent from the system linker cache.
Its language adapters provide persistent Python and SQL within that worker process.
The SQL router uses a DBI provider in embedded R or a DB-API provider in CPython.
The R provider owns a managed DuckDB connection by default and can retain a user-selected DBI connection; the Python provider retains user-selected DB-API connections and, without R, a lazy worker-owned DuckDB connection without converting objects or result rows through Rust or R.
Its private R environment bridge conditionally wraps `base::library` and runs R's unchanged `base::loadNamespace` body in a private lexical environment that intercepts its retry restart; it applies accepted managed libraries and reports activation outcomes.
The reticulate adapter retains discovery, selection precedence, and R-side hints.
Console captures a complete `NativePython` identity: executable spelling, embedding library, Python home, and all four prefixes.
Managed selections and prepared targets are inspected by the execution-host preparation owner; a fresh reticulate compatibility selection is inspected in an owned worker child.
Both paths use the same inspection program and bootstrap contract.
Inspection requires CPython 3.10 or later and checks the library's runtime build and ABI; the selected executable's spelling is preserved, including virtualenv paths.
The selected installation must remain stable through inspection and initialization; concurrent replacement is unsupported.
A private result file separates configuration from startup output, and the existing resolver lifecycle owns child output, cancellation, and reaping.
The worker's existing interrupt wakeup cancels inspection without committing an interpreter or changing retained requirements, and a failed inspection permits retry in the same worker.
Console supplies the inspected embedding fields to both native initialization and reticulate's subsequent attachment; cached or already initialized selections are not inspected again.
Reticulate supplies conversion metadata and selection hints, but no longer configures the generic Python environment.
`python::startup` applies `PATH`, `VIRTUAL_ENV`, and the effective `PYTHONPATH` before CPython executes startup hooks; Linux child library paths are derived from the inspected prefixes.
Console sets CPython's program name to the selected executable so its normal path initialization can find the installation and `pyvenv.cfg`.
It leaves PythonHome unset because that override can bypass virtualenv discovery; it does not replay an activation script during bridge attachment.
Shared setup validates the observed prefixes, explicitly sets `sys.executable` to the selected spelling, updates an already-imported multiprocessing module's executable, and installs the working-directory import entry.
The process PATH also lets R's `system()` and `system2()` find package entry points installed in the selected Python environment.
Reticulate adds its bridge module directory without applying a second generic environment configuration or changing CPython's prefixes and base executable.
The Rust Python facade loads and retains that file-backed `libpython`, initializes CPython without holding its library-state lock through interpreter code, or attaches its handle if CPython was already initialized.
The Python facade retains an optional reticulate adapter after R initializes.
That adapter owns unresolved R selection and bridge attachment; `python::startup` provides bootstrap and shared setup operations without depending on the R adapter.
The retained CPython library owns interpreter lifetime and shared setup completion, so interrupted setup can resume without replacing the interpreter.
Environment identity validation is a prerequisite: if startup hooks leave incompatible prefixes or environment setup fails, Console retires the worker instead of retrying that partial initialization.
If an R startup package initializes reticulate before the adapter is installed, Console captures that interpreter's live executable and prefixes together with reticulate's loaded-library configuration.
It registers this identity before installing bridge hooks, preserves external interpreter ownership, and enters common setup without rerunning environment activation.
Ordinary Python cells enter common bootstrap and the private evaluator directly through CPython.
An explicit or independently materialized selection does not initialize R or attach reticulate.
If R is already initialized, unresolved R declarations and selection callbacks keep their compatibility precedence.
Reticulate attaches only when an operation needs interoperability.
Common setup installs Console's stream, input, interrupt, and plot services before processing executable `.pth` files and `sitecustomize` in a Console-owned interpreter.
Site processing restores normal Python flags before running hooks, so child interpreters can load installed packages.
Successful site processing is retained across later setup retries; interrupted hooks can retry in the same interpreter with managed input and interrupts connected.
Externally initialized interpreters keep their completed site processing.
The private evaluator is installed after site processing, including an interrupted attempt so it can report the retained exception.
Startup customizations precede the evaluator's module defaults.
Common setup then installs the SQL adapter and configures automatic import resolution through the retained CPython interface.
An interruption during SQL adapter installation remains a retryable setup result.
Reticulate attachment reasserts the same services after reticulate installs its hooks.
The same setup accepts a managed import policy or a disabled reason independently of R.
Python initialization, common setup completion, and bridge attachment have separate completion state.
Runtime availability is captured on the execution host at session startup and passed through internal launch configuration to each worker.
`local_runtime::Selection` retains an optional R home and an independently optional inspected Python selection, including managed local and SSH environments and prepared Docker/SBX targets with both runtimes.
An absent Python selection in a managed R-capable worker leaves selection lazy.
Prepared targets instead expose their inspected capabilities: genuine Python absence permits R-only operation; a broken explicit selection is an error.
Availability, captured identity, library initialization, shared setup completion, and bridge attachment are separate state.
During bootstrap, R startup packages are deferred until Console installs input, graphics, and selection callbacks.
Late R initialization also defers packages when Python is already running.
Bootstrap owns a graphics scope without marking a user cell active; its output and plots use the ordinary output tape.
An accepted first cell receives bootstrap output through its existing prelude and active routes.
Console attaches its SQL and Python tools after the startup packages, retaining search position 2 in either initialization order.
Attachment obtains conversion metadata from the captured executable.
The native configuration retains the `RETICULATE_PYTHON` hint present at initialization; an unchanged hint is not resolved again against a later working directory or `PATH`.
Reconstructed reticulate configuration carries the managed environment's `ephemeral` marker.
Conflicting later selections require restart; a completed selection callback is not replayed during attachment.
Failed partial R initialization requires worker replacement.
Concurrent native process-environment access during late R startup remains an unresolved safety constraint.
`Rf_initialize_R()` processes the system Renviron, and `setup_Rmainloop()` changes `R_SESSION_TMPDIR` and library environment variables before returning control to the embedder.
R's embedding API has no default-package parameter or callback between base-profile initialization and default-package loading; `--default-packages` is an Rscript frontend option implemented through the process environment.
Removing Console's temporary `R_DEFAULT_PACKAGES` mutation alone would not make that bootstrap safe with foreign environment readers or writers.
The GIL and a Rust mutex cannot serialize arbitrary native threads with these mutations.
Local discovery uses `src/local_runtime.rs`; SSH discovery uses the remote preparation owner and returns structured native configuration to the controller.
When R is absent, the preparation owner resolves the default Python manifest, including DuckDB; an explicit `python` setting instead selects an existing environment without uv.
When `HOME` is absolute, the managed path captures DuckDB's shared home extension directory and passes it to the host resolver and worker through internal configuration.
Managed Python startup requires an absolute `HOME` to prepare the default SQLite extension before runtime readiness.
The Python DB-API adapter uses that directory when captured and otherwise leaves DuckDB's default, while keeping spill and stored secrets in the worker's private temporary directory.
Without either selection or uv, startup reports an error rather than searching PATH for Python.
The execution-host preparation owner inspects the selected executable before runtime readiness and each candidate before retirement or live activation.
The session retains the managed result and inspected environment identity, independently of reticulate's user-selection variable.
The same coordinator constructs an absent R integration, native Python runtime, and SQL router without an R DBI backend.
Native CPython path initialization follows the selected executable's virtualenv configuration; shared setup verifies its prefixes and configures child-process selection.
Python setup failures retain their tracebacks on the ordered console diagnostic channel; every coordinator return restores the Python thread before extension-library exit destructors.
The native runner owns sandbox temporary storage; direct relay lifetimes own a private directory and retire it after the worker, including failed startup.
Neither lifetime owns resolver cache removal.
The retained library state records each completed installation step and marks setup configured only after environment, module defaults, and managed or disabled import policy succeed, so an incomplete setup can retry without initializing the interpreter again.
The native requirement owner calls Console's Python activation helper through that retained library; the helper runs the selected environment's activation script and completes process-environment setup.
The shared Python module loader applies NumPy, pandas, and Matplotlib defaults once, preserving existing nondefault widths and subsequent user overrides.
Applying defaults to modules loaded by startup hooks is a retryable setup step after the evaluator is installed; an interrupt preserves the running interpreter and its objects.
The R setup entry point preserves the caller's interrupt context while Python hooks run and protects R conversions separately.
Reticulate attachment does not rerun these module defaults.
The native calls preserve the caller's R interrupt state while Python runs.
Failed setup retains the original Python exception and traceback.
Python demand reports it on the console diagnostic channel; an R call preserves reticulate's R condition and interrupt conversion.
Console also installs native callbacks for Python input, text output, diagnostics, and plot publication.
Managed Python input shares the worker's length-aware stdin buffer with R; R's console callback retains its boolean success contract.
The worker's native signal handler wakes blocked input and marks interrupts for both runtimes.
Python acknowledgment respects R's suspended-interrupt state and clears accepted interrupts so nested calls do not deliver them twice.
Reticulate's event polling remains active.

Reticulate is required for R-side selection compatibility, object conversion, and cross-language integration.
The Python-side `r` proxy initializes R and attaches the bridge on its first actual use; unrelated Python cells do neither.
Reached imports use the same native managed-requirement callback with and without R.
Console owns initialization of the selected interpreter and tracks completion of its private runtime setup in the retained library state.
Python use from either language reaches that native owner before reticulate attaches.
A failed or interrupted selection has not applied generic process-environment changes.
Once CPython is running, attachment errors retain the selected environment and completed setup steps.
Attachment can retry before reticulate publishes its configuration; retry uses the existing interpreter.
Failure in a later initialization hook marks attachment incomplete and requires worker replacement because those hooks may have arbitrary partial effects.
Ordinary Python remains usable.
Neither path rolls back interpreter initialization.
`RETICULATE_PYTHONPATH`, when supplied, selects the common startup `PYTHONPATH` for both the interpreter and its children.
R's already-initialized interoperability marker remains a bridge input.
RStudio-only loader symlink manipulation and Windows Qt plugin setup are not part of Console's embedding path.
`src/python/requirements.rs` retains the worker's live normalized declaration and inspected identity, the last resolved candidate, and provisional R declaration values.
This replaces the separate `native.rs` store and reticulate adapter's resolved-selection slot.
These values describe worker state; only the server can accept an environment for a generation and retain it for replacement.
The R adapter preserves field presence and ordering, NA values, string bytes and encoding, attributes, history, protected lifetimes, and copy isolation without a JSON round trip.
R declarations retain reticulate's argument validation, warnings, live version/package checks, conditions, and add-only restrictions.
Reached imports resolve and activate through the common owner, projecting R metadata before mutation and publishing it after success when the adapter exists.
The host-inspected identity supplies the candidate library and prefixes.
Worker-side inspection of that selected executable supplies site paths, installed distribution versions, and additional conversion metadata; generic `python_config()` discovery is not used for activation.
Library compatibility rejection precedes conversion metadata and interpreter mutation.
The environment adapter rejects incompatible loaded distribution versions, replaces environment-owned paths while preserving user paths, and sets candidate identity before running site hooks.
Its transaction restores Console-owned paths, prefixes, executable, and process environment after interrupted activation.
Publication and the local commit defer interrupts, preserving an environment accepted before an interrupt is delivered.
The native requirement owner retains the exact pending candidate and optional R projection across this transaction.
An unsafe activation failure marks the generation restart-required even when reported through an R condition.
A successfully published activation remains accepted when the subsequent import or cell fails.
Idle tool preparation uses the same worker request and native owner with or without R.
The former host-supplied native preparation variant and reticulate-driven preparation implementation are removed.
The owner resolves a candidate, projects optional R metadata, mutates only an initialized interpreter, and publishes acceptance through existing generation checks.
Lazy declarations and snapshot restoration do not publish activation.
Successful pre-initialization preparation materializes the declaration and commits its inspected launch identity through the existing preparation receipt.
The live interpreter pin is resolver input, separate from retained user version constraints.
R activation commits its prepared configuration and declaration through the active binding inside the publication transaction; initial interpreter setup publishes through its own hook without that transient matching key.
The initial hook records the running inspected identity directly, including R-side selections that did not require a managed resolver candidate.
If a startup package already initialized reticulate, adapter installation records its managed state after registering that running identity and before common setup, without waiting for another initialization event.
Worker readiness precedes interpreter initialization.
Initial Python requirements publish directly through the common owner when Python starts; the former deferred pre-readiness publication path is removed.
If adoption bypassed the resolver hook, the first reached import inspects the accepted executable on its execution host before preparing a candidate; the worker still checks that candidate against its actual retained library before mutation.
An external startup package can select its original environment again on restart; accepted declarations survive, but adoption does not replay activation into that already-running interpreter.
Native console callbacks release the GIL while blocking on worker services and are confined to the configuring worker thread.
Background threads and fork children use their underlying streams and cannot enter R through these services.
Bare sessions leave managed resolution disabled.
The [peer-runtime completion notes](../design-sketches/peer-runtime-completion.md) record the implemented boundaries, unresolved environment-access constraint, and separate-thread and SQL-only exclusions.

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

For a local host target, the built-in server opens the host resolver command, which captures a stable resolver configuration and detects its capability without installing an environment.
The Python configuration captures an explicit `RETICULATE_UV` selection or `uv` on `PATH` independently of R discovery.
R bootstrap prefers `ir` on `PATH`, otherwise selects `uv` on `PATH` or an explicit `uv` path, and can obtain `uv` from reticulate when only `ir` or an ambient R installation is available.
It retains the selected bootstrap as pending setup and accepts MCP input before invoking it or resolving the default R, DuckDB, and managed Python environments.
An operation that first needs an environment resolves the defaults through the normal generation-owned resolver lifecycle and commits the complete candidate only after all preparation succeeds.
With directly available `uv`, local Python preparation runs before R library preparation and does not require a managed R library; reticulate bootstrap remains the R-backed fallback when direct `uv` is unavailable.
For an ordinary cell, this happens after evaluation admission, so the client can poll or interrupt preparation.
Explicit requirements remain preconditions of evaluation.
Default-add requests combine additions with pending defaults; set/reset calculate a whole-declaration candidate using the same environment owner and resolver transaction.
Retained R requirements describe the declaration separately from necessary R bridge and SQL infrastructure.
Managed Python already carries its logical manifest, including an empty package list.
A separate read-only projection is published at generation-checked environment commits.
Inspection reads that short-lived snapshot lock without acquiring the environment lock held during resolution.
If no resolver bootstrap is available, it accepts MCP input with an empty retained environment and a fixed bare capability that disables later dynamic resolution.
The worker itself starts lazily when an operation first needs it; preparing retained requirements can happen without launching a worker.
An explicit restart starts its replacement eagerly, including when the session had not started a worker before.

For each worker start, the server first constructs the relay target independently of sandboxing.
The built-in target is the current executable's `worker-relay` command followed by the worker command line; a configured relay is followed directly by the same worker command line.
For local host execution, the server then constructs an ordinary current-executable command for `sandbox --exit-with-parent <server-pid> -- <relay-target>`; with `--no-sandbox`, it uses the relay target directly.
SSH and Docker construct that command inside the target with a target-local owner PID.
Docker Sandbox compute enforcement uses the direct relay command inside the VM and never constructs a native launcher or native preflight.
It applies the retained environment and configures piped input and output plus inherited error in either mode.
In sandboxed mode, the frontend execs the runner with that environment and those streams; the runner establishes native enforcement and descendant observation before releasing the relay.
The relay creates the worker sideband and standard streams, launches the worker, and forwards its startup events.
The server admits the worker only after the required readiness exchange succeeds.
If sandbox setup fails before relay readiness, the launcher writes the detailed infrastructure error to inherited standard error and exits; the server reports a stable relay-startup failure from the closed transport.

### Selected-target sessions and timing

`src/target_session.rs` provides one concrete session enum for SSH, Docker, and SBX.
Worker orchestration asks it for the command and bootstrap, keeps the returned generation receipt, and reads the unchanged relay protocol through that generation's output adapter.
Ordinary local and custom-worker commands retain their direct launch path.
SSH preparation remains accessible separately and closes alongside worker shutdown; startup cancellation keeps the worker SSH connection open long enough to receive remote cleanup confirmation.

The same session code registers compute-setup cancellation, runs and decodes disposable runtime probes, rejects unexpected probe data, constructs owner requests, and latches unconfirmed retirement before another launch.
Only captured provider data enters the serialized owner request; controller replacement state stays local.
Docker owns endpoint/TLS capture, immutable image resolution, and exact container cleanup.
SBX owns CLI/template validation, creation acknowledgment, name/UUID checks, shared-path checks, and microVM removal.
`target_launch::owner` shares connection observation and byte forwarding while these adapters supply concrete creation and retirement operations.
Inner-launcher failure and outer-resource removal remain separate outcomes.

The waits retain distinct owners and allowances:

| Wait                                                          | Owner                             | Allowance                                                                     |
| ------------------------------------------------------------- | --------------------------------- | ----------------------------------------------------------------------------- |
| Target bootstrap/preflight and controller worker readiness    | `target_launch::SETUP_TIMEOUT`    | 30 seconds per setup wait                                                     |
| SSH preparation connection handshake                          | `ssh::preparation::SETUP_TIMEOUT` | 30 seconds; discovery and dependency operations have no installation deadline |
| Disposable compute runtime probe                              | `target_session::PROBE_TIMEOUT`   | 40 seconds                                                                    |
| Docker owner after probe cancellation / generation retirement | Docker profile                    | 8 / 6 seconds                                                                 |
| SBX owner after probe cancellation / generation retirement    | SBX profile                       | 20 / 20 seconds                                                               |
| SSH transport during generation retirement                    | SSH adapter                       | 6 seconds                                                                     |
| Local launcher after retirement request                       | Worker orchestration              | 6 seconds                                                                     |
| Target-side inner launcher retirement                         | Target launcher                   | 6 seconds, then a 1-second forced-exit wait                                   |

Provider CLI deadlines belong to their adapters: Docker setup commands and owner-request reads allow 10 seconds; cleanup listing, stop, removal, and confirmation each allow 2 seconds.
SBX version and owner-request reads allow 10 seconds, creation 25 seconds, listing 2 seconds, and forced removal 10 seconds.
Pulls and builds remain cancellable without a short setup deadline.
The outer allowances are independent fail-safes, not sums of these CLI deadlines: Docker cleanup can perform three sequential 2-second operations, or four when creation returned no identity, before transport and final-frame overhead.
SBX cleanup can list, remove, and list again under separate deadlines.
An outer deadline can therefore end observation before every individual cleanup allowance is exhausted; a missing receipt continues to block replacement.
These values preserve the existing timing behavior rather than establish a worst-case cleanup guarantee.

Provider diagnostic routing also remains explicit: `Capture` retains command stderr for failure reporting, `Data` captures stdout while streaming stderr, and `Diagnostics` streams setup output to controller stderr.
Owner probes keep provider diagnostics outside their framed stdout, including Docker's existing owner-input stderr inheritance.
The [relay protocol](RELAY_PROTOCOL.md#target-launch-envelope) defines the envelope once; each provider guide retains its configuration and lifecycle rules.

### Evaluation

The server admits one cell against the current generation, starts a worker if needed, and registers the operation before sending it through the relay.
Worker sideband output and direct fd output are published to the server-owned output tape as the relay observes them.
The server waits, polls, or completes the MCP response without moving response ownership into the relay or worker.

When a code-bearing `send` declares requirements, the server treats them as preconditions of that evaluation.
One exclusive environment transition covers requirement-delta calculation, host resolution, live preparation or a pre-start retained-environment commit, and reservation and launch of the evaluation in the same generation.
Sans-R managed Python uses the existing trusted preparation owner on its execution host: `resolve` locally or the SSH preparation process remotely.
It runs with full host permissions, independently of worker policy; it does not isolate worker-writable resolver inputs.
The [requirements trust boundary](REQUIREMENTS.md#host-resolution-and-trust) documents the resulting escape paths.
The mutable session environment owns the accepted manifest, executable, and embedding configuration together.
Candidate inspection completes before worker retirement; failure or cancellation preserves the old selection and worker.
In sans-R startup and restart transitions, the inspected Python candidate's DuckDB installation API prepares all retained extension names that need preparation.
A changed Python candidate requires this step even when the extension names are unchanged.
An idle live extension-only addition uses the accepted managed Python and captured extension cache through that same resolver operation, then commits the extension declaration only if its generation remains current.
It sends no worker activation or SQL command, preserving Python objects, the managed catalog, and the selected connection.
An idle live Python addition, optionally with DuckDB extensions, resolves against the accepted executable, inspects the candidate, checks library compatibility, and prepares the complete retained extension set before worker activation.
The existing preparation receipt carries the approved native configuration to the worker; `PythonActivated` commits the candidate manifest and launch configuration in the current generation before a same-call cell can run.
The execution-host preparation owner owns the Python helper's process group, cancellation, output, and cleanup.
Resolver and inspection results use bounded reads from the original open descriptors.
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
It also yields without a callback when the importing Python module or native extension and the active eager importlib initializer belong to the same installed distribution's recorded files.
Ordinary loading and reload share that ownership check; frames inside the current import entrypoint, including an R adapter's wrapper, are import machinery.
Metadata is read at the loaded import root's actual package paths, including paths retained across compatible activation and portions added by `pkgutil.extend_path`, without a cache.
This preserves ordinary optional-dependency handling; later calls and local modules, including cached Python callbacks invoked by installed initializers, remain eligible for automatic resolution.
An installation without a recorded file matching the selected origin does not establish ownership and remains eligible for automatic resolution.
Malformed installed file metadata is unsupported; parsing errors from corrupted records propagate instead of silently skipping the distribution.
Deferred module bodies, including `importlib.util.LazyLoader` execution on later attribute access, fall outside this ordinary importlib initialization boundary.
Cached C callbacks expose no Python frame of their own and inherit the visible initializer's context; calls after initialization remain eligible.
It derives one bare distribution from the top-level import through a curated mapping or a conservative same-name fallback; the server validates that name through the existing managed-Python requirement validator.

The Python finder calls the shared native requirement owner, which forms an additive request from its retained manifest.
It uses the synchronous `ResolvePython` exchange; the relay only forwards that message and its reply.
Optional R metadata is projected separately and does not own resolution.

The server resolves a complete managed-Python candidate on the host and returns it provisionally.
The worker checks the host-inspected candidate against its loaded library, activates it through the common operation, and reports `PythonActivated` before the finder retries the import.
When the R adapter exists, its active binding commits the projected metadata at that same boundary.
The server commits only a matching candidate owned by the current generation.
The worker emits that report before it invalidates import caches and resumes the original import through Python's current meta-path finders.
An automatic request records a differently named import and distribution on its provisional candidate, and the server renders that mapping as a bounded bracketed notice only when it commits the matching activation.
The cell is not replayed.

A successful activation remains retained if the inferred distribution does not contain the requested module or later cell code fails.
An ordinary pre-activation failure leaves the worker and accepted environment usable; the R adapter restores its earlier reticulate manifest.
Restart, shutdown, and generation checks discard unactivated candidates owned by an old worker.

The finder uses a reentrancy guard while its callback runs.
It also records the worker PID and configuring Python thread; a missing import reached from a fork child or another thread fails without calling R, reticulate, the sideband, or a host resolver.
These checks keep R callbacks on the embedded-R thread and prevent nested resolver waits in both runtimes.

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
It queues relay shutdown and resolver cancellation before closing the preparation connection; preparation and worker retirement then proceed together.
Closing preparation cancels remaining resolver work and ends control admission on that connection.
After worker retirement, the server writes responses for calls accepted before input closes, including collected output and a shutdown notice for an active evaluation.
Tool results whose response reservation was cancelled remain suppressed during the MCP library's shutdown drain.
It bounds that delivery wait so a blocked MCP output pipe cannot hold shutdown open indefinitely.
The protocol documents define the exact closure and retirement order.

## Output ownership

The server owns one ordered pending-output tape across worker lifetimes.
The relay publishes observations to it, but neither the relay nor worker decides which MCP call receives them.
The server assigns output to an evaluation, poll, restart, controlled send, or later idle response; collects bounded head-and-tail text previews; preserves image order; adds lifecycle notices; and assembles MCP content.
During ingestion, it incrementally decodes direct streams and compacts carriage-return and backspace redraws within each consecutive run from one producer.
It retains bounded text at the beginning and latest tail, coalesces adjacent text and omission metadata, and admits images under separate byte, metadata, and count limits.
Small text appends reuse the current buffer; compaction runs after a bounded batch of new bytes.
A response cut seals this projection and its raw-file receipt without reading the file.
Intervals with no rendered text publish any finished file summary and discard their receipt; unrecorded text still keeps its source boundary.
Fully omitted intervals account for their per-cell omissions and release their receipts into one bounded summary, which names the journal containing individual file paths and counts.
The canonical response builder preserves typed control notices during composition; its final projection applies one 8 KiB UTF-8 text budget, including all generated notices, across the complete tool result.
Sizing counts the projected text and notices without constructing content blocks or copying images.
Collection and response composition keep bounded state even after raw-file retention fails or is disabled.

A controlled send produces one MCP response.
When a completed or interrupted evaluation precedes a new cell, the server transfers the prior response region into the new evaluation's prelude instead of acknowledging it separately.
The resulting delivery owner covers prior-operation output, restart lifecycle notices when present, new-cell output, and the final combined state marker in that order.
Console retains one recoverable response until local transport write or cancellation settles ownership.
A cancelled or failed delivery returns the complete combined response to its owner.
This does not establish exactly-once client observation: cancellation can race with bytes already made visible to the client.
An active evaluation replays its unclaimed response before collecting later output.
Restart finishes that cell's raw output record before composing its recovered intervals with later output, so a source summary can publish the journal entry before releasing the file receipt.

Each relay producer preserves its own order.
The ordered event stream gives the server one observation order, but it does not establish chronology between independent worker sideband, stdout, and stderr transports.
The [relay protocol](RELAY_PROTOCOL.md) owns that ordering guarantee, and the [built-in runtime guide](BUILTIN_RUNTIME.md) describes the resulting console behavior.

## Recording, cell output, and image artifacts

Recording is a server responsibility and does not add messages to either private protocol.
On the first `send` call, the server creates a private run directory under the launch working directory's `.agents/console/sessions/` if `.agents/console` already exists there.
Otherwise it writes under `~/.agents/console/sessions/` without creating a project `.agents` directory.
`MCP_CONSOLE_HOME` can replace the default `~/.agents/console` directory; project directory selection still takes precedence.
Raw-log paths returned to clients are relative to the launch directory for project recordings and absolute for home recordings.
It appends tool calls and assembled results to `internal/events.jsonl`.
The initial `session_started` event records whether dynamic environment resolution is available.
Sans-R uv-managed sessions separately record `python_preparation: true` while dynamic resolution remains disabled.
The Quarto projection uses that capability and the captured Python-only managed selection separately to declare initial requirements.
For SSH sessions, it also records `target.transport` and the initial remote `target.workspace` separately from the local launch `working_directory`.
Docker session metadata additionally records compute kind, requested/resolved image identity, and container workspace; generation events identify created containers.
Docker Sandbox metadata records its compute provider, template digest, CLI version, target workspace, and shares; generation events identify VM names and UUIDs separately from container IDs.
Declared binds and shared paths can expose controller records to the workload.
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
The source-only Quarto document contains the source from calls with exactly one submitted R, Python, or SQL field in call order; it omits stdin, options, results, errors, polls, and artifacts.
It includes qualifying source from rejected calls and failed evaluations.
Its `ir` front matter declares the managed built-in R and Python requirements followed by cumulative explicit declarations from recorded calls.
Python-only managed sessions declare NumPy, pandas, and DuckDB plus packages from committed `python_environment_accepted` events, including live additions.
They omit R defaults and rejected or discarded candidate requirements; accepted manifests remain recorded even if later worker replacement fails.
Bare sessions omit both managed defaults and rejected requirement payloads.
It does not declare a Python version, so `ir render transcript.qmd` uses reticulate's default managed Python selection.
In R-present sessions the declarations are submitted inputs, not an exact record of successful retained and automatically inferred requirements.
Neither mode records a dependency lockfile.
For local sessions, rendering executes the captured client-authored cells in order in a fresh Quarto/knitr runtime outside the MCP Console worker sandbox and exports their new output.
Rendering does not reconstruct session control, stdin, recorded results, or artifacts.
SQL chunks require a DBI connection supplied by the document user.

SSH, Docker, and Docker Sandbox projections identify the execution target and omit the local execution root.
Rendering them executes the captured cells, so the user must prepare an appropriate environment and files first; the document does not reproduce remote files.
A committed set/reset records the normalized declaration, Python constraints, and originating call ID in a `requirements_selected` event.
The Markdown projection displays it; Quarto inserts it before the accompanying cell, marks environment boundaries, and disables evaluation.
One header manifest cannot replay cells with incompatible historical requirements.
QMD without replacement boundaries includes the `ir render transcript.qmd` command in a frontmatter comment.

For a local session, render the source projection from the recording directory with:

```sh
uv tool run --from r-lib-ir ir render transcript.qmd
```

When `ir` is installed on `PATH`, `ir render transcript.qmd` is equivalent.
IR rendering requires R on the render host, including for documents recorded in a Console session without R.

Each admitted evaluation also owns `outputs/call-NNNNNN.log` beneath the run directory.
The server attaches that file to the ordered output tape at the same boundary as the worker operation, appends console text and direct stdout and stderr before preview collection omits the middle, and detaches it at the evaluation's completion or restart cut.
Response cuts flush the active file, so output already returned by `send` is also visible through ordinary file reads while the evaluation remains active.
The file is limited to 1 GiB; later worker output is still drained and counted after the limit or a file failure.

A `cell_output` journal event records the initiating call, relative path, retained raw bytes, rendered UTF-8 bytes omitted from previews (`inline_omitted_bytes`), raw bytes not retained in the file (`discarded_bytes`), and retention limit.
File completion seals the raw totals; response projection or interval summarization accounts for its omissions before publishing this summary and the corresponding tool result.
Shared interval receipts prevent delivery recovery from counting the same omission twice.
If a recovered response is later composed with more output, additional omissions can publish an updated cumulative summary for the same cell; the most recent summary owns its totals.
These counts describe separate projections: normalization can change rendered byte counts, and raw bytes not retained in the file may still appear in the preview.
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
