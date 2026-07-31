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

```yaml
server_to_worker:
  evaluate:
    type: evaluate
    fields:
      r: string
    wire_example:
      type: evaluate
      r: 1 + 1

  shutdown:
    type: shutdown
    fields: {}
    wire_example:
      type: shutdown

worker_to_server:
  ready:
    type: ready
    fields: {}
    wire_example:
      type: ready

  output:
    type: output
    fields:
      data: string
    wire_example:
      type: output
      data: |
        [1] 2

  completed:
    type: completed
    fields: {}
    wire_example:
      type: completed
```

Unknown worker-to-server fields are rejected.

## Handshake and evaluation

The first worker message must be `ready`.
The server does not send an evaluation before receiving it.

One evaluation has this shape:

```text
worker -> server  {"type":"ready"}

server -> worker  {"type":"evaluate","r":"hello"}
worker -> server  {"type":"output","data":"zod: "}
worker -> server  {"type":"output","data":"hello\n"}
worker -> server  {"type":"completed"}
```

The worker may send zero or more `output` messages.
The server concatenates their text in arrival order.
`completed` ends the evaluation and permits the next one.

If the worker sends no output before `completed`, the current MCP projection returns `[done]`.
That marker is produced by the server; it is not a sideband message.

The protocol has no request IDs because only one evaluation can be in flight over this sideband.
Concurrent MCP calls wait on the worker process mutex and reach the worker sequentially.

## Protocol states

```yaml
spawned:
  expected_worker_message: ready
  on_ready: idle
  on_anything_else: startup_error

idle:
  accepted_server_messages:
    evaluate: evaluating
    shutdown: exiting

evaluating:
  accepted_worker_messages:
    output: evaluating
    completed: idle
  on_ready_or_invalid_frame: evaluation_error
  server_may_send: shutdown
  on_worker_observes_shutdown: exiting
  if_shutdown_is_not_observed: forced_termination

exiting:
  worker_reply: none
  expected_result: process_exit

startup_error:
  active_call: failed
  worker: stopped
  next_evaluation: may_retry_startup

evaluation_error:
  active_call: failed
  worker: retained
  next_evaluation: reuses_the_same_worker
```

Malformed JSON, invalid UTF-8, an unexpected message, or sideband EOF fails the active operation.
There is no structured protocol error message and no automatic worker restart.
After an evaluation error, the cached worker and its sideband remain in place, so a later evaluation may encounter a misaligned stream and fail again.

## Shutdown

The server requests shutdown when MCP input closes.
Dropping the final shared client state also drops the cached worker, whose destructor requests shutdown.
It sends:

```json
{ "type": "shutdown" }
```

The worker sends no acknowledgment; it exits.
The server waits up to one second, polling the child every 25 milliseconds.
If the child is still running, the server attempts to kill and reap it; kill and wait errors are currently ignored.

Shutdown uses a control handle separate from the evaluation lock.
This lets the server terminate a child while another thread is blocked waiting for worker output.
If the worker cannot observe the shutdown frame while evaluating, the bounded kill is the completion path.

The internal `ShutdownState` is a shutdown gate, not the worker's full status:

```yaml
ShutdownState:
  Open:
    control:
      before_worker_spawn: null
      after_worker_spawn: WorkerControl
  Requested:
    meaning: no worker may remain running
```

The `Open` variant contains an optional control handle.
It is absent before spawn and present after spawn, including while the client waits for `ready`.

The client checks this state before and after acquiring the process mutex.
Startup publishes the control handle before waiting for `ready`.
If shutdown was requested in between, publication immediately stops the new child.

## Current limits

The current implementation has no startup timeout, evaluation timeout, frame-size limit, or accumulated-output limit.
Only shutdown has a timeout.

It does not capture worker standard output or standard error.
It does not support arbitrary binary output.
It does not report a structured worker error or restart a failed worker.
In worker mode, the MCP handler requires exactly one `r` string even though the static tool description still says “Echo” and its schema still permits any JSON object.

## Zod fixture behavior

Zod implements the protocol as an executable Python shebang.
For a normal `evaluate`, it sends two output chunks followed by `completed`:

```text
zod: <r>\n
```

When `r` is exactly `stall`, Zod creates the file named by `ZOD_STALL_PATH` and sleeps forever.
That behavior is test synchronization for bounded shutdown; it is not part of the worker protocol.
