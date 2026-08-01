# Worker protocol

This document describes the worker protocol implemented by `mcp-console serve`, the built-in R worker, and `tests/fixtures/zod`.
It describes the current code, not the broader design under `design-sketches/`.
The message enums in `src/worker_protocol.rs` and the framing in `src/sideband.rs` are the source of truth.

## Scope

The current implementation provides one worker for one server process.
It evaluates one `r` cell at a time and accepts exact `stdin` text only while that evaluation is waiting in `ReadConsole`.

The protocol does not yet include interrupts, request IDs, sessions, capabilities, or protocol version negotiation.

Plain `serve` selects the built-in R worker.
The hidden `serve --worker PATH` option replaces it with a development worker.

## Launch contract

The worker starts lazily on the first `send` call.
On macOS, the server uses the same `SandboxedCommand` builder as the `sandbox` command.
For `--worker PATH`, `PATH` is one program name or path, with no arguments or shell parsing, producing a launch equivalent to:

```text
/usr/bin/sandbox-exec <policy> -- PATH
```

The built-in path launches `mcp-console worker`.
Inside the sandbox, the worker takes ownership of the sideband, discovers `R_HOME` through the selected R executable, and initializes R through `libr` and `harp`.
Harp opens `R_HOME/lib/libR.dylib` by its absolute path, so the worker does not self-execute or set a dynamic-loader environment variable.

The server launches the sandboxed worker with null standard input, output, and error streams.
The sideband pipes are its only communication channel.
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
The R worker also closes the descriptors in fork-only descendants.
Zod uses `os.environ.pop()` and `os.set_inheritable(fd, False)`.

## Transport

The sideband consists of two anonymous pipes:

```text
server writer  ──>  worker reader
server reader  <──  worker writer
```

Each frame is one UTF-8 JSON object followed by `\n`.
The sender flushes every frame.
Output text is carried directly in a JSON string.
JSON escaping represents newlines, quotes, and other control characters on the wire.

Worker standard output and standard error are not part of the protocol and are currently discarded.

## Messages

The complete implemented message set is:

| Direction | Frame | Meaning |
| --- | --- | --- |
| server → worker | `{"kind":"evaluate","r":"..."}` | Evaluate the `r` string. |
| server → worker | `{"kind":"input","stdin":"..."}` | Supply exact text to the active input request. |
| server → worker | `{"kind":"shutdown"}` | Exit without replying. |
| worker → server | `{"kind":"ready"}` | Startup is complete. |
| worker → server | `{"kind":"output","data":"..."}` | Append one output text chunk. |
| worker → server | `{"kind":"input_requested","prompt":"..."}` | Suspend at an R input request. |
| worker → server | `{"kind":"input_pending","prompt":"..."}` | The supplied text contains no complete line yet. |
| worker → server | `{"kind":"completed"}` | The evaluation is complete. |
| worker → server | `{"kind":"language_error","message":"..."}` | Complete with a normal R language outcome. |
| worker → server | `{"kind":"fatal","message":"..."}` | Stop after an internal worker failure. |

Every frame uses `kind` to select its message variant.
Unknown message variants and fields are rejected in either direction.

## Handshake and evaluation

The first worker message must be `ready`.
The server does not send an evaluation before receiving it.

One evaluation has this shape:

```text
worker -> server  {"kind":"ready"}

server -> worker  {"kind":"evaluate","r":"hello"}
worker -> server  {"kind":"output","data":"zod: "}
worker -> server  {"kind":"output","data":"hello\n"}
worker -> server  {"kind":"completed"}
```

The worker may send zero or more `output` messages.
The server concatenates their text in arrival order.
`completed` ends the evaluation and permits the next one.

If the worker sends no output before `completed`, the current MCP projection returns `[done]`.
That marker is produced by the server; it is not a sideband message.

The protocol has no request IDs because only one evaluation can be in flight over this sideband.
The client rejects another MCP operation while the worker process mutex is held.

### Interactive input

When evaluating code calls `readline()` or enters `browser()`, the worker sends `input_requested`.
The current MCP response returns the output collected so far, the R prompt, and an `[input]` marker.
The next MCP `stdin` call sends its text unchanged:

```text
server -> worker  {"kind":"evaluate","r":"readline('Name: ')"}
worker -> server  {"kind":"input_requested","prompt":"Name: "}

server -> worker  {"kind":"input","stdin":"Ada\n"}
worker -> server  {"kind":"output","data":"[1] \"Ada\"\n"}
worker -> server  {"kind":"completed"}
```

The worker adds no newline.
It buffers partial and multiple lines.
If a chunk contains no complete line, `input_pending` returns another `[input]` boundary.
A complete line resumes evaluation, which may complete or request more input.
Unused buffered text is discarded when the outer evaluation ends.

New R code is rejected while input is required.
`stdin` is rejected before the first input request and after the evaluation completes.
Those are MCP-side state errors; the worker also treats an out-of-state protocol message as fatal.

## State transitions

| From | Frame | To |
| --- | --- | --- |
| starting | worker → server `ready` | idle |
| idle | server → worker `evaluate` | evaluating |
| evaluating | worker → server `output` | evaluating |
| evaluating | worker → server `input_requested` | input required |
| input required | server → worker `input` | evaluating |
| evaluating | worker → server `input_pending` | input required |
| evaluating | worker → server `completed` | idle |
| evaluating | worker → server `language_error` | idle |
| starting, idle, evaluating, or input required | worker → server `fatal` | stopped |
| starting, idle, evaluating, or input required | server → worker `shutdown` | terminal |

Malformed JSON, invalid UTF-8, an unexpected message, or sideband EOF fails the active operation.
`fatal` reports an internal worker failure; other protocol failures are detected by the server.
Startup and post-ready failures leave the logical worker in a sticky stopped state.
Later calls return the same stopped error rather than starting a replacement.

R parse, evaluation, auto-print, and task-callback errors are normal language outcomes.
R usually writes the native error text through `output`, after which the worker sends `language_error` with an empty message.
Host-classified errors such as incomplete source use its message.
The server completes the tool operation with `isError: false`, and the worker remains reusable.

## Shutdown

The server begins shutdown when MCP input closes or RMCP releases its transport.
At that moment it fixes a deadline one second in the future and closes the client's shutdown gate.
It then attempts to send:

```json
{ "kind": "shutdown" }
```

The worker sends no acknowledgment; it exits.
The graceful write runs independently of the deadline so a full sideband pipe cannot postpone forced termination.
The sandbox child waits only for the time remaining before the original deadline.
If its direct process is still running at the deadline, the sandbox force-stops its process group and reaps that direct process.

Shutdown uses a stop handle separate from the evaluation lock.
This lets the server terminate a child while another thread is blocked waiting for worker output.
If the worker cannot observe the shutdown frame while evaluating, the bounded kill is the completion path.

Shutdown closes a one-way gate that the client checks before and after acquiring the worker lock.
Startup registers a separate stop handle before waiting for `ready`.
If shutdown already closed the gate, startup stops the new child and fails immediately.

## Built-in R worker

The built-in worker runs each complete cell through `R_ReplDLLinit()` and repeated `R_ReplDLLdo1()` calls.
R parses and evaluates its expressions sequentially in the persistent global environment.
This supplies native visible-value printing, warning flushing, `.Last.value`, traceback bookkeeping, and top-level task callbacks with the submitted parsed expression.
A successful silent cell produces no sideband output, so the MCP result is `[done]`.

The CLI runs `worker` synchronously without a Tokio runtime, so R initialization and evaluation remain on the process main thread.
A small C shim owns the DLL-REPL frame across R's top-level long-jump boundary.
The custom `ReadConsole` callback keeps cell source separate from evaluation-time stdin and uses R's busy callback rather than prompt text to distinguish them.

Cell source is consumed as a stream.
If a cell contains complete expressions followed by incomplete source, the earlier expressions have already run when the worker returns the incomplete-code language error.
Submitted functions do not yet receive a virtual source filename.

## Current limits

The current implementation has no startup timeout, evaluation timeout, frame-size limit, or accumulated-output limit.
Only shutdown has a deadline.

It does not capture worker standard output or standard error.
It does not support arbitrary binary output.
Worker failures are projected as plain-text MCP tool errors.
The current sandbox child does not yet supervise descendants after its direct process exits, or descendants that leave its process group.

## Zod fixture behavior

Zod implements the protocol as an executable uv script requiring Python 3.11 or newer.
For a normal `evaluate`, it sends two output chunks followed by `completed`:

```text
zod: <r>\n
```

When `r` is exactly `stall`, Zod creates a checkpoint in its private temporary directory and sleeps forever.
When `r` is `violate protocol`, it sends an unexpected second `ready` message.
When `r` is `exit unexpectedly`, it exits with status 86 without replying.
Other fixture-only modes verify that the sandbox denies host writes and that a blocked sideband writer cannot delay shutdown.
Those behaviors are test fixtures, not part of the worker protocol.
