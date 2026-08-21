# `mcp-console`

# 🚧 UNDER CONSTRUCTION 🚧

**This project is not ready for use.**

`mcp-console` is a ground-up rewrite of [`mcp-repl`](https://github.com/posit-dev/mcp-repl).
It applies the lessons learned from `mcp-repl` to a substantially different product---different enough that a new name makes sense.

The repository currently contains the initial Rust binary package.
The following commands are implemented:

```bash
mcp-console serve
mcp-console --help
mcp-console help [COMMAND]
mcp-console --version
mcp-console sandbox -- COMMAND [ARG]...
```

`mcp-console` requires a subcommand.
`mcp-console serve` runs a minimal MCP server over stdio.
Run `mcp-console --help` or `mcp-console COMMAND --help` for command-line help.
The server registers `send` and a narrow initial `session` tool.
The first ordinary, non-task `send` or `session` call creates a run-specific record under `.mcp-console/sessions/<UTC-first-use>-<pid>/` in the server's initial working directory.
Initialization, tool listing, unknown tool calls, and an otherwise unused `serve` process do not create `.mcp-console`.
On Unix, newly created record directories use mode `0700`, and journal and artifact files use mode `0600`.
It appends `session_started`, `tool_call`, `artifact_created`, and `tool_result` events to `internal/events.jsonl` for each ordinary, non-task `send` or `session` call, including timestamps, request and call IDs, exact arguments, ordered text and image blocks, and tool errors.
Image bytes received during a `send` operation are decoded and flushed under `artifacts/` immediately, including images from an evaluation that is never polled again.
A background image first received while idle or during live requirement preparation is validated and queued.
It is persisted when a response drains and assembles that pending output, including the current failed preparation response, a later `send`, or a restart response.
The JSONL result refers to each image's relative artifact path while the MCP response remains unchanged.
The result record captures server assembly, not delivery; cancellation or disconnection may suppress the response.
Recording is optional: if the run record cannot be created or a later write fails, MCP Console disables recording for that server process, emits one diagnostic to standard error, and continues serving console calls.
An existing journal may therefore end with the last successfully flushed event.
Submitted source, stdin, and tool-result output are recorded without redaction.
Complete evaluation-output spools and the generated Quarto projection described in the design sketches are not yet implemented.
Supplying exactly one of `r`, `python`, or `sql` evaluates one complete code cell and waits up to the optional `timeout_ms`, which defaults to 60 seconds.
When that wait expires during evaluation, the call drains output produced so far, appends the newline-prefixed banner `\n[running]`, and leaves the computation running; call `send` without a code field to poll again.
If an established worker fails, the same call uses its remaining wait for one automatic replacement attempt.
When that wait expires during replacement startup, the response ends with `[worker starting]`; later polls report the same state until the worker reports ready, then return startup output followed by `[idle]`.
On macOS, the server remains outside the sandbox and starts one worker relay inside it for each worker generation.
The relay starts the worker inside the same sandbox, creates its unchanged sideband and standard streams there, and forwards ordered JSONL events to the server over its own standard output.
Only the relay's fd 0/1/2 cross the server/sandbox boundary; relay standard error passes through outside the protocol and is normally empty.
The relay validates each worker standard-output and standard-error read independently.
Valid UTF-8 chunks use readable string events, while invalid chunks use base64 byte events so arbitrary bytes cannot corrupt the private JSONL framing.
After a worker reports ready, the server continuously consumes the relay event stream containing its sideband, standard output, and standard error activity.
With no evaluation active, an empty `send` immediately drains the output collected so far and returns it followed by `\n[idle]`, or `\n[stdin needed]` when an idle callback has an outstanding console read.
It sends no worker command, does not wait for idle callbacks to finish, and is not delayed by `timeout_ms`.
Output that reaches the server after that snapshot remains pending for a later response.
An empty call does not start an initial or stopped worker.
A call may also supply exact standard-input text with a code cell, during an evaluation, or while the worker is idle:

```json
{ "r": "readline('name> ')", "stdin": "Ada\n" }
```

The server sends the cell first, then asks the relay to queue the string's UTF-8 bytes to worker fd 0 without inspecting or echoing them, adding a newline, imposing a size limit, or waiting for an input request.
A stdin-only call while idle lazily starts the worker when needed, queues the bytes, and immediately returns the current output snapshot.
Queuing bytes does not acknowledge that a callback consumed them, so that response may still end with `\n[stdin needed]`; a later empty call observes an `input_received` frame and returns `\n[idle]`.
Every `input_requested` event adds a server-owned record such as `[input requested: "name> "]`; the prompt is encoded as a JSON string so spaces and escaped characters remain explicit.
During an evaluation, when that request remains outstanding for up to 10 milliseconds, bounded by the call deadline, `send` follows the record with the newline-prefixed banner `\n[stdin needed]`; a later call can supply more bytes with `{ "stdin": "Ada\n" }`.
An immediate `input_received` or `input_cancelled` receipt retains the request record but suppresses `[stdin needed]`, so prequeued input can satisfy a console read without forcing another tool call and an interrupt can cancel it.
That receipt describes the runtime read, not a particular stdin payload; direct fd-0 reads emit no request or receipt.
Payload end is not EOF, and queued input is not an acknowledgment of consumption.
Unread bytes may be completed by later stdin or satisfy a later worker read or evaluation.
On macOS, the built-in server resolves a default R library containing tidyverse, `github::rstudio/reticulate`, DBI, DuckDB, arrow, and nanoarrow before it accepts MCP input.
The GitHub reticulate requirement supplies the fork-aware output handling required by the worker; the host R installation must also provide reticulate to bootstrap managed Python before the worker library is applied.
When Python is managed, its default environment contains NumPy and pandas.
The built-in server also prepares DuckDB's JSON and ICU extensions in DuckDB's native cache.
Packages and extensions are available but are not attached, imported, or loaded automatically.
The MCP client can prepare additive R and Python requirements and other DuckDB extensions for the implicit session:

```json
{
  "action": "prepare",
  "requirements": {
    "r": ["data.table"],
    "python": ["polars>=1"],
    "duckdb": ["fts"]
  }
}
```

Default and explicit requirement resolution happens on the host: package installation or build hooks, managed Python environment startup, Matplotlib cache warming, and DuckDB extension installation run outside the worker sandbox.
Use only trusted requirements and host environment settings.
MCP Console sets `IR_NO_LOCAL_SOURCES` for every R resolution, so IR refuses package installation from direct or transitive local sources while retaining its accepted package syntax.
The policy prevents local package installation code from running; IR may still reuse an already materialized library.

Before the worker starts, this `session` call resolves each complete requirement set outside the worker sandbox, using IR for R, reticulate with uv for Python, and DuckDB's own `INSTALL` for extensions, then returns `[prepared]`.
It retains the requested configuration only after every resolution succeeds.
It does not load packages or extension code into the worker, or start the worker.
Before each R resolution, the server requires `ir --version` from `PATH` to report 0.4.0 or later.
The server runs IR with the same Rscript selection as the worker and prepends the returned library to the worker's inherited `R_LIBS`, leaving its other R libraries available.

DuckDB extension requirements are strict extension names such as `fts`, `spatial`, or `excel`.
Each name must start with a lowercase ASCII letter and otherwise contain only lowercase ASCII letters, digits, and underscores; repository names, paths, URLs, and version expressions are not accepted.
The host resolver lets DuckDB select its default repository and native extension cache.
That cache persists across workers and is scoped by DuckDB version and platform.
Whenever R preparation selects a new library, the host resolver repeats the retained extension installs with that library's DuckDB version.
Already installed extensions are a no-op for DuckDB.
While a worker remains live, new extensions are also installed for each resolved R library that could have supplied its loaded DuckDB namespace; a replacement resets that list to the retained library.
JSON and ICU are retained defaults for the built-in worker; FTS and other extensions remain on demand.

After a worker starts, `prepare` can apply new R requirements while the worker is idle when that worker implements the `prepare_r` protocol.
The server resolves the complete R requirement set outside the sandbox, then prepends the new library to the live `.libPaths()` and removes the previous managed IR entry.
Each candidate contains the complete retained R requirement set, so replacing the previous managed entry keeps the live worker aligned with later worker generations and avoids accumulating stale managed libraries.
Other live library paths and the worker's in-memory state are preserved.
The server retains the new library only after the worker confirms the change, and later worker generations reuse it.

An idle server-managed worker can also materialize an uninitialized Python manifest or activate a same-`libpython` environment without replacing the worker.
An idle worker that implements R preparation can prepare DuckDB extensions without replacing the worker or losing its in-memory state.
The host resolver installs them but never loads them; explicit `LOAD` and DuckDB's automatic extension loading occur later inside the sandbox.
This path invokes DuckDB's parser and installer directly rather than inspecting submitted SQL text.
A successful Python activation or explicit materialization is retained as soon as the worker reports success.
In a mixed R, Python, and DuckDB preparation, that Python environment can therefore remain retained even if a later R update fails.
The R and DuckDB configurations are retained only after the complete preparation succeeds.
An earlier extension from a failed multi-extension request may remain in DuckDB's host cache, but it is not retained as prepared.
If a live preparation failure may have partially changed the live worker, evaluation remains available so its state can be saved, but new requirement additions return `[restart required]` until a successful explicit restart.
Transport or protocol failures still stop the worker when its usability is unknown.
The server returns `[prepared]` only after the complete operation succeeds.
Exact repeats are idempotent.

Preparation during an active cell is an error.
Preparation that overlaps worker startup returns `[requirements not prepared: worker is starting]` without resolving the additions or changing the retained requirements, R library, Python manifest, or DuckDB extension set.
A failed automatic replacement leaves the worker stopped; a `prepare` call with new additions then returns `[restart required]` and does not retain them or configure the next replacement attempt.
Caller-selected Python environments cannot accept managed Python additions, but their built-in workers can still apply R requirements and prepare DuckDB extensions.
Custom workers skip the default R, Python, and DuckDB extension preflights, but can prepare explicit R requirements and DuckDB extensions.
Each prepared custom-worker R library also includes DBI, DuckDB, and jsonlite for host extension installation.
They must honor the server-provided `R_LIBS` and acknowledge live `prepare_r` requests.
Prepared extensions use DuckDB's native default cache; custom workers must use that cache to load them.
The hidden worker option replaces the executable, but R still starts from the user-selected installation and layers resolved libraries onto it.
A custom worker must apply its first resolved R library before loading DuckDB; a DuckDB namespace loaded earlier from inherited libraries is outside the extension-preparation contract.
Managed Python additions remain unavailable with a custom worker; R and DuckDB additions are supported by both `prepare` and `restart`.

An active host resolver or live worker can be sent an interrupt:

```json
{ "action": "interrupt" }
```

The call requests `SIGINT` for an active host resolver process group, or otherwise asks the relay to send it to the live worker, and returns `[interrupt sent]` after the resolver accepts the request or the relay reports that the worker signal succeeded, without waiting for the resolver or evaluation to finish.
It does not start a process; when neither target exists, it returns `worker is not running`.
A resolver signal error is returned by both the interrupt and resolution calls; an interrupted resolver otherwise reports its ordinary resolution failure.
A worker signal is not assigned to a cell: an idle signal is consumed at the next managed boundary, and a signal during R, reticulate Python, or DuckDB is handled by that runtime.
Code can catch or delay the signal, so use `restart` when the worker does not return.
An interrupt cancels a managed `readline()`, Python `input()`, or debugger prompt when runtime interrupt handling is active; a read inside R's `suspendInterrupts()` keeps waiting and the pending interrupt is handled at a later managed boundary after input arrives.
Full callback buffers without a newline remain provisional until that managed operation completes a logical line.
If the operation ends first, including after an interrupt between callbacks, every provisional chunk is replayed before fd 0 on the next managed console read.
Code reading fd 0 directly does not consume that pushback, and restart discards it with the worker.
`interrupt` accepts no `requirements`.

The client can explicitly replace the worker and add R, Python, and DuckDB requirements in the same call:

```json
{
  "action": "restart",
  "requirements": {
    "r": ["praise"],
    "python": ["py-yaml12"],
    "duckdb": ["fts"]
  }
}
```

The client can omit `requirements` to retain the current R, Python, and DuckDB configuration unchanged.
When requirements are supplied, the server merges them into the complete retained sets and resolves every changed candidate before stopping the current worker.
It commits the resulting candidates together only after every required resolution succeeds.
A resolution failure leaves the current worker, its in-memory state, and its retained environment unchanged.
Restart returns `[idle]` after the replacement reports ready.
It loses all in-memory R, Python, SQL, debugger, and unread-stdin state.
The implicit session exists for the server lifetime, so restart starts its first worker if none exists yet.
Internally, the server gives the relay the time remaining in the existing one-second worker-shutdown period.
It queues relay shutdown before cancelling a nested host resolver, then lets the ordered event dispatcher retire the active operation within that original deadline.
The relay closes worker stdin, sends the unchanged sideband shutdown message, and, if needed at the deadline, kills the direct worker, stops the other members still in its process group, reaps the worker, and flushes the generation's final events.
When the server observes the relay's acceptance before the original deadline, it permits up to two additional seconds for that retirement work without extending the worker deadline.
For non-intentional startup or runtime failure, it sends zero worker grace and shares the same absolute bounded relay-retirement allowance between ordered dispatcher catch-up and the relay drain, so output, stream closures, and the process outcome can arrive before the outer fail-safe runs.
It then closes the complete sandbox process-group lifetime and reaps the relay even when it already exited; the same outer path is the fail-safe if the relay does not accept shutdown or stalls.
It joins the relay transport tasks after ordered operation retirement before reporting that the worker stopped or launching a replacement.
The replacement generation is marked ready after its `ready` frame and before callback dispatch starts.
Code and idle stdin remain associated with the worker that admitted them and cannot run in the replacement.
Without a waiting `send`, the restart response includes retained output from the old worker, `[active evaluation stopped by session restart request]` when restart interrupts an unfinished cell, `[worker stopped: in-memory state lost]` when restart retires a ready worker, `[starting new worker]`, startup output, and finally `[idle]`, in that order.
If a `send` is waiting on the interrupted cell, that call receives the old worker's text and images through retirement, followed by `[stopped by session restart request before evaluation finished]` and, when restart retires a ready worker, `[worker stopped: in-memory state lost]`.
The server writes that `send` reply before starting the replacement or returning the restart response.
The restart response contains `[active evaluation stopped by session restart request]`, its own stopped notice when it retires a ready worker, `[starting new worker]`, replacement startup output, and `[idle]` without repeating the old worker's output.
Idle callbacks do not create a waiting `send`; continuous collection leaves their output pending for the restart response before the worker is retired.
On macOS, the default R and DuckDB extension preflights and, when required, the managed-Python preflight happen during `serve` startup; a successful `prepare` extends those initial selections before the first nonempty stdin submission or evaluation lazily starts the sandboxed relay and embedded R worker.
Later calls reuse the same global R state, reticulate Python interpreter, and in-memory DuckDB catalog.
An infrastructure or protocol failure stops that worker and discards its in-memory R, Python, and SQL state.
The failed `send` includes retained worker output, the specific bracketed error, and `[worker stopped: in-memory state lost]`, then emits `[starting new worker]` and makes one replacement attempt within the same deadline.
If the replacement reports ready in time, startup output and `[idle]` complete that error response.
If it remains in startup at the deadline, the response ends with `[worker starting]`, and a later poll waits on the same attempt.
A startup failure ends that attempt; a later call can try again and emits a new starting notice.
Initial lazy startup and retries before any worker reaches ready remain silent.

## A mixed-language analysis

An MCP client can use R, Python, and DuckDB as one persistent workspace and choose the clearest language for each step without exporting intermediate files.
After the `session` call below, each language-labeled block contains one complete cell for the named `send` field.

First, call `session` to prepare Matplotlib, which is not part of the default environments:

```json
{
  "action": "prepare",
  "requirements": {
    "python": ["matplotlib"]
  }
}
```

Read data, fit a model, and keep the augmented data in R global state with an `r` cell:

```r
measurements <- readr::read_csv("measurements.csv", show_col_types = FALSE)
fit <- lm(response ~ temperature + group, data = measurements)
measurements <- dplyr::mutate(
  measurements,
  .fitted = fitted(fit),
  .residual = residuals(fit)
)
summary(fit)
```

Read that R data frame as a pandas object from a `python` cell:

```python
frame = r.measurements
frame.describe()
```

Query the R data frame directly by name with an `sql` cell:

```sql
SELECT
  "group",
  count(*) AS n,
  avg(abs(".residual")) AS mean_abs_residual
FROM measurements
GROUP BY "group"
ORDER BY mean_abs_residual DESC
```

Use another `python` cell to return a Matplotlib figure as an MCP image:

```python
import matplotlib.pyplot as plt

plt.scatter(frame["temperature"], frame[".residual"])
plt.axhline(0, color="black", linewidth=1)
```

Switch back to an `r` cell for a ggplot2 diagnostic:

```r
ggplot2::ggplot(
  measurements,
  ggplot2::aes(.fitted, .residual, color = group)
) +
  ggplot2::geom_point() +
  ggplot2::geom_hline(yintercept = 0)
```

Python reads R globals as `r.name`, and R reads Python globals as `py$name` without attaching reticulate.
SQL can scan data frames in R global state; Python data frames become visible to SQL after they are bound to an R name, as above.
Requirements make packages available but do not import or attach them.

The worker runs each R cell through R's native top-level loop, captures R console output, prints each visible value, and maintains `.Last.value`.
Each worker generation starts with `options(width = 200L)`; later changes to that option persist for the generation.
Immediately before every R, Python, or SQL cell, the worker gives R's registered input handlers one nonblocking turn.
It gives them a second turn after a normal language outcome only if worker shutdown has not begun and the cell recorded no infrastructure failure.
Shutdown or an infrastructure failure during the initial turn aborts the submitted cell; an infrastructure failure recorded by the cell skips the final turn.
After either turn, a worker-stdin hangup marks shutdown before the worker can dispatch or complete the cell, including when a callback reads fd 0 directly.
Between cells, the worker uses `R_checkActivity()` to wait for either a registered R handler or its relay sideband, without busy polling or a worker-owned fixed interval.
Callbacks registered by packages such as `later` can therefore run after a cell has returned.
A generation-long server reader incrementally parses and enqueues flat relay events so retirement can cancel a partially received frame and idle output is not bounded by sideband pipe capacity.
One ordered dispatcher publishes forwarded console output and images, services nested managed-Python requests, commits operation results, and updates console input state.
An empty `send` snapshots the output already collected without signaling the worker or waiting for the callback.
Before applying a live requirement preparation, the built-in worker gives registered R handlers one nonblocking turn, so a callback already ready when the command arrives is collected first.
An empty `send` surfaces an idle callback's input request as `[stdin needed]`; a later stdin-only `send` continues it, and a call that already includes stdin can prequeue the input.
A code-bearing `send` can also supply input requested by an idle callback.
A noninteractive requirement preparation stops the worker if it encounters such an input request instead of waiting indefinitely.
If a cell ends while an expression is incomplete, earlier complete expressions from that cell remain applied.
The worker installs a worker-owned `grDevices::png()` function as R's default graphics device and opens it lazily when a cell draws.
Each managed page is returned as an MCP image when its device finalizes it by opening a new page or closing.
R console output is returned when R writes it, so text produced while a page is still open can appear before that page's image.
At cell end, including after a normal R error, the worker closes its managed devices and returns their remaining pages.
Managed devices are cell scoped, so all drawing operations that modify one plot must be submitted together.
Their default size is 800 by 600 pixels at 96 DPI.
These persistent R options control their dimensions in inches and resolution:

```r
options(
  console.plot.width = 6,
  console.plot.height = 4,
  console.plot.dpi = 120
)
plot(1:10)
```

Graphics devices opened explicitly by evaluated code, such as with `grDevices::png()`, are user-owned: the worker does not close them, read their files, or return them as MCP images.
Python cells execute statements in persistent `__main__` state and display their final expression through Python's display hook.
Python formatters see a 200-column terminal width; pandas `display.width` and NumPy `linewidth` also start at 200 and remain user-configurable.
Python reads R globals through reticulate's `r.name` bridge, and R reads Python globals through the attached `py$name` binding.
R plots invoked from a Python cell through reticulate's `r` bridge use the same managed default device, sizing options, cell scope, and MCP image output as plots invoked from an R cell.
At the end of each Python cell, including after a Python error, every open figure managed by `matplotlib.pyplot` is rendered in memory, returned once as a PNG image, and closed.
`plt.show()` is optional, and calling `savefig()` does not suppress this capture while the figure remains open.
These figures are cell scoped, so one plot's drawing operations must be submitted together.
Figures closed before cell end and figures not registered with `pyplot` are not captured.
Unless an inherited setting selects otherwise, the worker uses Matplotlib's noninteractive Agg backend.
Built-in workers inherit an existing user `matplotlibrc` as a read-only file while keeping Matplotlib's writable configuration and XDG cache directories under the worker's private temporary directory.
Evaluated code can use the user's settings but cannot modify that host file through the sandbox.
After each server-managed Python environment resolves, the host resolver starts its exact interpreter and attempts to import `matplotlib.font_manager`.
Matplotlib may reuse or create its local `fontlist-v*.json` index in the user's inherited nonempty `MPLCONFIGDIR`, or in `$HOME/.matplotlib` when that setting is unset or empty; the font scan itself does not require network access.
Each worker links matching indexes from that user directory read-only into its private Matplotlib directory, so restarts reuse them without copying them or granting evaluated code persistent writes.
If the import fails or no usable index is available, Python resolution still succeeds and the worker performs private font discovery when needed.
The resolver import executes the selected Matplotlib package outside the worker sandbox as part of managed Python preparation.
Caller-selected non-managed Python environments do not receive host prewarming, but can reuse a matching user index that already exists.
Reticulate routes Python text written through `sys.stdout` and `sys.stderr`, including tracebacks, through the same sideband console output path as R.
Writes through `sys.stdout.buffer`, `sys.stderr.buffer`, or fd 1/2 directly remain on the captured standard streams.
After a Python cell calls `os.fork()`, reticulate restores the child's original fd-backed text streams after its sideband is disabled, so its ordinary stdout and stderr are captured too.
Native extensions that fork without running CPython's registered fork callbacks and then resume Python are unsupported.
Fork-child text capture requires reticulate from its `main` branch or a release containing fork-aware stream restoration.
An exec descendant that retains fd 1/2 creates fresh standard streams backed by those descriptors, so its ordinary stdout and stderr are captured while that worker generation's output boundary remains open.
SQL cells and `sql_connection()` lazily open one in-memory DuckDB connection through the `duckdb` and `DBI` R packages and reuse it for the worker generation.
The connection leaves extension discovery to DuckDB while keeping secret and spill storage under the worker's private R temporary directory.
DuckDB's native extension cache is readable but not writable from the sandbox, and the sandbox denies network access.
Explicit `LOAD` and DuckDB's default automatic-extension behavior run inside the sandbox.
The worker sends the complete SQL source out of band to a private R bridge and executes query results through DBI's streaming Arrow API.
It fetches at most 21 rows, uses the final row only to detect that more data exists, and renders at most 20 rows and 12 columns in a 200-column layout, with a 160-character per-cell limit and a 12 KiB SQL-preview limit.
The preview shows Arrow column types, SQL `NULL`, and empty-result schemas; DuckDB converts only the bounded displayed cells to text and applies the cell limit before returning them to R, preserving values such as `BIGINT`, `DECIMAL`, lists, and structs when they fit.
It reports omitted rows without counting the complete result and reports omitted columns explicitly; the final byte limit may reduce the displayed rows or columns further.
Statements without result columns are silent, so they return `[done]` when they produce no other output.
DuckDB errors are normal console results and leave the worker available for later cells.
DuckDB first resolves unqualified relation names in its persistent catalog.
When no catalog table or view matches, it can scan a data frame bound in the persistent R global environment.
An SQL view over a scanned name observes later changes to that R binding.
R code can call `sql_connection()` to borrow the worker-owned DBI connection for established DuckDB, DBI, and dplyr interfaces.
The worker exposes `py` and `sql_connection()` in its attached `tools:mcp-console` environment, so both remain available after clearing the global R workspace with `rm(list = ls())`; callers must not disconnect the returned connection.
For example, `dplyr::tbl(sql_connection(), "answers")` creates a lazy relation that observes later catalog changes until it is collected.
These paths avoid an eager snapshot transfer, but do not promise end-to-end zero-copy behavior: DuckDB converts R values during execution, and collecting a lazy relation materializes its result in R.
Automatic Python relation sharing and affected-row summaries do not exist yet.

The server appends relay-forwarded sideband text and images, worker standard-output and standard-error bytes, failures, and lifecycle notices to one pending output tape.
Each successful `send` boundary drains the events available then; output produced while the worker is idle can therefore appear on a later idle poll before the server-owned `\n[idle]` banner.
Standard-stream bytes remain undecoded until a response drains them, so incomplete UTF-8 can remain pending for a later response.
Each worker source preserves its own order, and complete relay frames follow the order in which their reader threads enqueue them.
Independent standard-output, standard-error, and sideband pipes provide no chronological cross-source ordering: raw output written before an operation result may be observed after it and remain pending for a later response.
For an established worker, unexpected exit codes and termination signals are reported before stopped and replacement notices.
An unexpected pre-ready exit reports the same diagnostic with its startup failure; intentional restart and shutdown suppress crash diagnostics.
R language failures, uncaught Python exceptions, and DuckDB errors remain ordinary console results rather than MCP tool errors.
Server-owned timeline, state, and admission facts are bracketed; worker console text remains unchanged.
A silent successful R, Python, or SQL cell sends no sideband console-text frame, still sends `completed`, and projects to `[done]` when no other response text is pending.

Python cells require the `reticulate` R package.
Matplotlib figure capture requires the Python `matplotlib` package; prepare it before use when it is not already available.
SQL cells use the default DBI and duckdb packages.
Previews use nanoarrow for DuckDB's DBI Arrow stream and arrow for the bounded record batch that is sliced, formatted, and temporarily registered back with DuckDB.
Tidyverse supplies pillar, tibble, dplyr, and dbplyr for lazy relations created from `sql_connection()`.
MCP Console installs tidyverse, GitHub reticulate, DBI, DuckDB, arrow, and nanoarrow into its default IR library, but the host R installation still needs reticulate for managed-Python preflight.
It does not automatically install that host-bootstrap package.
Default and explicit R resolution runs `ir run` outside the sandbox with the requested package references as command arguments and a constant expression that prints the resolved library path.
IR may access the network, write its normal host caches, and execute package installation code.
If IR is absent, too old, or cannot resolve the default library, built-in `serve` exits before accepting MCP requests.
A later explicit R resolution failure is a tool error and leaves the prior configuration unchanged.
Default and explicit DuckDB extension preparation runs the managed DuckDB package outside the sandbox and calls its own installer once for each extension name.
The resolver may access the network and write DuckDB's native version- and platform-specific extension cache, but it does not load extension code.
The extension resolver shares the cancellable process-group lifecycle used by the R and Python resolvers; no DuckDB-specific message is added to the worker sideband protocol, though an accompanying R-library change still uses `prepare_r`.
When `RETICULATE_PYTHON` is unset or is `managed`, `mcp-console serve` runs reticulate's uv environment resolver outside the worker sandbox with its NumPy and pandas baseline, where it can use the normal host caches and network access.
Other configured values, including an empty value, are preserved when no Python requirements are prepared and skip the Python startup preflight; they do not skip the default R or DuckDB extension preflight.
Both managed and configured interpreters must provide Python 3.10 or later; the Python bridge rejects an older interpreter before evaluating a cell.
An explicit `session` preparation selects its resolved managed environment even when `RETICULATE_PYTHON` was configured, so a successful call guarantees that its requirements are present.
The server retains the selected interpreter and normalized manifest and applies them to each sandboxed worker; the worker forces `UV_OFFLINE=1` and otherwise uses the existing sandbox policy unchanged.
For a server-managed worker, MCP Console seeds reticulate's requirement manifest and intercepts its internal uv environment and Python-version resolution.
It does not wrap `py_require()`, so reticulate retains its package attribution and activation behavior.
After reticulate accepts a managed environment, the worker sends a standalone `python_activated` event and the server immediately retains the matching resolved environment.
Acceptance and the restart-generation check are atomic; a receipt that remains pending when restart claims the generation is discarded with that worker.
`completed` and `python_prepared` carry no manifest.
Idle explicit preparation passes structured additions through the same bridge and materializes an uninitialized manifest or activates a same-`libpython` environment while preserving live state.
Its payload-free `python_prepared` receipt accepts a successfully materialized candidate that did not require live activation.
A lazy pre-initialization `py_require()` declaration remains worker-owned until Python initializes or explicit preparation materializes it, so a worker failure before either boundary loses that declaration.
Each runtime environment resolution sends the physical resolver manifest and the logical manifest to retain if accepted, together with the worker's current `UV_*` settings except `UV_OFFLINE`; those settings are not retained or replayed across worker generations.
Runtime Python-version selection sends only version constraints and the same transient settings, and creates no environment candidate.
After Python initializes, reticulate resolves late additions against the exact active Python patch version while leaving the logical `py_require()` Python constraints unchanged.
The requirement strings and forwarded settings are structured data rather than R code, and the resolver does not evaluate the submitted cell.
However, evaluated R code or an R package load can request this resolution, and reticulate and uv may access the network, write normal host caches, and execute a source distribution's build backend outside the worker sandbox.
Startup preflight has no MCP timeout and cannot be cancelled by closing MCP input because it completes before that input is accepted.
If the Python preflight cannot select an interpreter, `serve` exits before accepting MCP requests.
A failed Python resolution is a tool error and leaves the prior configuration unchanged.
For uv tool failures, the error includes a JSON resolver-input manifest with reticulate's Python selection and the complete candidate package set, followed by uv's stderr.
It omits reticulate's helper command, temporary output path, and interactive `py_require()` guidance.
Resolution has no per-call timeout.
When its direct resolver process exits, MCP Console stops any remaining in-group descendants before collecting resolver output; closing MCP input force-stops an in-flight resolver group.
Python `input()` and `breakpoint()`/`pdb` use reticulate's R console bridge, so each read emits `input_requested` before reading, then `input_received` after success or `input_cancelled` after an interrupt.
They accept proactively queued or follow-up stdin, including repeated debugger commands.
Reads through Python `sys.stdin` or fd 0 directly bypass the bridge and emit neither event.
Its MCP initialization identity remains `mcp-console`.
The intended default client registration name is `console`:

```bash
codex mcp add console -- mcp-console serve
```

Under Codex's current naming convention, the implemented tools are `mcp__console.send` and `mcp__console.session`; `session` supports R and Python requirement preparation, DuckDB extension preparation, live late additions, best-effort resolver or worker interruption, and explicit restart with optional additive R, Python, and DuckDB requirements for the implicit session.

On macOS, `sandbox` launches the command under `/usr/bin/sandbox-exec`.
The command can read the host filesystem, can write regular files only in a dedicated temporary directory, and cannot access the network.
The policy also permits the device and IPC operations needed for supported R, Python, and SQL workflows, including sandbox-created PTYs and Python multiprocessing semaphores.
This initial launcher waits only for the direct command.
Background descendants are unsupported: they may outlive the launcher, which attempts to remove their dedicated temporary directory on a best-effort basis when it returns.
Descendant supervision is intentionally deferred because it must account for process groups, session-detached children, signal forwarding, and PID reuse together.
Linux and Windows are not supported yet.

The proposed product and architecture remain under [`design-sketches/`](design-sketches/README.md).

## Development

Run the local checks with:

```bash
scripts/check
```

Format the repository with each installed formatter:

```bash
scripts/format
```

Formatter errors remain visible but do not stop the remaining formatters or make the script fail.

See [`tests/transcripts/README.md`](tests/transcripts/README.md) for running and authoring process-boundary transcript tests.
The `client_server/r` suite exercises the built-in worker.
The `client_server/python` suite exercises Python cells through reticulate in that worker.
The `client_server/sql` suite exercises DuckDB cells through DBI in that worker.
The `server_relay/protocol` suite launches a deterministic relay as the server's direct sandbox child and records the private JSONL frames in both directions.
The `relay_worker/protocol` suite drives `serve` through a transparent proxy, asserts the public MCP result, and records the relay-worker sideband and standard-stream boundary.
The `client_server/zod` suite uses the hidden `serve --worker PATH` development option to exercise the same protocol with an executable Python fixture.
These built-in-worker and protocol suites run on macOS, where the sandbox policy is implemented.
See [`docs/WORKER_PROTOCOL.md`](docs/WORKER_PROTOCOL.md) for the exact implemented launch and message contract.
See [`docs/RELAY_PROTOCOL.md`](docs/RELAY_PROTOCOL.md) for the private server-to-relay transport and sandbox process boundary.
See [`docs/TOOL_DESCRIPTIONS.md`](docs/TOOL_DESCRIPTIONS.md) for the exact descriptions registered for the MCP tools.

## License

MCP Console is licensed under the [MIT license](LICENSE).
