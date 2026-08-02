# Worker protocol

This document describes the worker protocol implemented by `mcp-console serve`, the built-in R worker, and `tests/fixtures/zod`.
It describes the current code, not the broader design under `design-sketches/`.
The message enums in `src/worker_protocol.rs`, the framing in `src/sideband.rs`, and the fd-0 routing in `src/worker_client.rs` are the source of truth.

## Scope

The current implementation provides one worker for one server process.
It evaluates one `r` cell at a time and accepts exact `stdin` text while that evaluation remains active.
Evaluations run sequentially.

The protocol does not yet include interrupts, request IDs, structured errors, sessions, capabilities, or protocol version negotiation.

Plain `serve` selects the built-in R worker.
The hidden `serve --worker PATH` option replaces it with a development worker.

## Launch contract

The worker starts lazily on the first `send` call that supplies `r`.
On macOS, the server uses the same `SandboxedCommand` builder as the `sandbox` command.
For `--worker PATH`, `PATH` is one program name or path, with no arguments or shell parsing, producing a launch equivalent to:

```text
/usr/bin/sandbox-exec <policy> -- PATH
```

The built-in path launches `mcp-console worker`.
Inside the sandbox, the worker takes ownership of the sideband, discovers `R_HOME` through the selected R executable, and initializes R through `libr` and `harp`.
Harp opens `R_HOME/lib/libR.dylib` by its absolute path, so the worker does not self-execute or set a dynamic-loader environment variable.

The server launches the sandboxed worker with piped standard input and null standard output and error streams.
Sideband frames carry control and output; interactive input bytes travel through the worker's fd 0.
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

The server also owns an independent FIFO writer connected to worker fd
0. Each accepted `stdin` string is UTF-8 encoded and queued to that
writer without inspection or framing.
There is no sideband input frame.

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
| server → worker | `{"kind":"shutdown"}` | Exit without replying. |
| worker → server | `{"kind":"ready"}` | Startup is complete. |
| worker → server | `{"kind":"output","data":"..."}` | Append one output text chunk. |
| worker → server | `{"kind":"input_requested","prompt":"..."}` | Report that the runtime requested input. |
| worker → server | `{"kind":"input_received"}` | Report that the current read succeeded. |
| worker → server | `{"kind":"completed"}` | The evaluation is complete. |

Every frame uses `kind` to select its message variant.
Unknown fields are rejected in either direction.

## Handshake and evaluation

The first worker message must be `ready`.
The server does not send an evaluation before receiving it.

One evaluation has this shape:

```text
worker -> server  {"kind":"ready"}

server -> worker  {"kind":"evaluate","r":"echo"}
worker -> server  {"kind":"output","data":"zod: "}
worker -> server  {"kind":"output","data":"echo\n"}
worker -> server  {"kind":"completed"}
```

The worker may send zero or more `output` messages.
The server concatenates their text in arrival order.
`input_requested` starts one provisional input state, and the matching `input_received` clears it after the runtime read succeeds.
Only one request may be outstanding: a second request, a receipt without a request, or completion before its receipt is a protocol failure.
`completed` ends the sideband evaluation; collecting its MCP result permits the next one.

If the worker sends no output before `completed`, the current MCP projection returns `[done]`.
That marker is produced by the server; it is not a sideband message.

The protocol has no request IDs because only one evaluation can be in flight over this sideband.
New code is rejected while an evaluation or its uncollected result is active.

## MCP waiting and polling

The optional MCP `timeout_ms` argument defaults to 60,000 milliseconds.
It bounds how long that `send` call waits for the worker; it is not sent over the sideband and does not bound or stop computation.
The wait includes lazy worker startup.

An `input_requested` frame remains provisional for 10 milliseconds.
If `input_received` arrives first, the server restores the retained prompt output for an unexposed request and continues waiting for another request, completion, or the MCP deadline.
If the grace expires first, the call returns output collected so far, the prompt, and `[input]` before that deadline.
Supplying nonempty stdin for an outstanding request starts a fresh 10-millisecond grace window; the MCP deadline reports a still-outstanding request immediately, even inside that window.
A pending input request wins over `[running]` at the deadline.
A later `send` call without `r` polls that evaluation with its own `timeout_ms`; it may include `stdin` to queue bytes before waiting.
Completion returns output not already delivered at an `[input]` boundary, or `[done]` when it produced none.
If the poll wait expires first, it returns `[running]` again.
A call without `r` or `stdin` while no evaluation is active returns `[idle]`; stdin alone while idle is an error.

Except for prompt boundaries, this slice does not expose partial output while an evaluation is running.
Output cursors and general incremental polling remain unimplemented.

### Interactive input

When evaluated code calls `readline()` or enters `browser()`, the worker may send `input_requested`.
The MCP response returns the output collected so far, the trimmed prompt, and an `[input]` marker.
A later `send` call supplies its `stdin` unchanged:

```text
server -> worker  {"kind":"evaluate","r":"readline('name> ')"}
worker -> server  {"kind":"input_requested","prompt":"name> "}

server -> fd 0    Ada\n
worker -> server  {"kind":"input_received"}
worker -> server  {"kind":"output","data":"[1] \"Ada\"\n"}
worker -> server  {"kind":"completed"}
```

An MCP call may contain both `r` and `stdin`.
The server flushes `evaluate` first, then attaches the evaluation to the worker's stdin writer and drains any queued input in submission order.
A later stdin-only call uses the same route without acquiring the evaluation's worker lock, including after an earlier call returned `[running]`.

The server writes each string blindly.
It adds no newline, does not split or validate lines, and imposes no stdin size limit.
The end of a payload does not close fd 0 and is not an EOF marker.
A newline-free fragment remains pending until later stdin completes it or worker shutdown closes the stream.
The R console callback consumes only through one newline or its supplied buffer; it does not prefetch later lines from fd 0.
`input_requested` is an observation of worker state, not permission to write.
After a nonempty callback read, `input_received` closes that provisional request before the runtime resumes.
It does not acknowledge a particular stdin submission, identify which bytes satisfied the read, or report bytes consumed by code that reads fd 0 directly.
If no receipt arrives during the grace window, the request remains exposed as `[input]`; a partial follow-up therefore returns `[input]` again rather than `[running]`.
Empty stdin writes no bytes and leaves an exposed request immediately reportable.
Code that reads fd 0 directly can consume bundled input or input sent after a polling timeout without sending either input frame.

Acceptance means the bytes were queued, not that the current evaluation consumed them.
The server does not retract or drain bytes after `completed`; data already in the pipe or retained by a runtime reader may satisfy later reads or later evaluations.
Worker shutdown or failure discards whatever remains.
New R code is rejected while an evaluation or its uncollected result is active.

## State transitions

| From | Frame | To |
| --- | --- | --- |
| starting | worker → server `ready` | idle |
| idle | server → worker `evaluate` | evaluating |
| evaluating | worker → server `output` | evaluating |
| evaluating | worker → server `input_requested` | evaluating, input provisional |
| evaluating, input provisional | worker → server `input_received` | evaluating |
| evaluating, with or without input reported | MCP stdin submission | evaluating |
| evaluating, no provisional input | worker → server `completed` | idle |
| starting, idle, or evaluating | server → worker `shutdown` | terminal |

Malformed JSON, invalid UTF-8, an unexpected message, or sideband EOF fails the active operation.
There is no structured protocol error message.
Startup failure leaves no cached worker, so a later evaluation retries startup.
After `ready`, a sideband failure force-stops and discards the worker; a later evaluation starts a fresh worker.
R parse and evaluation errors are not sideband failures: the built-in worker sends them as output followed by `completed` and remains reusable.

## Shutdown

The server begins shutdown when MCP input closes or RMCP releases its transport.
At that moment it fixes a deadline one second in the future and closes the client's shutdown gate.
It then attempts to send:

```json
{ "kind": "shutdown" }
```

The worker sends no acknowledgment; it exits.
The shutdown task queues worker-stdin closure, then attempts the sideband write.
It runs independently of the deadline so a blocked stdin writer or full sideband pipe cannot postpone forced termination.
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
R parses and evaluates its expressions sequentially in the persistent global environment, captures console output, prints every visible value, and performs native top-level bookkeeping such as updating `.Last.value`.
A cell that ends while R requires continuation input produces `Error: Incomplete code`; earlier complete expressions from that cell remain applied.
A successful silent cell produces no sideband output, so the MCP result is `[done]`.
The CLI runs `worker` synchronously without a Tokio runtime, so R initialization and evaluation remain on the process main thread.

The worker supplies cell source through `ReadConsole` before each top-level evaluation starts.
For every evaluation-time `ReadConsole` call, the callback sends `input_requested`, then reads fd 0 directly until one newline arrives or R's supplied buffer is full.
After a nonempty read succeeds, it sends `input_received` before returning the bytes to R.
A newline-free fragment shorter than that buffer keeps the callback blocked, while bytes after a returned chunk remain in the pipe for a later `ReadConsole` call or a direct fd-0 reader.
It uses R's busy callback rather than prompt text to distinguish cell source from evaluated-code input.
Unread fd-0 input remains available across evaluation boundaries.
Submitted source references are not retained.
Parse, evaluation, and print errors are returned as console text followed by `completed`, so the worker remains available even though the protocol has no structured language-error message.

## Current limits

The current implementation has no worker startup or execution timeout, frame-size limit, stdin queue limit, or accumulated-output limit.
`timeout_ms` limits one MCP wait without terminating the worker or a blocked stdin write; only shutdown has a process deadline.
The 10-millisecond input grace controls when a provisional request becomes visible and does not limit evaluation or stdin reads.
It is a latency heuristic: scheduling can delay a receipt past the grace and expose an extra `[input]` boundary even when queued bytes subsequently satisfy the read.

It does not capture worker standard output or standard error.
It does not support arbitrary binary output.
Worker failures are reported as plain-text MCP tool errors, not structured worker events.
Concurrent MCP `send` calls are outside the current contract.
The current sandbox child does not yet supervise descendants after its direct process exits, or descendants that leave its process group.

## Zod fixture behavior

Zod implements the protocol as an executable uv script requiring Python 3.11 or newer.
When `r` is exactly `echo`, it sends two output chunks followed by `completed`:

```text
zod: echo\n
```

When `r` is exactly `stall`, Zod creates a checkpoint in its private temporary directory and sleeps forever.
When `r` is `complete after timeout`, it pauses briefly before returning `zod: complete after timeout\n`.
When `r` is `violate protocol`, it sends an unexpected second `ready` message.
When `r` is `exit unexpectedly`, it exits with status 86 without replying.
When `r` is `request input`, it sends `input_requested`, calls Python `input()` to consume one line from fd 0, and sends `input_received` after that call returns.
The `request input after timeout` mode gates that request until an earlier MCP wait expires, then covers retention of output attached to a still-unexposed request.
The `input without request` and `input length without request` modes call `input()` without first sending a frame, covering proactive fd-0 delivery.
The `input without request then request input` mode performs one direct read before a reported request/receipt pair, covering the distinction between direct fd-0 reads and callback-style input state.
Zod echoes the input or its byte length and completes.
Its acceptance supplies newline-terminated text because Python `input()` waits for a complete line; partial-input boundaries are covered by the built-in R worker.
Other fixture-only modes verify that the sandbox denies host writes and that a blocked sideband writer cannot delay shutdown.
Other commands fail instead of being echoed implicitly.
Those behaviors are test fixtures, not part of the worker protocol.
