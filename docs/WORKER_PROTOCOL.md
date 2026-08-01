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

The worker starts lazily on the first `send` call.
On macOS, the server uses the same `SandboxedCommand` builder as the `sandbox` command.
For `--worker PATH`, `PATH` is one program name or path, with no arguments or shell parsing, producing a launch equivalent to:

```text
/usr/bin/sandbox-exec <policy> -- PATH
```

The built-in path launches `mcp-console worker bootstrap`.
Inside the sandbox, that command discovers `R_HOME` and self-executes as `mcp-console worker run` with R's library directory on the dynamic loader path.
The final process initializes R through `libr` and `harp`.

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
Before it runs other programs or user code, it must remove the bootstrap environment variables and prevent descendants from inheriting the descriptors.
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
`completed` ends the evaluation and permits the next one.

If the worker sends no output before `completed`, the current MCP projection returns `[done]`.
That marker is produced by the server; it is not a sideband message.

The protocol has no request IDs because only one evaluation can be in flight over this sideband.
Concurrent MCP calls wait on the worker process mutex and reach the worker sequentially.

## State transitions

| From | Frame | To |
| --- | --- | --- |
| starting | worker → server `ready` | idle |
| idle | server → worker `evaluate` | evaluating |
| evaluating | worker → server `output` | evaluating |
| evaluating | worker → server `completed` | idle |
| starting, idle, or evaluating | server → worker `shutdown` | terminal |

Malformed JSON, invalid UTF-8, an unexpected message, or sideband EOF fails the active operation.
There is no structured protocol error message and no automatic worker restart.
Startup failure discards the worker, so a later evaluation may retry startup.
After `ready`, a sideband failure does not discard the worker; a later evaluation reuses it even if the process has exited or unread frames remain from the failed evaluation.
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

The built-in worker parses each complete cell in memory, evaluates its expressions in the persistent global environment, captures R console output, and prints every visible value.
A successful silent cell produces no sideband output, so the MCP result is `[done]`.
The CLI runs `worker run` synchronously without a Tokio runtime, so R initialization and evaluation remain on the process main thread.

This initial path does not use R's DLL REPL loop.
It does not provide interactive input, top-level warning flushing, `.Last.value`, task callbacks, traceback bookkeeping, or source references.
Parse, evaluation, and print errors are returned as console text followed by `completed`, so the worker remains available even though the protocol has no structured language-error message.

## Current limits

The current implementation has no startup timeout, evaluation timeout, frame-size limit, or accumulated-output limit.
Only shutdown has a deadline.

It does not capture worker standard output or standard error.
It does not support arbitrary binary output.
It does not report a structured worker error or restart a failed worker.
The current sandbox child does not yet supervise descendants after its direct process exits, or descendants that leave its process group.

## Zod fixture behavior

Zod implements the protocol as an executable uv script requiring Python 3.11 or newer.
For a normal `evaluate`, it sends two output chunks followed by `completed`:

```text
zod: <r>\n
```

When `r` is exactly `stall`, Zod creates a checkpoint in its private temporary directory and sleeps forever.
Other fixture-only modes verify that the sandbox denies host writes and that a blocked sideband writer cannot delay shutdown.
Those behaviors are test synchronization, not part of the worker protocol.
