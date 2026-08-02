# Worker protocol

This document describes the worker protocol implemented by `mcp-console serve`, the built-in R worker, and `tests/fixtures/zod`.
It describes the current code, not the broader design under `design-sketches/`.
The message enums in `src/worker_protocol.rs` and the framing in `src/sideband.rs` are the source of truth.

## Scope

The current implementation provides one worker for one server process.
It supports one operation: evaluate an `r` string and return its output.
Evaluations run sequentially.

The protocol does not yet include interactive input, interrupts, request IDs, errors, sessions, capabilities, or protocol version negotiation.

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
| server → worker | `{"kind":"shutdown"}` | Exit without replying. |
| worker → server | `{"kind":"ready"}` | Startup is complete. |
| worker → server | `{"kind":"output","data":"..."}` | Append one output text chunk. |
| worker → server | `{"kind":"completed"}` | The evaluation is complete. |

Every frame uses `kind` to select its message variant.
Unknown worker-to-server fields are rejected.

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
`completed` ends the sideband evaluation; collecting its MCP result permits the next one.

If the worker sends no output before `completed`, the current MCP projection returns `[done]`.
That marker is produced by the server; it is not a sideband message.

The protocol has no request IDs because only one evaluation can be in flight over this sideband.
New code is rejected while an evaluation or its uncollected result is active.

## MCP waiting and polling

The optional MCP `timeout_ms` argument defaults to 60,000 milliseconds.
It bounds how long that `send` call waits for the worker; it is not sent over the sideband and does not bound or stop computation.
The wait includes lazy worker startup.

If the wait expires before `completed`, the call returns `[running]` and retains the active evaluation.
A later `send` call without `r` polls that evaluation with its own `timeout_ms`.
If the evaluation completes, the poll returns all output accumulated since it started, or `[done]` when it produced none.
If the poll wait expires first, it returns `[running]` again.
A call without `r` while no evaluation is active returns `[idle]`.
Only one `send` call may wait on or poll the active evaluation at a time; an overlapping call is a tool error.

This slice does not expose partial output while an evaluation is running.
Output cursors and incremental polling remain unimplemented.

## State transitions

| From | Frame | To |
| --- | --- | --- |
| starting | worker → server `ready` | idle |
| idle | server → worker `evaluate` | evaluating |
| evaluating | worker → server `output` | evaluating |
| evaluating | worker → server `completed` | idle |
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
R parses and evaluates its expressions sequentially in the persistent global environment, captures console output, prints every visible value, and performs native top-level bookkeeping such as updating `.Last.value`.
A cell that ends while R requires continuation input produces `Error: Incomplete code`; earlier complete expressions from that cell remain applied.
A successful silent cell produces no sideband output, so the MCP result is `[done]`.
The CLI runs `worker` synchronously without a Tokio runtime, so R initialization and evaluation remain on the process main thread.

The worker supplies cell source through `ReadConsole` before each top-level evaluation starts.
An evaluation-time `ReadConsole` request receives EOF because interactive input is not implemented yet.
Submitted source references are not retained.
Parse, evaluation, and print errors are returned as console text followed by `completed`, so the worker remains available even though the protocol has no structured language-error message.

## Current limits

The current implementation has no worker startup or execution timeout, frame-size limit, or accumulated-output limit.
`timeout_ms` limits one MCP wait without terminating the worker; only shutdown has a process deadline.

It does not capture worker standard output or standard error.
It does not support arbitrary binary output.
Worker failures are reported as plain-text MCP tool errors, not structured worker events.
The current sandbox child does not yet supervise descendants after its direct process exits, or descendants that leave its process group.

## Zod fixture behavior

Zod implements the protocol as an executable uv script requiring Python 3.11 or newer.
For a normal `evaluate`, it sends two output chunks followed by `completed`:

```text
zod: <r>\n
```

When `r` is exactly `stall`, Zod creates a checkpoint in its private temporary directory and sleeps forever.
When `r` is `complete after timeout`, it pauses briefly before returning its normal output.
When `r` is `violate protocol`, it sends an unexpected second `ready` message.
When `r` is `exit unexpectedly`, it exits with status 86 without replying.
Other fixture-only modes verify that the sandbox denies host writes and that a blocked sideband writer cannot delay shutdown.
Those behaviors are test fixtures, not part of the worker protocol.
