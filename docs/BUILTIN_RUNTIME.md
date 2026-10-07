# Built-in runtime

The built-in worker keeps R, Python, and SQL state in one process.
This guide covers behavior visible through `send`; [operation order](SEND_OPERATIONS.md) owns admission and control, [requirements](REQUIREMENTS.md) owns dependency preparation, and [architecture](ARCHITECTURE.md) explains implementation boundaries.

## Session model

There is one implicit session and one active cell at a time.
Globals, imports, options, database state, debugger state, and unread input survive between cells.
Cells are not transactions: an error can leave earlier effects in place.
Restart or worker loss discards all live state; accepted requirements remain in the server for replacement workers.

The default worker starts in the background while MCP discovery remains usable.
Worker transport readiness precedes background initialization of enabled R and Python on the serialized interpreter thread.
Startup hooks run even without submitted code; their output, plots, and input prompts remain available through `send`.
When SQL is enabled and its optional provider is installed, bootstrap opens the managed DuckDB connection; first-query work remains lazy.
Python-owned SQL warms only an installed DuckDB package matching the active import candidate; workspace modules/packages shadowing it are treated as absent during bootstrap.
An absent provider can be prepared on later SQL demand.
Warmup does not prepare missing packages.
Provider errors remain visible through `send` and leave unrelated cells usable; later SQL demand retries connection creation.
Interrupting warmup withholds an early waiting cell and retains the worker.
Early calls use [shared startup](SEND_OPERATIONS.md#server-readiness), not an independent worker per call.
Custom workers retain lazy launch.

The captured [`languages` setting](CONFIGURATION.md#model-visible-languages) controls model-facing code fields independently of this runtime initialization.
SQL-only and SQL+Python interfaces can use an installed but hidden R provider; without R, SQL can use hidden Python.
No setup cell is needed for the managed connection.
Hidden-provider package requirements, ordinary polling, input, interruption, and restart remain available.
An explicit public selection does not change the internal `MCP_CONSOLE_LANGUAGES` bootstrap configuration.

The server, resolver, relay, and worker run on the same host.
For remote work, run the MCP client and Console together on the remote host.

## Python sessions without R

R discovery uses `R_HOME` or `R` on the execution host's PATH.
Genuine absence allows Python/SQL; a broken selected R installation is an error, not fallback.
Sessions without an explicit Python selection require uv on that host.
There is no automatic PATH-Python fallback.
Managed defaults are NumPy, pandas, DuckDB, and the SQLite extension; Matplotlib is not a default dependency.

Set `python: .venv/bin/python` to use an existing environment without uv or managed Python preparation.
Paths and legacy selection precedence are described in [configuration](CONFIGURATION.md#python-environment-selection).
CPython 3.10+ with a usable shared embedding library is required.
Selected virtualenv paths and prefixes are preserved for imports, subprocesses, and multiprocessing.

Explicit Python selections use preinstalled packages and can run without NumPy, pandas, or DuckDB.
Missing DuckDB disables managed SQL, not Python or a user-selected DB-API connection.
Managed Python uses the captured [resolver extension cache](RESOLVER.md).
Worker spill, secrets, and caches use private lifetime storage, not ownership by R's session tempdir.

## Cells and polling

Submit exactly one complete `r`, `python`, or `sql` source string.
R and Python parse the entire cell before evaluating it; SQL passes the complete string to the selected driver.
Sources are not fragments accumulated across calls.

Submit a coherent cell, inspect its result, then submit the next.
Leave the main result last for ordinary display.
A timeout does not cancel execution: after `[running; poll with an empty send]`, poll with no code or stdin rather than resubmitting.
An adjacent `[elapsed: 1.2s since admission]` notice reports the cell's age across polls, including startup and preparation before execution.
It adds `; no new output` when the observed interval received no worker text or images, even if server notices appeared.
Worker output counts before preview compaction or omission.
An interrupt response that cannot claim another call's output reports only elapsed time; recovered responses retain their original elapsed notice.
New code is rejected while a cell or its uncollected result is active.
Completion returns text/images, or `[done]` when there is no content; an idle poll returns pending output and `[idle]`.

## Standard input and managed reads

`stdin` queues exact UTF-8 bytes, adds no newline, echoes nothing, and does not close the stream or acknowledge consumption.
Empty text queues nothing.
Line-oriented input normally needs a trailing `\n`.
Unread bytes may satisfy a later read or cell and are discarded on restart.

R `readline()` / `browser()` and main-thread Python `input()` / `pdb` report managed input requests.
An outstanding request can end a response with `[waiting for stdin]`.
Answer with `stdin` alone, not another cell.
For example, at R's `Browse[1]>`, send `ls.str()\n` to inspect, `c\n` to continue, or `Q\n` to quit.
The evaluation remains active while the debugger waits.

Direct `sys.stdin`, fd-0, and descendant reads bypass input notifications.
They can consume queued bytes but cannot recover a partial line already buffered by a managed console read.
Interrupted managed reads preserve that prefix only for a later managed read.
Input ordering guarantees enqueue order, not which reader consumes it; see [operations](SEND_OPERATIONS.md#operations).

## Interruption

`control: "interrupt"` signals the active resolver first, otherwise the worker.
It does not retarget a replacement.
If neither is running, it reports a tool error without starting a worker.
Interrupts are cooperative: code may catch, delay, replace, or block them.
Use restart when a fresh worker is required.

The normal output/state response still applies.
Input and a following cell may accompany control, but the exact grace, admission, and partial effects belong to [send ordering](SEND_OPERATIONS.md), not language behavior.

## Explicit restart

`control: "restart"` waits for retirement and replacement startup.
Requirements are prepared before retirement; same-call input and code go only to the ready replacement.
Failed preparation preserves the old worker; failure after retirement cannot restore it.

After an established worker, notices identify state loss and new-worker startup.
A restart without code ends in `[idle]`; a completed combined control-and-cell response ends in `[done]`.
Old output precedes lifecycle notices and new output.
A separately waiting evaluation keeps its own response ownership until delivery or recovery settles.
See [output ownership](ARCHITECTURE.md#output-and-delivery) for cancellation races; local recovery is not exactly-once client observation.

## R

For fresh R state, send `control: "restart"` with the next cell; for background work, use `timeout_ms: 0` and poll with an empty `send`.
Use a subprocess such as `callr` when the task needs separate process isolation, preserving the current session while running independently, or ordinary R behavior without Console's runtime hooks.

Accepted cells run in persistent global state through R's native console semantics.
Every visible top-level expression may autoprint.
Parse errors reject the whole cell without running earlier expressions or changing `.Last.value`, `.Traceback`, history, task callbacks, or `options(error)`.
Evaluation and print errors are console outcomes; they preserve earlier effects and leave the worker usable.

R's native bootstrap and event APIs are component-local to Unix and Windows.
Both use the same console callbacks, parser, REPL, graphics scopes, and environment integration on the coordinator's interpreter thread.
Bootstrap restores the captured R installation immediately before startup; argument strings and Windows startup paths live until worker exit.
Bootstrap defers default packages when needed to attach runtime services and the R/Python adapter first.
Windows installs an interrupt-delivery callback and wakes its idle command wait for R's window messages.
Event dispatch runs inside R's top-level error boundary, after releasing the command reader and within the ordinary graphics/input scope.

On macOS, Linux, and Windows, R event handlers, including `later` callbacks, run while idle.
They may change state and produce output returned by a later poll, Python cell, or SQL cell.
When needed, `[output produced while idle]` separates that region from new-cell output.
The initial display width is 200 columns and remains user-configurable.

Use package-loading operations normally.
Managed sessions resolve reached missing plain packages without scanning or replaying the source.
Bare sessions retain ordinary R behavior.
See [automatic R resolution](REQUIREMENTS.md#automatic-r-package-resolution).
In a sandboxed R worker, a writable temporary library precedes managed libraries; manual installations there last only for that generation and remain subject to network policy and build prerequisites.

## Python

Cells execute in persistent `__main__.__dict__`; the final expression uses the normal display hook.
Ordinary exceptions print their tracebacks and leave the worker usable.
Private Console frames are omitted, but user, standard-library, and third-party frames remain.
An uncaught `SystemExit` terminates the worker; catching it is normal control flow, and an exit in a background thread ends only that thread.

Console owns CPython initialization with or without R.
An explicit or host-resolved Python selection initializes before optional R setup; unresolved R-side selection uses the compatibility adapter.
Bare R remains usable when that adapter is unavailable.
When unresolved discovery finds no Python interpreter, background initialization finishes with R alone; an actual Python request still reports the selection error.
Explicit selection errors and incompatible interpreters retain their ordinary failure behavior.
Startup services are connected before executable `.pth` files and `sitecustomize` run.
Completed site processing is not repeated on later setup or bridge attachment.
`RETICULATE_PYTHONPATH`, when set, overrides `PYTHONPATH` for Python and its children.
The working-directory import entry follows `os.chdir()`.

NumPy/pandas display defaults use width 200 without overwriting nondefault startup settings or later user changes.
Bridge attachment preserves the running interpreter, objects, selected connection, and user stream redirections.
Interrupted setup can retry completed-safe steps; incompatible identity or unsafe partial initialization requires replacement.
Python-owned SQL cells also finish incomplete Python setup before execution.
See [current limitations](#current-limitations).

Main-thread text uses Console channels.
Binary buffers, native descriptors, background threads, and fork children use raw streams.
Cached Console streams fall back to child streams after `fork`; that does not make R or arbitrary native extensions safe in a fork child.
Console adds no notebook event loop: asynchronous work must be started and managed explicitly.

Use imports directly in managed sessions.
[Automatic Python resolution](REQUIREMENTS.md#automatic-python-import-resolution) explains inference, optional dependencies, and thread restrictions.
Explicit Python environments and bare workers require installed packages.

## R and Python interoperability

Reticulate supplies the on-demand bridge: Python uses `r.name` for R globals and functions; R uses `py$name` for Python globals.
Accessing attributes through `py` or `reticulate::py` initializes the bridge on demand, including after restart.
When Python is already running, the bridge attaches to that same interpreter and preserves its existing objects; no preceding `py_eval()` or `py_available(initialize = TRUE)` call is needed.
Loading reticulate or reading its `py` module proxy alone does not initialize Python or attach the bridge.
During a `reticulate.python.beforeInitialized` callback, `py` retains reticulate's `NULL` behavior until attachment publishes its configuration, so reading it does not reenter initialization.
An actual `r` access can initialize R when shared bootstrap has not completed it.
Conversion follows reticulate's rules; objects/proxies do not survive worker replacement.

An R-only configuration need not start Python.
Ordinary Python need not attach reticulate.
Both interpreters and reentrant bridge calls share the worker's owning thread.

## SQL and DuckDB

Managed SQL uses one in-memory DuckDB connection and persistent catalog.
With R available it belongs to R/DBI; without R it belongs to Python/DB-API.
[Captured startup source](CONFIGURATION.md#session-startup-source) can select a native DBI or DB-API connection after session helpers are installed and before SQL runs, without opening an unused managed default.
SQL-only use still needs one of those adapters.
DuckDB CLI dot commands are not supported.
Defaults prepare SQLite for read-only attachment; use `READ_ONLY` when opening databases outside sandbox-writable paths.

With R-owned DuckDB, unqualified relation names can refer to R global data frames; a table/view with that name takes precedence.
A view sees later rebinding of the R name.
Python frames must be assigned to an R global first.
With Python-owned DuckDB, register frames explicitly with `sql_connection().register("name", frame)`; Python and R globals are not scanned or copied into the Python catalog.

Select another backend without moving its connection between languages:

```r
connection <- DBI::dbConnect(RSQLite::SQLite(), ":memory:")
console_sql_connection(connection)
# Restore managed DuckDB before disconnecting connection:
console_sql_connection(NULL)
```

```python
import sqlite3

connection = sqlite3.connect(":memory:")
console_sql_connection(connection)
console_sql_connection(None)  # Restore the existing managed catalog.
```

The latest selection controls SQL cells.
An R connection selected before Python initializes can execute SQL without starting Python; later Python initialization preserves that selection.
SQL on a selected R connection can continue while Python setup is incomplete without resuming that setup.
Interrupting selection replay leaves Python setup incomplete; a later Python cell retries setup while preserving the R connection, its transaction, and live worker state.
User connections remain user-owned; restoring managed DuckDB does not close them.
Never disconnect Console's managed connection.
R `sql_connection()` returns its selected R-owned connection even while SQL cells use a Python selection.
Without R, Python `sql_connection()` returns its active Python connection.
Reset from either language restores the automatic managed provider and the same managed catalog without closing user connections or changing their transactions.
Reset does not replay captured startup; failed startup keeps SQL withheld until explicit restart.
R previews of user DBI connections use a private in-memory rendering catalog, opened only when a preview needs it.

R submits cells through `DBI::dbSendQuery()`.
Python uses the connection's `execute()` when available, otherwise its cursor protocol.
The driver owns SQL dialect, transactions, and type mappings.
Use DBI's statement interface from R when needed.
Adapters do not retry through another method: the first attempt may already have changed state.
Errors leave the selected connection available.

### Result previews

A preview fetches at most 21 rows to display at most 20 rows and 12 columns.
Values are capped at 160 characters and formatted at width 200.
The table's 12 KiB ceiling removes rows, then columns; the stricter whole-response 8 KiB budget still applies afterward.
Omissions are reported without counting the full result.
Empty results with columns retain headers; no-column results have no preview or affected-row count.

## Plots and images

R's managed default device returns PNG pages and finalizes open pages at cell end, including after language errors.
A later cell cannot add layers to an already finalized plot.
Defaults are 800 by 600 pixels at 96 DPI.
Set positive, finite persistent `console.plot.width_in`, `console.plot.height_in` (**inches**), and `console.plot.dpi` options to change them.
For example, a 1600 by 1050 pixel image at 100 DPI uses:

```r
options(
  console.plot.width_in = 16,
  console.plot.height_in = 10.5,
  console.plot.dpi = 100
)
```

Explicit user devices are not closed or captured by Console.
R plots invoked through Python follow these same rules.

At Python cell end, including after an exception, all open pyplot figures are returned in figure-number order and closed.
`show()` is optional; `savefig()` does not suppress capture, but closing a figure does.
Figures outside pyplot are not captured.
An inherited `MPLBACKEND` is respected; otherwise Console uses `Agg`.
Host configuration/font caches may be read while new cache writes are redirected to private worker storage.

## Output and notices

Language errors and warnings are ordinary console text.
Explicit preparation, transport, worker, and protocol failures are tool errors.
Bracketed input and lifecycle notices are server state, not runtime output.
Each producer's order is preserved; independent sideband/stdout/stderr streams have no global chronology.

An established worker failure ends the cell without replay and makes one replacement attempt.
The failing call remains a tool error even if the new worker becomes `[idle]`.
If it returns `[worker starting]`, poll until startup settles.
Discovery failures require a new server; other startup retries follow [server readiness](SEND_OPERATIONS.md#server-readiness).

Each complete response, including generated notices, has at most **8 KiB UTF-8 text**.
Large output retains its beginning and latest tail.
Consecutive progress redraws from one producer are compacted within a response interval: carriage return replaces the frame, backspace removes a Unicode scalar, and CRLF remains a newline.
Raw streams use incremental decoding; invalid UTF-8 is replaced for display, not in retained raw logs.

Previews project the following [ANSI controls](https://invisible-island.net/xterm/ctlseqs/ctlseqs.html) to plain text:

| Control                                                                 | Preview behavior                                                                                                                                      |
| ----------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| SGR (`CSI ... m`), including semicolon/colon color parameters           | Remove styling and preserve text.                                                                                                                     |
| Erase line (`CSI K` or `CSI 0 K`)                                       | Erase the current frame after CR; otherwise leave it unchanged.                                                                                       |
| Erase whole line (`CSI 2 K`)                                            | Erase the current frame, including any omitted middle. Finished lines remain.                                                                         |
| OSC title, clipboard, and hyperlink commands; DCS, SOS, PM, APC strings | Suppress payloads through the string terminator (`ESC \` or decoded U+009C); OSC also accepts BEL. Hyperlink labels outside the commands remain text. |
| Other complete CSI/ESC controls and decoded C1 controls                 | Suppress the control without screen or cursor emulation.                                                                                              |

Parsing is incremental within one contiguous producer/response interval.
Each native launcher or preparation diagnostic reader is a separate producer from worker stderr and other native readers.
Producer switches, response cuts, images, notices, completion, and retirement discard incomplete controls.
An unterminated control string suppresses the rest of that interval; its payload is never buffered.
CSI syntax is inspected up to 128 bytes; longer sequences are suppressed through their final character.
ESC intermediate syntax is consumed without buffering.
Outside strings, a new ESC restarts parsing, CR/LF/backspace cancel incomplete controls and retain their ordinary behavior, and an invalid scalar resumes plain text at that scalar.
Decoded C1 introducers follow the same projection rules; isolated invalid raw C1 bytes still use UTF-8 replacement.
These rules retain CR frame replacement and Unicode-scalar backspace, without terminal cell-width or multiline fidelity.
Suppressed controls use no rendered-text allowance; retained raw logs keep their exact original bytes.

Images have independent limits: 8 MiB encoded data, 64 KiB MIME metadata, and 4,096 images per undrained interval and complete result.
Whole images are admitted; text limits do not consume their allowance.
One notice reports omitted text/images and names available raw logs or image artifacts on the Console host.
Multiple sources share a directory location; later gaps use `[…]` without repeating retrieval instructions.
Long locations use exact session/file paths explicitly relative to the Console recording directory, keeping retrieval notices within the text budget.
If recording is disabled or fails, the notice says when omitted output is unavailable.
Partially retained logs are identified as prefixes, and images rejected before recording are marked as not retained.

Polling consumes an observed interval, including its omitted middle.
Reading a raw file does not change that cursor.
Raw per-cell files retain up to 1 GiB and can be read during evaluation; startup/idle output without a cell log cannot borrow another cell's path.
Retrieval requires filesystem access to the Console host's recording directory; only retained output is available there.
See [recording](RECORDING.md) for loss counts, artifacts, privacy, and report generation.

## Current limitations

There are no named sessions, parallel cells, aggregate recording quota, automatic retention cleanup, or Console file-read/search interface.
No general worker-frame or stdin-queue limit is defined.
Recordings are not checkpoints and cannot recover data a language printer never emitted.

**Initialize R before starting background threads that may access the native process environment.** R bootstrap reads and mutates environment variables; Console's interpreter thread and the GIL cannot serialize arbitrary native threads.
Partial R initialization or unsafe bridge/startup failure can require restart even when ordinary Python remains usable.

macOS and Linux are supported.
Windows x64 supports experimental [local R and Python](WINDOWS.md), including managed dependency resolution; SQL is deferred.
Native enforcement and descendant retirement have explicit [sandbox lifetime limits](SANDBOX.md#supported-hosts-and-lifetime-limits).
`--no-sandbox` removes native enforcement and descendant cleanup.
Preparation remains a separate [trusted host operation](REQUIREMENTS.md#host-resolution-and-trust).
