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

The optional `timeout_ms` defaults to 60,000 milliseconds.
It limits only how long a call waits after starting or attaching to an evaluation, including its worker startup and one automatic replacement attempt.
A stdin-only call with no active evaluation instead waits without that deadline if it must start an initial or stopped worker.
The deadline does not stop worker startup, dependency resolution, or evaluation.
When the deadline expires, the response contains output available so far and ends in a state notice such as `[running]` or `[worker starting]`.

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

When one call contains both nonempty `stdin` and a cell, the bytes are queued before the evaluation command.
That is a transport-order guarantee, not a consumption guarantee.
An already waiting idle read can consume the bytes before the new cell begins.
A stdin-only call can queue bytes while an evaluation is running or while the worker is idle; it starts the worker lazily when necessary.

The built-in worker reports managed reads from:

- R `readline()` and `browser()`; and
- Python `input()`, `breakpoint()`, and `pdb` when they use reticulate's R console bridge.

A reported read adds a record such as `[input requested: "name> "]`.
If it remains outstanding after a 10-millisecond exposure grace, the response ends in `[stdin needed]`.
Input that was already queued can satisfy the read before that marker is returned.
The grace controls only when the request becomes visible; it is not a read timeout.

Direct reads from Python `sys.stdin`, fd 0, or a descendant bypass managed input reporting.
They can consume bytes still queued on fd 0 but produce no request or receipt record.
They cannot consume a partial line that the built-in worker has preserved for the next managed console callback.

## Interruption

`session(action = "interrupt")` first targets an active host dependency resolver; otherwise it requests `SIGINT` for the live worker.
The call returns after the request or signal is sent, not after user code stops.
If neither a resolver nor a worker is running, the call does not start a worker and returns the tool error `worker is not running`.
A resolver signal error is returned by both the interrupt and resolution calls, and the server stops that resolver during cleanup; an interrupted resolver otherwise reports its ordinary resolution failure.

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
`show()` is optional and is replaced with a no-op for the noninteractive runtime.
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

Bracketed records such as `[running]`, `[stdin needed]`, `[idle]`, and worker-replacement notices are server state, not language output.
R errors, Python exceptions, and DuckDB errors are ordinary console text and normally leave the worker reusable.
Host dependency-resolver failures are MCP tool errors, but preserve any current worker and its in-memory state; [requirements and environments](REQUIREMENTS.md) describes their request-specific effects.
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
