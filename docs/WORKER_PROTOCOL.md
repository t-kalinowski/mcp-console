# Worker protocol

This document describes the worker protocol implemented by `mcp-console serve`, the built-in worker, and `tests/fixtures/zod`.
It describes the current code, not the broader design under `design-sketches/`.
The message enums in `src/worker_protocol.rs`, the framing in `src/sideband.rs`, the language bridges in `src/python.rs`, `src/r_bridge.rs`, `src/r_graphics.rs`, and `src/sql.rs`, and the standard-stream routing in `src/worker_client.rs` are the source of truth.

## Scope

The current implementation provides one worker for one server process.
It evaluates one complete R, Python, or SQL cell at a time and accepts exact `stdin` text whether the worker is evaluating or idle.
Evaluations run sequentially.

The protocol does not yet include interrupts, request IDs, general structured errors, sessions, capabilities, or protocol version negotiation.

Plain `serve` selects the built-in worker.
The hidden `serve --worker PATH` option replaces it with a development worker.

## Launch contract

For the built-in worker, when inherited `RETICULATE_PYTHON` is absent or exactly `managed`, server initialization asks reticulate to resolve its baseline NumPy environment outside the sandbox.
The resolver is equivalent to this R call and receives the manifest as JSON on `Rscript` standard input:

```text
reticulate:::uv_get_or_create_env(
  packages = unique(c("numpy", manifest$packages)),
  python_version = manifest$python_version,
  exclude_newer = manifest$exclude_newer
)
```

The server uses `$R_HOME/bin/Rscript` when `R_HOME` is set and otherwise selects `Rscript` from `PATH`.
It removes inherited `UV_OFFLINE`, allows reticulate and uv to use their normal global caches, and requires the command to return a valid interpreter path.
The server retains the result and normalized manifest and applies them to each server-managed worker.
Other inherited values, including an empty value, bypass the startup preflight unchanged.

Before the worker starts, `session` with `action = "prepare"` can add Python requirements to the implicit session.
The server merges exact strings with the retained in-memory set, passes the complete candidate to the same host resolver, and commits the requirements and returned interpreter only after resolution succeeds.
This explicit preparation takes precedence over an inherited Python selection.
It returns `[prepared]` without creating sideband pipes or starting the worker.
New explicit `session` requirements after worker startup return `restart required` without changing the retained manifest or running resolver work; exact retained requirements remain idempotent.
This restriction does not apply to additive requirements declared through `reticulate::py_require()` inside a server-managed worker.
Custom workers reject preparation and skip managed-Python resolution.

`session` with `action = "restart"` accepts no requirements in the current implementation.
It retains the server's checkpointed Python environment, terminates the current worker generation, eagerly starts its replacement, and returns `[restarted]` after `ready`.
All worker-owned R, Python, SQL, debugger, and unread-stdin state is lost.
The implicit session exists for the server lifetime, so restart starts its first worker if none exists yet.
Evaluations and idle stdin writes capture their admitted generation; work from the old generation is rejected rather than delivered to the replacement.
Direct standard-stream bytes collected around the boundary remain in the shared output queue and may prefix a later `send` response from the replacement generation.
Restart uses the same one-second stdin-close, sideband-shutdown, and process-group escalation path described below, but it reopens the lifecycle gate for the new generation instead of ending the MCP server.

Each resolver process receives only a requirement manifest on standard input, not submitted cells or `send` stdin, and may use the network and write normal host caches.
Runtime requests also supply the worker's current `UV_*` settings except `UV_OFFLINE`; the server removes its own `UV_*` settings before applying that exact set to the resolver.
Those settings are inputs to that resolution only; the server does not retain or replay them.
Requirements and settings remain structured data rather than R expressions, but uv may execute source-distribution build backends in this unsandboxed resolver process.
Evaluated R code and R package load hooks can request resolution through `py_require()`, but the resolver does not evaluate their submitted source.
A preflight failure prevents server initialization.
A preparation failure is an MCP tool error and leaves the prior configuration unchanged.
For a uv tool failure, `Rscript` captures reticulate's message stream and sends its selected Python version on stdout; uv's inherited stderr remains separate.
The server combines that selection with the complete candidate package set it submitted and renders them as a JSON resolver-input manifest before uv's stderr.
It discards reticulate's helper command, temporary output path, hints, and R call information.
The resolver leads a dedicated process group registered with the server shutdown gate before requirement input is written.
Closing MCP input force-stops an active explicit or runtime resolver group and reaps `Rscript`; startup preflight finishes before MCP input is accepted and does not participate in this cancellation path.

Outside an explicit restart, the worker starts lazily on the first `send` call that supplies `r`, `python`, `sql`, or nonempty `stdin`.
On macOS, the server uses the same `SandboxedCommand` builder as the `sandbox` command.
For `--worker PATH`, `PATH` is one program name or path, with no arguments or shell parsing, producing a launch equivalent to:

```text
/usr/bin/sandbox-exec <policy> -- PATH
```

The built-in path launches `mcp-console worker`.
Inside the sandbox, the worker takes ownership of the sideband, discovers `R_HOME` through the selected R executable, and initializes R through `libr` and `harp`.
Harp opens `R_HOME/lib/libR.dylib` by its absolute path, so the worker does not self-execute or set a dynamic-loader environment variable.

The server launches the sandboxed worker with piped standard input, standard output, and standard error.
Sideband frames carry control and managed output; interactive input bytes travel through the worker's fd 0, while the server drains fd 1 and fd 2 continuously.
The sandbox child leads a dedicated process group so the current bounded shutdown can stop a live wrapper and its in-group descendants.

This launch contract currently works only on macOS because the sandbox is unsupported elsewhere.
The executable receives two inherited file descriptor numbers:

```yaml
environment:
  MCP_CONSOLE_SIDEBAND_READ_FD: <worker reads server messages here>
  MCP_CONSOLE_SIDEBAND_WRITE_FD: <worker writes messages to the server here>
```

The server clears `FD_CLOEXEC` on the child endpoints before spawning the worker.
It drops its duplicate child endpoints immediately after the spawn attempt.

The worker takes ownership of those descriptors.
Before it runs other programs or user code, it must remove the sideband environment variables and prevent descendants from inheriting the descriptors.
The built-in worker also closes the descriptors in fork-only descendants.
Zod uses `os.environ.pop()` and `os.set_inheritable(fd, False)`.

## Transport

The sideband consists of two anonymous pipes:

```text
server writer  ──>  worker reader
server reader  <──  worker writer
```

The server also owns an independent FIFO writer connected to the worker's standard input (fd 0) and independent readers for standard output and standard error.
Each accepted `stdin` string is UTF-8 encoded and queued to that writer without inspection or framing.
There is no sideband input frame.

Each frame is one UTF-8 JSON object followed by `\n`.
The sender flushes every frame.
Output text is carried directly in a JSON string.
JSON escaping represents newlines, quotes, and other control characters on the wire.

Worker standard output and standard error are not protocol frames.
Each pipe reader queues raw byte chunks without decoding them.
When a response is assembled, the server decodes the queued chunks for each pipe as UTF-8, retains an incomplete trailing sequence for a later response, and replaces invalid sequences.
It preserves order within each stream, but makes no relative ordering guarantee between standard output, standard error, and sideband output.
Descendants that inherit fd 1 or fd 2 write into the same pipes even when the interpreter is idle.

## Messages

The complete implemented message set is:

| Direction | Frame | Meaning |
| --- | --- | --- |
| server → worker | `{"kind":"evaluate","language":"r","source":"..."}` | Evaluate one complete source string in the selected language. |
| server → worker | `{"kind":"python_resolved","python":"..."}` | Return the interpreter from one host resolution request. |
| server → worker | `{"kind":"python_resolution_failed","message":"..."}` | Return the failure from one host resolution request. |
| server → worker | `{"kind":"shutdown"}` | Exit without replying. |
| worker → server | `{"kind":"ready"}` | Startup is complete. |
| worker → server | `{"kind":"output","data":"..."}` | Append one output text chunk. |
| worker → server | `{"kind":"image","data":"...","mime_type":"image/png"}` | Append one base64-encoded image. |
| worker → server | `{"kind":"input_requested","prompt":"..."}` | Report that the runtime requested input. |
| worker → server | `{"kind":"input_received"}` | Report that the current read succeeded. |
| worker → server | `{"kind":"resolve_python","request":{"requirements":{"packages":["numpy"]},"environment":{}}}` | Resolve the complete proposed reticulate manifest outside the sandbox. |
| worker → server | `{"kind":"completed","python_checkpoint":{"packages":["numpy","py-yaml12"]}}` | Complete the evaluation and report its normalized Python manifest. |
| worker → server | `{"kind":"completed"}` | Complete without a managed-Python checkpoint. |

Every frame uses `kind` to select its message variant.
Unknown fields are rejected in either direction.
The `language` value is `r`, `python`, or `sql`.
Python manifests contain `packages` and may contain `python_version` and `exclude_newer`; empty optional fields are omitted.
The `environment` object may contain only `UV_*` settings other than `UV_OFFLINE`.
The optional `python_checkpoint` is the complete normalized manifest, not reticulate's request history.
Custom workers, caller-configured Python workers, and server-managed workers that never load reticulate omit it.

## Handshake and evaluation

The first worker message must be `ready`.
The server does not send an evaluation before receiving it.

One evaluation has this shape:

```text
worker -> server  {"kind":"ready"}

server -> worker  {"kind":"evaluate","language":"r","source":"echo"}
worker -> server  {"kind":"output","data":"zod: "}
worker -> server  {"kind":"output","data":"echo\n"}
worker -> server  {"kind":"completed"}
```

The worker may send zero or more `output` or `image` messages.
The server preserves their arrival order as MCP content blocks and concatenates adjacent text chunks.
An image frame's `data` must already be base64 encoded, and `mime_type` becomes the MCP image `mimeType`; the server passes both strings through without decoding or validation.
`input_requested` appends one server-owned MCP request record and starts one provisional input state.
The matching `input_received` clears that state after the runtime read succeeds without removing the record.
Only one request may be outstanding: a second request, a receipt without a request, or completion before its receipt is a protocol failure.
`completed` ends the sideband evaluation.
The server must accept its optional Python checkpoint before the MCP evaluation completes and the next cell is permitted.

A server-managed worker may send `resolve_python` during an evaluation when reticulate invokes its internal `uv_get_or_create_env` binding.
The request contains the complete proposed manifest, not a history delta.
The server performs the resolution while the worker waits and replies with exactly one `python_resolved` or `python_resolution_failed` frame on the same sideband.
No request ID is needed because the worker can have only one such synchronous request in flight.
Every successful reply remains a candidate until the evaluation completes.
If managed reticulate is loaded but Python remains uninitialized at cell end, the worker invokes the replacement resolver to materialize the final manifest before sending `completed`.
For a live interpreter, reticulate must pass its exact-`libpython` check, run the candidate's `activate_this.py`, swap its Python configuration, and update its manifest.
The same Python interpreter, `__main__` namespace, and existing objects remain live through a successful activation.
On `completed`, the server accepts the last candidate whose normalized manifest matches `python_checkpoint`, or retains the prior environment when its manifest matches.
Any other checkpoint is a protocol failure, and unmatched candidates are discarded.
Normal R, Python, and SQL language outcomes reach this checkpoint because their side effects remain live in the worker.
An infrastructure or protocol failure before `completed` leaves the prior server checkpoint unchanged.

If no sideband content or input-request record remains pending at `completed` and no standard-stream text is pending, the current MCP projection returns `[done]`.
That marker is produced by the server; it is not a sideband message.

The protocol has no request IDs because only one evaluation and one nested Python resolution can be in flight over this sideband.
New code is rejected while an evaluation or its uncollected result is active.

## MCP waiting and polling

The optional MCP `timeout_ms` argument defaults to 60,000 milliseconds.
It bounds how long that `send` call waits for the worker; it is not sent over the sideband and does not bound or stop computation.
For a call with `r`, `python`, or `sql`, the evaluation wait includes lazy worker startup.

Every `input_requested` frame immediately adds `[input requested: <prompt>]` to pending MCP output, with the prompt encoded as a JSON string.
Its outstanding state remains provisional for 10 milliseconds.
If `input_received` arrives first, the server retains the request record and continues waiting for another request, completion, or the MCP deadline.
If the grace expires first, the call returns output collected so far, the request record, and the `\n[stdin needed]` banner before that deadline.
Supplying nonempty stdin for an outstanding request starts a fresh 10-millisecond grace window; the MCP deadline reports a still-outstanding request immediately, even inside that window.
A pending input request wins over the `\n[running]` banner at the deadline.
A later `send` call without a code field polls that evaluation with its own `timeout_ms`; it may include `stdin` to queue bytes before waiting.
Every `send` response decodes and drains complete UTF-8 prefixes from standard-stream bytes already collected when that response is assembled.
Bytes collected after that snapshot and incomplete trailing sequences remain for the next response; standard-stream output does not itself wake a waiting call.
Completion returns decoded standard-stream text followed by pending evaluation content, including sideband text, images, and input-request records not already delivered at a `[stdin needed]` boundary, or `[done]` when neither produced content.
If evaluation instead ends in an infrastructure or protocol failure, all pending evaluation output received before the failure precedes the tool error.
When runtime output shares that response, the server starts the bracketed error on a new line, inserting a newline only when the output does not already end with one.
A tool error returned without runtime output or a restart notice remains bare.
If the poll wait expires first and no restart notice is pending, the literal `\n[running]` banner is appended to any collected standard-stream text.
A call without a code field or `stdin` while no evaluation is active and no restart notice is pending appends the literal `\n[idle]` banner to collected standard-stream text.
A stdin-only call in that state queues the bytes and uses the same idle response projection.

After an infrastructure or protocol failure discards a ready worker, its successfully started replacement eagerly queues the literal `[worker restarted: in-memory state lost]\n` banner in pending MCP response output.
Whichever response is assembled next drains that banner exactly once.
This is server-owned MCP response text, not a sideband frame.
It follows collected standard-stream, evaluation, or error text and starts on a new line, without adding a blank line when that text already ends with a newline.
If a final `[stdin needed]`, `[running]`, or `[idle]` banner follows, the restart notice's trailing newline supplies its separator.
With no preceding or following text, the response is `\n[worker restarted: in-memory state lost]\n`.
When it is the only text from a completed evaluation, it replaces `[done]`.
Initial lazy startup and retries after a failure before `ready` remain silent because no established worker state was lost.

Except for outstanding-input boundaries, this slice does not expose partial sideband output while an evaluation is running.
Standard-stream text is attached to whichever response is sent next, including `[running]`, `[stdin needed]`, or `[idle]` responses.
Without a preceding restart notice, each state banner has a newline before it, including when no worker or evaluation output precedes it.
An existing trailing newline supplies that boundary for `[stdin needed]`; `[running]` and `[idle]` always add one, so their preceding output may leave a blank line.
When a tool error shares the response with runtime output or a restart notice, brackets distinguish it from worker text and the server inserts a newline before it only when needed.
Output cursors and general incremental polling remain unimplemented.

### Interactive input

The built-in worker sends `input_requested` when evaluated R code calls `readline()` or enters `browser()`, and when Python uses built-in `input()` or `breakpoint()`/`pdb` through reticulate's R console bridge.
For every frame, the server appends exactly one record such as `[input requested: "name> "]` to pending MCP text.
It encodes the prompt as a JSON string, preserving trailing spaces while escaping quotes, backslashes, newlines, and control characters.
If the request remains outstanding, the response ends with `\n[stdin needed]`.
A later `send` call supplies its `stdin` unchanged:

```text
server -> worker  {"kind":"evaluate","language":"r","source":"readline('name> ')"}
worker -> server  {"kind":"input_requested","prompt":"name> "}
server -> MCP     [input requested: "name> "]\n[stdin needed]

server -> fd 0    Ada\n
worker -> server  {"kind":"input_received"}
worker -> server  {"kind":"output","data":"[1] \"Ada\"\n"}
worker -> server  {"kind":"completed"}
```

When stdin is already queued, the receipt can arrive inside the grace window.
The intermediate MCP response and `[stdin needed]` marker are then suppressed, but the eventual response still contains the request record.
Each record ends in a newline when it is recorded.
That delimiter separates an immediately received record from later evaluation output and remains in a silent completion; if the request stays outstanding, `[stdin needed]` follows it in the same response.

An MCP call may contain one code field and `stdin`.
The server flushes `evaluate` first, then attaches the evaluation to the worker's stdin writer and drains any queued input in submission order.
A later stdin-only call uses the same route without acquiring the evaluation's worker lock, including after an earlier call returned `\n[running]`.
When no evaluation is tracked, nonempty stdin lazily starts the worker if necessary and enters the same worker-owned FIFO; empty stdin is a no-op.

The server writes each string blindly and does not echo it into MCP output.
It adds no newline, does not split or validate lines, and imposes no stdin size limit.
The end of a payload does not close fd 0 and is not an EOF marker.
A newline-free fragment remains pending until later stdin completes it or worker shutdown closes the stream.
The R console callback consumes only through one newline or its supplied buffer; it does not prefetch later lines from fd 0.
`input_requested` is an observation of worker state, not permission to write.
After a nonempty callback read, `input_received` closes that provisional request before the runtime resumes.
Each request frame produces one record, regardless of how many stdin payloads or polls occur while it remains outstanding.
It does not acknowledge a particular stdin submission, identify which bytes satisfied the read, or report bytes consumed by code that reads fd 0 directly.
If no receipt arrives during the grace window, the request remains exposed as `\n[stdin needed]`; a partial follow-up therefore returns only `\n[stdin needed]` again rather than repeating the request record or returning `\n[running]`.
Empty stdin writes no bytes and leaves an exposed request immediately reportable.
Python `sys.stdin` and other code that reads fd 0 directly can consume bundled input or input sent after a polling timeout without sending either input frame.

Acceptance means the bytes were queued, not that an evaluation consumed them.
The server does not retract or drain bytes after `completed`; data already in the pipe or retained by a runtime reader may satisfy an idle background consumer, later reads, or later evaluations.
Worker shutdown or failure discards whatever remains.
New code is rejected while an evaluation or its uncollected result is active.

## State transitions

| From | Frame | To |
| --- | --- | --- |
| starting | worker → server `ready` | idle |
| starting, idle, or evaluating | worker or descendant → fd 1 or fd 2 | unchanged |
| absent or idle | MCP stdin submission | idle |
| idle | server → worker `evaluate` | evaluating |
| evaluating | worker → server `output` | evaluating |
| evaluating | worker → server `image` | evaluating |
| evaluating | worker → server `input_requested` | append request record; evaluating, input provisional |
| evaluating, input provisional | worker → server `input_received` | retain request record; evaluating |
| evaluating | worker → server `resolve_python` | host resolving; worker waiting |
| host resolving | server → worker `python_resolved` | evaluating; retain candidate |
| host resolving | server → worker `python_resolution_failed` | evaluating; prior checkpoint unchanged |
| evaluating, with or without input reported | MCP stdin submission | evaluating |
| evaluating, no provisional input | worker → server `completed` | validate checkpoint, then idle |
| starting, idle, or evaluating | server → worker `shutdown` | terminal |
| starting, idle, or evaluating | MCP `session` restart | starting in a new generation |

Malformed JSON, invalid UTF-8, an unexpected message, or sideband EOF fails the active operation.
`python_resolution_failed` is a reply to a valid resolution request, not a general protocol error message.
There is no structured message for other protocol or infrastructure failures.
Startup failure leaves no cached worker, so a later evaluation retries startup without a replacement notice.
After `ready`, a sideband failure force-stops and discards the worker; a later evaluation or nonempty idle stdin submission starts a fresh worker and queues the replacement notice described above for the next response.
Sideband content received before that failure is retained and precedes the tool error.
Standard-stream text collected before an infrastructure failure is attached to its tool error when available at the response boundary; text collected later remains for the next `send` response.
If either output path contributed text, the server starts the bracketed error on a new line.
R parse and evaluation errors, Python exceptions, and DuckDB errors are not sideband failures: the built-in worker sends them as output followed by `completed`, checkpoints any resulting manifest, and remains reusable.

## Shutdown

The server begins shutdown when MCP input closes or RMCP releases its transport.
At that moment it fixes a deadline one second in the future and closes the client's shutdown gate.
If explicit preparation or a worker-triggered Python resolution is active, shutdown force-stops the resolver process group and reaps its direct `Rscript` process.
It then attempts to send:

```json
{ "kind": "shutdown" }
```

The worker sends no acknowledgment; it exits.
The shutdown task queues worker-stdin closure, then attempts the sideband write.
It runs independently of the deadline so a blocked stdin writer or full sideband pipe cannot postpone forced termination.
The sandbox child waits only for the time remaining before the original deadline.
If its direct process is still running at the deadline, the sandbox force-stops its process group and reaps that direct process.
Shutdown does not wait for the standard-stream readers to reach EOF because descendants may retain those descriptors.

Shutdown owns stop handles independently of the evaluation lock, including simultaneous handles for the worker and its nested host resolver.
This lets the server terminate both processes while another thread is blocked waiting for resolver or worker output.
If the worker cannot observe the shutdown frame while evaluating, the bounded kill is the completion path.

Shutdown closes a one-way gate that the client checks before and after acquiring the worker lock.
Startup registers a separate stop handle before waiting for `ready`.
If shutdown already closed the gate, startup stops the new child and fails immediately.

## Built-in worker

### R cells

The built-in worker runs each complete cell through `R_ReplDLLinit()` and repeated `R_ReplDLLdo1()` calls.
R parses and evaluates its expressions sequentially in the persistent global environment, captures console output, prints every visible value, and performs native top-level bookkeeping such as updating `.Last.value`.
A cell that ends while R requires continuation input produces `Error: Incomplete code`; earlier complete expressions from that cell remain applied.
A successful silent R cell sends no `output` frame but still sends `completed`; if no other response text is pending, the server projects that completion as `[done]`.
The CLI runs `worker` synchronously without a Tokio runtime, so R initialization and evaluation remain on the process main thread.

The worker supplies cell source through `ReadConsole` before each top-level evaluation starts.
For every evaluation-time `ReadConsole` call, the callback sends `input_requested`, then reads fd 0 directly until one newline arrives or R's supplied buffer is full.
The built-in worker sends R's prompt field verbatim, including trailing spaces or an empty prompt.
The server preserves that value but JSON-quotes it in the MCP input-request record instead of appending it as bare prompt text.
After a nonempty read succeeds, it sends `input_received` before returning the bytes to R.
A newline-free fragment shorter than that buffer keeps the callback blocked, while bytes after a returned chunk remain in the pipe for a later `ReadConsole` call or a direct fd-0 reader.
It uses R's busy callback rather than prompt text to distinguish cell source from evaluated-code input.
Unread fd-0 input remains available across evaluation boundaries.
Submitted source references are not retained.
Parse, evaluation, and print errors are returned as console text followed by `completed`, so the worker remains available even though the protocol has no structured language-error message.
Subprocesses and descendants that write directly to retained fd 1 or fd 2 bypass the R console callbacks, but their output is still collected through the standard-stream pipes.

At startup, the worker installs a managed function as R's default graphics device.
It opens a direct `grDevices::png()` device lazily only when evaluated code requests the default device; a cell that does not plot performs no managed plot file operations.
The device writes numbered PNG pages beneath the worker's private temporary directory.
The worker wraps each managed device's new-page and close callbacks.
After the original callback returns normally, the worker reads, base64-encodes, removes, and emits the PNG that the callback finalized.
R console output is emitted immediately, so text produced while a page is still open can precede that page's image.
At cell end, including after a normal R language error, the worker closes every still-open managed device, whose close callback emits its remaining page, and then sends `completed`.
The server projects those frames as `image/png` MCP content before completion.

Only worker-owned default devices are cell scoped.
The worker closes them after every cell, so later calls cannot add layers to an earlier managed plot; one plot and all operations that modify it must be submitted in the same cell.
The default dimensions are 800 by 600 pixels at 96 DPI.
The persistent R options `console.plot.width`, `console.plot.height`, and `console.plot.dpi` select positive finite width and height values in inches and the resolution.
Graphics devices opened explicitly by evaluated code, such as with `grDevices::png()`, remain user-owned: the worker does not close them, read their files, or emit images for them.

### Python cells

The worker embeds one persistent Python `__main__` interpreter through reticulate.
At worker startup, it sets `RETICULATE_REMAP_OUTPUT_STREAMS=1` once, before user R can initialize Python.
Within the worker process, reticulate then routes Python text writes through R's console callbacks, including when user R initializes Python before the first Python cell.
Calls such as `print()`, `sys.stderr.write()`, and traceback printing therefore produce sideband `output` frames in call order.
Writes through `sys.stdout.buffer`, `sys.stderr.buffer`, or native fd 1/2 bypass that remap and use the captured standard-stream pipes.
When a Python cell calls `os.fork()`, reticulate's registered CPython child callback replaces its inherited remappers with their original fd-backed streams after the worker disables the child's sideband.
Ordinary `print()` and `sys.stderr.write()` calls in that child therefore use the captured standard-stream pipes without sharing the parent-only sideband.
Native extensions that call `fork()` without running CPython's registered fork callbacks and then resume Python are unsupported.
This behavior requires reticulate from its `main` branch or a release containing fork-aware stream restoration.
An exec descendant that retains fd 1/2 creates fresh standard streams backed by those descriptors, so its ordinary stdout and stderr writes are captured.
There is no relative ordering guarantee between those pipes and sideband output, as described under [Transport](#transport).

The built-in worker receives either a server-managed requirement manifest selected by startup or explicit preparation, or the caller's existing `RETICULATE_PYTHON` value when no managed resolution occurred.
Before initializing R, it forces `UV_OFFLINE=1`, overwriting any inherited value before user code runs.
For a server-managed worker, MCP Console seeds reticulate's manifest and replaces only the namespace binding for its internal `uv_get_or_create_env` function.
It does not replace `py_require()`, so reticulate retains its package attribution, manifest history, compatibility checks, activation, and configuration behavior within the live R process.
When Python is already initialized, only additive package requirements are supported.
The worker sends the complete proposed manifest and its current `UV_*` settings except `UV_OFFLINE`, then waits for the server's resolver reply within the same evaluation.
Those settings are not retained after the resolution.
Reticulate checks that each candidate uses the exact live `libpython`, runs `activate_this.py`, swaps its configuration, and updates its manifest.
The interpreter is not restarted, so its `__main__` namespace and existing Python objects remain available.
If reticulate is loaded but Python remains uninitialized at cell end, the worker calls the replacement resolver to materialize the final manifest.
The worker then sends that normalized manifest as `completed.python_checkpoint`; it does not send reticulate's history.
The server accepts the last candidate from the evaluation with that manifest, or its prior environment if the manifest did not change.
An R package load hook may trigger this path while its namespace is loading.
An explicit `session prepare` addition after worker startup still returns `restart required`; it does not use the runtime layering path.

Each Python cell receives a synthetic filename such as `<mcp-console:python:e1>`.
The worker stores the source in a process-lifetime private R environment and calls its evaluator with only a short evaluation ID.
The evaluator derives the synthetic filename from that ID, so neither the source nor the bridge implementation appears in its R call expression.
That evaluator parses the complete cell with Python's `ast` module, executes statements in `__main__.__dict__`, and sends a final expression through `sys.displayhook()`.
Assignments, imports, and objects remain available to later Python cells and through reticulate's R/Python object bridge.
Python cells enter the same managed graphics lifecycle as R cells.
An R plot invoked through reticulate's `r` bridge therefore uses the managed default device, returns as MCP image content, and follows the same sizing, cell-scope, device-ownership, and finalization rules.

An uncaught Python exception prints its traceback and completes as a normal language outcome.
The worker remains reusable, and state changes made before the exception remain applied.
A successful Python cell without output or a final expression sends no `output` frame but still sends `completed`; if no other response text is pending, the server projects that completion as `[done]`.
Reticulate routes Python's built-in `input()` through R's console callback, and `breakpoint()`/`pdb` uses that built-in for each debugger prompt.
These reads produce request and receipt frames and accept proactively queued or follow-up stdin, including repeated debugger commands.
Direct `sys.stdin` or fd-0 reads bypass the callback and produce neither frame.

### SQL cells

The worker stores each SQL source string in a process-lifetime private R environment and calls its evaluator with a short evaluation ID.
The first SQL cell or call to `sql_connection()` lazily creates one in-memory DuckDB connection through `duckdb` and `DBI`; later operations reuse that connection and its catalog for the worker generation.
Environment scanning is enabled.
The driver receives explicit extension, stored-secret, and spill directories beneath R's worker-private temporary directory, so it does not select or prompt for ambient DuckDB storage.
The bridge disables DuckDB progress output on the connection so previews contain only query results.

The private bridge sends each query through a zero-argument closure enclosed by R's global environment.
DuckDB therefore searches the persistent R session environment rather than the private environment that holds the bridge's `connection` and `source` state.
An unqualified catalog table or view takes precedence over an R binding with the same name.
When the catalog has no match, DuckDB can scan a data frame bound in the R global environment; an SQL view over that name observes a later rebinding when it is queried.
A prepared query retains the data frame it scanned until its DBI result is cleared.

The bridge installs only `sql_connection()` in a worker-owned `tools:mcp-console` environment at search position 2.
Clearing R's global environment with `rm(list = ls())` does not remove the helper, while a same-named global binding still takes precedence through normal R lookup.
It returns a borrowed reference to the same worker-owned DBI connection, allowing established DuckDB, DBI, and dplyr interfaces to use the persistent catalog.
Callers must not disconnect it, and objects that use it remain tied to the current worker generation.
Existing functions such as `duckdb::duckdb_register()` and `duckdb::duckdb_register_arrow()` can register relations on it; the worker adds no separate registration API.
A dplyr relation created with `dplyr::tbl(sql_connection(), name)` remains lazy and observes later catalog changes until collection.
Neither direction promises end-to-end zero-copy transfer: DuckDB converts R values during query execution, and collecting a lazy relation materializes its result in R.

The evaluator calls `DBI::dbSendQueryArrow()` and renders only DuckDB results whose private return type is `QUERY_RESULT`.
It transfers each query result to `DBI::dbFetchArrow()` with a chunk size of 21, reads its schema and at most one batch, releases the nanoarrow stream, and clears the DBI result before formatting.
The first 20 rows become the candidate preview; row 21 only determines whether more rows exist.
The evaluator never counts the complete result for display.

The preview selects at most 12 columns and uses the Arrow schema for the original column names and visible physical types.
For nonempty results, the nanoarrow batch crosses into Arrow through the C Data Interface without copying its payload, then a 20-row by 12-column view becomes a private temporary DuckDB Arrow relation whose name is checked against catalog objects and existing Arrow registrations before registration.
DuckDB casts only the selected 20-row by 12-column preview to text and applies the 160-character limit before returning those strings to R, preserving SQL `NULL` and exact values including `BIGINT`, `DECIMAL`, lists, and structs when they fit without first converting them to lossy R data-frame columns.
Pillar lays out that bounded text with an explicit 160-character cell limit and fixed print options, and its footer identifies selected columns that do not fit in the table body.
Empty results still show their selected names and types followed by `[0 rows]`.
`[additional rows omitted]`, `[N additional columns omitted]`, and `[cell values truncated to 160 characters]` report structural omissions.
The complete SQL preview, including its trailing newline, is limited to 12 KiB; if necessary, formatting removes candidate rows and then columns until it fits and updates the omission markers.

DDL and DML statements whose results have no columns produce no output and project to `[done]`.
This slice does not report affected-row counts.

The bridge catches DuckDB and DBI errors, prints an `Error: ` prefix followed by the condition message, and completes normally.
The worker and connection remain available to later cells.
SQL source containing NUL is rejected as a normal language error before it reaches the bridge.

## Current limits

No timeout bounds managed-Python startup preflight, worker startup, host resolution, or execution.
Python requirement resolution has no per-call timeout; MCP shutdown cancels an in-flight explicit or runtime resolution.
The current implementation has no general frame-size limit, stdin queue limit, or accumulated-output limit.
The 12 KiB cap applies only to a recognized SQL query preview; arbitrary R and Python console text, worker standard streams, and text accompanying that preview remain uncapped.
`timeout_ms` limits one MCP wait without terminating the worker or a blocked stdin write; server shutdown and explicit restart use a process deadline.
An idle stdin-only call does not wait on an evaluation, so `timeout_ms` does not bound lazy worker startup for that call.
The 10-millisecond input grace controls when provisional state becomes visible as `[stdin needed]`; it does not control request-record retention or limit evaluation or stdin reads.
It is a latency heuristic: scheduling can delay a receipt past the grace and expose an extra `[stdin needed]` boundary even when queued bytes subsequently satisfy the read.

Standard output and standard error are decoded as UTF-8 only when a response is assembled, with replacement for invalid sequences; arbitrary binary output is not preserved byte for byte.
Worker failures are reported as plain-text MCP tool errors, not structured worker events.
Concurrent MCP `send` calls are outside the current contract.
Python cells require an installed reticulate R package.
SQL cells require installed arrow, DBI, duckdb, nanoarrow, pillar, and tibble R packages.
Lazy dplyr relations created from `sql_connection()` additionally require dplyr and dbplyr.
MCP Console does not install these packages.
The default preflight must be able to resolve or provision its interpreter and initial requirements outside the sandbox.
An explicitly configured interpreter must be initializable under the offline worker policy.
Python requirements are retained only in server memory.
Server-managed workers can activate additive package requirements and checkpoint their final manifest after startup, but an explicit late `session prepare` still requires restart.
Runtime Python version changes, `exclude_newer` changes, and non-additive package changes after initialization are not supported by the layering path.
Named sessions, R requirements, restart with new requirements, and environment provenance do not exist.
The Python input bridge does not observe direct `sys.stdin` or fd-0 reads.
The SQL adapter does not expose Python objects as relations or provide a separate registration API.
The current sandbox child does not yet supervise descendants after its direct process exits, or descendants that leave its process group; capturing inherited standard streams does not change that boundary.

## Zod fixture behavior

Zod implements the protocol as an executable uv script requiring Python 3.11 or newer.
As a custom worker, it omits `python_checkpoint` from every `completed` frame.
When an R `source` is exactly `echo`, it sends two output chunks followed by `completed`:

```text
zod: echo\n
```

The Python and SQL `echo` modes return `zod python: echo\n` and `zod sql: echo\n`, verifying that the server preserves each language tag.
The `emit image` mode sends text, a valid one-pixel PNG image, and more text before completion, verifying ordered MCP content projection.
When an R `source` is exactly `stall`, Zod creates a checkpoint in its private temporary directory and sleeps forever.
When the source is `complete after timeout`, it pauses briefly before returning `zod: complete after timeout\n`.
When the source is `violate protocol`, it sends an unexpected second `ready` message.
When the source is `exit unexpectedly`, it exits with status 86 without replying.
The `emit stdout` and `start background stderr` modes exercise continuous standard-stream capture during evaluation and after completion.
When the source is `request input`, it sends `input_requested`, calls Python `input()` to consume one line from fd 0, and sends `input_received` after that call returns.
The `request input after timeout` mode gates that request until an earlier MCP wait expires, consumes prequeued stdin, emits output while the request remains provisional, then checkpoints after its receipt is processed to cover retention and delimiting of that still-unexposed request record.
The `input without request` and `input length without request` modes call `input()` without first sending a frame, covering proactive fd-0 delivery, including input queued while Zod is idle.
The `input without request then request input` mode performs one direct read before a reported request/receipt pair, covering the distinction between direct fd-0 reads and callback-style input state.
Zod emits fixture output containing the input or its byte length and completes; the server itself does not echo submitted stdin.
Its acceptance supplies newline-terminated text because Python `input()` waits for a complete line; partial-input boundaries are covered by the built-in worker's R console.
Other fixture-only modes verify that the sandbox denies host writes and that a blocked sideband writer cannot delay shutdown.
Other commands fail instead of being echoed implicitly.
Those behaviors are test fixtures, not part of the worker protocol.
