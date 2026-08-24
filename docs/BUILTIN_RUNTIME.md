# Built-in runtime

**Status:** Implemented current behavior

This document describes the console behavior visible to users of the built-in worker.
It covers R, Python, DuckDB SQL, input, output, plots, and interoperability.
The [worker protocol](WORKER_PROTOCOL.md) defines the lower-level contract for built-in and custom workers, while [requirements and environments](REQUIREMENTS.md) owns dependency preparation.
The [registered tool descriptions](TOOL_DESCRIPTIONS.md) mirror the current agent-facing MCP text.

## Session model

MCP Console provides one implicit session.
Each worker generation contains:

- one persistent R global environment;
- one persistent Python `__main__` namespace embedded through reticulate; and
- one persistent in-memory DuckDB connection and catalog.

Objects, imports, options, attached packages, database objects, and unread standard input remain available across cells in the same worker generation.
Language errors do not reset the worker, and changes made before an error remain applied.
Restart, worker replacement, or server exit discards all in-memory state.
Prepared requirements are server-owned and survive restart as described in [Requirements and environments](REQUIREMENTS.md).

Only one cell can run at a time.
Call `send` sequentially and collect a running cell before submitting another.

## Cells and polling

A `send` call accepts exactly one complete `r`, `python`, or `sql` cell.
The source is not an interactive fragment assembled across calls.
R uses its native top-level evaluation behavior; Python parses the entire submitted source before executing it; SQL passes the complete string to DuckDB.

Use a REPL-style workflow: submit one coherent cell, inspect its result, then submit the next cell based on what the result showed.
One assistant turn can make several sequential calls.
Leave the primary result last so the runtime displays it normally; use explicit printing only for additional intermediate output.
R also autoprints earlier visible top-level expressions.

For example, an analysis can progress through these calls:

```r
jobs <- read.csv("hpc_jobs.csv")
str(jobs)
```

```r
jobs$queued <- jobs$pending_job_count > 0
aggregate(queued ~ protocol_id, jobs, mean)
```

```python
jobs = r.jobs
jobs.shape
```

Each call reuses state created by earlier calls, and its output informs the next cell.

A code-bearing call can declare additive R packages, Python packages, or DuckDB extensions in `requirements`, regardless of the cell language.
The server prepares changed requirements before it dispatches the cell and retains successful additions for later cells and restarts.
Already-retained requirements add no preparation work, and a successful combined call returns only the normal cell result, without `[prepared]`.
Explicit preparation makes packages and extensions available but does not attach, import, or load them.
R resolves missing plain package names when execution reaches a supported package-loading operation.
The built-in managed Python environment likewise resolves a missing import when Python's ordinary import finders cannot satisfy it.
Neither language's source is scanned in advance, and MCP Console does not replay a cell after a package-load or import failure.
If explicit requirement validation, pre-dispatch resolution, or live preparation fails, or if requirement changes require an explicit restart, the call is a tool error and the cell is not run.
Calls without `requirements` skip that pre-dispatch preparation path; R package loads and managed Python imports can still resolve packages while the cell runs.

The optional `timeout_ms` defaults to 60,000 milliseconds.
For a combined call, requirement resolution and live preparation finish before the cell is dispatched and before this deadline begins applying, so the complete MCP call can take longer than `timeout_ms`.
The deadline then limits only how long a call waits after starting or attaching to an evaluation, including its worker startup and one automatic replacement attempt.
A stdin-only call with no active evaluation instead waits without that deadline if it must start an initial or stopped worker.
The deadline does not stop worker startup, dependency resolution, or evaluation.
Automatic R and Python import resolution begins after the cell is dispatched and remains part of that active evaluation.
If it outlives the call's wait, the response can end in `[running; poll with an empty send]` while the resolver and cell continue.
When the deadline expires, the response contains output available so far and ends in a state notice such as `[running; poll with an empty send]` or `[worker starting]`.

Call `send` again without code or standard input to poll.
While an evaluation is active, the poll waits up to its own deadline and collects newly available output.
While the worker is idle, an empty poll returns immediately with pending output and `[idle]`.
It does not start an initial or stopped worker.

Evaluation completion returns collected text and images, or `[done]` if the completed region has no other content.
New code is not admitted while an evaluation or its uncollected result is active.

## Standard input and managed reads

The `stdin` field contributes the UTF-8 bytes of its string to one generation-long standard-input stream.
MCP Console:

- does not append a newline;
- does not echo the bytes;
- treats an empty string as no input;
- does not close the stream at the end of a payload; and
- does not acknowledge that a runtime consumed the bytes.

Line-oriented reads normally need an explicit `\n`.
Bytes that no reader has consumed remain queued on fd 0 for later reads or later cells.
When a managed read has already consumed part of a line, the built-in worker preserves that prefix as managed-console pushback if the read is interrupted or the operation ends between console callbacks.
Only a later managed console callback can replay the preserved prefix.
To complete that line, the next reader must therefore be managed; Python `sys.stdin`, direct fd-0 reads, and descendants cannot consume the prefix.
All unread input and managed-console pushback are discarded when the worker generation ends.

When one call contains requirements, nonempty `stdin`, and a cell, requirement preparation completes first.
The bytes are then queued before the evaluation command.
That is a transport-order guarantee, not a consumption guarantee.
An already waiting idle read can consume the bytes before the new cell begins.
A stdin-only call can queue bytes while an evaluation is running or while the worker is idle; it starts the worker lazily when necessary.

The built-in worker reports managed reads from:

- R `readline()` and `browser()`; and
- Python `input()`, `breakpoint()`, and `pdb` when they use reticulate's R console bridge.

A reported read adds a record such as `[input requested: "name> "]`.
If the request is still outstanding when either its 10-millisecond exposure grace ends or the call reaches its deadline, the response ends in `[waiting for stdin]`.
Input that was already queued can satisfy the read before that marker is returned.
The grace controls only when the request becomes visible; it is not a read timeout.

Interactive debugger state also persists between calls.
For example, this R cell enters `browser()`:

```r
inspect_mean <- function(x) {
  browser()
  mean(x)
}

inspect_mean(c(1, 2, 3))
```

After the response shows `Browse[1]>`, send `sys.calls()\n`, `ls.str()\n`, or `x\n` through `stdin` without a code field.
Send `c\n` to continue or `Q\n` to quit the debugger.
Do not submit another code cell until the active evaluation finishes.

Direct reads from Python `sys.stdin`, fd 0, or a descendant bypass managed input reporting.
They can consume bytes still queued on fd 0 but produce no request or receipt record.
They cannot consume a partial line that the built-in worker has preserved for the next managed console callback.

## Interruption

`session(action = "interrupt")` first targets an active host dependency resolver; otherwise it requests `SIGINT` for the live worker.
The call returns after the request or signal is sent, not after user code stops.
If neither a resolver nor a worker is running, the call does not start a worker and returns the tool error `worker is not running`.
A resolver signal error is returned by both the interrupt and resolution calls, and the server stops that resolver during cleanup.
An interrupted automatic R or Python resolver reports an interrupted outcome to the running cell.
A live but idle built-in worker still receives `SIGINT` and the call returns `[interrupt sent]`.
R may consume that signal while idle or at the next managed boundary, so the following cell can execute while its response contains a bare interrupt newline attributable to the earlier interrupt.

R, Python, and DuckDB observe interruption through their normal console/runtime mechanisms.
Managed console reads are cancelled when the active runtime accepts the interrupt.
User code can catch, delay, replace, or block `SIGINT`, so interruption is cooperative rather than a termination guarantee.
Use restart when the worker must be replaced.

## Explicit restart

`session(action = "restart")` retires the current worker, discards its in-memory state, and starts a replacement from the retained environment.
The restart call waits for retirement and replacement startup.
On success after a worker was ready, its response ends with `[worker stopped: in-memory state lost]`, `[starting new worker]`, replacement-startup output, and `[idle]`.
Restarting a session that has not established a worker omits the worker-stopped notice.
Requirements supplied with restart are resolved before retirement as described in [Requirements and environments](REQUIREMENTS.md).

Without a waiting `send`, restart owns pending output from the old worker.
If it stops an unfinished evaluation, that output is followed by `[active evaluation stopped by session restart request]` before the worker and replacement notices.

When restart interrupts a `send` that is still waiting on an unfinished evaluation, the two calls keep separate response ownership.
The waiting `send` receives its retained text and images, followed by `[stopped by session restart request before evaluation finished]` and, when a ready worker was retired, `[worker stopped: in-memory state lost]`; that `send` is a tool error.
The restart call waits for the waiting response to be written, then returns its own active-evaluation, worker-loss, and replacement notices without repeating the worker output.
If the waiting response cannot be delivered, restart reclaims its output so it is returned exactly once.
A waiting `send` whose evaluation finishes before restart interrupts it receives its normal completed response.

## R

R cells run in persistent global state through R's native console loop.
Global bindings and `.Last.value` remain available to later calls.
R parse, evaluation, and print errors are console output followed by normal completion; the worker stays reusable.
Because R consumes top-level expressions as a console does, earlier complete expressions may take effect before a later expression in the same cell fails or remains incomplete.

Between cells, the worker continues servicing R event handlers such as `later` callbacks, which can mutate persistent R state and produce output.
Output produced while idle remains pending until a later response drains it; when that response belongs to a new cell and both regions contain output, `[output produced while idle]` separates them.

Ordinary R console output and diagnostics remain distinct worker channels but both appear as MCP text.
The built-in startup width is 200 columns; evaluated code may change its options.
Packages prepared for the session are available but are not attached automatically.

### On-demand R packages

The built-in worker can prepare a missing plain R package name while the current cell is running.
This covers direct `library()`, `require()`, `requireNamespace()`, and `loadNamespace()` calls and package use through `::` and `:::`.
Use these operations normally; there is no need to probe package availability or call `install.packages()` first.

The worker wraps `base::library` and `base::loadNamespace` and delegates to their captured originals after any required activation.
A `loadNamespace()` wrapper alone would miss `library()` because `library()` checks `find.package()` first.
The wrappers preserve ordinary R behavior: `library()` and `require()` attach only when the original call does, while `::`, `:::`, `requireNamespace()`, and `loadNamespace()` load a namespace without attaching the package.
They bypass automatic resolution for already available packages, `library()` help and listing calls, an explicit non-NULL `lib.loc`, and partial namespace loads.

Runtime discovery accepts plain package names only.
Use `requirements.r` to stage a package before evaluation or to supply an explicit IR reference such as a remote source.
The worker does not inspect R source before evaluation.
Each missing package is resolved only when execution reaches a covered operation, so unreachable or quoted code does not invoke IR and several new packages in one cell can cause several incremental IR calls in execution order.

When the server returns a candidate library, the worker prepends it through the managed `.libPaths()` bridge and reports activation before resuming the original base call.
The server retains the library only after that report.
The worker is not replaced, so its PID, R globals, loaded namespaces, Python objects, DuckDB catalog, and unread input remain available.
Once activation succeeds, the retained environment survives later namespace or cell errors and is reused by later cells and restart.

An ordinary resolution failure follows the original base operation: `library()` and namespace loads report R errors, while `require()` may return `FALSE`; the worker remains reusable.
An activation failure also leaves the worker available for state recovery, but further requirement changes need restart because the live and retained library state may differ.
An unchanged restart or shutdown cancels an active resolver and discards candidates owned by the old worker generation.
A restart that adds requirements waits for active environment resolution before preparing its additions and replacing the worker; generation checks still prevent an unactivated old candidate from committing.
Transport, protocol, and bridge-infrastructure failures retain the normal worker-failure behavior.

The worker installs `py` and `sql_connection()` in `tools:mcp-console` at search position 2.
R can read Python globals through `py$name` and use the borrowed DuckDB connection through DBI or dplyr.
Do not disconnect the connection returned by `sql_connection()`.

## Python

Python cells execute in one persistent `__main__.__dict__`.
Imports, assignments, functions, and objects remain available across cells and through R's `py$name` bridge.
The final expression of a cell is displayed through Python's normal display hook; source is not echoed.

An uncaught exception prints its traceback and completes as a language outcome.
The Python session remains usable, including state established before the exception.
Python 3.10 or later is required.
The built-in startup display width for NumPy and pandas is 200 columns, and evaluated code may change it.

Reticulate maps ordinary Python standard output and diagnostics into the R console channels.
Writes to binary stream buffers, native fd 1 or 2, and descendant process streams use the captured standard streams instead.
There is no guaranteed chronology between independent sideband, stdout, and stderr sources, although each source's order is preserved.

Asynchronous Python work runs only when user code starts and manages it explicitly.
MCP Console does not add notebook event-loop behavior.

### On-demand Python packages

The built-in server-managed Python environment resolves missing imports while the current cell runs.
Import the packages appropriate for the task directly; do not probe for their installation or run pip in the worker.
Availability queries such as `importlib.util.find_spec()` inspect the current environment without triggering resolution.
Successful resolution emits no `[prepared]` marker.
When the import and inferred distribution have different names, the server reports the committed mapping, for example `[resolved PyPI distribution 'py-yaml12' for Python import 'yaml12']`.
Same-name resolution emits no notice.

The private runtime appends a finder to `sys.meta_path` after Python's existing finders.
Built-in, frozen, standard-library, local, already-installed, and already-loaded modules therefore resolve normally before MCP Console sees an import.
Ordinary `import` statements, `from ... import ...`, and `importlib.import_module()` all use this machinery.
Missing optional imports reached while the default NumPy or pandas package is initializing stay on Python's ordinary path, so importing either available default does not start host resolution.
Import an optional dependency directly after initialization, or declare it through `requirements.python`, when it is needed.
When every earlier finder misses, MCP Console takes the top-level name from the requested import.
A curated table maps established differences such as `yaml` to `pyyaml`, `PIL` to `pillow`, and `sklearn` to `scikit-learn`.
For other conservative ASCII identifiers, it assumes that the PyPI distribution has the same name as the top-level module.
Automatic inference produces one bare distribution name; it does not infer versions, extras, markers, URLs, paths, or other requirement syntax.

MCP Console declines the fallback when it cannot safely identify one distribution.
This includes broad shared namespaces such as `google`, `azure`, `zope`, `opentelemetry`, and `backports`, a missing submodule whose top-level package is already present, and a standard-library module absent from the selected Python build.
The resulting import error asks for the correct distribution through `requirements.python` when explicit preparation can help.
A direct missing-submodule import retains its ordinary `ModuleNotFoundError`; for the exact submodule lookup performed by `from package import missing`, MCP Console uses `ImportError` so CPython does not suppress the guidance.
Both forms report the full missing-submodule name.

Resolution starts only when execution reaches the missing import.
Python source is not scanned, so imports in unreachable branches or uncalled functions do not invoke the resolver.
Each reached missing import resolves in execution order, and the cell is never replayed.

The finder calls the private R bridge, which adds the inferred distribution to reticulate's managed manifest and asks the existing host uv resolver for a compatible environment.
After reticulate activates that environment, the worker reports the complete manifest to the server.
Only then does the original import resume against invalidated import caches.
Preparation makes the distribution available; the original import still performs the import normally.
The automatic resolver request carries a differently named import and distribution together, and the server adds the bounded notice when it commits the matching activation.

This transition does not restart the worker or Python interpreter.
Python and R globals, Python objects, the DuckDB catalog, worker PID, and stdin state remain available.
The server retains a successfully activated environment for later cells and restart, even if the inferred distribution does not provide the requested module or later code in the cell fails.
An ordinary resolution failure before activation restores the earlier reticulate manifest and leaves the worker usable.
Errors include the inferred distribution, the host resolver diagnostic when available, and an explicit `requirements.python` recovery example.

Use `requirements.python` when the correct distribution differs from the inferred name, a version, extra, or environment marker is needed, a namespace is ambiguous, or the package should be prepared before the cell starts.
Explicit preparation accepts supported named PEP 508 registry requirements and does not import the package.

Automatic resolution can call R and reticulate only from the main worker process and the Python thread that configured the runtime.
A missing import reached from a fork child or another Python thread reports that the distribution must be prepared before that child or thread starts; it does not invoke the host resolver.
Imports already handled by ordinary Python finders remain available in those contexts.

A nonempty user-selected `RETICULATE_PYTHON` disables both automatic managed resolution and `requirements.python`.
Its missing-import error directs the user to install the distribution into that environment or restart MCP Console with managed Python enabled.

Automatic import resolution counts toward the active evaluation's `timeout_ms` wait.
A short wait can therefore return `[running; poll with an empty send]`; poll with an empty `send`, interrupt the active resolver with `session(action = "interrupt")`, or restart according to the normal generation lifecycle.

## R and Python interoperability

The two languages share reticulate's live bridge:

- Python reads R globals and calls R functions through `r.name`;
- R reads and writes Python globals through `py$name`; and
- objects converted or proxied by reticulate remain subject to reticulate's conversion rules.

An R data frame can be queried by name from SQL.
A Python data frame is not automatically visible to SQL; bind or convert it to an R global first.
Objects and proxies tied to a worker generation become invalid when that generation ends.

## DuckDB SQL

The first SQL cell or call to `sql_connection()` lazily creates one in-memory DuckDB connection.
Later SQL cells, DBI calls, and dplyr relations reuse its catalog.
DuckDB CLI dot commands are not supported.

Environment scanning lets an unqualified relation name refer to an R data frame in global state.
A DuckDB table or view with the same name takes precedence.
A view over an R data-frame name observes a later rebinding when queried.
The SQL adapter does not expose Python objects as relations and adds no separate registration API.

Query results are previews, not complete result materializations for display.
The preview:

- fetches at most 21 rows, using the twenty-first only to detect additional rows;
- displays at most 20 rows and 12 columns;
- truncates displayed cell values to 160 characters;
- formats at a width of 200 columns; and
- fits the complete preview, including omission markers, within 12 KiB by removing rows and then columns if necessary.

The renderer reports omitted rows, columns, and truncated cells.
It does not count the complete query result.
Queries with result columns but zero rows still return column names, Arrow types, and `[0 rows]`.
Statements with no result columns, including DDL and DML, return no preview and do not report affected-row counts.

DuckDB and DBI errors are printed as ordinary console errors and leave the connection available for later cells.
Extension preparation and sandbox constraints are documented in [Requirements and environments](REQUIREMENTS.md).

## Plots and images

### R graphics

R's managed default graphics device opens lazily during a cell and returns PNG pages as MCP image blocks.
Any managed pages still open at cell end are finalized, including after an ordinary R language error.
Text can appear before an image whose page is still open.

Managed default devices are cell scoped.
Later cells cannot add layers to an earlier managed plot, so all operations for one plot must be in the same cell.
The default is 800 by 600 pixels at 96 DPI.
Persistent options select positive finite dimensions and resolution:

```r
options(
  console.plot.width = 8,
  console.plot.height = 6,
  console.plot.dpi = 144
)
```

Width and height are in inches.
Devices opened explicitly by user code are user-owned: MCP Console does not close them, read their files, or return their images.

### Matplotlib

At the end of every Python cell, including after an exception, the worker renders each open `matplotlib.pyplot` figure in figure-number order, returns it as PNG, and closes all pyplot-managed figures.
`show()` is optional and is replaced with a no-op by the runtime.
When `MPLBACKEND` is absent, the worker sets it to Matplotlib's noninteractive `Agg` backend.
A nonempty inherited `MPLBACKEND` takes precedence and may select an interactive backend that fails inside the sandbox.
Calling `savefig()` does not suppress return of an open figure; calling `close()` before cell end does.
Figures not registered with pyplot are not captured.

The built-in worker preserves an existing host `matplotlibrc` selected through inherited `MATPLOTLIBRC` or `MPLCONFIGDIR` (falling back to `$HOME/.matplotlib`) while redirecting Matplotlib configuration and cache writes to worker-private storage.
It can reuse matching host font indexes read-only; sandboxed worker code does not modify the host configuration or cache.

R plots created through Python's `r` bridge follow the R graphics rules.

## Output and notices

Console output, diagnostics, captured stdout and stderr, input-request records, images, and server lifecycle notices are assembled in one server-owned output stream for delivery.
The server preserves each producer's order but cannot reconstruct chronology across independent pipes.
It does not normalize carriage returns, ANSI sequences, or ordinary whitespace.
Invalid UTF-8 from raw standard streams is replaced when projected to MCP text; the private relay transport still preserves the bytes.

Bracketed records such as `[running; poll with an empty send]`, `[waiting for stdin]`, `[idle]`, mapped Python import resolutions, and worker-replacement notices are server state, not language output.
R errors, Python exceptions, and DuckDB errors are ordinary console text and normally leave the worker reusable.
Warnings are ordinary runtime output too.
Inspect warnings before relying on a result, especially warnings about coercion, overflow, dropped observations, or model convergence.
Host dependency-resolver failures during explicit preparation are MCP tool errors, but preserve any current worker and its in-memory state.
An ordinary automatic R resolver failure is instead reported inside the running R evaluation.
An ordinary automatic Python failure becomes an actionable `ModuleNotFoundError` in the running Python evaluation.
[Requirements and environments](REQUIREMENTS.md) describes the request-specific effects.
Worker, relay, and protocol failures are MCP tool errors and may stop and replace the worker.

If initial lazy startup for a code-bearing `send` fails before the worker reaches `ready`, the call reports startup failure details without worker-loss or replacement notices, and its cell is not replayed.
A later code-bearing `send` makes a fresh startup attempt for only its new cell; the server does not add replacement notices.

When an established worker fails during a cell, the server does not run that cell again.
The failing `send` retains available output and failure details, adds `[worker stopped: in-memory state lost]`, and starts one automatic replacement attempt.
It waits for that attempt only for the call's remaining wait time.
`[starting new worker]` marks the start of that attempt.
The response then:

- ends in `[idle]` if the replacement becomes ready;
- ends in `[worker starting]` if the call's wait time expires first; or
- includes replacement-startup failure details if the attempt fails.

The original `send` remains an MCP tool error even when it ends in `[idle]`; that notice means only that later cells can run in a fresh worker.
After `[worker starting]`, poll with `send` without code or stdin until the replacement reaches `[idle]` or reports startup failure.
Do not submit another cell while replacement startup is still active.
The failing call does not repeat a failed startup attempt; after that failure is collected, a later code-bearing `send` makes a fresh startup attempt and, if it succeeds, runs only the new cell.

Each undrained output segment is limited to:

- 8 MiB of console text and raw standard-stream bytes;
- 8 MiB of encoded image data;
- 64 KiB of image MIME-type data; and
- 4,096 ordinary output events.

The first event that exceeds a limit adds a typed `[output truncated: ...]` notice.
A fitting text prefix is retained, while an image that does not fit is omitted as a whole.
After that first overflow, all later console text, raw standard-stream output, and images in the same undrained segment are discarded, even if another budget still has room.
Lifecycle and control events remain available.
The separate 12 KiB SQL-preview limit is applied before SQL text enters these budgets.

When session recording is active, an evaluation image that passes the pending-output limits is persisted immediately and remains associated with the `send` call that started the evaluation.
It can therefore appear in the recording before a later poll returns it.
By contrast, an image produced while the worker is idle or during dependency preparation is persisted only when a later tool response returns it, and is associated with that responding call.
An image omitted by the pending-output limits is not recorded.
The [implemented architecture](ARCHITECTURE.md) describes the session record and artifact files.

## Current limitations

- There is one implicit session and no named-session interface.
- `send` calls are sequential; concurrent calls are unsupported.
- Restart and failure replacement discard every in-memory language, database, debugger, graphics, and unread-input state.
- No general worker-frame or stdin-queue size limit is defined.
- Direct fd-0 readers do not participate in managed input notifications.
- SQL cannot query Python objects until they are bound as R data.
- SQL previews do not include affected-row counts or total result counts.
- Only default-device R graphics and open pyplot figures are captured automatically.
- Descendants that leave the relay's process group are unsupported.
- Linux and Windows are not supported.

The [architecture](ARCHITECTURE.md) explains lifecycle and process ownership.
The [worker protocol](WORKER_PROTOCOL.md) defines exact message and closure rules.
Source and public transcript tests are authoritative if this document and implementation disagree.
