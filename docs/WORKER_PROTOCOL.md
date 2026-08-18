# Worker protocol

This document describes the worker protocol implemented by `mcp-console serve`, the built-in worker, and `tests/fixtures/zod`.
It describes the current code, not the broader design under `design-sketches/`.
The message enums in `src/worker_protocol.rs`, the framing in `src/sideband.rs`, the language bridges in `src/python.rs`, `src/r_bridge.rs`, `src/r_environment.rs`, `src/r_graphics.rs`, and `src/sql.rs`, and the worker-client orchestration, platform runtime, evaluation state, and output assembly in `src/worker_client.rs` and `src/worker_client/` are the source of truth.

## Scope

The current implementation provides one worker for one server process.
It evaluates one complete R, Python, or SQL cell at a time and accepts exact `stdin` text whether the worker is evaluating or idle.
One generation-long server reader continuously consumes worker sideband frames; evaluations and live preparations register only their expected terminal messages.

The sideband protocol does not include interrupt frames, request IDs, general structured errors, sessions, capabilities, or protocol version negotiation.
MCP `session` interrupt is out of band: the server requests `SIGINT` for an active host resolver process group, or otherwise sends it to the live worker process.

Plain `serve` selects the built-in worker.
The hidden `serve --worker PATH` option replaces it with a development worker.

## Launch contract

For the built-in worker on macOS, server initialization first asks IR to resolve the retained default R requirements `tidyverse`, `github::rstudio/reticulate`, `DBI`, `duckdb`, `arrow`, and `nanoarrow` outside the sandbox.
The GitHub requirement supplies the fork-aware output-stream restoration required by the worker; the host R installation must also provide reticulate for the managed-Python resolver, which runs before the worker `R_LIBS` is applied.
It requires `ir` 0.4.0 or later and uses the same Rscript selection and `IR_NO_LOCAL_SOURCES` policy described below.
The returned library becomes the first worker `R_LIBS` entry for every generation.
Server initialization then uses that library's DuckDB package to install the `json` and `icu` extensions in DuckDB's native cache outside the sandbox.
These extensions form the built-in retained default set.
The resolver does not load their native code.
Extensions outside that set, including `fts`, remain explicit requirements.

When inherited `RETICULATE_PYTHON` is absent or exactly `managed`, server initialization also asks reticulate to resolve its baseline NumPy and pandas environment outside the sandbox.
The resolver is equivalent to this R call and receives the manifest as JSON on `Rscript` standard input:

```text
reticulate:::uv_get_or_create_env(
  packages = unique(c("numpy", "pandas", manifest$packages)),
  python_version = manifest$python_version,
  exclude_newer = manifest$exclude_newer
)
```

The server uses `$R_HOME/bin/Rscript` when `R_HOME` is set and otherwise selects `Rscript` from `PATH`.
It removes inherited `UV_OFFLINE`, allows reticulate and uv to use their normal global caches, and requires the command to return a valid interpreter path.
The server retains the result and normalized manifest and applies them to each server-managed worker.
Other inherited values, including an empty value, bypass the Python startup preflight unchanged.
They do not bypass default R or DuckDB extension resolution.
Custom workers skip the default R, Python, and DuckDB extension preflights.

`session` with `action = "prepare"` can add R or Python requirements or DuckDB extensions to the implicit session.
R and Python requirements remain exact strings.
DuckDB requirements are names that start with a lowercase ASCII letter and otherwise contain only lowercase ASCII letters, digits, and underscores; paths, URLs, repositories, versions, and SQL fragments are rejected.
Before built-in worker startup, the server merges exact strings with the retained tidyverse, GitHub reticulate, DBI, DuckDB, arrow, and nanoarrow requirements and managed Python baseline, merges DuckDB names with the retained `json` and `icu` extension set, then resolves the complete candidates outside the sandbox.
Custom workers skip the built-in package set, but every explicitly prepared R candidate includes DBI, DuckDB, and jsonlite so that the same library can service later DuckDB extension requests.
Before R resolution, the server requires `ir --version` from `PATH` to report 0.4.0 or later.
It then runs `ir run` with the same Rscript selection as the worker, one `--with` argument per requirement, and a constant expression that prints the resolved library path.
The server sets `IR_NO_LOCAL_SOURCES` for every invocation, so IR refuses package installation from direct or transitive local sources while retaining ownership of package-reference parsing.
Python requirements use the host resolver described above and take precedence over an inherited Python selection.
DuckDB requirements use the resolved managed R library and DuckDB's own `INSTALL` statement outside the sandbox.
DuckDB selects its default repository and native extension cache, whose layout separates versions and platforms; the resolver never loads the installed native code.
Every newly resolved R candidate repeats the complete retained extension installation with that candidate's DuckDB version.
DuckDB treats files already present in the matching version-and-platform cache as installed, so a warm repeat is a no-op.
When a live worker may have loaded DuckDB from an earlier resolved R library, new extensions are installed with every such library as well as the pending candidate.
A replacement worker resets this generation-specific target list to the retained R library.
The server commits all retained candidates together only after every requested resolution succeeds.
DuckDB cache writes are external side effects, so an earlier install from a failed multi-extension request may remain cached without entering the retained extension set.
It returns `[prepared]` without creating sideband pipes or starting the worker.

After startup, an idle worker that implements R preparation accepts a resolved R library through `prepare_r`, updates its live `.libPaths()`, and preserves in-memory state.
An idle server-managed worker accepts compatible Python additions through `prepare_python`.
DuckDB extensions can also be prepared while an existing worker is idle without replacing it or changing its in-memory state.
The extension installation itself is host-only and adds no DuckDB-specific worker request or receipt.
When the same preparation selects a new R library, including the first custom-worker DuckDB request, that library still uses the existing `prepare_r` exchange.
A successful Python activation is retained as soon as the worker reports it.
In a mixed request, that Python activation can therefore remain retained even if a later R update fails.
The R and DuckDB configurations are retained only after the complete operation succeeds.
After a live preparation failure may have partially changed the live worker, the server rejects new requirement additions until a successful explicit restart.
Evaluations remain available so the caller can save in-memory state.
Transport or protocol failures still stop a worker whose usability is unknown.
Custom workers skip the default R, Python, and DuckDB extension preflights but can prepare explicit R requirements and DuckDB extensions.
The server supplies the retained R library through `R_LIBS`, and a running custom worker must acknowledge `prepare_r` with `r_prepared`.
Prepared extensions use DuckDB's native default cache; the server does not resolve or inject that path.
Custom workers must use the same native cache to load them.
The hidden worker option replaces the executable, but R still starts from the user-selected installation and layers resolved libraries onto it.
A custom worker must apply its first resolved R library before loading DuckDB; a DuckDB namespace loaded earlier from inherited libraries is outside the extension-preparation contract.
Managed Python additions remain unavailable with a custom worker.
If preparation overlaps worker startup, the server returns `[requirements not prepared: worker is starting]` without resolving the additions or changing the retained requirements, R library, Python manifest, or DuckDB extension set.

`session` with `action = "restart"` may include additive R, Python, and DuckDB requirements or omit them to retain the current configuration.
The server merges additions into the complete retained sets and resolves every changed candidate outside the sandbox before terminating the current worker.
A new R candidate repeats installation of the complete retained DuckDB extension set with that candidate's DuckDB version.
The server commits the R library, DuckDB extension set, and Python environment together only after every required resolution succeeds.
A resolution failure leaves the current worker and retained environment unchanged.
Custom workers accept R and DuckDB additions but reject Python additions.
After successful resolution, the server terminates the current worker generation, eagerly starts its replacement, and returns `[idle]` after `ready`.
All worker-owned R, Python, SQL, debugger, and unread-stdin state is lost.
The implicit session exists for the server lifetime, so restart starts its first worker if none exists yet.
After any requirement resolution succeeds, restart starts the same one-second stdin-close, sideband-shutdown, and process-group escalation path described below.
It reopens the lifecycle for the new worker instead of ending the MCP server.

`session` with `action = "interrupt"` accepts no requirements and requires an active registered resolver or live worker; it does not start a process.
The server queues an interrupt for the active resolver operation when one exists, or otherwise sends `SIGINT` to the direct worker PID.
It returns `[interrupt sent]` after the resolver accepts the request or the worker signal succeeds, without waiting for a sideband reply or for the resolver or evaluation to finish.
The resolver owner sends `SIGINT` to its current process group, and a signal error is returned by both the interrupt and resolution calls.
The registered resolver operation and worker process handle keep the request on that resolver or worker rather than retrying it against a replacement.
An interrupted resolver reports its ordinary resolution failure.
A worker signal is not assigned to an evaluation and remains best-effort runtime input: code can catch or delay it.
When neither target exists, the call returns `worker is not running`.
Custom workers receive the worker process signal and are responsible for their own signal behavior.

These boundary details apply:

- Evaluations and idle stdin writes stay associated with the worker that admitted them.
  Work from the old worker is rejected rather than delivered to the replacement.
- An R preparation cancelled while its IR resolver is active reports resolver cancellation.
  After preparation reaches the live worker, restart cancellation returns `R preparation cancelled by restart` when the call includes R and `Python preparation cancelled by restart` otherwise.
  Worker shutdown precedes host-resolver cancellation so cancellation cannot release an unrelated command queued on the retiring worker.
  Sideband failures from the active generation remain infrastructure errors.
- Standard-output and standard-error bytes collected from the old worker are retained through retirement.
- When a `send` is waiting on an unfinished evaluation, that call owns the old worker's text and images.
  Restart releases it only after retirement with `[stopped by session restart request before evaluation finished]` and, when it retired a ready worker, `[worker stopped: in-memory state lost]`.
  The server finishes writing that reply before starting the replacement or returning the restart response.
  The restart response reports `[active evaluation stopped by session restart request]` and its own worker lifecycle facts without repeating that worker output.
- Idle callbacks do not create a waiting `send`.
  Without a waiting evaluation response, restart returns retained old-worker output itself.

The IR resolver receives R package references as process arguments.
The Python environment resolver receives only a requirement manifest on standard input, and the Python version resolver receives only version constraints; neither receives submitted cells or `send` stdin.
The DuckDB extension resolver receives validated extension names as data and runs DuckDB's own installer; it does not receive or inspect submitted SQL.
These resolvers may use the network and write their normal host caches outside the sandbox; R and Python package resolution may execute package installation or build code, and managed Python environment startup and the Matplotlib font-manager import also run there.
DuckDB extension preparation performs installation but not loading outside the sandbox.
`IR_NO_LOCAL_SOURCES` prevents IR from running package installation code for local sources; it may reuse a library that was already materialized.
Runtime requests also supply the worker's current `UV_*` settings except `UV_OFFLINE`; the server removes its own `UV_*` settings before applying that exact set to the resolver.
Those settings are inputs to that resolution only; the server does not retain or replay them.
Requirements and settings remain data rather than evaluated cell source; the IR invocation uses a constant R expression that does not contain requirement text.
Evaluated R code and R package load hooks can request resolution through `py_require()`, but the resolver does not evaluate their submitted source.
A default R or Python preflight failure prevents server initialization.
A preparation failure is an MCP tool error and leaves the prior configuration unchanged.
For a uv tool failure, `Rscript` captures reticulate's message stream and sends its selected Python version on stdout; uv's inherited stderr remains separate.
The server combines that selection with the complete candidate package set it submitted and renders them as a JSON resolver-input manifest before uv's stderr.
It discards reticulate's helper command, temporary output path, hints, and R call information.
Each R, Python, or DuckDB resolver leads a dedicated process group registered with the server lifecycle control before requirement input is written.
Before exec, each resolver child restores the default `SIGINT` disposition and unblocks the signal.
A session interrupt asks the resolver owner to send `SIGINT` to its current group and lets the resolution return its ordinary failure.
Restart, shutdown, and transport cancellation retain the bounded cancellation path, which force-stops the group.
The server waits for either lifecycle cancellation or a non-reaping notification that the direct resolver process exited.
Direct-process exit ends the resolver-group lifetime: the server force-stops any remaining in-group descendants, reaps the direct process, and then collects the resolver's standard streams.
Closing MCP input force-stops an active explicit or runtime resolver group and reaps its direct process; startup preflights finish before MCP input is accepted and do not participate in this cancellation path.

Outside an explicit restart, the worker starts lazily on the first `send` call that supplies `r`, `python`, `sql`, or nonempty `stdin`.
On macOS, the server's `WorkerRuntime` uses the same `SandboxedCommand` builder as the `sandbox` command.
For `--worker PATH`, `PATH` is one program name or path, with no arguments or shell parsing, producing a launch equivalent to:

```text
/usr/bin/sandbox-exec <policy> -- PATH
```

The built-in path launches `mcp-console worker`.
Inside the sandbox, the worker takes ownership of the sideband, discovers `R_HOME` through the selected R executable, and initializes R through `libr` and `harp`.
Harp opens `R_HOME/lib/libR.dylib` by its absolute path, so the worker does not self-execute or set a dynamic-loader environment variable.
The server prepends the validated default and explicitly prepared IR library to inherited `R_LIBS` before this initialization.
R then places that library first in `.libPaths()` while retaining its remaining user, site, and base libraries.

For server-managed Python, the host resolver warms Matplotlib's local installed-font index before returning a resolved environment.
Before replacing the inherited `MPLCONFIGDIR`, the worker resolves an existing `matplotlibrc` from `MATPLOTLIBRC`; otherwise it uses the inherited `MPLCONFIGDIR`, or `$HOME/.matplotlib` when `MPLCONFIGDIR` is unset or empty.
It exposes the resolved regular file through `MATPLOTLIBRC`; a `matplotlibrc` in the working directory at Matplotlib import time retains Matplotlib's normal higher precedence.
The sandbox permits reads of the resolved host file but not writes to it.
The user cache directory is the inherited nonempty `MPLCONFIGDIR`, or `$HOME/.matplotlib` when `MPLCONFIGDIR` is unset or empty.
After the existing managed-Python resolver selects an interpreter, it invokes that exact interpreter with isolated Python import settings and attempts to import `matplotlib.font_manager`; Matplotlib itself may reuse or create its versioned index in the user cache.
Starting that environment and importing its font manager run environment startup hooks and selected package code outside the worker sandbox, within the resolver process group, so cancellation and atomic prepare or restart behavior are unchanged.
The import is a best-effort cache warm: its exit status and output do not affect Python resolution.
Before Python initializes, the worker creates its private `$TMPDIR/matplotlib` directory, links regular versioned font indexes from the inherited user directory into it, and sets `MPLCONFIGDIR` to the private directory.
The sandbox permits reads through that link but denies writes to its host target; evaluated code can unlink or replace only its worker-private directory entry.
After runtime Python resolution, the waiting worker rescans the user cache for new indexes; later worker generations scan it during startup.
The server neither copies cache bytes nor grants the user cache directory as a writable sandbox path.
Matplotlib configuration, styles, TeX state, lock files, and the broader XDG cache remain worker-private apart from the selected read-only `matplotlibrc`.
Without a readable matching user index, Matplotlib discovers fonts in the worker-private directory normally.
Caller-selected non-managed Python environments skip resolver-owned prewarming but can reuse an existing matching user index; custom workers receive neither behavior.

The server launches the sandboxed worker with piped standard input, standard output, and standard error.
Sideband frames carry control and managed output; interactive input bytes travel through the worker's fd
0. After the `ready` handshake, one reader owns the sideband for the
worker generation while independent readers drain fd 1 and fd 2.
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
Console text is carried directly in a JSON string, with `console_output` and `console_diagnostic` kinds distinguishing ordinary and diagnostic text.
JSON escaping represents newlines, quotes, and other control characters on the wire.

Worker standard output and standard error are not protocol frames.
Each pipe reader queues raw byte chunks without decoding them.
The server appends those chunks, sideband console text, images, failures, and lifecycle notices to one pending output tape as it accepts them.
When a response drains the tape, the server decodes queued chunks for each pipe as UTF-8, retains an incomplete trailing sequence for a later response, and replaces invalid sequences.
Direct standard output, direct standard error, console output, and console diagnostics remain distinct until this projection step.
The current MCP projection renders both console channels as ordinary text and coalesces adjacent text exactly as it did before channels were retained.
It preserves order within each stream, but makes no relative ordering guarantee between standard output, standard error, and sideband output.
Descendants that inherit fd 1 or fd 2 write into the same pipes even when the interpreter is idle, until retirement closes that worker generation's capture boundary.

## Messages

The complete implemented message set is:

| Direction | Frame | Meaning |
| --- | --- | --- |
| server → worker | `{"kind":"evaluate","language":"r","source":"..."}` | Evaluate one complete source string in the selected language. |
| server → worker | `{"kind":"prepare_r","library":"..."}` | Replace the prior managed R library in the live search path. |
| server → worker | `{"kind":"prepare_python","packages":["py-yaml12"]}` | Add packages through reticulate in an idle server-managed worker. |
| server → worker | `{"kind":"python_resolved","python":"..."}` | Return the interpreter from one host resolution request. |
| server → worker | `{"kind":"python_resolution_failed","message":"..."}` | Return the failure from one host resolution request. |
| server → worker | `{"kind":"python_version_resolved","version":"3.12.11"}` | Return the version selected by one host version request. |
| server → worker | `{"kind":"python_version_resolution_failed","message":"..."}` | Return the failure from one host version request. |
| server → worker | `{"kind":"shutdown"}` | Exit without replying. |
| worker → server | `{"kind":"ready"}` | Startup is complete. |
| worker → server | `{"kind":"console_output","data":"..."}` | Append one ordinary console-text chunk. |
| worker → server | `{"kind":"console_diagnostic","data":"..."}` | Append one diagnostic console-text chunk. |
| worker → server | `{"kind":"image","data":"...","mime_type":"image/png"}` | Append one base64-encoded image. |
| worker → server | `{"kind":"input_requested","prompt":"..."}` | Report that the runtime requested input. |
| worker → server | `{"kind":"input_received"}` | Report that the current read succeeded. |
| worker → server | `{"kind":"r_prepared","library":"..."}` | Confirm the normalized live R library path. |
| worker → server | `{"kind":"r_preparation_failed","message":"..."}` | Report a live R update failure without discarding the worker. |
| worker → server | `{"kind":"resolve_python","request":{"requirements":{"packages":["numpy","pandas"]},"retained_requirements":{"packages":["numpy","pandas"]},"environment":{}}}` | Resolve the complete proposed reticulate manifest outside the sandbox. |
| worker → server | `{"kind":"resolve_python_version","request":{"constraints":[],"environment":{}}}` | Select a Python version with reticulate and uv outside the sandbox. |
| worker → server | `{"kind":"python_activated","requirements":{"packages":["numpy","pandas","py-yaml12"]}}` | Report that reticulate accepted this normalized managed-Python manifest. |
| worker → server | `{"kind":"python_prepared"}` | Finish explicit Python preparation. |
| worker → server | `{"kind":"python_preparation_failed","message":"..."}` | Report an ordinary explicit-preparation failure without discarding the worker. |
| worker → server | `{"kind":"completed"}` | Complete an evaluation. |

Every frame uses `kind` to select its message variant.
Unknown kinds and fields are rejected in either direction.
The server maps `console_output` and `console_diagnostic` to distinct internal console channels.
The `language` value is `r`, `python`, or `sql`.
Python manifests contain `packages` and may contain `python_version` and `exclude_newer`; empty optional fields are omitted.
For `resolve_python`, `requirements` is the physical manifest submitted to the host resolver and `retained_requirements` is the logical manifest that a successful activation will retain.
Their packages and `exclude_newer` must match; only `python_version` may differ when reticulate resolves a late addition against the exact active Python patch version while preserving the user's logical constraint.
The `environment` object may contain only `UV_*` settings other than `UV_OFFLINE`.
`python_activated.requirements` is the complete normalized logical manifest, not reticulate's request history.
The receipt is reserved for server-managed workers.
The built-in worker sends it after reticulate accepts a managed environment; the server immediately retains the matching resolved candidate or its unchanged current environment.
Receipt acceptance and the restart-generation check are atomic.
A receipt still pending when restart claims the generation is discarded with that worker.
Custom workers and caller-configured Python workers do not send `python_activated`; a custom worker that sends it fails the active operation with a managed-Python-activation protocol error.
`completed` and `python_prepared` carry no Python manifest.

## Handshake and explicit operations

The first worker message must be `ready`.
The server does not send an evaluation before receiving it.

One evaluation has this shape:

```text
worker -> server  {"kind":"ready"}

server -> worker  {"kind":"evaluate","language":"r","source":"echo"}
worker -> server  {"kind":"console_output","data":"zod: "}
worker -> server  {"kind":"console_output","data":"echo\n"}
worker -> server  {"kind":"completed"}
```

No sideband interrupt, poll, synchronization, or acknowledgment frame exists.
A `SIGINT` that reaches the process while it is idle remains pending until the next managed boundary; the entry check consumes it and the next cell proceeds.
A signal that arrives after a cell's final check is handled at the same later boundary.

The worker may send zero or more `console_output`, `console_diagnostic`, or `image` messages.
The server retains the console distinction, preserves frame arrival order as MCP content blocks, and concatenates adjacent text chunks without exposing the distinction in MCP content.
An image frame's `data` must be valid base64.
The recorder decodes it byte-for-byte into an artifact, while the MCP image retains the original string.
The frame's `mime_type` becomes the MCP image `mimeType` unchanged; only `image/png` receives a format-specific `.png` artifact suffix, and other MIME types use `.bin`.
`input_requested` appends one server-owned MCP request record and starts one provisional input state.
The matching `input_received` clears that state after the runtime read succeeds without removing the record.
Only one request may be outstanding: a second request, a receipt without a request, or completion before its receipt is a protocol failure.
`completed` ends the sideband evaluation.
It carries no managed-Python state.

After `ready`, the generation-long reader accepts console, image, input, Python-resolution, Python-version-resolution, Python-activation, and operation-terminal frames continuously.
Console output and images are published to the shared output tape immediately.
Nested Python requests are serviced even while no explicit operation is active, so an idle callback does not wait for a later client command.
Idle input state is retained until fd 0 satisfies the read or a later evaluation adopts it.
Evaluations and live preparations register only the terminal they expect.
A worker that is waiting for a nested resolver reply queues an unrelated command, finishes that callback after the reply arrives, and then processes the command.
Before applying a live preparation command, the built-in worker gives registered R handlers one nonblocking turn, so a ready callback is handled first.
An input request from that callback fails the noninteractive preparation instead of leaving it blocked.

An explicit live R preparation has this shape after the server resolves the complete R requirement set:

```text
server -> worker  {"kind":"prepare_r","library":"..."}
worker -> server  {"kind":"r_prepared","library":"..."}
# or
worker -> server  {"kind":"r_preparation_failed","message":"..."}
```

`prepare_r` is idle-only.
The built-in worker passes the path to a fixed private R bridge rather than evaluating submitted source.
The bridge tracks the current managed path, prepends the new library, removes its predecessor, and preserves every other live library path.
The resolved library contains the complete retained R requirement set, so the predecessor is not needed by later worker generations.
The server accepts only an acknowledgment for the requested normalized path and retains the candidate for future worker generations only after the complete public preparation succeeds.
`r_preparation_failed` leaves the worker evaluable but prevents new requirement additions until explicit restart because its live search path may have changed without a retained R configuration.
A Python environment already accepted by the same mixed operation remains retained.
An R bridge infrastructure error or protocol failure still stops the worker.

An explicit live Python preparation has this shape:

```text
server -> worker  {"kind":"prepare_python","packages":["py-yaml12"]}
worker -> server  {"kind":"resolve_python","request":{"requirements":{"packages":["numpy","pandas","py-yaml12"]},"retained_requirements":{"packages":["numpy","pandas","py-yaml12"]},"environment":{}}}
server -> worker  {"kind":"python_resolved","python":"..."}
# after live activation
worker -> server  {"kind":"python_activated","requirements":{"packages":["numpy","pandas","py-yaml12"]}}
worker -> server  {"kind":"python_prepared"}
```

`prepare_python` is idle-only and calls additive `reticulate::py_require()`.
Before initialization it materializes the manifest; afterward reticulate validates the live `libpython` and activates the candidate.
A matching `python_activated` immediately retains the resolved environment.
Before initialization, payload-free `python_prepared` accepts the last successfully materialized candidate.
For a mixed public preparation, an accepted Python environment remains retained if a later R update fails.
`python_preparation_failed` restores the live manifest, discards unmatched candidates, and leaves the worker usable.

A server-managed worker may send `resolve_python` during an evaluation, live preparation, or preceding idle callback when reticulate invokes its internal `uv_get_or_create_env` binding.
The request contains both the complete physical resolver manifest and the complete logical retained manifest, not a history delta.
Their packages and `exclude_newer` must match, while the physical manifest may select the exact active Python patch version without replacing the logical constraint that will be retained.
The server performs the resolution while the worker waits and replies with exactly one `python_resolved` or `python_resolution_failed` frame on the same sideband.
No request ID is needed because the worker can have only one such synchronous request in flight.
Every successful reply remains a candidate until a matching `python_activated` event arrives.
For a live interpreter, reticulate must pass its exact-`libpython` check, run the candidate's `activate_this.py`, swap its Python configuration, and update its manifest before the worker reports the activation.
The server immediately retains the matching candidate, or its unchanged current environment when that manifest matches.
The same Python interpreter, `__main__` namespace, and existing objects remain live through a successful activation.
A later language, infrastructure, or protocol failure does not roll back an already reported activation.
Unmatched candidates are discarded when the operation ends, except that successful explicit preparation accepts its last materialized candidate.

A pre-initialization `py_require()` call updates only the worker's lazy reticulate manifest.
It is not durable across worker loss until Python initialization resolves and reports the manifest through `python_activated`, or explicit preparation materializes it and returns `python_prepared`.

A server-managed worker may send `resolve_python_version` during an evaluation, live preparation, or preceding idle callback when reticulate invokes its internal `resolve_python_version` binding.
The request contains only version constraints and the current `UV_*` settings other than `UV_OFFLINE`.
The server runs reticulate's version selection while the worker waits and replies with exactly one `python_version_resolved` or `python_version_resolution_failed` frame.
This request returns no interpreter, creates no environment candidate, and does not affect retained Python state.
The selected version can support managed-Python operations such as displaying or writing the current requirements; an eventual tool command from `uv_run_tool()` still executes inside the worker sandbox.

When the reader accepts evaluation `completed`, it records the current output-tape position and returns that checkpoint to the evaluation.
Output accepted after the checkpoint belongs to later background activity and remains pending for the next response.
If no content or input-request record exists through the checkpoint and no standard-stream text is pending, the MCP projection returns `[done]`.
That marker is produced by the server; it is not a sideband message.

The protocol has no request IDs because only one evaluation or explicit requirement preparation can be in flight over this sideband, with at most one synchronous nested Python resolver request.
New code is rejected while an evaluation and its uncollected result are active.

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
A later `send` call without a code field polls that operation with its own `timeout_ms`; it may include `stdin` to queue bytes before waiting.
Every successful `send` response drains pending tape events available at its response boundary, including sideband text and images and complete UTF-8 prefixes from standard-stream bytes.
For evaluation completion, that boundary is the checkpoint recorded with `completed`; for an idle call, it is the immediate server-side snapshot.
Events accepted after the boundary and incomplete trailing byte sequences remain for the next response; new output does not itself wake a waiting call.
Evaluation completion returns the pending content in tape order, including input-request records not already delivered at an earlier boundary, or `[done]` when the tape is empty.
If the operation instead ends in an infrastructure or protocol failure, all pending operation output received before the failure precedes the bracketed tool error.
The server inserts a newline before that error only when the preceding output does not already end with one.
A worker failure adds `[worker stopped: in-memory state lost]` after that error once shutdown has finished and no standard-stream reader can append more output.
The same `send` then emits `[starting new worker]`, makes one automatic replacement attempt, and waits for it within the call's original deadline.
If the replacement reports `ready`, startup output and `[idle]` complete the response; the response remains an MCP tool error because the operation failed.
If the deadline expires first, the response ends with `[worker starting]`; a later poll waits on the same attempt and reports `[worker starting]` again if its own deadline expires.
If startup fails, that error ends the automatic attempt and the worker remains stopped.
Server-owned timeline, state, and admission facts are bracketed; request-validation and standalone resolver diagnostics remain ordinary MCP tool-error text.
If the poll wait expires first, the literal `\n[running]` banner is appended to any collected output.
With no evaluation active, an empty `send` immediately drains the server's pending output tape and returns `\n[idle]`, or `\n[stdin needed]` when the continuous sideband reader is tracking an idle console read.
It sends no worker frame, does not wait for a callback to finish, and ignores `timeout_ms` because no worker operation is being polled.
Output accepted after that snapshot remains pending for the next response.
An initial or stopped worker is not started by an empty call.
A stdin-only idle call queues the bytes first, lazily starting a worker when needed, and immediately returns the same server-side snapshot.

The server adds `[starting new worker]\n` before each announced replacement attempt, in the `send` or `session` response that waits for it.
The notice is recorded before launch, so startup output and startup errors follow it.
A failed replacement remains stopped, and each retry emits a new starting notice.
Initial lazy startup and its retries before any worker has reached `ready` remain silent because no established worker state was lost.
Without a waiting `send`, an explicit restart reports retained old-worker output, an active-operation notice when it interrupts an unfinished cell, the stopped notice when it retires a ready worker, the starting notice, replacement startup output, and `[idle]` in its `session` response.
If an unfinished operation has a waiting `send`, restart gives that response the old-worker tape content, its operation-specific restart-cancellation notice, and a worker-stopped notice when restart retired a ready worker.
The restart call waits for that response to be written, then reports its operation-specific active-stop notice, its own worker-stopped notice when it retired a ready worker, the starting notice, replacement startup output, and `[idle]`.

An ordinary `[running]`, `[stdin needed]`, or `[idle]` response drains all pending tape content before appending its state banner.
Each ordinary state banner has a newline before it, including when no worker or operation output precedes it.
An existing trailing newline supplies that boundary for `[stdin needed]`; `[running]` and `[idle]` always add one, so their preceding output may leave a blank line.
Replacement readiness appends `[idle]` with one line boundary after startup output.
Brackets distinguish server timeline facts and operational failures from worker text, and the server inserts a newline before them only when needed.
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
worker -> server  {"kind":"console_output","data":"[1] \"Ada\"\n"}
worker -> server  {"kind":"completed"}
```

When stdin is already queued, the receipt can arrive inside the grace window.
The intermediate MCP response and `[stdin needed]` marker are then suppressed, but the eventual response still contains the request record.
Each record ends in a newline when it is recorded.
That delimiter separates an immediately received record from later evaluation output and remains in a silent completion; if the request stays outstanding, `[stdin needed]` follows it in the same response.

An MCP call may contain one code field and `stdin`.
The server flushes `evaluate` first, then attaches the evaluation to the worker's stdin writer and drains any queued input in submission order.
A later stdin-only call uses the same route without acquiring the evaluation's worker lock, including after an earlier call returned `\n[running]`.
When no operation is tracked, nonempty stdin lazily starts the worker if necessary and enters the same worker-owned FIFO.
The call then returns the current server-side output snapshot immediately.
Because queuing bytes does not acknowledge consumption, an outstanding idle input request can remain visible as `[stdin needed]` until the reader accepts `input_received`.

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
New code is rejected while an evaluation and its uncollected result are active.

## State transitions

Between-cell callbacks do not create a separate server-side operation state.
The generation-long reader publishes their frames immediately.
An evaluation's `completed` frame records a tape checkpoint; callback frames accepted after that checkpoint remain pending for the next response.
After any operation terminal, the reader waits for the operation owner to commit its terminal state before reading another frame.
Later idle frames therefore cannot overtake environment retention or evaluation completion.

| From | Frame | To |
| --- | --- | --- |
| starting | worker → server `ready` | idle |
| starting, idle, evaluating, preparing R, or preparing Python | worker or descendant → fd 1 or fd 2 | unchanged; publish pending output |
| absent or idle | MCP stdin submission | idle |
| idle | server → worker `evaluate` | evaluating after any preceding callback finishes |
| idle | server → worker `prepare_r` | preparing R after any preceding callback finishes |
| idle | server → worker `prepare_python` | preparing Python after any preceding callback finishes |
| any phase with an active resolver or registered live worker | MCP `session` interrupt | unchanged; request `SIGINT` outside sideband |
| idle, evaluating, or preparing R or Python | worker → server `output` or `image` | unchanged; publish pending output |
| idle | worker → server `input_requested` | append request record; idle, input outstanding |
| idle, input outstanding | worker → server `input_received` | retain request record; idle |
| evaluating | worker → server `input_requested` | append request record; evaluating, input provisional |
| evaluating, input provisional | worker → server `input_received` | retain request record; evaluating |
| preparing R or Python | worker → server `input_requested` | append request record; fail preparation and stop worker |
| idle, evaluating, or preparing R or Python | worker → server `resolve_python` | host resolving; worker waiting |
| host resolving | server → worker `python_resolved` | prior operation; retain candidate |
| host resolving | server → worker `python_resolution_failed` | prior operation; no activation |
| idle, evaluating, or preparing R or Python | worker → server `python_activated` | retain matching environment; prior operation |
| idle, evaluating, or preparing R or Python | worker → server `resolve_python_version` | host selecting version; worker waiting |
| host selecting version | server → worker `python_version_resolved` | prior operation; no candidate created |
| host selecting version | server → worker `python_version_resolution_failed` | prior operation; retained Python state unchanged |
| idle or evaluating, with or without input reported | MCP stdin submission | prior operation |
| evaluating, no provisional input | worker → server `completed` | idle |
| preparing R | worker → server `r_prepared` | validate library path, then idle |
| preparing R | worker → server `r_preparation_failed` | block requirement changes; then idle |
| preparing Python | worker → server `python_prepared` | idle |
| preparing Python | worker → server `python_preparation_failed` | discard candidates, then idle |
| starting, idle, evaluating, preparing R or Python, host resolving, or host selecting version | server → worker `shutdown` | terminal |
| starting, idle, evaluating, preparing R or Python, host resolving, or host selecting version | MCP `session` restart | starting in a new generation |

Malformed JSON, invalid UTF-8, an unexpected message, or sideband EOF fails the active operation or records an idle worker failure.
`python_resolution_failed` and `python_version_resolution_failed` reply to valid resolver requests; they are not general protocol error messages.
There is no structured message for other protocol or infrastructure failures.
Initial startup failure leaves no cached worker, so a later evaluation retries startup silently.
After `ready`, a sideband failure retires the worker before its tool error reports `[worker stopped: in-memory state lost]`.
A nonempty `send` that fails during an active operation then makes one announced replacement attempt before its deadline; a later call starts a new announced attempt only if that replacement failed.
An empty `send` that discovers an idle failure stops the worker and reports the failure without starting a replacement; a later nonempty `send` or explicit restart starts the next worker.
Sideband content received before that failure is retained and precedes the tool error.
Worker retirement waits for the standard-stream readers, so all accepted standard-stream text precedes the tool error and stopped notice.
If either output path contributed text, the server starts the bracketed error on a new line.
R parse and evaluation errors, Python exceptions, and DuckDB errors are not sideband failures: the built-in worker sends them as output followed by `completed` and remains reusable.
Any earlier `python_activated` event has already updated retained state.

## Shutdown

The server begins shutdown when MCP input closes or RMCP releases its transport.
At that moment it fixes a deadline one second in the future and closes the client lifecycle.
It first attempts to send:

```json
{ "kind": "shutdown" }
```

The worker sends no acknowledgment; it exits.
The shutdown task queues worker-stdin closure, then attempts the sideband write.
It runs independently of the deadline so a blocked stdin writer or full sideband pipe cannot postpone forced termination.
The sandbox child waits only for the time remaining before the original deadline.
If its direct process is still running at the deadline, the sandbox force-stops its process group and reaps that direct process.
After the worker stops, shutdown force-stops any resolver process group that was active for explicit preparation or worker-triggered Python resolution and reaps its direct process.
After both stop paths complete, shutdown cancels the continuous sideband reader, its stdin writer, and its standard-stream readers.
Sideband cancellation also interrupts a wait for terminal-state commit.
Shutdown drains the finite standard-stream bytes already buffered at that boundary and joins the tasks.
This closes the old generation's server-side pipe boundary before shutdown returns, even when a background descendant retains a pipe descriptor or a blocked stdin write.
The descendant itself remains unsupervised as described below, and any later write to the closed pipe is not captured.

Shutdown owns stop handles independently of the active-operation lock, including simultaneous handles for the worker and its nested host resolver.
This lets the server terminate both processes while another thread is blocked waiting for resolver or worker output, while ensuring the worker stop path completes before resolver cancellation can release queued work.
A resolver that finishes independently while restart begins remains ordered against the shutdown write by ordinary sideband publication; restart adds no stronger preemption boundary before the worker observes shutdown.
If the worker cannot observe the shutdown frame while running a cell or R callback, the bounded kill is the completion path.

Shutdown closes a one-way gate that the client checks before and after acquiring the worker lock.
Startup registers a separate stop handle before waiting for `ready`.
If shutdown already closed the gate, startup stops the new child and fails immediately.

## Built-in worker

### R cells

The built-in worker runs each complete cell through `R_ReplDLLinit()` and repeated `R_ReplDLLdo1()` calls.
R parses and evaluates its expressions sequentially in the persistent global environment, captures console output, prints every visible value, and performs native top-level bookkeeping such as updating `.Last.value`.
After R initializes, each worker generation sets `options(width = 200L)` before reporting ready; evaluated code can change the option for the rest of that generation.
A cell that ends while R requires continuation input produces `Error: Incomplete code`; earlier complete expressions from that cell remain applied.
A successful silent R cell sends no console-text frame but still sends `completed`; if no other response text is pending, the server projects that completion as `[done]`.
The CLI runs `worker` synchronously without a Tokio runtime, so R initialization and evaluation remain on the process main thread.

Immediately before every R, Python, or SQL cell, the worker checks R's registered input handlers without blocking and runs one ready handler turn under `R_ToplevelExec()`.
It runs a second turn after a normal language outcome only if worker shutdown has not begun and the cell recorded no infrastructure failure.
Shutdown or an infrastructure failure during the initial turn aborts the submitted cell; an infrastructure failure recorded by the cell skips the final turn.
After either turn, the worker polls fd 0 once without blocking and treats `POLLHUP` as shutdown before it can dispatch or complete the cell.
This also covers callbacks that read fd 0 directly and therefore bypass `ReadConsole`.
Package callbacks therefore share the cell's console and input routing, while their default-device plots use a separate managed graphics scope.
Output and images from the final turn precede `completed`.
Between cells, the worker temporarily registers its sideband read descriptor as an R input handler with no callback and blocks in `R_checkActivity()`.
R activity wakes the same main thread for one managed handler turn; sideband activity returns control to the existing command dispatcher.
The temporary handler is removed before any R handler runs, including after an R long jump, so supported fork children never inherit it.
The minimum positive `R_wait_usec` or `Rg_wait_usec` value bounds the wait so `R_PolledEvents` can run; otherwise the worker blocks until a descriptor is ready.
This uses R's descriptor wait without a separate event loop or worker-owned fixed polling interval.

After an idle handler turn, the worker returns to the same wait without sending an activity-specific terminal frame.
The generation-long server reader publishes idle callback output and images as they arrive, services managed-Python requests, and retains idle input state.
An empty `send` only snapshots that server state; it sends no frame and does not wait for the callback to finish.
A later stdin-only `send` can continue an outstanding idle input request, and a later code-bearing `send` adopts that request into the evaluation's ordinary input state.
Requirement preparation is noninteractive, so an idle input request stops the worker instead of blocking indefinitely.

Before R initializes, the worker restores the default `SIGINT` disposition and unblocks the signal so R can install its handler.
The worker checks whether an interrupt is pending before and after every `R_ReplDLLdo1()` call and at cell boundaries, and calls `R_CheckUserInterrupt()` only when one is pending.
It temporarily suspends runtime interrupt handling during worker-owned graphics setup and cleanup, Python checkpointing, and worker-local preparation, then handles or discards any pending interrupt immediately afterward, so those internal calls do not turn a normal or late interrupt into an infrastructure failure.
Graphics and checkpoint boundaries report the normal runtime outcome; worker-local preparation silently discards the late signal so it does not introduce an unexpected console frame into the preparation exchange.
Checks that may jump remain inside the C REPL shim or `R_ToplevelExec`; their callback frames hold no Rust values that require destruction.
R, reticulate, and DuckDB perform their own checks during managed execution.
The Python and SQL bridges observe escaping R interrupt conditions without handling them; the shared top-level R boundary classifies them as ordinary language outcomes after they propagate, so they do not retire the worker.
Host resolution remains interruptible through its own process-group target while worker-side runtime interrupt handling stays suspended across preparation.

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
The worker maps `R_WriteConsoleEx` type 0 to `console_output` and every nonzero type to `console_diagnostic`.
It also maps `R_ShowMessage` and worker-generated language diagnostics to `console_diagnostic`.
An empty managed console read has no periodic interrupt boundary: the fd-0 read retries `EINTR` and continues waiting.
This applies to R `readline()` and to Python `input()` and debugger prompts routed through reticulate.
The signal remains pending until the read returns; explicit restart is the bounded way to stop that wait.
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
Before R or Python initializes, it sets `COLUMNS=200`; when NumPy or pandas loads, reticulate hooks set NumPy `linewidth` and pandas `display.width` to 200.
Evaluated code can change those Python settings after module load.
At worker startup, it sets `RETICULATE_REMAP_OUTPUT_STREAMS=1` once, before user R can initialize Python.
Within the worker process, reticulate then routes Python text writes through R's console callbacks, including when user R initializes Python before the first Python cell.
Python standard output uses R's ordinary console path and produces `console_output` frames.
Python standard error, including `sys.stderr.write()` and traceback printing, uses R's diagnostic console path and produces `console_diagnostic` frames in call order.
Writes through `sys.stdout.buffer`, `sys.stderr.buffer`, or native fd 1/2 bypass that remap and use the captured standard-stream pipes.
When a Python cell calls `os.fork()`, reticulate's registered CPython child callback replaces its inherited remappers with their original fd-backed streams after the worker disables the child's sideband.
Ordinary `print()` and `sys.stderr.write()` calls in that child therefore use the captured standard-stream pipes without sharing the parent-only sideband.
Native extensions that call `fork()` without running CPython's registered fork callbacks and then resume Python are unsupported.
This behavior requires reticulate from its `main` branch or a release containing fork-aware stream restoration.
An exec descendant that retains fd 1/2 creates fresh standard streams backed by those descriptors, so its ordinary stdout and stderr writes are captured.
There is no relative ordering guarantee between those pipes and sideband output, as described under [Transport](#transport).

The built-in worker receives either a server-managed requirement manifest selected by startup or explicit preparation, or the caller's existing `RETICULATE_PYTHON` value when no managed resolution occurred.
Before initializing R, it forces `UV_OFFLINE=1`, overwriting any inherited value before user code runs.
For a server-managed worker, MCP Console seeds reticulate's manifest and replaces the namespace bindings for its internal `uv_get_or_create_env` and `resolve_python_version` functions.
It does not replace `py_require()`, so reticulate retains its package attribution, manifest history, compatibility checks, activation, and configuration behavior within the live R process.
When Python is already initialized, only additive package requirements are supported.
The worker sends the complete physical resolver manifest, the logical manifest to retain after successful activation, and its current `UV_*` settings except `UV_OFFLINE`, then waits for the server's resolver reply before returning to reticulate.
The two manifests must agree on packages and `exclude_newer`; only the physical manifest may substitute the exact active Python patch version.
Those settings are not retained after the resolution.
Reticulate checks that each candidate uses the exact live `libpython`, runs `activate_this.py`, swaps its configuration, and updates its manifest.
The worker then sends `python_activated`, and the server immediately retains the matching candidate.
The interpreter is not restarted, so its `__main__` namespace and existing Python objects remain available.
The worker sends the complete normalized logical manifest through `python_activated`; it does not send reticulate's history.
The server immediately retains the matching candidate or its unchanged current environment.
An R package load hook may trigger this path while its namespace is loading.
Before Python initializes, that hook's lazy declaration remains worker-owned until initialization or explicit `prepare_python` materializes it.

Each Python cell receives a synthetic filename such as `<mcp-console:python:e1>`.
The worker stores the source in a process-lifetime private R environment and calls its evaluator with only a short evaluation ID.
The evaluator derives the synthetic filename from that ID, so neither the source nor the bridge implementation appears in its R call expression.
That evaluator parses the complete cell with Python's `ast` module, executes statements in `__main__.__dict__`, and displays a final expression through `sys.displayhook()`.
Assignments, imports, and objects remain available to later Python cells and through reticulate's R/Python object bridge.
An R plot invoked through reticulate's `r` bridge uses the managed R default device and follows its sizing, cell-scope, device-ownership, and finalization rules.
When `matplotlib.pyplot` loads, the worker replaces `show()` with a no-op so common notebook-style calls do not warn under the noninteractive backend or finalize figures before cell-end collection.
At Python cell end, including after a Python error, the worker visits every still-open pyplot figure in figure-number order, renders it in memory as `image/png`, and then closes all pyplot-managed figures.
Calling `savefig()` does not suppress this capture while the figure remains open; calling `close()` before cell end does.
Figures not registered with `pyplot` are not captured.

An uncaught Python exception prints its traceback and completes as a normal language outcome.
The worker remains reusable, and state changes made before the exception remain applied.
A successful Python cell without output or a final expression sends no console-text frame but still sends `completed`; if no other response text is pending, the server projects that completion as `[done]`.
Reticulate routes Python's built-in `input()` through R's console callback, and `breakpoint()`/`pdb` uses that built-in for each debugger prompt.
These reads produce request and receipt frames and accept proactively queued or follow-up stdin, including repeated debugger commands.
Direct `sys.stdin` or fd-0 reads bypass the callback and produce neither frame.

### SQL cells

The worker stores each SQL source string in a process-lifetime private R environment and calls its evaluator with a short evaluation ID.
The first SQL cell or call to `sql_connection()` lazily creates one in-memory DuckDB connection through `duckdb` and `DBI`; later operations reuse that connection and its catalog for the worker generation.
Environment scanning is enabled.
The driver leaves extension discovery to DuckDB while keeping stored-secret and spill directories beneath R's worker-private temporary directory.
DuckDB's native extension cache is readable but not writable from the sandbox, and the sandbox denies network access.
Explicit `LOAD` and DuckDB's default automatic-extension behavior run inside the sandbox.
SQL is passed directly to DuckDB without regex interception.
The bridge disables DuckDB progress output on the connection so previews contain only query results.

The private bridge sends each query through a zero-argument closure enclosed by R's global environment.
DuckDB therefore searches the persistent R session environment rather than the private environment that holds the bridge's `connection` and `source` state.
An unqualified catalog table or view takes precedence over an R binding with the same name.
When the catalog has no match, DuckDB can scan a data frame bound in the R global environment; an SQL view over that name observes a later rebinding when it is queried.
A prepared query retains the data frame it scanned until its DBI result is cleared.

The bridge installs `sql_connection()` and a forwarding active binding for reticulate's `py` in a worker-owned `tools:mcp-console` environment at search position 2.
Clearing R's global environment with `rm(list = ls())` does not remove either binding, while same-named global bindings still take precedence through normal R lookup.
The active binding resolves reticulate's persistent Python main module when it is read, so it does not force Python initialization during worker startup.
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
Pillar lays out that bounded text within 200 columns while retaining the 160-character per-cell limit, and its footer identifies selected columns that do not fit in the table body.
Empty results still show their selected names and types followed by `[0 rows]`.
`[additional rows omitted]`, `[N additional columns omitted]`, and `[cell values truncated to 160 characters]` report structural omissions.
The complete SQL preview, including its trailing newline, is limited to 12 KiB; if necessary, formatting removes candidate rows and then columns until it fits and updates the omission markers.

DDL and DML statements whose results have no columns produce no output and project to `[done]`.
This slice does not report affected-row counts.

The bridge catches DuckDB and DBI errors, prints an `Error: ` prefix followed by the condition message, and completes normally.
The worker and connection remain available to later cells.
SQL source containing NUL is rejected as a normal language error before it reaches the bridge.

## Current limits

No `timeout_ms` deadline terminates default R or managed-Python startup preflight, worker startup, host resolution, or execution.
R, Python, and DuckDB requirement resolution have no per-call timeout; MCP shutdown cancels an in-flight explicit or runtime resolution.
The current implementation has no general frame-size limit, stdin queue limit, or accumulated-output limit.
The 12 KiB cap applies only to a recognized SQL query preview; arbitrary R and Python console text, worker standard streams, and text accompanying that preview remain uncapped.
`timeout_ms` limits one MCP wait through evaluation and one automatic replacement attempt without terminating either operation.
If replacement startup outlives that wait, later polls continue waiting on it.
Server shutdown and explicit restart use a process deadline.
For an idle stdin-only call, `timeout_ms` does not bound lazy worker startup and does not delay the immediate output snapshot after startup.
The 10-millisecond input grace controls when provisional state becomes visible as `[stdin needed]`; it does not control request-record retention or limit evaluation or stdin reads.
It is a latency heuristic: scheduling can delay a receipt past the grace and expose an extra `[stdin needed]` boundary even when queued bytes subsequently satisfy the read.

Standard output and standard error are decoded as UTF-8 only when a response is assembled, with replacement for invalid sequences; arbitrary binary output is not preserved byte for byte.
Worker failures are reported as plain-text MCP tool errors, not structured worker events.
Concurrent MCP `send` calls are outside the current contract.
The default IR library supplies tidyverse, including dplyr, pillar, and tibble, plus the worker's GitHub reticulate build, DBI, DuckDB, arrow, nanoarrow, and their dependency sets, without attaching packages automatically.
Managed-Python preflight also requires an installed reticulate R package in the host R library.
Tidyverse supplies dbplyr for lazy dplyr relations created from `sql_connection()`.
MCP Console does not automatically install that host-bootstrap package.
The default preflights must be able to resolve or provision the R library, interpreter, and initial requirements outside the sandbox.
An explicitly configured interpreter must be initializable under the offline worker policy.
R requirements, the selected IR library, and Python requirements are retained only in server memory.
Server-managed workers can activate additive package requirements and report each accepted manifest after startup through evaluated `py_require()` calls or idle explicit preparation.
Runtime Python version changes, `exclude_newer` changes, and non-additive package changes after initialization are not supported by the layering path.
A session interrupt targets an active host resolver before the worker; the resolver owner sends `SIGINT` to its current process group, and the resolver reports its ordinary failure.
Evaluated code that replaces or blocks the `SIGINT` handler is outside the interrupt contract.
An interrupt sent during worker startup is best effort and can terminate the process before the runtime installs its handler.
Named sessions and environment provenance do not exist.
The Python input bridge does not observe direct `sys.stdin` or fd-0 reads.
The SQL adapter does not expose Python objects as relations or provide a separate registration API.
The current sandbox child does not yet supervise descendants after its direct process exits, or descendants that leave its process group; capturing inherited standard streams until worker retirement does not change that boundary.

## Zod fixture behavior

Zod implements the protocol as an executable uv script requiring Python 3.11 or newer.
As a custom worker, it sends no `python_activated` events.
It acknowledges `prepare_r` and can report whether the server supplied its live R library and prepared the JSON extension in DuckDB's native cache.
A dedicated mode sends a managed Python activation frame to verify that the server rejects it from a custom worker with a specific protocol error.
The `emit console kinds` mode sends adjacent `console_output` and `console_diagnostic` frames to verify that MCP still returns one merged text block.
When an R `source` is exactly `echo`, it sends two output chunks followed by `completed`:

```text
zod: echo\n
```

The Python and SQL `echo` modes return `zod python: echo\n` and `zod sql: echo\n`, verifying that the server preserves each language tag.
The `interrupt` mode waits after evaluation dispatch, handles a real process `SIGINT`, records its receipt, completes, and then accepts a later evaluation.
The `emit image` mode sends text, a valid one-pixel PNG image, and more text before completion, verifying ordered MCP content projection.
When an R `source` is exactly `stall`, Zod creates a checkpoint in its private temporary directory and sleeps forever.
When the source is `complete after timeout`, it pauses briefly before returning `zod: complete after timeout\n`.
When the source is `violate protocol`, it sends an unexpected second `ready` message.
When the source is `exit unexpectedly`, it exits with status 86 without replying.
The `emit stdout` and `start background stderr` modes exercise continuous standard-stream capture during evaluation and after completion.
The `stall with detached stdin` mode leaves fd 0 open in a session-detached child without reading it so shutdown coverage can fill the pipe and verify bounded writer cancellation.
When the source is `request input`, it sends `input_requested`, calls Python `input()` to consume one line from fd 0, and sends `input_received` after that call returns.
The `request input after timeout` mode gates that request until an earlier MCP wait expires, consumes prequeued stdin, emits output while the request remains provisional, then checkpoints after its receipt is processed to cover retention and delimiting of that still-unexposed request record.
The `input without request` and `input length without request` modes call `input()` without first sending a frame, covering proactive fd-0 delivery, including input queued while Zod is idle.
The `input without request then request input` mode performs one direct read before a reported request/receipt pair, covering the distinction between direct fd-0 reads and callback-style input state.
Zod emits fixture output containing the input or its byte length and completes; the server itself does not echo submitted stdin.
Its acceptance supplies newline-terminated text because Python `input()` waits for a complete line; partial-input boundaries are covered by the built-in worker's R console.
Other fixture-only modes verify that the sandbox denies host writes and that a blocked sideband writer cannot delay shutdown.
Other commands fail instead of being echoed implicitly.
Those behaviors are test fixtures, not part of the worker protocol.
