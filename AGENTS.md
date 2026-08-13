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
The server registers `send` and `session` tools.
The first ordinary, non-task `send` or `session` call creates a run-specific directory under `.mcp-console/sessions/` in the server's initial working directory.
Initialization, tool listing, unknown tool calls, and an otherwise unused `serve` process create no record.
On Unix, newly created record directories use mode `0700`, and journal and artifact files use mode `0600`.
It appends schema-versioned `session_started`, `tool_call`, `artifact_created`, and `tool_result` records for ordinary, non-task `send` and `session` calls to `internal/events.jsonl`.
Tool records preserve timestamps, MCP request IDs, normalized parsed call parameters, final results or errors, and content-block order.
`tool_result` records server assembly, not delivery; cancellation or disconnection may suppress the response.
Image blocks remain in MCP results and are also decoded byte-for-byte under the run's `artifacts/` directory as soon as the worker publishes them, including when the evaluation is never polled again; the journal replaces their base64 data with relative paths.
Journal writes are flushed before a tool begins and after its result is assembled.
If the run record cannot be created or a later recording write fails, the server disables recording, emits one diagnostic to standard error, and continues serving console calls.
An existing journal may therefore end with the last successfully flushed event.
Submitted source, stdin, and tool-result output are recorded without redaction.
Generated Quarto transcripts and complete output spools do not exist yet.
Supplying exactly one of `r`, `python`, or `sql` starts one complete cell and waits for up to `timeout_ms`, which defaults to 60 seconds.
If that wait expires, `send` drains output produced so far, appends the newline-prefixed banner `\n[running]`, and leaves the computation running; a later call without a code field polls it, and a poll while idle returns `\n[idle]`.
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
The implemented `session` surface accepts `action = "prepare"` with one or more R or Python requirement strings or `action = "restart"` with optional Python requirement strings for the implicit session.
Requirements are exact, additive, and idempotent.
Before the worker starts, each successful prepare resolves the complete candidate sets outside the sandbox, atomically retains them in server memory, and returns `[prepared]` without starting the worker.
Before each R resolution, the server requires `ir --version` from `PATH` to report 0.4.0 or later; it then uses `ir run` with the worker's Rscript, and the result becomes the first worker `R_LIBS` entry.
Python requirements use reticulate and uv and replace any inherited Python selection with the resolved interpreter.
The server sets `IR_NO_LOCAL_SOURCES` for every R resolution, so IR prevents direct or transitive local package installation while retaining ownership of package-reference parsing.
A failed resolution leaves the prior requirements, R library, and interpreter unchanged.
For a uv tool failure, the tool error reports a JSON manifest containing reticulate's selected Python and the complete candidate package set, followed by uv's stderr, while omitting the helper command, temporary output path, and reticulate's `py_require()`-oriented guidance.
The direct resolver process defines its process-group lifetime: after it exits, the server force-stops any remaining in-group descendants before reaping it and collecting its standard streams.
Closing MCP input cancels an in-flight explicit or runtime resolution by force-stopping its host resolver process group; startup preflight completes before MCP input is accepted and is not cancellable through that lifecycle.
An idle server-managed worker can apply Python-only additions through reticulate without replacement.
It materializes an uninitialized manifest or activates a same-`libpython` environment while preserving live state.
The server returns `[prepared]` only after checkpointing the result; failure preserves the live and retained manifests.
Preparation during evaluation is rejected.
A call with a new R requirement after startup returns `[restart required]` and applies none of that call's additions.
New requirements also return that marker while a failed worker awaits replacement; prepare does not start or configure the replacement.
Restart retains the prepared R library, merges any supplied Python additions into the complete checkpointed manifest, and resolves the candidate before terminating the current worker.
A failed restart resolution leaves the current worker, its in-memory state, requirements, R library, and Python interpreter unchanged.
After successful resolution, restart retains the prepared R library and candidate Python environment, loses all worker-owned in-memory state and unread stdin, eagerly starts a replacement, and returns `[restarted]` after it reports ready.
The implicit session exists for the server lifetime, so restart starts its first worker if none exists yet.
It first queues worker-stdin closure and the sideband shutdown message without waiting behind an evaluation, then force-stops the process group and reaps the direct sandbox process at the one-second deadline if that process remains live.
It then waits for the active sideband operation to end, cancels the worker's stdin writer and standard-stream readers, drains standard-stream bytes already buffered at that boundary, and joins the tasks before reporting `[worker stopped: in-memory state lost]` or launching the replacement.
Each admitted evaluation or idle stdin write carries its worker generation, so work admitted before restart cannot reach the replacement.
A live Python preparation invalidated by restart returns `Python preparation cancelled by restart`; active-generation sideband failures retain their transport diagnostics.
The explicit restart response preserves old-worker output, the stopped notice when a worker existed, `[starting new worker]`, replacement startup output, and `[restarted]` in that order.
Named sessions and runtime R requirement additions do not exist yet.
On macOS, managed-Python preflight happens during `serve` startup when required; the first nonempty stdin submission or evaluation still lazily starts the built-in worker under the same sandbox policy as the `sandbox` command.
The worker embeds R through `libr` and `harp`, retains global state, and feeds each complete R cell through R's DLL REPL iterator.
R parses and evaluates its expressions sequentially, captures console output, prints visible values, and performs native top-level bookkeeping.
Cell EOF while R requires continuation input is an error; earlier complete expressions from that cell remain applied.
R parse, evaluation, and auto-print failures are normal language outcomes with `isError: false`.
The worker installs a worker-owned `grDevices::png()` function as R's default graphics device and opens it lazily during plotting.
After a managed device's new-page or close callback returns normally, the worker immediately reads, removes, and emits the finalized PNG as `image/png` MCP content.
R console text is emitted immediately rather than deferred for unfinished plots.
Cell-end cleanup closes every still-open managed device and emits its remaining page, including after normal R errors.
Managed devices are cell scoped, so one plot's drawing operations must be submitted in the same cell.
Their default dimensions are 800 by 600 pixels at 96 DPI; persistent `console.plot.width`, `console.plot.height`, and `console.plot.dpi` options configure positive finite dimensions in inches and resolution.
Graphics devices opened explicitly by evaluated code, such as with `grDevices::png()`, remain user-owned: the worker does not close them, read their files, or emit images for them.
A silent successful R cell sends `completed` without an `output` frame and projects to `[done]` when no other response text is pending.
Submitted R functions do not currently retain a source filename.
Python cells run in the same worker through reticulate, retain `__main__` state, execute statements, and display a final expression through `sys.displayhook()`.
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
Within the worker process, reticulate then routes Python text writes, including `print()`, `sys.stderr.write()`, and tracebacks, through the R console callback as sideband `output` frames.
Writes through `sys.stdout.buffer`, `sys.stderr.buffer`, or native fd 1/2 bypass that remap and use the captured standard streams.
After a Python cell calls `os.fork()`, the child cannot use the sideband, so reticulate's registered CPython fork callback restores the original fd-backed text streams and ordinary stdout and stderr writes remain captured.
Native extensions that fork without running CPython's registered fork callbacks and then resume Python are unsupported.
Fork-child text capture requires reticulate from its `main` branch or a release containing fork-aware stream restoration.
An exec descendant that retains fd 1/2 creates fresh standard streams backed by those descriptors, so its ordinary stdout and stderr are captured while that worker generation's output boundary remains open.
When inherited `RETICULATE_PYTHON` is absent or exactly `managed`, built-in server startup calls reticulate's internal uv environment resolver with its NumPy baseline outside the sandbox and retains the resulting interpreter and normalized manifest for every worker generation.
Other inherited values, including an empty value, are preserved and skip that startup preflight; a later successful explicit preparation takes precedence over them.
Custom workers skip resolution and reject R and Python requirement preparation and restart requests with Python requirements.
R and Python resolution may access the network, write normal host caches, and execute package installation or build code outside the sandbox; managed Python environment startup and the Matplotlib font-manager import also run there.
Requirement strings remain process-argument or JSON data rather than R source, and no submitted cell is evaluated by the resolver.
Before initializing R, the worker forces `UV_OFFLINE=1`, overwriting any inherited value to match the sandbox's network denial.
Reticulate reuses the server-resolved or caller-selected interpreter.
For a server-managed worker, MCP Console seeds reticulate's requirement manifest and intercepts only its internal `uv_get_or_create_env` binding, without wrapping `py_require()`.
Each runtime or explicit-preparation request sends the complete proposed manifest and the worker's current `UV_*` settings except `UV_OFFLINE` to the host resolver; those settings are transient inputs and are not retained or replayed.
Explicit preparation sends structured additions and reports a separate checkpoint or failure without evaluating a cell.
If managed reticulate is loaded but Python remains uninitialized at cell end or after explicit preparation, the worker invokes the resolver once to materialize the final manifest.
After initialization, additive package requirements resolve to candidate environments outside the sandbox, and reticulate performs its exact-`libpython` check, `activate_this.py`, configuration swap, and manifest assignment.
The worker retains every resolved candidate for the active operation.
The server accepts the last candidate matching its reported checkpoint, or the prior environment when its manifest still matches, and only then updates its retained state.
Normal language outcomes reach the evaluation checkpoint; an infrastructure or protocol failure leaves the prior checkpoint unchanged.
The live Python interpreter and its state are retained during successful activation.
Evaluated R code or an R package load can therefore trigger host resolution, which may use the network, write host caches, and execute package build backends outside the worker sandbox; the structured requirements and forwarded settings are data, and the submitted cell is not evaluated by the resolver.
Python reads R globals through reticulate's `r.name` bridge, and R reads Python globals through the worker-attached `py$name` binding.
A silent successful Python cell sends `completed` without an `output` frame and projects to `[done]` when no other response text is pending.
Python `input()` and `breakpoint()`/`pdb` use reticulate's R console bridge, so they emit `input_requested` before a read and `input_received` after it succeeds.
They accept proactively queued or follow-up stdin, including repeated debugger commands.
Python `sys.stdin` and other direct fd-0 reads bypass the bridge and emit neither frame.
SQL cells use the `duckdb` and `DBI` R packages through a private R bridge; previews also require `nanoarrow`, `arrow`, `tibble`, and `pillar`.
The first SQL cell or call to `sql_connection()` lazily opens one in-memory connection with environment scanning enabled, and later operations in that worker generation reuse its catalog.
DuckDB extension, secret, and spill paths are explicit children of the worker's private R temporary directory.
The connection disables DuckDB progress output so previews contain only query results.
The worker stores SQL source in private R state and calls the bridge with a short evaluation ID.
The bridge sends queries through a zero-argument closure enclosed by R's global environment, so DuckDB searches the persistent R session rather than the private bridge environment.
An unqualified catalog table or view takes precedence over a same-named R binding; otherwise DuckDB can scan a data frame in the R global environment, and an SQL view over that name observes later rebinding.
The bridge installs a forwarding active binding for reticulate's `py` and the `sql_connection()` helper in a worker-owned `tools:mcp-console` environment at search position 2, so clearing R's global environment does not remove them and same-named global bindings still take precedence.
It returns a borrowed reference to the same worker-owned connection; callers must not disconnect it.
Established DuckDB, DBI, and dplyr interfaces can use that connection, and lazy dplyr relations observe later catalog changes until collection.
Prepared queries retain scanned data frames until their DBI results are cleared.
Query results use `DBI::dbSendQueryArrow()` and one streaming `DBI::dbFetchArrow()` batch of at most 21 rows.
The worker displays at most 20 rows and 12 columns through pillar, limits cells to 160 characters, and limits the SQL preview itself to 12 KiB; the byte limit may reduce rows or columns further.
The 21st row determines only whether to append the omitted-row marker; the worker does not count or materialize the complete result.
Arrow schemas keep column names and types visible for empty results, while DuckDB stringifies only the bounded displayed batch and applies the cell limit before returning text to R so `NULL`, `BIGINT`, `DECIMAL`, and nested values remain exact when they fit.
Temporary Arrow relations use collision-checked names and are unregistered after formatting.
DDL and DML results without columns are silent; affected-row summaries do not exist yet.
DuckDB errors are normal language outcomes with `isError: false`, and the connection remains reusable.
Automatic Python relation sharing and a separate relation-registration API do not exist.
Sideband text and images, worker standard-output and standard-error bytes, failures, and lifecycle notices share one pending output tape in publication order.
Each pipe reader queues raw byte chunks, and each successful `send` response drains all tape events available at its response boundary, decoding complete UTF-8 prefixes and retaining incomplete suffixes for a later response.
Idle, running, and outstanding-input responses append the literal `\n[idle]`, `\n[running]`, or `\n[stdin needed]` banner; its leading newline is present even when no output precedes it.
After an infrastructure failure, the server finishes worker shutdown and its I/O readers before appending `[worker stopped: in-memory state lost]` after the specific error.
Each replacement attempt appends `[starting new worker]\n` before launch, so its startup output or error follows that fact.
Initial lazy startup and retries before a worker reaches ready are silent.
Completion returns pending text, images, and input-request records instead of `[done]` when any content was produced.
A failed evaluation likewise returns all pending output before its infrastructure or protocol error.
When worker output or a lifecycle notice shares that response, the server starts the bracketed error on a new line; an error returned alone remains bare.
Ordering between the two standard streams and sideband output is best effort; incomplete UTF-8 remains with its pipe until a later response, and invalid UTF-8 is replaced when output is rendered.
The built-in worker and custom workers send console prompt fields verbatim; the server preserves each value without trimming it and renders it as a JSON-quoted `[input requested: ...]` record.
Writes to inherited fd 1 or fd 2 from descendants follow the same path until worker retirement cancels those pipe readers; a descendant retaining fd 0 likewise cannot keep a blocked server write alive past retirement.
This does not add descendant supervision, and forked descendants cannot use the inherited sideband.
The hidden `worker` command takes ownership of the sideband, discovers `R_HOME` through the selected R executable inside the sandbox, and opens `R_HOME/lib/libR.dylib` by its absolute path.
It does not self-execute or set a dynamic-loader environment variable.
The worker command runs synchronously on the process main thread; only `serve` creates a Tokio runtime.
The hidden development option `serve --worker PATH` replaces the built-in worker with an executable that implements the same sideband request/receipt protocol and fd-0 input contract.
The Python fixture `tests/fixtures/zod` provides deterministic acceptance coverage for R, Python, and SQL language tags at that boundary, MCP image content, direct fd-0 input, captured standard streams, and server-owned timeout and polling mechanics.
An infrastructure or protocol failure is returned as a tool error, fully retires that worker, and lets the next evaluation or nonempty idle stdin submission start a fresh worker with the starting notice above.
When MCP input closes, the server cancels any active host resolver and starts a one-second deadline for graceful sideband shutdown without delaying it.
If the direct sandbox process is still running when time expires, the sandbox boundary force-stops its process group and reaps that direct process.
The version command prints the package name and version.
On macOS, the sandbox command launches a subprocess under `sandbox-exec` with host filesystem reads allowed, regular-file writes limited to a dedicated per-launch temporary directory, runtime device and IPC exceptions, and network access denied.
This initial launcher waits only for the direct command.
Background descendants are unsupported: they may outlive the launcher, which attempts to remove their dedicated temporary directory on a best-effort basis when it returns.
Descendant supervision is intentionally deferred because it must account for process groups, session-detached children, signal forwarding, and PID reuse together.
The sandbox command and worker are unsupported on Linux and Windows.
Named sessions, runtime R requirement additions, Python relation sharing, the sidecar API, viewer, complete output retention, and generated Quarto transcripts do not exist yet.

## Product direction

MCP Console is intended to become a persistent, sandboxed R, Python, and DuckDB SQL console exposed through MCP.
The public MCP surface has two tools:

- `send` evaluates complete R, Python, or SQL cells, writes to the session's stdin stream, and polls for output.
- `session` manages session requirements and lifecycle operations.

R and Python requirement preparation, live late Python additions, and explicit restart with optional additive Python requirements are implemented for `session`; its broader lifecycle surface remains planned.

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
- `src/resolver/managed_r.rs` — macOS IR-backed R library resolution.
- `src/resolver/managed_python.rs` — macOS reticulate/uv-managed Python resolution.
- `src/resolver/process.rs` — shared macOS resolver process-group lifecycle and cancellation.
- `src/resolver/unsupported.rs` — non-macOS resolver stubs.
- `src/r_bridge.rs` — shared private R-environment bridge used by graphics, Python, and SQL adapters.
- `src/r_graphics.c` — C-owned forwarding boundary for managed graphics-device callbacks that may long-jump.
- `src/r_graphics.rs` — cell-scoped managed R graphics device and PNG image publication.
- `src/server.rs` — MCP stdio server, `send` tool, and worker selection.
- `src/sql.rs` — persistent DuckDB/DBI SQL bridge and bounded streaming Arrow previews.
- `src/transcript.rs` — append-only MCP tool journal and image artifact persistence.
- `src/r_repl.c` — C-owned per-cell DLL-REPL iterator and long-jump boundary.
- `src/sideband.rs` — macOS inherited-pipe JSON-lines transport.
- `src/worker.rs` — embedded R initialization, cell dispatch, and console callbacks.
- `src/worker_client.rs` — server-side worker orchestration and lazy worker access.
- `src/worker_client/environment.rs` — requirement preparation and managed environment checkpoints.
- `src/worker_client/evaluation.rs` — per-cell evaluation, stdin, input-request, and wait state.
- `src/worker_client/lifecycle.rs` — worker generations, restart coordination, and process shutdown.
- `src/worker_client/output.rs` — response assembly and captured standard-stream buffering.
- `src/worker_client/macos.rs` — macOS worker launch, sideband exchange, fd-0 writing, and process control.
- `src/worker_client/unsupported.rs` — non-macOS worker-runtime stubs.
- `src/worker_protocol.rs` — shared sideband message definitions.
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
- `tests/transcripts/_run.py` — discovers transcript suites and compares case snapshots.
- `tests/transcripts/_support.py` — shared transcript types and MCP stdio client.
- `tests/transcripts/<suite>.py` — suites of named imperative transcript cases.
- `tests/transcripts/golden/SUITE/` — human-readable YAML 1.2 case transcripts.
- `tests/transcripts/README.md` — transcript test usage and authoring guide.
- `scripts/test` — builds the binary and runs selected external Python tests through `uv`.
- `scripts/format` — attempts each repository-wide formatter without requiring it.
- `scripts/check` — local formatting, Clippy, and test checks.
- `.github/workflows/ci.yaml` — formatting, Clippy, and test checks.
- `docs/TOOL_DESCRIPTIONS.md` — exact registered MCP tool and property descriptions.
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
- Format embedded R, Python, SQL, and shell test programs as multiline raw strings.
  Use escape sequences such as `\n` only when the program needs that character as data, not to lay out its source.
- Keep complete code cells separate from interactive `stdin`.
- Keep the MCP adapter independent of interpreter implementation details.
- Treat submitted R, Python, and SQL execution as shell-class capability and place safety at the worker-process boundary.
  Managed-Python startup and explicit R or Python preparation are host-bootstrap exceptions.
  R requirements are IR command arguments resolved with `IR_NO_LOCAL_SOURCES`; Python requirements use a JSON standard-input manifest.
  Neither is evaluated as R source, though accepted package installation, build code, managed Python startup, and the Matplotlib font-cache import may execute outside the worker sandbox.
- Update this file when a PR changes the implemented surface or repository map.
- Before every commit, run `scripts/format` and review its changes.
- Run `scripts/check` before opening a PR.
