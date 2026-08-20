# AGENTS.md

Keep this file synchronized with the code that exists in the repository.
The documents under `design-sketches/` describe intended behavior, not implemented behavior.

## Transcript goldens

Never hand-edit files under `tests/transcripts/golden/`.
They may change only through `scripts/test --update ...` or Yamark via `scripts/format`.
If regeneration produces an incorrect snapshot, fix the code or serializer and regenerate it.

## Current state

MCP Console is an initial Rust binary package.
The implemented commands are:

```text
mcp-console serve
mcp-console --help
mcp-console help [COMMAND]
mcp-console --version
mcp-console sandbox [--] COMMAND [ARG]...
```

The binary requires a subcommand.
The `serve` command runs an MCP server over stdio.
Clap provides command help, version output, argument parsing, and usage errors.
The server registers `send` and `session` tools.
The first ordinary, non-task `send` or `session` call creates a run-specific directory under `.mcp-console/sessions/` in the server's initial working directory.
Initialization, tool listing, unknown tool calls, and an otherwise unused `serve` process create no record.
On Unix, newly created record directories use mode `0700`, and journal and artifact files use mode `0600`.
It appends schema-versioned `session_started`, `tool_call`, `artifact_created`, and `tool_result` records for ordinary, non-task `send` and `session` calls to `internal/events.jsonl`.
Tool records preserve timestamps, MCP request IDs, normalized parsed call parameters, final results or errors, and content-block order.
`tool_result` records server assembly, not delivery; cancellation or disconnection may suppress the response.
Image blocks received during a `send` operation remain in MCP results and are also decoded byte-for-byte under the run's `artifacts/` directory immediately, including when the evaluation is never polled again; the journal replaces their base64 data with relative paths.
A background image first received while idle or during live requirement preparation is validated and queued.
It is persisted when a response drains and assembles that pending output, including the current failed preparation response, a later `send`, or a restart response.
Journal writes are flushed before a tool begins and after its result is assembled.
If the run record cannot be created or a later recording write fails, the server disables recording, emits one diagnostic to standard error, and continues serving console calls.
An existing journal may therefore end with the last successfully flushed event.
Submitted source, stdin, and tool-result output are recorded without redaction.
Generated Quarto transcripts and complete output spools do not exist yet.
Supplying exactly one of `r`, `python`, or `sql` starts one complete cell and waits for up to `timeout_ms`, which defaults to 60 seconds.
If an established worker fails during that cell, the same `send` makes one automatic replacement attempt within that deadline.
If the wait expires while the cell is still evaluating, `send` drains output produced so far, appends the newline-prefixed banner `\n[running]`, and leaves the computation running.
If it expires while the replacement is starting, the response instead ends with `[worker starting]`; later polls report the same state until the worker reports ready, then return startup output followed by `[idle]`.
Concurrent `send` calls are unsupported.
On macOS, the server remains outside the sandbox and starts one worker relay inside it for each worker generation.
The relay is the direct sandbox child and process-group leader; it starts the worker inside the same sandbox and process group.
Only relay fd 0/1/2 cross the server/sandbox boundary.
Relay stdin and stdout carry ordered framed JSONL, while relay stderr passes through outside the protocol and is normally empty.
The relay creates the unchanged worker sideband and standard streams after it enters the sandbox.
After a worker reports ready, the server continuously consumes relay events containing sideband, standard-output, and standard-error activity.
With no evaluation active, an empty `send` immediately drains the output collected so far and returns `\n[idle]`, or `\n[stdin needed]` when an idle callback has an outstanding console read.
It sends no worker frame, does not wait for idle callbacks, and is not delayed by `timeout_ms`.
Output accepted after the snapshot remains pending for a later response.
An empty call does not start an initial or stopped worker.
If it discovers an idle worker failure, it stops that worker and reports the failure without starting a replacement; a later nonempty `send` or explicit restart starts the next worker.
Supplying `stdin` with a code cell, during an evaluation, or while idle queues exact UTF-8 bytes to worker fd 0 without adding a newline, inspecting, echoing, or limiting the text, or waiting for an input request.
A nonempty idle stdin call lazily starts the worker when needed, queues the bytes, and immediately returns the current output snapshot; `timeout_ms` does not bound that startup or delay the snapshot.
Queuing bytes does not acknowledge their consumption, so the response may still report `\n[stdin needed]` until the continuous reader receives `input_received` or `input_cancelled`.
Payload end is not EOF; the R console callback reads through one newline or its supplied buffer, and unread bytes may satisfy later console or direct reads, including in a later evaluation.
Every `input_requested` frame immediately appends `[input requested: <JSON-quoted prompt>]` to pending response output.
During an evaluation its outstanding state is provisional for up to 10 milliseconds; a matching `input_received` after a successful console read or `input_cancelled` after an interrupt retains the request record but suppresses the `\n[stdin needed]` banner, while an unmatched request returns that marker after the grace or at the MCP deadline, whichever comes first.
The terminal frame describes that runtime read, not a submitted payload or byte count, and direct fd-0 reads emit neither frame.
New code is rejected until the active cell result has been collected.
Worker `image` frames carry base64 data and a MIME type.
Worker `console_output` and `console_diagnostic` frames carry ordinary and diagnostic console text.
The server retains console channels and direct fd 1/2 identity until MCP projection.
The server preserves sideband text and image order as MCP content blocks, coalesces adjacent text, and does not add `[done]` when an image is the only output.
One generation-long relay reader continuously publishes forwarded background console and image frames, handles Python-resolution, Python-version-selection, and Python-activation frames, and retains idle input state.
The relay continuously assembles worker-sideband frames and drains raw worker standard streams; the server incrementally assembles relay JSONL, so an incomplete frame cannot block retirement and idle output is not bounded by the worker sideband pipe capacity.
A worker whose background callback is waiting for a nested resolver reply queues an unrelated command, finishes the callback after the reply arrives, and then processes that command.
An explicit operation registers only its expected terminal.
At evaluation `completed`, the reader records an output-tape checkpoint so later background activity remains pending for the next response.
An idle input request can join a later evaluation's ordinary stdin flow, but fails a noninteractive requirement-preparation operation instead of leaving it blocked.
The implemented `session` surface accepts `action = "prepare"` with one or more R or Python requirement strings or DuckDB extension names, `action = "interrupt"` without requirements, or `action = "restart"` with optional R, Python, and DuckDB requirements for the implicit session.
Interrupt requests `SIGINT` for an active host resolver process group, or otherwise asks the relay to send it to the live worker, and returns `[interrupt sent]` after the resolver accepts the request or the relay reports that the worker signal succeeded, without waiting for the resolver or evaluation to finish.
It does not start a process, and a worker signal is not assigned to a cell.
The built-in worker checks pending interrupts at managed evaluation boundaries, while R, reticulate Python, and DuckDB retain their native in-evaluation handling.
User code can catch or delay the signal.
A resolver signal error is returned by both the interrupt and resolution calls; an interrupted host resolver otherwise reports its ordinary resolution failure.
Managed R and Python console input waits poll R's pending-interrupt flag and cancel when `SIGINT` arrives while runtime interrupts are active.
A wait inside R's `suspendInterrupts()` remains active until input arrives, after which the worker handles the deferred interrupt at a managed boundary.
Full callback buffers without a newline remain provisional for the current evaluation or ready-handler turn.
A newline commits that logical line; if the operation ends first, including after an interrupt between callbacks, the worker pushes every provisional chunk back ahead of fd 0 for the next managed console read.
Direct fd-0 readers do not consume that pushback, and restart discards it with the worker.
Requirements are exact, additive, and idempotent.
On macOS, plain built-in `serve` resolves the retained default R requirements `tidyverse`, `github::rstudio/reticulate`, `DBI`, `duckdb`, `arrow`, and `nanoarrow` through IR before accepting MCP input.
The GitHub reticulate requirement supplies the fork-aware output restoration required by the worker; host R must also provide reticulate to bootstrap managed Python before the worker library is applied.
The resulting library is retained across worker generations; tidyverse packages, reticulate, DBI, DuckDB, arrow, nanoarrow, and their dependency sets are available but are not attached automatically.
The built-in server then prepares the retained default DuckDB extensions `json` and `icu` in DuckDB's native cache without loading them; extensions such as `fts` remain on demand.
Before the worker starts, each successful prepare resolves the complete candidate sets outside the sandbox, atomically retains them in server memory, and returns `[prepared]` without starting the worker.
Before each R resolution, the server requires `ir --version` from `PATH` to report 0.4.0 or later; it then uses `ir run` with the worker's Rscript, and the result becomes the first worker `R_LIBS` entry.
Python requirements use reticulate and uv and replace any inherited Python selection with the resolved interpreter.
DuckDB extension requirements must start with a lowercase ASCII letter and otherwise contain only lowercase ASCII letters, digits, and underscores.
The host resolver uses the managed R library and DuckDB's own `INSTALL` statement outside the sandbox, letting DuckDB select its default repository and native version- and platform-specific extension cache.
Every newly resolved R candidate repeats the complete retained extension installation with its DuckDB version; DuckDB treats a matching warm cache as already installed.
Within a live worker generation, new extensions are also installed with every resolved R library that could have supplied the loaded DuckDB namespace, and replacement resets that target list to the retained library.
It does not load extension code outside the sandbox and does not inspect or intercept submitted SQL.
The server sets `IR_NO_LOCAL_SOURCES` for every R resolution, so IR prevents direct or transitive local package installation while retaining ownership of package-reference parsing.
A failed resolution leaves the prior retained requirements, R library, interpreter, and DuckDB extension set unchanged.
For a uv tool failure, the tool error reports a JSON manifest containing reticulate's selected Python and the complete candidate package set, followed by uv's stderr, while omitting the helper command, temporary output path, and reticulate's `py_require()`-oriented guidance.
The direct resolver process defines its process-group lifetime: after it exits, the server force-stops any remaining in-group descendants before reaping it and collecting its standard streams.
Each resolver child restores the default `SIGINT` disposition and unblocks the signal before exec.
Closing MCP input cancels an in-flight explicit or runtime resolution by force-stopping its host resolver process group; startup preflights complete before MCP input is accepted and are not cancellable through that lifecycle.
An idle worker that implements R preparation can apply new R requirements without replacement.
The server resolves the complete R requirement set outside the sandbox, prepends the new library to the live `.libPaths()`, removes the previous managed IR entry, preserves the other live library paths and in-memory state, and retains the confirmed library for later worker generations.
Each IR candidate contains the complete retained R requirement set, so replacing the previous managed entry keeps the live search path aligned with restart and crash replacement instead of accumulating stale managed libraries.
An idle server-managed worker can also materialize an uninitialized Python manifest or activate a same-`libpython` environment while preserving live state.
An idle worker that implements R preparation can prepare DuckDB extensions on the host without replacement or loss of live state.
The DuckDB resolver uses the existing cancellable resolver process-group lifecycle and adds no DuckDB-specific worker sideband messages; an accompanying R candidate still uses `prepare_r`.
A successful Python activation or explicit materialization is retained immediately.
Before forwarding any worker message or worker-sideband closure, the relay checkpoints both raw-output readers and first publishes bytes already available from fd 1 and fd 2, so a message that the server rejects cannot cause retirement before preceding raw output arrives.
After any operation terminal, the relay's worker-sideband reader waits for the server operation owner to commit terminal state and acknowledge that terminal before reading the next worker frame, so later idle activity cannot overtake that retention; raw stream readers continue draining during the barrier.
In a mixed live R, Python, and DuckDB preparation, that activation can remain retained even if a later R update fails.
The R and DuckDB configurations are retained only after the complete operation succeeds.
An earlier DuckDB install from a failed multi-extension request may remain in the host cache without entering the retained extension set.
After a live preparation failure may have partially changed the live worker, evaluation remains available so its state can be saved, but new requirement additions return `[restart required]` until a successful explicit restart.
Transport or protocol failures still stop the worker when its usability is unknown.
The server returns `[prepared]` only after the complete operation succeeds.
Preparation during an active cell is rejected.
Preparation that overlaps worker startup returns `[requirements not prepared: worker is starting]` without resolving the additions or changing the retained requirements, R library, Python manifest, or DuckDB extension set.
A failed automatic replacement leaves the worker stopped; new requirements then return `[restart required]`, and prepare does not start or configure the next replacement attempt.
Restart merges any supplied R, Python, and DuckDB additions into the complete retained sets and resolves every changed candidate before terminating the current worker.
A newly resolved R candidate repeats the complete retained DuckDB extension installation with its DuckDB version.
A failed restart resolution leaves the current worker, its in-memory state, requirements, R library, Python interpreter, and DuckDB extension set unchanged.
After every required resolution succeeds, restart commits the R library, DuckDB extension set, and Python environment together, loses all worker-owned in-memory state and unread stdin, eagerly starts a replacement, and returns `[idle]` after it reports ready.
The implicit session exists for the server lifetime, so restart starts its first worker if none exists yet.
The replacement generation becomes lifecycle ready after its `ready` frame and before its continuous dispatcher starts, so immediate resolver and activation callbacks observe the new generation as ready.
Restart completion is scoped to that generation, so a completed restart cannot mark a later overlapping restart ready.
Restart registers one relay-shutdown request and sends the relay a shutdown command with the time remaining in the existing one-second worker deadline.
The relay flushes a sequenced `shutdown_started` event before it begins worker shutdown; if the server observes that single acceptance event by the original deadline, it allows up to two additional seconds after the deadline for relay retirement without extending the worker grace.
The relay queues worker-stdin closure and the unchanged sideband shutdown message without waiting behind an active cell.
At the worker deadline it first kills the direct worker if needed, then stops every other live process whose current process group is exactly the relay's group while remaining alive as group leader, reaps the direct worker, finishes the worker stream boundaries, and flushes its final events before exiting.
Clean relay-stdin EOF performs the same worker shutdown with a new one-second grace and no `shutdown_started` event; EOF midway through a frame is a transport failure.
The server observes relay exit without reaping it, keeping the relay PID unavailable for reuse until the outer sandbox owner always closes the complete process-group lifetime and reaps the relay; this cleanup runs after either wait, including when the relay already exited, and is the fail-safe when the relay does not accept shutdown or stalls.
The sandbox owner records that retirement before releasing the process-group identity, so concurrent or repeated cleanup returns the stored result without signaling a reused relay PID or process group.
The server then waits for the active evaluation or preparation owner to release the generation and joins the relay transport tasks before reporting `[worker stopped: in-memory state lost]` or launching the replacement.
Each admitted cell or idle stdin write carries its worker generation, so work admitted before restart cannot reach the replacement.
An R preparation cancelled while its IR resolver is active reports resolver cancellation.
After preparation reaches the live worker, restart cancellation returns `R preparation cancelled by restart` when the call includes R and `Python preparation cancelled by restart` otherwise; active-generation sideband failures retain their transport diagnostics.
Without a waiting `send`, the explicit restart response preserves old-worker output, reports `[active evaluation stopped by session restart request]` when it interrupts an unfinished cell, and then returns the stopped notice when it retires a ready worker, `[starting new worker]`, replacement startup output, and `[idle]` in that order.
When a `send` is waiting on the interrupted cell, it exclusively receives old-worker output through retirement followed by `[stopped by session restart request before evaluation finished]` and, when restart retires a ready worker, `[worker stopped: in-memory state lost]`.
The server writes that `send` reply before starting the replacement or returning the restart response, which contains `[active evaluation stopped by session restart request]`, its own stopped notice when it retires a ready worker, `[starting new worker]`, replacement startup output, and `[idle]` without repeating the old-worker output.
Idle callbacks do not create a waiting `send`; continuous collection leaves their output pending for the restart response before the worker is retired.
Named sessions do not exist yet.
On macOS, default R and DuckDB extension preflights and managed-Python preflight happen during `serve` startup when required; the first nonempty stdin submission or evaluation still lazily starts a sandboxed relay and built-in worker under the same sandbox policy as the `sandbox` command.
The worker embeds R through `libr` and `harp`, retains global state, and feeds each complete R cell through R's DLL REPL iterator.
R parses and evaluates its expressions sequentially, captures console output, prints visible values, and performs native top-level bookkeeping.
Immediately before every R, Python, or SQL cell, the worker gives R's registered input handlers one nonblocking turn under an R top-level boundary.
It gives them a second turn after a normal language outcome only if worker shutdown has not begun and the cell recorded no infrastructure failure.
Shutdown or an infrastructure failure during the initial turn aborts the submitted cell; an infrastructure failure recorded by the cell skips the final turn.
After either turn, a worker-stdin hangup marks shutdown before the worker can dispatch or complete the cell, including when a callback reads fd 0 directly.
Ready callbacks run within a managed graphics scope and their output precedes cell completion.
Between cells, the worker temporarily adds the sideband descriptor to R's input-handler set and blocks in `R_checkActivity()` for either R activity or a relay-forwarded server command.
It removes that temporary handler before running R code, so fork children inherit no stale sideband handler.
R handler errors remain below `R_ToplevelExec()`, and the worker uses no worker-owned fixed polling interval or second event loop.
A generation-long server reader continuously consumes relay events, publishes forwarded idle console output and images, services nested managed-Python requests, and retains idle console-input state.
The relay assembles newline-delimited worker-sideband frames incrementally while the server does the same for relay JSONL, so a partial frame cannot block worker retirement and pipe backpressure cannot pause ordinary idle output.
Before applying a live requirement preparation, the built-in worker gives registered R handlers one nonblocking turn, so a callback already ready when the command arrives is collected first.
An empty `send` immediately snapshots an idle callback's pending output and surfaces an outstanding input request as `[stdin needed]`; a later stdin-only `send` continues it, and a call that already includes stdin can prequeue the input.
A code-bearing `send` can also continue an idle input request.
A noninteractive requirement preparation that encounters the request stops the worker instead of blocking indefinitely.
Each worker generation starts with `options(width = 200L)`; evaluated code can change that persistent option.
Cell EOF while R requires continuation input is an error; earlier complete expressions from that cell remain applied.
R parse, evaluation, and auto-print failures are normal language outcomes with `isError: false`.
The worker maps `R_WriteConsoleEx` type 0 to `console_output`, nonzero types and `R_ShowMessage` to `console_diagnostic`, and currently renders both as ordinary MCP text.
The worker installs a worker-owned `grDevices::png()` function as R's default graphics device and opens it lazily during plotting.
After a managed device's new-page or close callback returns normally, the worker immediately reads, removes, and emits the finalized PNG as `image/png` MCP content.
R console text is emitted immediately rather than deferred for unfinished plots.
Cell-end cleanup closes every still-open managed device and emits its remaining page, including after normal R errors.
Managed devices are cell scoped, so one plot's drawing operations must be submitted in the same cell.
Their default dimensions are 800 by 600 pixels at 96 DPI; persistent `console.plot.width`, `console.plot.height`, and `console.plot.dpi` options configure positive finite dimensions in inches and resolution.
Graphics devices opened explicitly by evaluated code, such as with `grDevices::png()`, remain user-owned: the worker does not close them, read their files, or emit images for them.
A silent successful R cell sends `completed` without a console-text frame and projects to `[done]` when no other response text is pending.
Submitted R functions do not currently retain a source filename.
Python cells run in the same worker through reticulate, retain `__main__` state, execute statements, and display a final expression through `sys.displayhook()`.
Bridge helpers initialize before user Python runs in the reserved `_mcp_console` module, which remains available through `sys.modules` without adding a `__main__` binding.
The reserved `_mcp_console_dispatch` builtins entry holds the stable direct-conversion callable, so changing `builtins.__import__` does not affect dispatch.
The Matplotlib load hook is registered before runtime initialization, and the module reference is retained only after import completes, so an interrupted first initialization retries without losing or duplicating the hook or its logging filter.
Dispatch does not add or remove globals, and rebinding `__import__`, `exec`, `setattr`, or other ordinary globals does not replace bridge dispatch.
Replacing `__main__.__builtins__` itself is unsupported.
Python sees a 200-column terminal width, and NumPy `linewidth` and pandas `display.width` start at 200 when those modules load; evaluated code can change those settings.
Python source uses a synthetic evaluation filename, and uncaught exceptions print a Python traceback as a normal language outcome with `isError: false`.
R plots invoked through reticulate's `r` bridge use the managed R graphics lifecycle and return as MCP images under the same sizing, cell-scope, and device-ownership rules as R cells.
At Python cell end, including after a Python error, the worker renders every open `matplotlib.pyplot` figure in memory, emits it once as `image/png`, and closes all pyplot-managed figures.
`matplotlib.pyplot.show()` is optional, and calling `savefig()` does not suppress capture while the figure remains open.
Pyplot-managed figures are cell scoped, so one plot's drawing operations must be submitted in the same cell.
Figures closed before cell end and figures not registered with `pyplot` are not captured.
Matplotlib rendering failures print a Python traceback as a normal language outcome; cell-end cleanup still closes all pyplot-managed figures and leaves the worker available.
Unless an inherited value configures it, the worker sets Matplotlib's backend to Agg.
Before Python initializes, a built-in worker resolves an existing user `matplotlibrc` from inherited `MATPLOTLIBRC`; otherwise it uses inherited `MPLCONFIGDIR`, or `$HOME/.matplotlib` when `MPLCONFIGDIR` is unset or empty, and exposes that regular file read-only through `MATPLOTLIBRC`.
It then forces Matplotlib's writable configuration and XDG cache directories under the worker's private temporary directory so font discovery can write within the sandbox.
After each server-managed environment resolves, the host resolver invokes that exact interpreter with `-I` and attempts to import `matplotlib.font_manager` before returning the environment.
Matplotlib may reuse or create its versioned font index in the inherited nonempty `MPLCONFIGDIR`, or in `$HOME/.matplotlib` when `MPLCONFIGDIR` is unset or empty.
Before replacing that setting, the worker retains the same user directory and links its regular versioned font indexes read-only into the worker's private Matplotlib directory; runtime resolution refreshes the links while the worker waits, and later generations link existing files at startup.
The user directory is never writable in the sandbox, and the server does not copy, parse, validate, or publish cache bytes.
The host resolver may create or replace user font indexes; the selected host `matplotlibrc` is read-only in the worker, while worker-created configuration, styles, TeX state, lock files, and the complete XDG cache remain worker-private.
An absent or broken Matplotlib import, unavailable user directory, or unusable font index does not reject Python resolution; Matplotlib discovers fonts in the worker-private cache when needed.
Caller-selected non-managed Python skips host prewarming, but a built-in worker can reuse a matching index already present in the inherited user directory.
The resolver import executes the selected Matplotlib package outside the worker sandbox as part of managed Python preparation and remains under the resolver process-group lifecycle.
Matplotlib remains optional, and capture inspects only modules already loaded by evaluated code.
At worker startup, MCP Console sets `RETICULATE_REMAP_OUTPUT_STREAMS=1` once, before user R can initialize Python.
Within the worker process, reticulate then routes Python standard output through R's ordinary console path as `console_output` and Python standard error, including tracebacks, through its diagnostic console path as `console_diagnostic`.
Writes through `sys.stdout.buffer`, `sys.stderr.buffer`, or native fd 1/2 bypass that remap and use the captured standard streams.
After a Python cell calls `os.fork()`, the child cannot use the sideband, so reticulate's registered CPython fork callback restores the original fd-backed text streams and ordinary stdout and stderr writes remain captured.
Native extensions that fork without running CPython's registered fork callbacks and then resume Python are unsupported.
Fork-child text capture requires reticulate from its `main` branch or a release containing fork-aware stream restoration.
An exec descendant that retains fd 1/2 creates fresh standard streams backed by those descriptors, so its ordinary stdout and stderr are captured while that worker generation's output boundary remains open.
When inherited `RETICULATE_PYTHON` is absent or exactly `managed`, built-in server startup calls reticulate's internal uv environment resolver with its NumPy and pandas baseline outside the sandbox and retains the resulting interpreter and normalized manifest for every worker generation.
Other inherited values, including an empty value, are preserved and skip the Python startup preflight but not default R or DuckDB extension resolution; a later successful explicit preparation takes precedence over them.
Custom workers skip the default R, Python, and DuckDB extension preflights but can prepare explicit R requirements and DuckDB extensions.
Every custom-worker R candidate includes DBI, DuckDB, and jsonlite so the same library can service later DuckDB extension requests.
They receive the retained library through `R_LIBS`, and a running custom worker must acknowledge live `prepare_r` requests.
Prepared extensions use DuckDB's native default cache; the server does not resolve or inject that path.
Custom workers must use the same native cache to load them.
The hidden worker option replaces the executable, but R still starts from the user-selected installation and layers resolved libraries onto it.
A custom worker must apply its first resolved R library before loading DuckDB; a DuckDB namespace loaded earlier from inherited libraries is outside the extension-preparation contract.
Custom workers reject Python additions for both prepare and restart.
R, Python, and DuckDB resolution may access the network and write normal host caches outside the sandbox; R and Python package resolution may execute package installation or build code, and managed Python environment startup and the Matplotlib font-manager import also run there.
Requirement strings remain process-argument or JSON data rather than R source, and no submitted cell is evaluated by the resolver.
Before initializing R, the worker forces `UV_OFFLINE=1`, overwriting any inherited value to match the sandbox's network denial.
Reticulate reuses the server-resolved or caller-selected interpreter.
Both managed and caller-selected interpreters must provide Python 3.10 or later; the Python bridge rejects an older interpreter before evaluating a cell.
For a server-managed worker, MCP Console seeds reticulate's requirement manifest and intercepts its internal `uv_get_or_create_env` and `resolve_python_version` bindings, without wrapping `py_require()`.
Environment resolution and Python-version selection become separate typed sideband requests, and the host resolver runs reticulate and uv with the requested `UV_*` settings outside the sandbox.
Version selection returns only the selected version and does not create a candidate environment or alter retained state.
Each environment-resolution request sends the physical resolver manifest, the logical manifest to retain if accepted, and the worker's current `UV_*` settings except `UV_OFFLINE` to the host resolver; those settings are transient inputs and are not retained or replayed.
After Python initializes, reticulate resolves late additions against the exact active Python patch version while leaving the logical `py_require()` Python constraints unchanged.
Explicit preparation sends structured additions and reports a payload-free completion or failure without evaluating a cell.
It materializes an uninitialized manifest.
After initialization, additive package requirements resolve to candidate environments outside the sandbox, and reticulate performs its exact-`libpython` check, `activate_this.py`, configuration swap, and manifest assignment.
After reticulate accepts a managed environment, the worker sends a standalone `python_activated` event.
The server immediately retains the matching resolved candidate or its unchanged current environment.
Acceptance and the restart-generation check are atomic; a receipt that remains pending when restart claims the generation is discarded with that worker.
`completed` and `python_prepared` carry no Python manifest.
When no live activation was required, a successful `python_prepared` retains the last materialized candidate.
A lazy pre-initialization `py_require()` declaration remains worker-owned until Python initializes or explicit preparation materializes it.
The live Python interpreter and its state are retained during successful activation.
Evaluated R code or an R package load can therefore trigger host resolution, which may use the network, write host caches, and execute package build backends outside the worker sandbox; the structured requirements and forwarded settings are data, and the submitted cell is not evaluated by the resolver.
Python reads R globals through reticulate's `r.name` bridge, and R reads Python globals through the worker-attached `py$name` binding.
A silent successful Python cell sends `completed` without a console-text frame and projects to `[done]` when no other response text is pending.
Python `input()` and `breakpoint()`/`pdb` use reticulate's R console bridge, so they emit `input_requested` before a read, then `input_received` after success or `input_cancelled` after an interrupt.
They accept proactively queued or follow-up stdin, including repeated debugger commands.
Python `sys.stdin` and other direct fd-0 reads bypass the bridge and emit neither frame.
SQL cells use the `duckdb` and `DBI` R packages through a private R bridge.
Previews require `nanoarrow` for DuckDB's DBI Arrow stream, `arrow` for bounded record-batch manipulation and temporary registration, and `tibble` and `pillar` for display.
The first SQL cell or call to `sql_connection()` lazily opens one in-memory connection with environment scanning enabled, and later operations in that worker generation reuse its catalog.
The connection leaves extension discovery to DuckDB while keeping secret and spill paths beneath the worker's private R temporary directory.
The sandbox permits reads but not writes to DuckDB's native extension cache and denies network access; explicit `LOAD` and DuckDB's default automatic-extension behavior execute inside the sandbox.
The connection disables DuckDB progress output so previews contain only query results.
The worker stores SQL source in private R state and calls the bridge with a short evaluation ID.
The bridge sends queries through a zero-argument closure enclosed by R's global environment, so DuckDB searches the persistent R session rather than the private bridge environment.
An unqualified catalog table or view takes precedence over a same-named R binding; otherwise DuckDB can scan a data frame in the R global environment, and an SQL view over that name observes later rebinding.
The bridge installs a forwarding active binding for reticulate's `py` and the `sql_connection()` helper in a worker-owned `tools:mcp-console` environment at search position 2, so clearing R's global environment does not remove them and same-named global bindings still take precedence.
It returns a borrowed reference to the same worker-owned connection; callers must not disconnect it.
Established DuckDB, DBI, and dplyr interfaces can use that connection, and lazy dplyr relations observe later catalog changes until collection.
Prepared queries retain scanned data frames until their DBI results are cleared.
Query results use `DBI::dbSendQueryArrow()` and one streaming `DBI::dbFetchArrow()` batch of at most 21 rows.
The worker displays at most 20 rows and 12 columns through pillar in a 200-column layout, limits cells to 160 characters, and limits the SQL preview itself to 12 KiB; the byte limit may reduce rows or columns further.
The 21st row determines only whether to append the omitted-row marker; the worker does not count or materialize the complete result.
Arrow schemas keep column names and types visible for empty results, while DuckDB stringifies only the bounded displayed batch and applies the cell limit before returning text to R so `NULL`, `BIGINT`, `DECIMAL`, and nested values remain exact when they fit.
Temporary Arrow relations use collision-checked names and are unregistered after formatting.
DDL and DML results without columns are silent; affected-row summaries do not exist yet.
DuckDB errors are normal language outcomes with `isError: false`, and the connection remains reusable.
Automatic Python relation sharing and a separate relation-registration API do not exist.
Relay-forwarded sideband text and images, worker standard-output and standard-error bytes, failures, and lifecycle notices share one pending output tape in publication order.
The relay immediately publishes available raw bytes in chunks of at most 8 KiB as base64 JSONL events, without line buffering or a coalescing timer, so the private transport is binary safe; each successful `send` response drains all tape events available at its response boundary, decoding complete UTF-8 prefixes and retaining incomplete suffixes for a later response.
Idle, running, and outstanding-input responses append the literal `\n[idle]`, `\n[running]`, or `\n[stdin needed]` banner; its leading newline is present even when no output precedes it.
After an infrastructure failure, the server finishes worker shutdown and its I/O readers before appending `[worker stopped: in-memory state lost]` after the specific error.
For a relay-owned protocol or I/O failure, the relay requests worker termination immediately but publishes `fatal` only after its worker transports have stopped and both raw-output readers have drained and joined.
After an established worker fails during a cell, the same `send` appends `[starting new worker]\n` and makes one replacement attempt.
If that attempt reports ready before the call deadline, its startup output and `[idle]` complete the failed response; if the deadline expires first, the call returns `[worker starting]` and later polls continue waiting for that same attempt.
A failed replacement remains stopped; a later call may make a new attempt, which emits its own starting notice.
Initial lazy startup and retries before a worker reaches ready are silent.
Cell completion returns text, images, and input-request records through the `completed` tape checkpoint instead of `[done]` when any content was produced.
Background activity accepted after that checkpoint remains pending for the next response.
A failed cell likewise returns all pending output before its infrastructure or protocol error.
Server-owned timeline, state, and admission facts are bracketed and separated from worker output; request-validation and standalone resolver diagnostics remain ordinary MCP tool errors.
Ordering between the two standard streams and sideband output is best effort; incomplete UTF-8 remains with its pipe until a later response, and invalid UTF-8 is replaced when output is rendered.
The built-in worker and custom workers send console prompt fields verbatim; the server preserves each value without trimming it and renders it as a JSON-quoted `[input requested: ...]` record.
Writes to inherited fd 1 or fd 2 from descendants follow the same path until worker retirement closes those relay readers; a descendant retaining fd 0 likewise cannot keep a blocked relay write alive past retirement.
On a forced worker stop, the relay kills its direct worker, terminates the other exact-group members, and then reaps the worker.
After the relay exits, the server keeps it unreaped while closing the complete outer process-group lifetime, including in-group descendants left by an earlier direct-worker exit, and then reaps it; descendants that leave that group remain unsupported.
Forked descendants cannot use the inherited sideband.
The hidden `worker` command takes ownership of the sideband, discovers `R_HOME` through the selected R executable inside the sandbox, and opens `R_HOME/lib/libR.dylib` by its absolute path.
It does not self-execute or set a dynamic-loader environment variable.
The hidden `worker-relay` command runs inside the sandbox, creates the worker transports, supervises the worker with blocking threads, and serializes relay events to fd 1.
The worker and worker-relay commands run synchronously; only `serve` creates a Tokio runtime.
The hidden development option `serve --worker PATH` replaces the built-in worker with an executable that implements the same sideband request/receipt protocol and fd-0 input contract.
The Python fixture `tests/fixtures/zod` provides deterministic acceptance coverage for R, Python, and SQL language tags at that boundary, MCP image content, direct fd-0 input, captured standard streams, and server-owned timeout and polling mechanics.
When MCP input closes, the server starts a one-second worker-shutdown deadline, asks the relay to close worker stdin and send sideband shutdown, waits through that deadline and, only after timely `shutdown_started` acceptance, allows up to two more seconds for relay retirement before cancelling any active host resolver.
The relay kills the direct worker and remaining exact-group members when its worker misses the grace period, reaps the direct worker, flushes final events, and exits.
The outer sandbox boundary keeps an exited relay unreaped until it closes the complete process-group lifetime and reaps the relay; the same path is the fail-safe if the relay does not accept shutdown or stalls.
The version command prints the package name and version.
On macOS, the sandbox command launches a subprocess under `sandbox-exec` with host filesystem reads allowed, regular-file writes limited to a dedicated per-launch temporary directory, runtime device and IPC exceptions, and network access denied.
This initial launcher waits only for the direct command.
Background descendants are unsupported: they may outlive the launcher, which attempts to remove their dedicated temporary directory on a best-effort basis when it returns.
Descendant supervision is intentionally deferred because it must account for process groups, session-detached children, signal forwarding, and PID reuse together.
The sandbox command, worker relay, and worker are unsupported on Linux and Windows.
Named sessions, Python relation sharing, the sidecar API, viewer, complete output retention, and generated Quarto transcripts do not exist yet.

## Product direction

MCP Console is intended to become a persistent, sandboxed R, Python, and DuckDB SQL console exposed through MCP.
The public MCP surface has two tools:

- `send` evaluates complete R, Python, or SQL cells, writes to the session's stdin stream, and polls for output.
- `session` manages session requirements and lifecycle operations.

R and Python requirement preparation, DuckDB extension preparation, live late additions, best-effort resolver or worker interruption, and explicit restart with optional additive R, Python, and DuckDB requirements are implemented for `session`; its broader lifecycle surface remains planned.

The MCP initialization identity remains `mcp-console`.
The intended default client registration name is `console`, for example `codex mcp add console -- mcp-console serve`.
Under Codex's current naming convention, the tools are `mcp__console.send` and `mcp__console.session`.
The implemented R slice embeds R through `libr` and `harp`.
The implemented runtime uses R as the host, embeds Python through reticulate, and owns an in-memory DuckDB connection through the `duckdb` and `DBI` R packages.
The backend for that broader runtime surface remains an open design decision.

See `design-sketches/README.md` for the product overview and `design-sketches/docs/ARCHITECTURE.md` for the tentative architecture.

## Repository map

- `Cargo.toml` — Rust package metadata.
- `build.rs` — macOS C-shim build.
- `src/main.rs` — current binary entry point.
- `src/cell.rs` — language-neutral complete-cell type shared by the server and worker protocol.
- `src/cli.rs` — clap command definitions and user-facing help.
- `src/python.rs` — worker environment and reticulate bridge.
- `src/resolver.rs` — platform-gated host-resolver facade.
- `src/resolver/managed_duckdb.rs` — macOS host-side DuckDB extension installation.
- `src/resolver/managed_r.rs` — macOS IR-backed R library resolution.
- `src/resolver/managed_python.rs` — macOS reticulate/uv-managed Python resolution.
- `src/resolver/process.rs` — shared macOS resolver process-group lifecycle and cancellation.
- `src/resolver/unsupported.rs` — non-macOS resolver stubs.
- `src/r_bridge.rs` — shared private R-environment bridge used by graphics, Python, and SQL adapters.
- `src/r_environment.rs` — private bridge for updating the live R library search path.
- `src/r_graphics.c` — C-owned forwarding boundary for managed graphics-device callbacks that may long-jump.
- `src/r_graphics.rs` — cell-scoped managed R graphics device and PNG image publication.
- `src/server.rs` — MCP stdio server, `send` tool, and worker selection.
- `src/server_transport.rs` — stdio response delivery and interrupted-restart ordering.
- `src/sql.rs` — persistent DuckDB/DBI SQL bridge and bounded streaming Arrow previews.
- `src/transcript.rs` — append-only MCP tool journal and image artifact persistence.
- `src/r_repl.c` — C-owned per-cell DLL-REPL iterator and long-jump boundary.
- `src/sideband.rs` — relay-created inherited-pipe JSON-lines worker transport.
- `src/worker.rs` — embedded R initialization, cell dispatch, and console callbacks.
- `src/worker_client.rs` — server-side worker orchestration and lazy worker access.
- `src/worker_client/environment.rs` — requirement preparation and retained managed environments.
- `src/worker_client/activity.rs` — generation-long forwarded-sideband dispatcher and operation-terminal routing.
- `src/worker_client/evaluation.rs` — per-cell stdin, input-request, and wait state.
- `src/worker_client/lifecycle.rs` — worker generations, restart coordination, and process shutdown.
- `src/worker_client/output.rs` — response assembly and captured standard-stream buffering.
- `src/worker_client/macos.rs` — macOS sandboxed-relay launch, relay dispatcher, and generation process control.
- `src/worker_client/unsupported.rs` — non-macOS worker-runtime stubs.
- `src/relay_protocol.rs` — private ordered JSONL protocol between the host server and sandboxed relay.
- `src/worker_protocol.rs` — shared sideband message definitions.
- `src/worker_relay.rs` — sandboxed worker launch, local I/O forwarding, signaling, and reaping.
- `src/sandbox.rs` — platform dispatch for the sandbox process launcher.
- `src/sandbox/` — platform implementation and macOS Seatbelt policy.
- `tests/cli.rs` — public binary acceptance tests.
- `tests/fixtures/py_require` — minimal R package that declares a Python requirement from its load hook.
- `tests/fixtures/r_install_escape` — local R package whose configure hook proves rejected sources are not installed.
- `tests/fixtures/zod` — executable Python sideband worker used by acceptance tests.
- `tests/fixtures/worker_mitm` — transparent worker proxy used to capture sideband, standard-stream, fd-0 closure, and worker-sideband closure events through `serve`.
- `tests/transcripts/r.py` — public built-in R worker acceptance suite.
- `tests/transcripts/r_requirements.py` — real-IR R requirement preparation and failure suite.
- `tests/transcripts/python.py` — public reticulate Python-cell acceptance suite.
- `tests/transcripts/sql.py` — public persistent-DuckDB acceptance suite.
- `tests/transcripts/worker.py` — public-server acceptance plus captured built-in worker wire events.
- `tests/transcripts/_run.py` — discovers transcript suites, checks the selected initialization reference case first, runs remaining cases in parallel by default, and compares case snapshots.
- `tests/transcripts/_support.py` — shared transcript types and MCP stdio client.
- `tests/transcripts/<suite>.py` — suites of named imperative transcript cases.
- `tests/transcripts/golden/SUITE/` — human-readable YAML 1.2 case transcripts.
- `tests/transcripts/README.md` — transcript test usage and authoring guide.
- `scripts/test` — builds the binary and runs selected external Python tests through `uv`.
- `scripts/format` — attempts each repository-wide formatter without requiring it.
- `scripts/check` — local formatting, Clippy, and test checks.
- `.github/workflows/ci.yaml` — formatting, Clippy, and test checks.
- `docs/TOOL_DESCRIPTIONS.md` — exact registered MCP tool and property descriptions.
- `docs/RELAY_PROTOCOL.md` — exact private server-to-relay transport and sandbox process boundary.
- `docs/WORKER_PROTOCOL.md` — exact implemented worker launch and sideband protocol.
- `design-sketches/` — tentative product and architecture documents.
- `README.md` — current user-facing project status.
- `LICENSE` — project license.

Refactor and reorganize internal modules and files freely when the implemented feature set has a clearer natural structure.
Do not add structure for planned or speculative behavior.
Treat roughly 500 lines of production source as a prompt to reassess a file's boundaries.
Split a file when it contains distinct responsibilities that can be named and understood independently.
The threshold is a review trigger, not a hard limit: keep cohesive code together and do not create thin modules solely to meet it.
Keep one Cargo package until the implemented code presents a concrete crate boundary.

## Working rules

- Keep PRs coherent, compact, and easy to review.
  For behavior-changing implementation, aim as a heuristic to keep changes under 200 added and deleted lines.
  Mechanical moves, internal-only reorganization, tests, golden snapshots, and documentation do not count toward this guideline.
  The line count is not a limit; prefer a larger coherent change over splits that make the work harder to understand or validate.
- Keep each behavior-changing PR to one coherent observable behavior.
  Internal-only refactors may be standalone and must preserve observable behavior.
  Update design documents in the same PR only when they describe the changed behavior.
- For every public-facing behavior change, add a public acceptance or regression test first and confirm that it fails before implementing the change.
  An internal-only refactor does not need a new test; verify it with the existing public test suite.
- Test through public interfaces.
  Do not add tests for private helpers.
- Preserve client-visible runtime output in transcript snapshots, including complete errors and tracebacks.
  Do not replace it with summaries or placeholders.
  Normalize only incidental fragments that create diff noise, such as run-specific temporary paths, and otherwise leave the content and ordering unchanged.
- Format embedded R, Python, SQL, and shell test programs as multiline raw strings.
  Use escape sequences such as `\n` only when the program needs that character as data, not to lay out its source.
- Keep complete code cells separate from interactive `stdin`.
- Keep the MCP adapter independent of interpreter implementation details.
- Treat submitted R, Python, and SQL execution as shell-class capability and place safety at the worker-process boundary.
  Default environment startup and explicit R, Python, or DuckDB requirement resolution are host-bootstrap exceptions.
  R requirements are IR command arguments resolved with `IR_NO_LOCAL_SOURCES`; Python requirements use a JSON standard-input manifest; DuckDB requirements are validated extension names passed to DuckDB's own installer.
  None is evaluated as submitted R or SQL source, though accepted package installation, build code, managed Python startup, and the Matplotlib font-cache import may execute outside the worker sandbox.
- Update this file when a PR changes the implemented surface or repository map.
- Before every commit, run `scripts/format` and review its changes.
- Run `scripts/check` before opening a PR.
