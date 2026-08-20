# Worker relay protocol

This document describes the private protocol between `mcp-console serve` and the per-generation worker relay on macOS.
It describes the current code, not a public or versioned interface.
The message definitions and framing in `src/relay_protocol.rs`, the relay implementation in `src/worker_relay.rs`, and the server-side transport in `src/worker_client/macos.rs` are the source of truth.

## Process boundary

The server remains outside the sandbox.
For each worker generation it starts one relay as the direct sandbox child, and the relay starts the configured worker inside the same sandbox:

```text
server <--> (worker relay <--> worker)
```

The parentheses mark the sandbox boundary.
The relay is also the dedicated sandbox process-group leader, and the worker inherits that group.

Only the relay's standard input, standard output, and standard error cross the server/sandbox boundary.
Standard input and output carry the framed relay protocol described below.
Relay standard error is inherited from the server and is not part of the protocol; it is normally empty and is reserved for fatal or infrastructure diagnostics.
Runtime failures are also represented by a `fatal` event when relay stdout remains usable.

The relay creates the worker's two private sideband pipes and its standard-input, standard-output, and standard-error pipes after entering the sandbox.
It passes the same sideband environment variables and fd-0/1/2 contract that the worker used before the relay was introduced.
The worker protocol is otherwise unchanged and is documented in [`WORKER_PROTOCOL.md`](WORKER_PROTOCOL.md).

The server still owns worker-generation state, evaluation and preparation admission, output projection, retained requirements, and host resolvers.
R, Python, and DuckDB requirement resolution remains outside the sandbox.
The relay owns only the worker process and its local transports, signal delivery, bounded termination, and reaping.

## Framing

Each direction is an ordered UTF-8 JSONL stream.
One frame is one JSON object followed by `\n`, and each frame is flushed after serialization.
A stream that closes midway through a frame is a transport error.

Raw worker standard-output and standard-error chunks are read in chunks of at most 8 KiB and encoded with padded standard base64 in relay events.
The relay forwards each available chunk immediately; it does not interpret those bytes, impose line buffering, or use a coalescing timer.
The server decodes them back to bytes before applying its existing per-stream UTF-8 completion and MCP projection rules.

Every relay event carries a generation-local `sequence` number, beginning at zero and increasing by one.
The relay uses one serializer for sideband, standard-output, standard-error, exit, and infrastructure events, so the server observes one ordered stream.
This preserves order within each worker source but does not claim an operating-system chronology between independently read sources.

## Server commands

The server can send these frames:

| Frame | Meaning |
| --- | --- |
| `{"kind":"worker_message","message":{...}}` | Forward one unchanged server-to-worker sideband message. |
| `{"kind":"stdin","data":"..."}` | Decode base64 and append the exact bytes to worker fd 0. |
| `{"kind":"interrupt","request_id":1}` | Send `SIGINT` to the live worker and return the result with the same request ID. |
| `{"kind":"shutdown","grace_millis":1000}` | Close worker stdin, send the unchanged worker `shutdown` message, and stop the worker within the supplied grace period. |
| `{"kind":"acknowledge","sequence":7}` | Release a relay ordering barrier for the named event. |

Worker messages retain their existing nested JSON shape.
For example, an evaluation is forwarded as:

```json
{
  "kind": "worker_message",
  "message": { "kind": "evaluate", "language": "r", "source": "1 + 1" }
}
```

`stdin` payload end is not EOF.
The relay writes accepted payloads in command order without adding bytes.

## Relay events

The relay can emit these frames:

| `payload` object | Meaning |
| --- | --- |
| `{"kind":"worker_message","message":{...}}` | Forward one unchanged worker-to-server sideband message. |
| `{"kind":"stdout","data":"..."}` | One base64-encoded raw fd-1 chunk. |
| `{"kind":"stderr","data":"..."}` | One base64-encoded raw fd-2 chunk. |
| `{"kind":"stream_closed","stream":"stdout"}` | The worker stdout reader reached EOF. |
| `{"kind":"stream_closed","stream":"stderr"}` | The worker stderr reader reached EOF. |
| `{"kind":"worker_sideband_closed"}` | The worker-to-relay sideband reached EOF. |
| `{"kind":"interrupt_result","request_id":1}` | The requested worker signal succeeded. |
| `{"kind":"interrupt_result","request_id":1,"error":"..."}` | The requested worker signal failed. |
| `{"kind":"shutdown_started"}` | The relay accepted the server's registered shutdown request. |
| `{"kind":"worker_exited","status":{"code":0}}` | The direct worker exited normally with the given code. |
| `{"kind":"worker_exited","status":{"signal":9}}` | The direct worker exited because of the given signal. |
| `{"kind":"fatal","message":"..."}` | Relay infrastructure or protocol failure while relay stdout is still usable. |

The full event wraps that object under `payload`, for example:

```json
{ "sequence": 3, "payload": { "kind": "stdout", "data": "aGVsbG8K" } }
```

The server requires the sequence to be exactly `0, 1, 2, ...` for the generation.
Each output stream closes exactly once and cannot carry data afterward, and the worker sideband closes exactly once.
`worker_exited` is the final event.
Relay stdout EOF is a clean retirement only after both output streams, the worker sideband, and the worker process have closed in that form.

## Ordering barriers

The server must commit operation-terminal state before the relay can publish sideband activity that logically follows an operation-terminal worker message.
Before the relay publishes any `worker_message`, it checkpoints both raw-output readers and publishes bytes already available from fd 1 and fd 2.
This prevents a worker message that the server rejects from causing retirement before preceding raw output reaches the server.
For each `completed`, `r_prepared`, `r_preparation_failed`, `python_prepared`, or `python_preparation_failed` message, the relay publishes the event and waits for an `acknowledge` command naming its sequence before it reads another worker sideband frame.
The standard-output and standard-error readers continue draining while that sideband barrier is held.

The initial `ready` message is not acknowledgment-gated; the server registers the generation as ready before starting its continuous relay dispatcher.
`ready`, `resolve_python`, and `resolve_python_version` use the common raw-output checkpoint but are not acknowledgment-gated.
Before publishing `worker_sideband_closed`, the relay also checkpoints both raw-output readers, so worker bytes accepted before sideband EOF cannot be overtaken by the failure that EOF causes on the server.
The server acknowledges an evaluation or preparation terminal only after the operation owner has committed its terminal state and output checkpoint.
While the relay is waiting at that barrier, an acknowledgment for any sequence other than the expected one is a protocol error.

## Interruption and shutdown

Host resolvers remain server processes with their existing independent process groups.
A session interrupt targets an active host resolver first.
Otherwise the server sends an `interrupt` command to the relay, which signals the live worker and returns `interrupt_result`; the request ID binds the response to that worker generation.

For restart or server shutdown, the server registers one relay-shutdown request and queues one `shutdown` command against the existing absolute one-second worker deadline.
The sole relay-command writer computes `grace_millis` from the time remaining when it serializes that command, so earlier queued writes cannot extend the worker deadline.

After parsing the command, the relay assigns and flushes a normal sequence number for `shutdown_started` before it begins worker shutdown.
The event is not acknowledgment-gated.
It has no request ID because each generation permits only the one shutdown request that the server registers before enqueueing the command.
The server rejects it when no shutdown request is registered or when the relay sends it twice.
If the server observes it by the original worker deadline, the event records timely relay acceptance and permits up to two additional seconds after that deadline for relay retirement.
This outer allowance does not extend the worker grace carried by the command.

The relay closes worker stdin and sends the unchanged worker-sideband `shutdown` message without waiting for one path before attempting the other.
If the worker remains live at its deadline, the relay first sends `SIGKILL` to the direct worker, then repeatedly stops every other live process whose current process group is exactly the relay's group while leaving the relay alive as group leader, and finally reaps the direct worker.
It finishes the worker stream boundaries, emits `worker_exited`, flushes relay stdout, and exits.
Clean relay-stdin EOF does not emit `shutdown_started`; it performs the same worker shutdown with a new one-second grace period measured from EOF.
EOF midway through a command frame is a transport failure instead.

The server observes direct relay exit without reaping it, so the waitable relay PID continues to pin the process-group identity.
It waits through the original deadline and uses the additional two-second retirement allowance only after timely `shutdown_started` acceptance.
It then always closes the complete sandbox process-group lifetime and reaps the relay, including when the relay already exited during either wait.
That outer cleanup is also the fail-safe when the relay does not accept shutdown or stalls, so an in-group descendant cannot survive the sandbox generation.
The sandbox owner records that retirement before releasing the process-group identity; concurrent or repeated retirement calls return the stored result and never signal the retired relay PID or process group again.
Descendants that leave the group remain unsupported.

Malformed relay JSON, invalid base64, an unexpected command or acknowledgment, a sequence discontinuity, a fatal event, or unexpected relay EOF fails the worker transport.
Worker-sideband EOF has its own relay event; other worker-sideband read failures become fatal relay events.
For a relay-owned protocol or I/O failure, the relay requests worker termination immediately but defers the `fatal` event until its worker transports have stopped and both raw-output readers have drained and joined.
Raw output accepted before the failure therefore precedes `fatal` in the outer stream regardless of which relay task detected the failure.
The server preserves a fatal message as the worker failure, stops publishing later sideband messages, and continues draining the relay stream through its output-close and worker-exit events before retiring the generation.
It likewise keeps draining and validating relay events if the local worker-sideband publisher has already closed during server-side retirement.
