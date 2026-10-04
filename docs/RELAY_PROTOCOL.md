# Server-relay protocol

This private interface connects the server to one generation's relay.
[`src/relay_protocol.rs`](../src/relay_protocol.rs) defines its frames; [`src/worker_relay.rs`](../src/worker_relay.rs) and [`src/worker_client/process.rs`](../src/worker_client/process.rs) implement the endpoints.
Windows uses the same JSONL frames with named pipes, process handles, and cooperative interrupt events; see [Windows execution](WINDOWS.md).
It has no independent negotiation; incompatible wire changes require a target-envelope version change.

## Process boundary

```text
server <--> [target transport] <--> [sandbox runner] <--> relay <--> worker
                                    lifetime owner      direct-child owner
```

The native runner inherits streams without proxying their contents.
Direct mode omits it; Docker and SBX still retain their outer-resource lifetime.
The relay need not be a sandbox root or process-group leader.
It owns the worker's standard streams, sideband pipes, direct-child signals and reaping, not dependency resolution, environment commits, or descendants outside that direct-child contract.

Relay stdin/stdout carry protocol traffic and have a single writer per direction.
The server's generation-owned writer serializes both target bootstrap and relay commands.
Forced retirement aborts that writer independently of its queue and stdout, closes command admission, and joins it before replacement.
An aborted partial frame ends that transport; Shutdown and Interrupt are never inserted into it.
Stderr is inherited and reserved for infrastructure diagnostics; when available, a framed `fatal` event is authoritative over best-effort stderr.
Unrelated descriptors are closed before launch.
The [worker protocol](WORKER_PROTOCOL.md) owns the inherited fd and worker-message contract; [sandbox integration](SANDBOX.md) owns native enforcement.

## Target launch envelope

SSH, Docker, and SBX wrap unchanged relay JSONL using [`src/target_launch.rs`](../src/target_launch.rs).
The current launch version is **11** for Docker/SBX and **10** for SSH, with matching Console package version required independently.
Version 11 requires conversion metadata in the Python identity returned by Docker/SBX runtime probes; version 10 peers are rejected before decoding those identities.
Version 10 distinguishes interrupted interpreter bootstrap from other incomplete setup.
Version 9 carries enabled languages captured on the controller; execution-host ambient values and workload policy cannot replace that selection.
Version 8 introduced the built-in interpreter-bootstrap completion event after transport readiness, preventing older target workers from leaving an admitted cell waiting indefinitely.
Increment launch compatibility for incompatible envelope or relay changes, even between development builds sharing a package version.
SSH preparation has its own protocol and connection.

Controller input begins with a four-byte unsigned big-endian length and at most 1 MiB of UTF-8 JSON bootstrap:

| Field                                    | Meaning                                                                                                          |
| ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `version`, `build`                       | Launch version and Console package version.                                                                      |
| `languages`                              | Controller-selected `r`, `python`, and `sql` booleans; omitted by private launch-only callers means all enabled. |
| `workspace`                              | Existing absolute execution-host directory.                                                                      |
| `policy`, `writable_roots`, `no_sandbox` | Captured policy, root array, and direct-launch selection.                                                        |
| `provider`                               | `native` by default, or `compute` for SBX.                                                                       |
| `environment`                            | Optional discovered capabilities and retained R/Python selections; required for prepared-target worker launch.   |
| `python`                                 | Optional Python selection for Docker/SBX probes only; rejected by SSH.                                           |

Consume exactly the bootstrap, forwarding every subsequent byte to relay stdin, including bytes received in the same read.
Validate compatibility before worker startup; relay `ready` does not substitute for this check.

Helper stdout frames contain a one-byte tag, four-byte unsigned big-endian length, and at most 64 KiB of payload:

| Tag | Payload and phase                                                                                                               |
| --- | ------------------------------------------------------------------------------------------------------------------------------- |
| `1` | JSON hello: `version`, `build`, optional authoritative `container_id` or `sandbox: {name, id}`.                                 |
| `2` | Raw relay stdout bytes; chunks need not align with JSONL frames. Invalid during probes.                                         |
| `3` | Terminal JSON `{confirmed: boolean, error: string or null}`, followed by EOF. Setup rejection may send this without a hello.    |
| `4` | Typed prepared-runtime result, exactly once after compatible hello during a Docker/SBX probe. Invalid for SSH or worker launch. |

The prepared descriptor rejects managed state, contradictory selections, unknown fields, and relative native paths.
It supports R-only, Python-only, and combined preinstalled runtimes.
Target paths remain opaque metadata on the controller; launch validates them again inside the target without rediscovery or fallback.
Unexpected stdout, bad versions, oversized/truncated frames, missing terminal confirmation, or trailing bytes are transport errors.
Diagnostics use stderr.

Docker/SBX ownership helpers consume a bounded owner request, create the resource, and forward its inner envelope through an outer envelope carrying authoritative resource identity.
Their terminal receipt confirms **outer resource removal**, not merely relay or inner-launcher exit.
The controller accepts a probe descriptor only after successful validation and confirmed removal, then reuses it with the captured image/template across generations.

SSH requires its remote helper's cleanup acknowledgment; local SSH exit is insufficient.
Copying preserves backpressure, while input-closure observation remains independent of blocked output.
Worker setup has a 30-second deadline separate from `send.timeout_ms`.
Provider creation/removal and native retirement have their own owners and bounds; no setup timeout proves cleanup after an undetected partition.
See [SSH](SSH.md), [Docker](DOCKER.md), and [SBX](DOCKER_SANDBOX.md) for placement-specific guarantees.

## Framing and raw bytes

Each relay direction is ordered UTF-8 JSONL, flushed per frame.
Unknown kinds/fields, wrong types, malformed JSON, and partial-frame EOF fail the transport.

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

The relay concurrently closes stdin and sends worker `shutdown`.
At the worker deadline it sends SIGKILL if needed, reaps the direct child, and retires transports.
Clean relay-input EOF performs shutdown with a fresh one-second grace but no `shutdown_started`; partial-command EOF is failure.
After the server aborts its command writer during retirement, the resulting partial-command EOF describes the abandoned transport and does not itself block replacement.
Other fatal failures, owned task joins, and confirmed launcher/provider cleanup still determine whether replacement is permitted.

The server's ordered retirement marker separates old-generation event ownership from replacement.
It is not a wire frame.
After the applicable relay deadline, the server requests launcher retirement with SIGTERM, allows six seconds before forced termination, then one second to observe exit.
Repeated retirement reuses its result rather than signaling an old PID again.
Forced launcher exit is not proof of descendant cleanup.
Remote/container adapters additionally require their own cleanup receipts.

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
Worker exit reports only the direct child, never native, remote, container, or VM cleanup.
Public failure/replacement notices belong to the [runtime guide](BUILTIN_RUNTIME.md#output-and-notices).
