# Server-relay protocol

This private interface connects the server to one generation's relay.
[`src/relay_protocol.rs`](../src/relay_protocol.rs) defines its frames; [`src/worker_relay.rs`](../src/worker_relay.rs) and [`src/worker_client/process.rs`](../src/worker_client/process.rs) implement the endpoints.
Windows uses the same JSONL frames with named pipes, process handles, and cooperative interrupt events; see [Windows execution](WINDOWS.md).
It has no independent negotiation; the server and relay use the same Console build.

## Process boundary

```text
server <--> [sandbox runner] <--> relay <--> worker
             lifetime owner      direct-child owner
```

The native runner inherits streams without proxying their contents.
Direct mode omits it.
The relay need not be a sandbox root or process-group leader.
It owns the worker's standard streams, sideband pipes, direct-child signals and reaping, not dependency resolution, environment commits, or descendants outside that direct-child contract.

Relay stdin/stdout carry protocol traffic and have a single writer per direction.
The server's generation-owned writer serializes both target bootstrap and relay commands.
Forced retirement aborts that writer independently of its queue and stdout, closes command admission, and joins it before replacement.
An aborted partial frame ends that transport; Shutdown and Interrupt are never inserted into it.
Stderr is inherited and reserved for infrastructure diagnostics; when available, a framed `fatal` event is authoritative over best-effort stderr.
Unrelated descriptors are closed before launch.
The [worker protocol](WORKER_PROTOCOL.md) owns the inherited fd and worker-message contract; [sandbox integration](SANDBOX.md) owns native enforcement.

## Framing and raw bytes

Each relay direction is ordered UTF-8 JSONL, flushed per frame.
While command input is open, unknown kinds/fields, wrong types, malformed JSON, and partial-frame EOF fail the transport.
The first explicit Shutdown ends command parsing; all trailing frames and bytes are ignored, including malformed or incomplete tails.
Worker output framing remains strict through retirement.

Raw stdout/stderr reads are chunks of at most 8 KiB.
An entirely valid UTF-8 chunk uses a text event; otherwise it uses padded standard base64.
The relay does not retain UTF-8 state between reads, so a split scalar may use byte-form events.
The server decodes those bytes and handles incremental UTF-8 projection.
There is no line buffering or coalescing timer.

## Server commands and relay events

All semantic commands and events from the [worker schema](WORKER_PROTOCOL.md#message-schemas) appear flat and unchanged on this boundary, with these transport controls:

| Command     | Fields and effect                                                                                                   |
| ----------- | ------------------------------------------------------------------------------------------------------------------- |
| `stdin`     | `data`: string; append exact UTF-8 bytes to worker fd 0.                                                            |
| `interrupt` | `request_id`: integer; attempt SIGINT delivery to the live direct worker.                                           |
| `shutdown`  | `grace_millis`: integer; close stdin and request bounded worker exit. This replaces payload-free worker `shutdown`. |

There is no nested `worker_message`, result acknowledgment, or inline-control-specific wire frame.
The relay additionally emits:

| Event                                                      | Fields and meaning                                             |
| ---------------------------------------------------------- | -------------------------------------------------------------- |
| `stdout`, `stderr`                                         | `data`: valid UTF-8 chunk.                                     |
| `stdout_bytes`, `stderr_bytes`                             | `data`: padded standard base64 chunk.                          |
| `stdout_closed`, `stderr_closed`, `worker_sideband_closed` | No payload; that stream's retirement boundary.                 |
| `interrupt_result`                                         | Matching `request_id`, optional string `error`.                |
| `shutdown_started`                                         | No payload; acceptance of the one registered shutdown request. |
| `worker_exited`                                            | `code`: direct-worker exit status.                             |
| `worker_signaled`                                          | `signal`: direct-worker termination signal.                    |
| `fatal`                                                    | `message`: infrastructure/protocol failure.                    |

Payload-free frames contain exactly `kind`.
A successful interrupt result means the OS accepted signal delivery, not that execution stopped.
Resolver-targeted interrupts do not cross this boundary.
The server owns stdin/control ordering, its 100-millisecond interrupt grace, cell admission, and restart sequencing; see [`send` operations](SEND_OPERATIONS.md).

## Event production and ordering

Sideband, stdout, stderr, and direct-worker lifecycle each produce complete frames into one FIFO; a single writer serializes them without interleaving.
Each producer's order is preserved, but the queue cannot reconstruct chronology across independent transports.
Raw output can arrive after a semantic operation result even when written earlier.

The relay reads past operation results without waiting for acknowledgment.
It carries no response cuts, MCP budgets, candidate state, or activation decisions.
Those remain server responsibilities.

## Output backpressure

The ordinary output queue admits 512 frames and 8 MiB of encoded payload, including the active write.
A larger single frame is admitted alone and occupies the budget until written.
Readers wait for capacity before reading more; frames are not split or rejected to fit this budget.
Each reader can also hold one frame awaiting admission, so this is not a total-memory limit.

Supervisor events have a separate 16-frame/64-KiB allowance but enter the same FIFO and cannot overtake earlier output.
All worker-originated frames, including completion and resolver requests, use the ordinary allowance.
Supervisor-budget exhaustion fails the transport.
Failed sideband event forwarding starts direct-worker retirement independently of the stdout writer's failure callback, which a blocking Windows write may delay.
Reader completion after cancellation is a separate observation and does not initiate retirement again.

For pipes, FIFOs, and sockets, output is nonblocking and retirement bounds both writes and queue admission.
Original descriptor flags are restored when the writer finishes; duplicate descriptors share `O_NONBLOCK` state.
Regular-file stdout retains filesystem blocking behavior and has no relay output deadline.

## Interruption and shutdown

Intentional shutdown registers one request against an absolute one-second worker deadline.
The command writer derives `grace_millis` from the time remaining, so queued writes cannot extend it.
The relay queues `shutdown_started` before beginning shutdown, without waiting for downstream delivery.
An unsolicited or duplicate acceptance is invalid.
Timely server observation permits up to two additional seconds for relay retirement, not more worker grace.
Failure retirement instead uses zero worker grace and the same bounded relay allowance.

The relay's supervisor closes command admission when shutdown or retirement begins.
It forwards no later evaluation, preparation, resolution, stdin or interrupt command, and sends at most one `shutdown_started` and one worker `shutdown`.
The first worker retirement deadline is retained; later Shutdown or EOF observations cannot renew it, and queued commands cannot keep an expired deadline alive.
Fatal failures still retire immediately.

Explicit Shutdown and clean relay-input EOF concurrently close stdin and send worker `shutdown`.
Clean input EOF starts a one-second grace only if retirement has not begun, without `shutdown_started`; partial-command EOF is failure while parsing remains open.
Worker-sideband EOF also closes admission and stdin and starts that grace only if needed.
Unix waits for the worker without sending another sideband command on this observation; Windows retains its cooperative worker `shutdown`.
Controller EOF, worker-sideband EOF, child exit and fatal transport failure remain distinct observations; cancellation completion does not count as EOF.
At the worker deadline the relay terminates the direct child if needed, reaps it, and retires transports.
Unix uses SIGKILL and reports the signal; Windows uses native termination and reports the numeric exit code.
After the server aborts its command writer during retirement, the resulting partial-command EOF describes the abandoned transport and does not itself block replacement.
Other fatal failures, owned task joins, and confirmed launcher cleanup still determine whether replacement is permitted.

The server's ordered retirement marker separates old-generation event ownership from replacement.
It is not a wire frame.
After the applicable relay deadline, the server requests launcher retirement with SIGTERM, allows six seconds before forced termination, then one second to observe exit.
Repeated retirement reuses its result rather than signaling an old PID again.
Forced launcher exit is not proof of descendant cleanup.

## Retirement and failure

Transport retirement cancels local readers/writers before joining them.
Only the local sideband write half is shut down to interrupt blocked commands; the read half remains available for drainage.
Sideband/stdout/stderr readers share 100 milliseconds for additional nonblocking reads and stop at EOF, lack of immediately readable data, or the deadline.
Complete buffered sideband frames are still offered to the queue; incomplete tails and later descendant output may be abandoned.
A reader never started during failed setup contributes no semantic frames before `fatal`.

For pipe/FIFO/socket output, one shared one-second deadline covers retirement queue admission and downstream flush.
After draining, the relay emits standard-stream closures, any retained fatal failure, sideband closure, and the direct-worker outcome when available.
No raw bytes follow their stream closure; no event follows `worker_exited` or `worker_signaled`.
Expiry with pending output is a transport error and may leave a partial JSONL prefix.

Clean relay-output EOF requires expected closures and the worker outcome.
Malformed frames/base64, unexpected events/EOF, and fatal failures stop the worker transport; available output and the failure remain observable before retirement.
Worker exit reports only the direct child, never native descendant cleanup.
Public failure/replacement notices belong to the [runtime guide](BUILTIN_RUNTIME.md#output-and-notices).
