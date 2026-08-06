# AGENTS.md

Keep this file synchronized with the code that exists in the repository.
The documents under `design-sketches/` describe intended behavior, not implemented behavior.

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
The server registers only a `send` tool.
Supplying exactly one of `r`, `python`, or `sql` starts one complete cell and waits for up to `timeout_ms`, which defaults to 60 seconds.
If that wait expires, `send` returns the newline-prefixed banner `\n[running]` without stopping the computation; a later call without a code field polls it, and a poll while idle returns `\n[idle]`.
Concurrent `send` calls are unsupported.
Supplying `stdin` with a code cell, during an evaluation, or while idle queues exact UTF-8 bytes to worker fd 0 without adding a newline, inspecting, echoing, or limiting the text, or waiting for an input request.
A nonempty idle stdin call lazily starts the worker when needed, queues the bytes, and returns `\n[idle]`; `timeout_ms` does not bound that startup because the call does not wait on an evaluation.
Payload end is not EOF; the R console callback reads through one newline or its supplied buffer, and unread bytes may satisfy later console or direct reads, including in a later evaluation.
Every `input_requested` frame immediately appends `[input requested: <JSON-quoted prompt>]` to pending response output.
Its outstanding state is provisional for up to 10 milliseconds; a matching `input_received` after a successful console read retains the request record but suppresses the `\n[stdin needed]` banner, while an unmatched request returns that marker after the grace or at the MCP deadline, whichever comes first.
The receipt describes that runtime read, not a submitted payload or byte count, and direct fd-0 reads emit neither frame.
New code is rejected until the running evaluation's result has been collected.
Worker `image` frames carry base64 data and a MIME type.
The server preserves sideband text and image order as MCP content blocks, coalesces adjacent text, and does not add `[done]` when an image is the only output.
On macOS, managed-Python preflight happens during `serve` startup when required; the first nonempty stdin submission or evaluation still lazily starts the built-in worker under the same sandbox policy as the `sandbox` command.
The worker embeds R through `libr` and `harp`, retains global state, and feeds each complete R cell through R's DLL REPL iterator.
R parses and evaluates its expressions sequentially, captures console output, prints visible values, and performs native top-level bookkeeping.
Cell EOF while R requires continuation input is an error; earlier complete expressions from that cell remain applied.
R parse, evaluation, and auto-print failures are normal language outcomes with `isError: false`.
The worker installs a worker-owned `grDevices::png()` function as R's default graphics device and opens it lazily during plotting.
At R top-level expression boundaries, it emits finalized managed pages as `image/png` MCP content in page order.
Once the first managed page opens, later R console text is deferred until cell end.
Cell-end cleanup closes every still-open managed device, emits its remaining pages, then emits all deferred console text, including normal R errors.
This conservative cell-level ordering does not reconstruct text emitted between individual pages.
Managed devices are cell scoped, so one plot's drawing operations must be submitted in the same cell.
Their default dimensions are 800 by 600 pixels at 96 DPI; persistent `console.plot.width`, `console.plot.height`, and `console.plot.dpi` options configure positive finite dimensions in inches and resolution.
Explicitly opened or selected devices remain user-owned: the worker does not close them, read their files, or emit images for them.
A silent successful R cell sends `completed` without an `output` frame and projects to `[done]` when no other response text is pending.
Submitted R functions do not currently retain a source filename.
Python cells run in the same worker through reticulate, retain `__main__` state, execute statements, and send a final expression through `sys.displayhook()`.
Python source uses a synthetic evaluation filename, and uncaught exceptions print a Python traceback as a normal language outcome with `isError: false`.
Python cells enter the same managed graphics lifecycle as R cells, so R plots invoked through reticulate's `r` bridge return as MCP images under the same sizing, cell-scope, device-ownership, and output-ordering rules.
At worker startup, MCP Console sets `RETICULATE_REMAP_OUTPUT_STREAMS=1` once, before user R can initialize Python.
Within the worker process, reticulate then routes Python text writes, including `print()`, `sys.stderr.write()`, and tracebacks, through the R console callback as sideband `output` frames.
Writes through `sys.stdout.buffer`, `sys.stderr.buffer`, or native fd 1/2 bypass that remap and use the captured standard streams.
After a Python cell calls `os.fork()`, the child cannot use the sideband, so reticulate's registered CPython fork callback restores the original fd-backed text streams and ordinary stdout and stderr writes remain captured.
Native extensions that fork without running CPython's registered fork callbacks and then resume Python are unsupported.
Fork-child text capture requires reticulate from its `main` branch or a release containing fork-aware stream restoration.
An exec descendant that retains fd 1/2 creates fresh standard streams backed by those descriptors, so its ordinary stdout and stderr are captured.
When inherited `RETICULATE_PYTHON` is absent or exactly `managed`, built-in server startup asks reticulate to resolve managed Python outside the sandbox and injects the resulting interpreter path into every worker generation.
Other inherited values, including an empty value, are preserved and skip that preflight; custom workers also skip it.
The preflight may access the network and write normal reticulate and uv host caches, but it runs before sideband setup and evaluates no submitted cell.
Before initializing R, the worker forces `UV_OFFLINE=1`, overwriting any inherited value to match the sandbox's network denial.
Reticulate reuses the preflight-selected or caller-selected interpreter.
Because the preflight-selected interpreter is passed as a concrete path, worker-side `py_require()` calls do not revise that selection.
R and Python share objects through reticulate's `py` and `r` bridges.
A silent successful Python cell sends `completed` without an `output` frame and projects to `[done]` when no other response text is pending.
Python `input()` and `breakpoint()`/`pdb` use reticulate's R console bridge, so they emit `input_requested` before a read and `input_received` after it succeeds.
They accept proactively queued or follow-up stdin, including repeated debugger commands.
Python `sys.stdin` and other direct fd-0 reads bypass the bridge and emit neither frame.
SQL cells use the `duckdb` and `DBI` R packages through a private R bridge; previews also require `nanoarrow`, `arrow`, `tibble`, and `pillar`.
The first SQL cell lazily opens one in-memory connection with environment scanning disabled, and later cells in that worker generation reuse its catalog.
DuckDB extension, secret, and spill paths are explicit children of the worker's private R temporary directory.
The connection disables DuckDB progress output so previews contain only query results.
The worker stores SQL source in private R state and calls the bridge with a short evaluation ID.
Query results use `DBI::dbSendQueryArrow()` and one streaming `DBI::dbFetchArrow()` batch of at most 21 rows.
The worker displays at most 20 rows and 12 columns through pillar, limits cells to 160 characters, and limits the SQL preview itself to 12 KiB; the byte limit may reduce rows or columns further.
The 21st row determines only whether to append the omitted-row marker; the worker does not count or materialize the complete result.
Arrow schemas keep column names and types visible for empty results, while DuckDB stringifies only the bounded displayed batch and applies the cell limit before returning text to R so `NULL`, `BIGINT`, `DECIMAL`, and nested values remain exact when they fit.
Temporary Arrow relations use collision-checked names and are unregistered after formatting.
DDL and DML results without columns are silent; affected-row summaries do not exist yet.
DuckDB errors are normal language outcomes with `isError: false`, and the connection remains reusable.
R relation scanning and registration do not exist.
Worker standard output and standard error are piped and collected continuously, including while the worker is idle.
Each pipe reader queues raw byte chunks, and each `send` response decodes and drains complete UTF-8 prefixes from bytes already collected at its response boundary; later bytes remain for the next response.
Without a pending restart notice, idle, running, and outstanding-input responses append the literal `\n[idle]`, `\n[running]`, or `\n[stdin needed]` banner; its leading newline is present even when no output precedes it.
After an infrastructure failure discards a ready worker, its successfully started replacement queues `[worker restarted: in-memory state lost]\n` in pending response output.
The next response drains it exactly once, after runtime or error text, inserting a preceding newline only when needed.
If an idle, running, or outstanding-input banner follows, the restart notice's trailing newline supplies its separator.
Initial lazy startup and retries before a worker reaches ready are silent.
Completion returns collected standard-stream and pending evaluation content, including sideband text, images, and input-request records, instead of `[done]` when any produced content.
A failed evaluation likewise returns all pending evaluation output and any complete standard-stream output available at the response boundary before its infrastructure or protocol error.
When worker output or a restart notice shares that response, the server starts the bracketed error on a new line; an error returned alone remains bare.
Ordering between the two standard streams and sideband output is best effort; incomplete UTF-8 remains with its pipe until a later response, and invalid UTF-8 is replaced when output is rendered.
The built-in worker and custom workers send console prompt fields verbatim; the server preserves each value without trimming it and renders it as a JSON-quoted `[input requested: ...]` record.
Writes to inherited fd 1 or fd 2 from descendants follow the same path, but this does not add descendant supervision; forked descendants cannot use the inherited sideband.
The hidden `worker` command takes ownership of the sideband, discovers `R_HOME` through the selected R executable inside the sandbox, and opens `R_HOME/lib/libR.dylib` by its absolute path.
It does not self-execute or set a dynamic-loader environment variable.
The worker command runs synchronously on the process main thread; only `serve` creates a Tokio runtime.
The hidden development option `serve --worker PATH` replaces the built-in worker with an executable that implements the same sideband request/receipt protocol and fd-0 input contract.
The Python fixture `tests/fixtures/zod` provides deterministic acceptance coverage for R, Python, and SQL language tags at that boundary, MCP image content, direct fd-0 input, captured standard streams, and server-owned timeout and polling mechanics.
An infrastructure or protocol failure is returned as a tool error, force-stops and discards that worker, and lets the next evaluation or nonempty idle stdin submission start a fresh worker with the replacement notice above.
When MCP input closes, the server starts a one-second deadline and attempts graceful sideband shutdown without delaying it.
If the direct sandbox process is still running when time expires, the sandbox boundary force-stops its process group and reaps that direct process.
The version command prints the package name and version.
On macOS, the sandbox command launches a subprocess under `sandbox-exec` with host filesystem reads allowed, regular-file writes limited to a dedicated per-launch temporary directory, runtime device and IPC exceptions, and network access denied.
This initial launcher waits only for the direct command.
Background descendants are unsupported: they may outlive the launcher, which attempts to remove their dedicated temporary directory on a best-effort basis when it returns.
Descendant supervision is intentionally deferred because it must account for process groups, session-detached children, signal forwarding, and PID reuse together.
The sandbox command and worker are unsupported on Linux and Windows.
The session model, SQL relation bridges, sidecar API, viewer, environment management, output retention, and transcript generation do not exist yet.

## Product direction

MCP Console is intended to become a persistent, sandboxed R, Python, and DuckDB SQL console exposed through MCP.
The planned public MCP surface has two tools:

- `send` evaluates complete R, Python, or SQL cells, writes to the session's stdin stream, and polls for output.
- `session` manages session requirements and lifecycle operations.

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
- `src/python.rs` — managed-Python preflight, worker environment, and reticulate bridge.
- `src/r_bridge.rs` — shared private R-environment bridge used by graphics, Python, and SQL adapters.
- `src/r_graphics.rs` — cell-scoped managed R graphics device and PNG image publication.
- `src/server.rs` — MCP stdio server, `send` tool, and worker selection.
- `src/sql.rs` — persistent DuckDB/DBI SQL bridge and bounded streaming Arrow previews.
- `src/r_repl.c` — C-owned per-cell DLL-REPL iterator and long-jump boundary.
- `src/sideband.rs` — macOS inherited-pipe JSON-lines transport.
- `src/worker.rs` — embedded R initialization, cell dispatch, and console callbacks.
- `src/worker_client.rs` — server-side worker launch, lifecycle, fd-0 input, and output collection.
- `src/worker_protocol.rs` — shared sideband message definitions.
- `src/sandbox.rs` — platform dispatch for the sandbox process launcher.
- `src/sandbox/` — platform implementation and macOS Seatbelt policy.
- `tests/cli.rs` — public binary acceptance tests.
- `tests/fixtures/zod` — executable Python sideband worker used by acceptance tests.
- `tests/fixtures/worker_mitm` — transparent worker proxy used to capture sideband and standard-stream events through `serve`.
- `tests/transcripts/r.py` — public built-in R worker acceptance suite.
- `tests/transcripts/python.py` — public reticulate Python-cell acceptance suite.
- `tests/transcripts/sql.py` — public persistent-DuckDB acceptance suite.
- `tests/transcripts/worker.py` — public-server acceptance plus captured built-in worker wire events.
- `tests/transcripts/_run.py` — discovers transcript suites and compares case snapshots.
- `tests/transcripts/_support.py` — shared transcript types and MCP stdio client.
- `tests/transcripts/<suite>.py` — suites of named imperative transcript cases.
- `tests/transcripts/golden/SUITE/` — human-readable YAML 1.2 case transcripts.
- `tests/transcripts/README.md` — transcript test usage and authoring guide.
- `scripts/test` — builds the binary and runs selected external Python tests through `uv`.
- `scripts/format` — attempts each repository-wide formatter without requiring it.
- `scripts/check` — local formatting, Clippy, and test checks.
- `.github/workflows/ci.yaml` — formatting, Clippy, and test checks.
- `docs/WORKER_PROTOCOL.md` — exact implemented worker launch and sideband protocol.
- `design-sketches/` — tentative product and architecture documents.
- `README.md` — current user-facing project status.
- `LICENSE` — project license.

Add modules only when implemented public behavior needs them.
Begin as one Cargo package and split crates only when a real boundary emerges.

## Working rules

- Keep PRs coherent, compact, and easy to review.
  As a heuristic, aim to keep implementation-code changes under 200 added and deleted lines.
  Tests, golden snapshots, and documentation do not count toward this guideline.
  The line count is not a limit; prefer a larger coherent change over splits that make the work harder to understand or validate.
- Each PR should implement and test one observable behavior.
  Update design documents in the same PR only when they describe that behavior.
- Add a public acceptance or regression test first and confirm that it fails before implementing behavior.
- Test through public interfaces.
  Do not add tests for private helpers.
- Format embedded R, Python, SQL, and shell test programs as multiline raw strings.
  Use escape sequences such as `\n` only when the program needs that character as data, not to lay out its source.
- Keep complete code cells separate from interactive `stdin`.
- Keep the MCP adapter independent of interpreter implementation details.
- Treat submitted R, Python, and SQL execution as shell-class capability and place safety at the worker-process boundary.
  The managed-Python preflight is a host-bootstrap exception: it runs before MCP input is accepted and never evaluates submitted code.
- Update this file when a PR changes the implemented surface or repository map.
- Before every commit, run `scripts/format` and review its changes.
- Run `scripts/check` before opening a PR.
