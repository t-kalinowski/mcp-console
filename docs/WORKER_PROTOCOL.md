# Development worker protocol

This document describes the worker protocol implemented by `mcp-console serve --worker PATH` and `tests/fixtures/zod`.
It describes the current code, not the planned production worker in `design-sketches/`.
The message enums in `src/worker_client.rs` and the framing in `src/sideband.rs` are the source of truth.

## Scope

The current implementation provides one development worker for one server process.
It supports one operation: evaluate an `r` string and return its output.
Evaluations run sequentially.

The protocol does not yet include interactive input, interrupts, request IDs, errors, sessions, capabilities, or protocol version negotiation.

Without `--worker PATH`, the server does not start a worker and `send` retains its JSON echo behavior.

## Launch contract

`PATH` is one program name or path, with no arguments and no shell parsing.
The worker starts lazily on the first worker-backed `send` call as if by:

```text
PATH
```

The server launches the worker directly with null standard input, output, and error streams.
The sideband pipes are its only communication channel.
The worker leads a dedicated process group so bounded shutdown also stops wrappers and descendants that remain in that group.

This launch contract currently works on Unix.
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
Zod does this with `os.environ.pop()` and `os.set_inheritable(fd, False)`.

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
After an evaluation error, the cached worker and its sideband remain in place, so a later evaluation may encounter a misaligned stream and fail again.

## Shutdown

The server requests shutdown when MCP input closes.
Dropping the final shared client state also drops the cached worker, whose destructor requests shutdown.
It sends:

```json
{ "kind": "shutdown" }
```

The worker sends no acknowledgment; it exits.
The server waits up to one second for the child process to exit.
If the child is still running, the server attempts to kill its process group and reap the direct child; kill and wait errors are currently ignored.

Shutdown uses a stop handle separate from the evaluation lock.
This lets the server terminate a child while another thread is blocked waiting for worker output.
If the worker cannot observe the shutdown frame while evaluating, the bounded kill is the completion path.

Shutdown closes a one-way gate that the client checks before and after acquiring the worker lock.
Startup registers a separate stop handle before waiting for `ready`.
If shutdown already closed the gate, startup stops the new child and fails immediately.

## Current limits

The current implementation has no startup timeout, evaluation timeout, frame-size limit, or accumulated-output limit.
Only shutdown has a timeout.

It does not capture worker standard output or standard error.
It does not support arbitrary binary output.
It does not report a structured worker error or restart a failed worker.
It does not supervise descendants that leave the worker process group.
In worker mode, the MCP handler requires exactly one `r` string even though the static tool description still says “Echo” and its schema still permits any JSON object.

## Zod fixture behavior

Zod implements the protocol as an executable uv script requiring Python 3.11 or newer.
For a normal `evaluate`, it sends two output chunks followed by `completed`:

```text
zod: <r>\n
```

When `r` is exactly `stall`, Zod creates the file named by `ZOD_STALL_PATH` and sleeps forever.
That behavior is test synchronization for bounded shutdown; it is not part of the worker protocol.
