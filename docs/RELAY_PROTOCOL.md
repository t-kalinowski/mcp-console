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
The framed event is authoritative; stderr diagnostics are best effort because the server's outer fail-safe can terminate a failed relay before its final diagnostic is written.

The relay creates the worker's two private sideband pipes and its standard-input, standard-output, and standard-error pipes after entering the sandbox.
It passes the same sideband environment variables and fd-0/1/2 contract that the worker used before the relay was introduced.
The worker protocol is unchanged and is documented in [`WORKER_PROTOCOL.md`](WORKER_PROTOCOL.md).

The server owns worker-generation state, evaluation and preparation admission, output projection, retained requirements, and host resolvers.
R, Python, and DuckDB requirement resolution remains outside the sandbox.
For live Python preparation, the server resolves candidate environments, while the worker owns reticulate manifest materialization and activation.
Python resolver events carry only requirement manifests or version constraints, not environment settings.
The server validates managed packages as named PEP 508 registry requirements and applies the trusted resolver configuration captured at server startup, so evaluated code cannot configure host resolution through the relay.
It accepts managed Python version numbers and supported PEP 440 comparison specifiers, rejecting interpreter selectors before starting a host resolver.
A nonempty user-selected `RETICULATE_PYTHON` value disables managed Python requirements for the built-in worker; the existing custom-worker policy is separate and also rejects managed Python requirements.
The relay owns the worker process and its local transports, translation between this protocol and the worker sideband, signal delivery, bounded termination, and reaping.

## Framing and raw bytes

Each direction is an ordered UTF-8 JSONL stream.
One frame is one JSON object followed by `\n`, and each frame is flushed after serialization.
A stream that closes midway through a frame is a transport error.

The worker sideband is also assembled incrementally with cancellable reads.
A dedicated thread cannot safely use a blocking `read_line()`: a worker descendant can retain the pipe after writing a partial frame, which would prevent the relay from joining that thread and flushing its final output and retirement events.
Killing the relay to release the read would discard events already buffered inside it.

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

| Frame | Meaning |
| --- | --- |
| `{"kind":"evaluate","language":"r","source":"1 + 1"}` | Send the unchanged worker-sideband evaluation command. |
| `{"kind":"prepare_r","library":"..."}` | Send the unchanged live R-preparation command. |
| `{"kind":"prepare_python","packages":["py-yaml12"]}` | Ask the worker to perform explicit live reticulate preparation. |
| `{"kind":"python_resolved","python":"..."}` | Return one host Python-resolution result. |
| `{"kind":"python_resolution_failed","message":"..."}` | Return one host Python-resolution failure. |
| `{"kind":"python_version_resolved","version":"3.12.11"}` | Return one host Python-version result. |
| `{"kind":"python_version_resolution_failed","message":"..."}` | Return one host Python-version failure. |
| `{"kind":"stdin","data":"..."}` | Encode the JSON string as UTF-8 and append the exact bytes to worker fd 0. |
| `{"kind":"interrupt","request_id":1}` | Attempt `SIGINT` delivery to the live worker and report the system-call result with the same request ID. |
| `{"kind":"shutdown","grace_millis":1000}` | Close worker stdin, send the unchanged worker `shutdown` message, and stop the worker within the supplied grace period. |

The relay translates semantic commands to the unchanged worker-sideband messages where applicable.
There is no nested `worker_message` envelope and no operation-result acknowledgment command.
Accepted `stdin` payloads contribute bytes to one unbuffered, generation-long worker fd-0 stream; they are not records.
The relay writes them in command order without adding bytes or applying line buffering.
Line-oriented reads generally require an explicit newline, payload end is not EOF, and fd 0 remains open until its closure retires the worker generation.

## Relay events

The relay can emit these flat frames:

| Frame | Meaning |
| --- | --- |
| `{"kind":"ready"}` | The worker completed startup. |
| `{"kind":"console_output","data":"..."}` | Forward ordinary worker console text. |
| `{"kind":"console_diagnostic","data":"..."}` | Forward diagnostic worker console text. |
| `{"kind":"image","data":"...","mime_type":"image/png"}` | Forward one worker image. |
| `{"kind":"input_requested","prompt":"..."}` | Forward a managed console-input request. |
| `{"kind":"input_received"}` | Forward successful managed input receipt. |
| `{"kind":"input_cancelled"}` | Forward managed input cancellation. |
| `{"kind":"r_prepared","library":"..."}` | Complete live R preparation successfully. |
| `{"kind":"r_preparation_failed","message":"..."}` | Complete live R preparation with an ordinary failure. |
| `{"kind":"resolve_python","request":{"requirements":{"packages":["numpy","pandas"]},"retained_requirements":{"packages":["numpy","pandas"]}}}` | Request host Python-environment resolution. |
| `{"kind":"resolve_python_version","request":{"constraints":[]}}` | Request host Python-version selection. |
| `{"kind":"python_activated","requirements":{...}}` | Report a retained managed-Python activation. |
| `{"kind":"python_prepared"}` | Return the worker's explicit Python-preparation success result, including before Python initialization. |
| `{"kind":"python_preparation_failed","message":"..."}` | Complete live Python preparation with an ordinary failure. |
| `{"kind":"completed"}` | Complete an evaluation. |
| `{"kind":"stdout","data":"hello\n"}` | One raw fd-1 chunk that is entirely valid UTF-8. |
| `{"kind":"stderr","data":"..."}` | One raw fd-2 chunk that is entirely valid UTF-8. |
| `{"kind":"stdout_bytes","data":"/w=="}` | One raw fd-1 chunk encoded as base64 because it is not valid UTF-8. |
| `{"kind":"stderr_bytes","data":"/w=="}` | One raw fd-2 chunk encoded as base64 because it is not valid UTF-8. |
| `{"kind":"stdout_closed"}` | The worker stdout reader reached its retirement boundary. |
| `{"kind":"stderr_closed"}` | The worker stderr reader reached its retirement boundary. |
| `{"kind":"worker_sideband_closed"}` | The worker-to-relay sideband reached its retirement boundary. |
| `{"kind":"interrupt_result","request_id":1}` | The `kill(SIGINT)` system call succeeded. |
| `{"kind":"interrupt_result","request_id":1,"error":"..."}` | The `kill(SIGINT)` system call failed. |
| `{"kind":"shutdown_started"}` | The relay accepted the server's registered shutdown request. |
| `{"kind":"worker_exited","code":33}` | The direct worker exited normally with this status. |
| `{"kind":"worker_signaled","signal":9}` | The direct worker terminated because of this signal. |
| `{"kind":"fatal","message":"..."}` | Relay infrastructure or protocol failure while relay stdout remains usable. |

Worker semantic events are the worker-sideband message variants flattened into the relay event namespace.
The relay translates them without changing the worker-sideband framing or message shapes.
Unknown event kinds and fields are rejected.
Payload-free events contain exactly the shown `kind` field: `ready`, `input_received`, `input_cancelled`, `python_prepared`, `completed`, `stdout_closed`, `stderr_closed`, `worker_sideband_closed`, and `shutdown_started` reject every additional field.
Their serialized JSON remains unchanged.

## Event production and ordering

Worker sideband, worker stdout, worker stderr, and worker lifecycle or supervision each have one producer.
Each producer enqueues complete relay events into one multi-producer queue.
One serializer owns relay stdout, writes one complete JSONL frame at a time, and flushes each frame.
Frames therefore never interleave, and each source preserves its own order.

Ordering between different sources is the order in which their reader or supervision threads enqueue events.
No chronological order is promised between independent worker sideband, stdout, and stderr pipes.
A mutex or queue cannot reconstruct the order in which the worker wrote to separate pipes, and the protocol does not rely on mutex fairness.
In particular, raw output written before an operation-result sideband frame can be serialized after that result and remain pending for a later MCP response.

The relay does not classify operation results and never waits for the server before reading the next worker-sideband frame.
The server's relay stdout reader only parses and enqueues events, including EOF and transport failures.
It continues draining relay stdout while semantic dispatch blocks on a host resolver.
`WorkerOperationState` separately owns the active evaluation or preparation, retained transport failure, and outstanding idle input.
One ordered semantic dispatcher consumes the event queue.
When it sees `completed`, `r_prepared`, `r_preparation_failed`, `python_prepared`, or `python_preparation_failed`, it commits the operation result and its output boundary before applying the next queued event.
Idle output, later resolver requests, EOF, and transport failures pass through the same ordered dispatcher instead of changing operation state from the reader.
`python_prepared` always terminates an explicit worker-side `prepare_python` operation.
Before Python initializes, it reports successful reticulate manifest materialization; after initialization, it follows any required worker-owned activation.

## Interruption and shutdown

Host resolvers remain server processes with their existing independent process groups.
A session interrupt targets an active host resolver first.
Otherwise the server sends an `interrupt` command to the relay, which calls `kill(worker_pid, SIGINT)` and returns `interrupt_result`; the request ID matches that result to the caller.
Success means that the operating system accepted signal delivery, not that the worker has already handled the signal or stopped its current operation.

For restart or server shutdown, the server registers one relay-shutdown request and queues one `shutdown` command against the existing absolute one-second worker deadline.
The sole relay-command writer computes `grace_millis` from the time remaining when it serializes that command, so earlier queued writes cannot extend the worker deadline.
The server queues this shutdown command before cancelling a nested host resolver.
After cancellation releases the resolver callback, the server enqueues an ordered retirement marker and waits for the dispatcher to reach it only until the original worker deadline.
Semantic events queued ahead of the marker are still validated and dispatched in order.
When restart reuses the retained environment, successful old-generation R and Python preparation results and managed-Python activations commit before replacement.
When restart has already committed a newly resolved environment, a typed lifecycle disposition discards those successful old-generation environment commits instead, and a discarded preparation reports restart cancellation to its caller.
Worker-reported preparation failures and protocol, resolver, or environment-commit failures remain failures.
That marker releases the operation caller and leaves a typed tombstone that consumes a matching late operation result without committing it.
Expected resolver responses, activation events, and transport fallout after the marker do not turn an intentional retirement into a worker failure.

After parsing the command, the relay supervision producer flushes `shutdown_started` before it begins worker shutdown.
It has no request ID because each generation permits only the one shutdown request that the server registers before enqueueing the command.
The server rejects it when no shutdown request is registered or when the relay sends it twice.
If the server observes it by the original worker deadline, the event records timely relay acceptance and permits up to two additional seconds after that deadline for relay retirement.
This outer allowance does not extend the worker grace carried by the command.
For non-intentional startup or runtime failure, the server sends zero worker grace and grants the same bounded relay-retirement allowance without requiring timely acceptance.
The failure retirement marker and physical relay wait share one absolute two-second allowance measured from that zero-grace deadline.
This keeps the relay reader alive for drained raw output, stream closures, and the final process outcome before the outer fail-safe runs.

The relay closes worker stdin and sends the unchanged worker-sideband `shutdown` message without waiting for one path before attempting the other.
If the worker remains live at its deadline, the relay first sends `SIGKILL` to the direct worker, then repeatedly stops every other live process whose current process group is exactly the relay's group while leaving the relay alive as group leader, and finally reaps the direct worker.
Clean relay-stdin EOF does not emit `shutdown_started`; it performs the same worker shutdown with a new one-second grace period measured from EOF.
EOF midway through a command frame is a transport failure instead.

The server observes direct relay exit without reaping it, so the waitable relay PID continues to pin the process-group identity.
It waits through the original deadline and uses the additional two-second retirement allowance only after timely `shutdown_started` acceptance or an ordered pre-retirement failure.
Failure retirement starts with an expired worker deadline but always uses that bounded allowance.
Intentional retirement waits for ordered dispatcher catch-up only through the original deadline; absent a retained pre-marker failure, it does not receive extra time merely because resolver cancellation or dispatcher work was slow.
It then always closes the complete sandbox process-group lifetime and reaps the relay, including when the relay already exited during either wait.
That outer cleanup is also the fail-safe when the relay does not accept shutdown or stalls, so an in-group descendant cannot survive the sandbox generation.
The sandbox owner records that retirement before releasing the process-group identity; concurrent or repeated retirement calls return the stored result and never signal the retired relay PID or process group again.
Descendants that leave the group remain unsupported.

## Retirement and failure

On worker exit or relay failure, the relay first stops the worker transports and drains and joins the stdout and stderr reader threads.
Cancellation of an already-started worker-sideband reader drains every complete buffered or immediately readable frame, then abandons an incomplete frame rather than waiting on a descendant that retained the pipe.
If transport setup fails before that reader starts, the relay discards pending sideband frames so `fatal` remains the first semantic event; raw stdout and stderr are still drained.
It then emits `stdout_closed` and `stderr_closed`, the retained `fatal` event when present, `worker_sideband_closed`, and the structured worker process outcome when one is available.
No raw output can follow its stream-closure event, and no event can follow `worker_exited` or `worker_signaled`.
This preserves exact bytes and per-stream order through retirement.

Relay stdout EOF is a clean retirement only after the expected stream closures and final worker process outcome.
`worker_exited` distinguishes ordinary exit, including status zero, from `worker_signaled` signal termination.
For unexpected termination of an established worker, the public MCP response includes `[worker exited with status N]` or `[worker terminated by signal N]` before the existing stopped and replacement notices.
An unexpected pre-ready exit adds the same diagnostic to its startup failure.
An intentional restart or server shutdown suppresses the public crash diagnostic; the relay still reports the structured outcome on its private stream.

Malformed relay JSON, invalid byte-form base64, an unexpected command, a fatal event, or unexpected relay EOF fails the worker transport.
Worker-sideband EOF has its own relay event; other worker-sideband read failures become fatal relay events.
For a relay-owned protocol or I/O failure, the relay requests worker termination immediately but retains the failure until the worker transports have stopped and the raw-output readers have drained and joined.
The server preserves that failure, processes the remaining closure and process-outcome events in order, and then retires the generation.
