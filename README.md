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
Supplying exactly one of `r`, `python`, or `sql` evaluates one complete code cell and waits up to the optional `timeout_ms`, which defaults to 60 seconds.
When that wait expires, the call returns the newline-prefixed banner `\n[running]` while computation continues; call `send` without a code field to poll for completion.
A call may also supply exact standard-input text with a code cell, during an evaluation, or while the worker is idle:

```json
{ "r": "readline('name> ')", "stdin": "Ada\n" }
```

The server sends the cell first, then queues the string's UTF-8 bytes to worker fd 0 without inspecting or echoing them, adding a newline, imposing a size limit, or waiting for an input request.
A stdin-only call while idle lazily starts the worker when needed, queues the bytes, and returns the newline-prefixed banner `\n[idle]`.
Every `input_requested` event adds a server-owned record such as `[input requested: "name> "]`; the prompt is encoded as a JSON string so spaces and escaped characters remain explicit.
When that request remains outstanding for up to 10 milliseconds, bounded by the call deadline, `send` follows the record with the newline-prefixed banner `\n[stdin needed]`; a later call can supply more bytes with `{ "stdin": "Ada\n" }`.
An immediate `input_received` receipt retains the request record but suppresses `[stdin needed]`, so prequeued input can satisfy a console read without forcing another tool call.
That receipt describes the runtime read, not a particular stdin payload; direct fd-0 reads emit no request or receipt.
Payload end is not EOF, and queued input is not an acknowledgment of consumption.
Unread bytes may be completed by later stdin or satisfy a later worker read or evaluation.
Before the worker starts, the MCP client can prepare additive Python requirements for the implicit session:

```json
{
  "action": "prepare",
  "requirements": { "python": ["numpy<2", "pandas"] }
}
```

This `session` call resolves the complete initial requirement set through reticulate and uv outside the worker sandbox, then returns `[prepared]`.
It does not import the packages or start the worker.
Exact repeated requirements are idempotent.
Once the worker has started, a new requirement returns `restart required` without changing the environment; runtime activation and an explicit restart action are not implemented yet.
On macOS, the default managed-Python preflight happens during `serve` startup when required; a successful `prepare` replaces that initial selection before the first nonempty stdin submission or evaluation lazily starts the sandboxed embedded R worker.
Later calls reuse the same global R state, reticulate Python interpreter, and in-memory DuckDB catalog.
An infrastructure or protocol failure discards that worker and its in-memory R, Python, and SQL state.
Worker output available when the failure response is assembled remains visible; when it shares that response with the MCP tool error, the server starts the bracketed error on a new line.
The next response after its replacement successfully starts includes the newline-delimited banner `[worker restarted: in-memory state lost]\n`, preceded by a newline when prior output does not already supply one; initial lazy startup remains silent.
The worker runs each R cell through R's native top-level loop, captures R console output, prints each visible value, and maintains `.Last.value`.
If a cell ends while an expression is incomplete, earlier complete expressions from that cell remain applied.
The worker installs a worker-owned `grDevices::png()` function as R's default graphics device and opens it lazily when a cell draws.
Finalized managed pages are returned in the order they finish, including when a cell switches among multiple managed devices.
Once the first managed page opens, later R console text waits until cell end so it cannot appear ahead of an unfinished page.
At cell end, including after a normal R error, the worker closes its managed devices, returns their remaining pages as MCP images, then returns all deferred console text.
This cell-level ordering does not reconstruct text emitted between individual pages.
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
Python cells execute statements in persistent `__main__` state and send a final expression through Python's display hook.
R and Python can exchange objects through reticulate's `py` and `r` bridges.
R plots invoked from a Python cell through reticulate's `r` bridge use the same managed default device, sizing options, cell scope, and MCP image output as plots invoked from an R cell.
Reticulate routes Python text written through `sys.stdout` and `sys.stderr`, including tracebacks, through the same sideband console output path as R.
Writes through `sys.stdout.buffer`, `sys.stderr.buffer`, or fd 1/2 directly remain on the captured standard streams.
After a Python cell calls `os.fork()`, reticulate restores the child's original fd-backed text streams after its sideband is disabled, so its ordinary stdout and stderr are captured too.
Native extensions that fork without running CPython's registered fork callbacks and then resume Python are unsupported.
Fork-child text capture requires reticulate from its `main` branch or a release containing fork-aware stream restoration.
An exec descendant that retains fd 1/2 creates fresh standard streams backed by those descriptors, so its ordinary stdout and stderr are captured.
SQL cells lazily open one in-memory DuckDB connection through the `duckdb` and `DBI` R packages and reuse it for the worker generation.
DuckDB extension, secret, and spill paths stay under the worker's private R temporary directory.
The worker sends the complete SQL source out of band to a private R bridge and executes query results through DBI's streaming Arrow API.
It fetches at most 21 rows, uses the final row only to detect that more data exists, and renders at most 20 rows and 12 columns with 160-character cells and a 12 KiB SQL-preview limit.
The preview shows Arrow column types, SQL `NULL`, and empty-result schemas; DuckDB converts only the bounded displayed cells to text and applies the cell limit before returning them to R, preserving values such as `BIGINT`, `DECIMAL`, lists, and structs when they fit.
It reports omitted rows without counting the complete result and reports omitted columns explicitly; the final byte limit may reduce the displayed rows or columns further.
Statements without result columns are silent, so they return `[done]` when they produce no other output.
DuckDB errors are normal console results and leave the worker available for later cells.
This initial slice does not enable R environment scanning, relation registration, or affected-row summaries.

The server also collects text written directly to the worker's standard output and standard error, including direct writes by descendants that retain those descriptors.
It retains raw bytes until the next `send` response is assembled; output produced while the worker is idle can therefore appear on a later idle poll before the server-owned `\n[idle]` banner.
Ordering among standard output, standard error, and sideband console or image output is best effort.
R language failures, uncaught Python exceptions, and DuckDB errors remain ordinary console results rather than MCP tool errors.
A silent successful R, Python, or SQL cell sends no sideband `output` frame, still sends `completed`, and projects to `[done]` when no other response text is pending.

Python cells require the `reticulate` R package.
SQL cells require the `arrow`, `DBI`, `duckdb`, `nanoarrow`, `pillar`, and `tibble` R packages.
When `RETICULATE_PYTHON` is unset or is `managed`, `mcp-console serve` runs reticulate's uv environment resolver outside the worker sandbox with its NumPy baseline, where it can use the normal host caches and network access.
Other configured values, including an empty value, are preserved when no requirements are prepared and skip this startup preflight.
An explicit `session` preparation selects its resolved managed environment even when `RETICULATE_PYTHON` was configured, so a successful call guarantees that its requirements are present.
The server passes the selected interpreter path to the sandboxed worker, which forces `UV_OFFLINE=1` and otherwise uses the existing sandbox policy unchanged.
The selection is fixed before worker startup; worker-side `py_require()` calls do not revise it.
Requirement strings are passed as data rather than evaluated as R code, but reticulate and uv run outside the sandbox and package preparation may execute a source distribution's build backend there.
The resolver does not evaluate submitted R or Python cells.
If the preflight cannot select an interpreter, `serve` exits before accepting MCP requests.
A failed `session` preparation is a tool error and leaves the prior requirements and interpreter selection unchanged.
MCP Console does not install these R packages.
Python `input()` and `breakpoint()`/`pdb` use reticulate's R console bridge, so each read emits `input_requested` before reading and `input_received` after a successful read.
They accept proactively queued or follow-up stdin, including repeated debugger commands.
Reads through Python `sys.stdin` or fd 0 directly bypass the bridge and emit neither event.
Its MCP initialization identity remains `mcp-console`.
The intended default client registration name is `console`:

```bash
codex mcp add console -- mcp-console serve
```

Under Codex's current naming convention, the implemented tools are `mcp__console.send` and `mcp__console.session`; `session` currently supports only initial Python requirement preparation for the implicit session.

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

See [`tests/transcripts/README.md`](tests/transcripts/README.md) for running and authoring external server transcript tests.
The `r` suite exercises the built-in worker.
The `python` suite exercises Python cells through reticulate in that worker.
The `sql` suite exercises DuckDB cells through DBI in that worker.
The `worker` suite drives `serve` through a transparent proxy, asserts the public MCP result, and records the built-in worker's sideband and standard-stream events.
The `zod` suite uses the hidden `serve --worker PATH` development option to exercise the same protocol with an executable Python fixture.
These built-in-worker and protocol suites run on macOS, where the sandbox policy is implemented.
See [`docs/WORKER_PROTOCOL.md`](docs/WORKER_PROTOCOL.md) for the exact implemented launch and message contract.

## License

MCP Console is licensed under the [MIT license](LICENSE).
