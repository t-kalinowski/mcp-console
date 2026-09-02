# Server-relay protocol

This document defines the private protocol between `mcp-console serve` and the per-generation worker relay on macOS.
It is an exact current interface, but it is neither public nor versioned.
The message definitions and framing in `src/relay_protocol.rs`, the relay implementation in `src/worker_relay.rs`, and the server-side transport in `src/worker_client/macos.rs` are the source of truth.
Transcript-runner progress lines are test user-interface output and never enter this protocol.

The [implemented architecture](ARCHITECTURE.md) explains why this boundary exists and which process owns each responsibility.
The [worker protocol](WORKER_PROTOCOL.md) defines the relay's other interface.

## Process boundary

The server remains outside the sandbox.
For each worker generation it starts `sandbox-exec` as the direct child.
That root first runs a hidden wrapper blocked on a private startup gate.
After the host manager begins observation, the server installs manager-failure recovery and commits manager ownership.
The server then releases the wrapper; it closes the gate and replaces itself with the configured relay in the same process identity.
The relay then starts the configured worker inside the same sandbox:

```text
server <--> (gated root -> worker relay <--> worker)
   \----> sandbox lifetime manager
```

The parentheses mark the sandbox boundary.
The relay is also the dedicated sandbox process-group leader, and the worker inherits that group.
The sandbox lifetime manager is a separate host-side process outside the parentheses.
It observes and retires the relay, worker, and their observed descendants as one sandbox lifetime.

Once relay code begins, only its standard input, standard output, and standard error cross the server/sandbox boundary.
Standard input and output carry the framed relay protocol described below.
Relay standard error is inherited from the server and is not part of the protocol; it is normally empty and is reserved for fatal or infrastructure diagnostics.
Runtime failures are also represented by a `fatal` event when relay stdout remains usable.
The framed event is authoritative; stderr diagnostics are best effort because the server's outer fail-safe can terminate a failed relay before its final diagnostic is written.
The server marks every nonstandard inherited descriptor close-on-exec in the forked child except the private startup gate.
The hidden wrapper closes that gate before relay exec, so a descriptor opened by another server thread and the private gate itself cannot reach relay code.
The server releases the gate only after manager-failure monitoring is installed and manager ownership of the observed lifetime and private directory is committed.
The gate is not part of the JSONL relay protocol, and the manager's private control channel never enters the sandbox.

The relay creates the worker's private full-duplex sideband socket pair and its standard-input, standard-output, and standard-error pipes after entering the sandbox.
It passes one worker sideband endpoint through `MCP_CONSOLE_SIDEBAND_FD` together with the fd-0/1/2 contract documented in [`WORKER_PROTOCOL.md`](WORKER_PROTOCOL.md).

The relay owns the direct worker process and its local transports, translation between this protocol and the worker sideband, direct-worker signal delivery, deadline-bounded direct-worker termination, cleanup of remaining members of its worker process group, and direct-worker reaping.
The host-side manager owns primary tracking and termination of the relay root and observed descendants across process-group and session changes, along with private-directory cleanup.
The server retains the directory-creation guard until manager readiness, then relinquishes it while keeping the manager monitor and the path needed for successful pre-commit recovery.
Once commitment begins, the manager is the sole directory-cleanup owner.
The server owns generation state and host-side dependency resolution; see [Requirements and environments](REQUIREMENTS.md) for that trust boundary.

## Framing and raw bytes

Each direction is an ordered UTF-8 JSONL stream.
One frame is one JSON object followed by `\n`, and each frame is flushed after serialization.
A stream that closes midway through a frame is a transport error.

Raw worker standard-output and standard-error data is read in chunks of at most 8 KiB.
Each chunk is validated independently.
An entirely valid UTF-8 chunk is emitted as a readable JSON string in `stdout` or `stderr`.
An invalid UTF-8 chunk is encoded with padded standard base64 in `stdout_bytes` or `stderr_bytes`.
The relay does not carry incremental UTF-8 state across chunks, so a scalar split across reads can cause each affected chunk to use the byte form.
The server decodes byte-form chunks before applying its existing per-stream UTF-8 completion and MCP projection rules.
The relay does not impose line buffering or use a coalescing timer.
Worker-sideband text and stdin remain UTF-8 JSON strings.

## Server commands

The server can send these flat frames:

- `{"kind":"evaluate","language":"r","source":"1 + 1"}` sends the unchanged worker-sideband evaluation command.
- `{"kind":"prepare_r","library":"..."}` sends the unchanged live R-preparation command.
- `{"kind":"r_resolved","library":"..."}` returns one provisional host R-resolution result.
- `{"kind":"r_resolution_failed","failure":"host","message":"..."}` returns one host R-resolution failure; `failure` is `host`, `interrupted`, or `operation`.
- `{"kind":"prepare_python","packages":["py-yaml12"]}` asks the worker to perform explicit live reticulate preparation.
- `{"kind":"python_resolved","python":"..."}` returns one host Python-resolution result.
- `{"kind":"python_resolution_failed","message":"..."}` returns one host Python-resolution failure.
- `{"kind":"python_version_resolved","version":"3.12.11"}` returns one host Python-version result.
- `{"kind":"python_version_resolution_failed","message":"..."}` returns one host Python-version failure.
- `{"kind":"stdin","data":"..."}` encodes the JSON string as UTF-8 and appends the exact bytes to worker fd 0.
- `{"kind":"interrupt","request_id":1}` attempts `SIGINT` delivery to the live worker and correlates the result with the same request ID.
- `{"kind":"shutdown","grace_millis":1000}` closes worker stdin, sends the unchanged worker `shutdown` message, and stops the worker within the supplied grace period.

The relay translates semantic commands to the unchanged worker-sideband messages where applicable.
There is no nested `worker_message` envelope and no operation-result acknowledgment command.
Inline `send.control` uses these existing interrupt, shutdown, stdin, preparation, and evaluation frames; it adds no relay command or event kind.
Accepted `stdin` payloads contribute bytes to one unbuffered, generation-long worker fd-0 stream; they are not records.
The relay writes them in command order without adding bytes or applying line buffering.
Without inline control, the server completes any host and live requirement preparation, reserves the active evaluation, registers its worker operation, then queues `stdin` and `evaluate` through the same ordered command sender.
Empty stdin queues no relay command.
The resulting `stdin`-then-`evaluate` wire order is guaranteed, but consumption timing is runtime-dependent: an already outstanding idle fd-0 read may consume some or all of those bytes before the cell begins.
Line-oriented reads generally require an explicit newline, payload end is not EOF, and fd 0 remains open until its closure retires the worker generation.

For a worker-targeted inline interrupt, the server queues `interrupt` and waits for its matching successful `interrupt_result` before it queues nonempty same-call `stdin`.
The server then waits the 100-millisecond grace outside the relay.
If the earlier evaluation settles and the generation remains current, any live requirement-preparation commands follow stdin, and `evaluate` follows successful preparation.
If the evaluation remains active or interrupt delivery fails, no new `evaluate` command is sent.
Resolver-targeted interruption does not emit a relay `interrupt` frame, but the server preserves the same control, stdin, grace, requirements, and evaluation admission order.

For inline restart, the server resolves declared requirements before it closes the old generation.
The retiring relay receives the existing `shutdown` command and closes its worker stdin, discarding unread bytes with that generation.
After the replacement relay reports readiness, same-call `stdin` and `evaluate` are queued only to that replacement in their normal order.

## Relay events

The relay can emit these flat frames:

- `{"kind":"ready"}` reports completed worker startup.
- `{"kind":"console_output","data":"..."}` forwards ordinary worker console text.
- `{"kind":"console_diagnostic","data":"..."}` forwards diagnostic worker console text.
- `{"kind":"image","data":"...","mime_type":"image/png"}` forwards one worker image.
- `{"kind":"input_requested","prompt":"..."}` forwards a managed console-input request.
- `{"kind":"input_received"}` forwards successful managed input receipt.
- `{"kind":"input_cancelled"}` forwards managed input cancellation.
- `{"kind":"r_prepared","library":"..."}` completes live R preparation successfully.
- `{"kind":"r_preparation_failed","message":"..."}` completes live R preparation with an ordinary failure.
- `{"kind":"resolve_r","packages":["cli","glue"]}` requests host resolution of plain R package names.
- `{"kind":"r_activated","library":"..."}` reports that the worker accepted a provisional R library.
- `{"kind":"r_activation_failed","library":"...","message":"..."}` reports that the worker could not apply a provisional R library.
- `{"kind":"resolve_python","request":{"requirements":{"packages":["numpy","pandas"]},"retained_requirements":{"packages":["numpy","pandas"]}}}` requests host Python-environment resolution.
  For an inferred mapping, `request` may additionally contain `"import_resolution":{"module":"yaml12","distribution":"py-yaml12"}`.
- `{"kind":"resolve_python_version","request":{"constraints":[]}}` requests host Python-version selection.
- `{"kind":"python_activated","requirements":{"packages":["numpy","pandas"]}}` reports a retained managed-Python activation.
- `{"kind":"python_prepared"}` returns the worker's explicit Python-preparation success result, including before Python initialization.
- `{"kind":"python_preparation_failed","message":"..."}` completes live Python preparation with an ordinary failure.
- `{"kind":"completed"}` completes an evaluation.
- `{"kind":"stdout","data":"hello\n"}` carries one raw fd-1 chunk that is entirely valid UTF-8.
- `{"kind":"stderr","data":"..."}` carries one raw fd-2 chunk that is entirely valid UTF-8.
- `{"kind":"stdout_bytes","data":"/w=="}` carries one raw fd-1 chunk encoded as base64 because it is not valid UTF-8.
- `{"kind":"stderr_bytes","data":"/w=="}` carries one raw fd-2 chunk encoded as base64 because it is not valid UTF-8.
- `{"kind":"stdout_closed"}` marks the worker stdout reader's retirement boundary.
- `{"kind":"stderr_closed"}` marks the worker stderr reader's retirement boundary.
- `{"kind":"worker_sideband_closed"}` marks the worker-to-relay sideband's retirement boundary.
- `{"kind":"interrupt_result","request_id":1}` reports successful `kill(SIGINT)` delivery.
- `{"kind":"interrupt_result","request_id":1,"error":"..."}` reports failed `kill(SIGINT)` delivery.
- `{"kind":"shutdown_started"}` reports acceptance of the server's registered shutdown request.
- `{"kind":"worker_exited","code":33}` reports ordinary direct-worker exit with this status; it does not report completion of host-side sandbox cleanup.
- `{"kind":"worker_signaled","signal":9}` reports direct-worker signal termination; it does not report completion of host-side sandbox cleanup.
- `{"kind":"fatal","message":"..."}` reports relay infrastructure or protocol failure while relay stdout remains usable.

The [worker protocol](WORKER_PROTOCOL.md#nested-managed-r-resolution) defines runtime R resolution, failure classes, and activation ordering.
Its [Python request section](WORKER_PROTOCOL.md#python-request-objects) defines the complete nested Python request and manifest schemas represented above.
The relay preserves the optional `import_resolution` object unchanged.
Worker semantic events are the worker-sideband message variants flattened into the relay event namespace.
The relay translates them without changing the worker-sideband framing or message shapes.
It does not run host resolvers, track provisional candidates, interpret activation, or commit retained environments; those are server responsibilities.
It keeps no nested-resolver wait state and applies no special queueing to these frames.
Unknown event kinds and fields are rejected.
Payload-free events contain exactly the shown `kind` field: `ready`, `input_received`, `input_cancelled`, `python_prepared`, `completed`, `stdout_closed`, `stderr_closed`, `worker_sideband_closed`, and `shutdown_started` reject every additional field.
Their serialized JSON remains unchanged.

## Event production and ordering

Worker sideband, worker stdout, worker stderr, and direct-worker lifecycle each have one producer.
Each producer enqueues complete relay events into one multi-producer queue.
One serializer owns relay stdout, writes one complete JSONL frame at a time, and flushes each frame.
Frames therefore never interleave, and each source preserves its own order.

Ordering between different sources is the order in which their reader or direct-worker lifecycle threads enqueue events.
No chronological order is promised between the independent worker sideband, stdout, and stderr transports.
A mutex or queue cannot reconstruct the order in which the worker wrote to separate transports, and the protocol does not rely on mutex fairness.
In particular, raw output written before an operation-result sideband frame can be serialized after that result and remain pending for a later MCP response.

The relay does not classify operation results and never waits for a server acknowledgment before reading another worker-sideband frame.
It does not carry response cuts, output acknowledgments, pending-output budgets, or MCP response state.
Those are server concerns described conceptually in [Implemented architecture](ARCHITECTURE.md).

## Interruption and shutdown

When the server sends an `interrupt` command, the relay calls `kill(worker_pid, SIGINT)` and returns `interrupt_result`; the request ID matches that result to the caller.
Success means that the operating system accepted signal delivery, not that the worker has already handled the signal or stopped its current operation.
Host-resolver interruption requests do not cross this boundary as relay `interrupt` commands.
The server can still classify the resulting runtime R reply as `r_resolution_failed` with `failure` set to `interrupted`.
The server then performs the `send`-owned stdin enqueue and 100-millisecond grace before it observes the earlier evaluation or considers a new cell.
A control-only call returns the state and output visible after that grace.
The relay does not implement the grace or decide whether evaluation can proceed.

For restart or server shutdown, the server registers one relay-shutdown request and queues one `shutdown` command against the existing absolute one-second worker deadline.
The sole relay-command writer computes `grace_millis` from the time remaining when it serializes that command, so earlier queued writes cannot extend the worker deadline.
The server then enqueues an ordered retirement marker in its event dispatcher.
Events ahead of that marker remain subject to normal validation and dispatch; events after it cannot extend the old generation's ownership into its replacement.
The marker is server state and is not a relay frame or acknowledgment.
For inline restart, replacement stdin and evaluation admission occur only after this retirement boundary and replacement readiness, so no old-generation relay can receive them.

After parsing the command, the relay's direct-worker lifecycle producer flushes `shutdown_started` before it begins worker shutdown.
It has no request ID because each generation permits only the one shutdown request that the server registers before enqueueing the command.
The server rejects it when no shutdown request is registered or when the relay sends it twice.
If the server observes it by the original worker deadline, the event records timely relay acceptance and permits up to two additional seconds after that deadline for relay retirement.
This outer allowance does not extend the worker grace carried by the command.
For non-intentional startup or runtime failure, the server sends zero worker grace and grants the same bounded relay-retirement allowance without requiring timely acceptance.
The failure retirement marker and physical relay wait share one absolute two-second allowance measured from that zero-grace deadline.
This keeps the relay reader alive for drained raw output, stream closures, and the final process outcome before the outer fail-safe runs.

The relay closes worker stdin and sends the unchanged worker-sideband `shutdown` message without waiting for one path before attempting the other.
If the worker remains live at its deadline, the relay sends `SIGKILL` to that direct child.
After direct-worker exit or force-stop, the relay stops any remaining member of its worker process group and reaps the direct child.
The resulting `worker_exited` or `worker_signaled` event describes only that direct child; it is not a sandbox-lifetime retirement acknowledgment.
Clean relay-stdin EOF does not emit `shutdown_started`; it performs the same worker shutdown with a new one-second grace period measured from EOF.
EOF midway through a command frame is a transport failure instead.

The host-side manager tracks the relay root and every descendant identity it observes by PID and start time, retaining those identities across process-group and session changes.
After a normal relay exit, it waits for observed-tree cleanup and then closes the original relay process group as a backstop for a same-group fork that raced observation; the server retains the waitable relay and the manager revalidates its recorded identity first.
On forced retirement or server loss, it instead closes the group while the root identity is still available, signals the exact recorded relay, and then waits for observed-tree cleanup.
It adopts a private temporary-directory guard and removes the directory only after both cleanup steps succeed; a cleanup failure preserves the directory.
If the relay exits or crashes, the manager treats root exit as retirement of the remaining observed lifetime.
If the server exits or crashes, closure of the manager's owner channel makes the manager stop the relay root and complete the same cleanup independently.
If the manager exits unsuccessfully after readiness while the server still owns a live, waitable relay root, including during the commit acknowledgement, the server reconstructs bounded tracking from that root's current process tree and completes cleanup before replacement.
That fallback cannot recover a descendant that had already detached from the root's ancestry before the manager failed.

The server leaves an exited relay waitable until sandbox-lifetime cleanup completes, preserving the relay identity while retirement finishes.
It waits through the worker deadline and uses the additional two-second allowance only after timely `shutdown_started` acceptance or a pre-retirement failure.
It then asks the lifetime owner to stop and join the relay root and observed descendants and reaps the relay, including when the relay stalls or has already exited.
The background manager has a separate one-second cleanup timeout, and its owner allows one additional second for manager exit and reaping.
If the manager misses that allowance, the owner sends exact-identity `SIGKILL`, keeps the relay root waitable while it reaps the manager, and may use one additional one-second cleanup interval to reconstruct and retire the root's current process tree.
Those manager bounds can extend past the relay allowance when the outer stop begins only at that allowance's deadline.
The server does not start the replacement sandbox lifetime until that retirement barrier completes.
Concurrent or repeated retirement reuses the recorded result and never signals a retired PID or process group again.
Darwin cannot resolve every later fork atomically, so a descendant that becomes orphaned before its fork event is resolved remains outside the guarantee.
The private startup gate prevents either relay implementation from running during initial manager observation and ownership commitment.

## Retirement and failure

On worker exit or relay failure, the relay first stops the worker transports and drains and joins the stdout and stderr reader threads.
Before joining the worker-sideband writer, it shuts down only its local write half, interrupting an in-flight command even when a detached worker descendant retains the peer endpoint without reading.
The relay read half remains available for retirement draining.
Cancellation of an already-started worker-sideband reader drains every complete buffered or immediately readable frame with per-call nonblocking receives, then abandons an incomplete frame rather than waiting on a descendant that retained the endpoint.
If transport setup fails before that reader starts, the relay discards pending sideband frames so `fatal` remains the first semantic event; raw stdout and stderr are still drained.
It then emits `stdout_closed` and `stderr_closed`, the retained `fatal` event when present, `worker_sideband_closed`, and the structured worker process outcome when one is available.
No raw output can follow its stream-closure event, and no event can follow `worker_exited` or `worker_signaled`.
This preserves exact bytes and per-stream order through retirement.

Relay stdout EOF is a clean retirement only after the expected stream closures and final worker process outcome.
`worker_exited` distinguishes ordinary exit, including status zero, from `worker_signaled` signal termination.
Neither event says that the host-side manager has completed sandbox-lifetime cleanup.
Public rendering of these outcomes belongs to the server and is described at the console level in [Built-in runtime](BUILTIN_RUNTIME.md).

Malformed relay JSON, invalid byte-form base64, an unexpected command, a fatal event, or unexpected relay EOF fails the worker transport.
Worker-sideband EOF has its own relay event; other worker-sideband read failures become fatal relay events.
For a relay-owned protocol or I/O failure, the relay requests direct-worker termination immediately but retains the failure until the worker transports have stopped and the raw-output readers have drained and joined.
The server preserves that failure, processes the remaining closure and process-outcome events in order, and then waits for host-side sandbox-lifetime retirement before replacing the generation.
