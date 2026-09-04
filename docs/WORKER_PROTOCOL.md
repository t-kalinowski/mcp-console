# Worker protocol

This document specifies the implemented protocol between one worker relay and one worker process, including the contract for a custom worker supplied through the hidden `serve --worker PATH` development option.

The protocol is current implementation documentation, not a public, versioned interface.
Its sources of truth are:

- `src/worker_protocol.rs` for message definitions;
- `src/sideband.rs` for framing and inherited descriptors; and
- `src/worker_relay.rs` for launch, standard streams, forwarding, and closure.

The relay forwards the semantic messages in this document between the server and worker without changing their JSON shapes.
The separate server-relay JSONL protocol is specified in [`RELAY_PROTOCOL.md`](RELAY_PROTOCOL.md).

Outside this wire contract, [`ARCHITECTURE.md`](ARCHITECTURE.md) owns process placement and lifecycle, [`BUILTIN_RUNTIME.md`](BUILTIN_RUNTIME.md) owns console behavior, [`REQUIREMENTS.md`](REQUIREMENTS.md) owns dependency preparation, and [`TOOL_DESCRIPTIONS.md`](TOOL_DESCRIPTIONS.md) mirrors the registered MCP descriptions.
Process-boundary test guidance lives in [`../tests/boundaries/README.md`](../tests/boundaries/README.md).

Direction labels below use the logical `server` and `worker` endpoints.
The relay translates matching server-relay commands and events at the transport boundary.

## Scope

One sideband belongs to one worker generation.
The server admits at most one evaluation or explicit live preparation at a time.
A server-managed worker may also make at most one synchronous nested R or Python resolver request at a time.

The sideband carries complete cells, live R and Python preparation, managed R and Python resolution, console text, images, managed-input observations, readiness, evaluation completion, and shutdown.

Worker fd 0 carries interactive input as one generation-long byte stream.
Worker fd 1 and fd 2 carry independent raw output streams.
They are not sideband messages.

The sideband has no protocol negotiation, capability exchange, session name, request ID, general structured error, poll command, output acknowledgment, or interrupt command.
Interrupt delivery is a process signal managed by the relay.
Response cuts, output budgets, and MCP response assembly are server state and never appear on this boundary.

The sideband transport implementation is compiled for macOS and Linux.
The complete protocol execution stack is currently supported only on macOS because the worker relay and sandbox runtime are macOS-only.

## Launch contract

For every worker generation, the sandboxed relay launches the configured worker with piped standard input, standard output, and standard error.
The relay is already inside the worker sandbox, and the worker inherits that sandbox and its process group.
The built-in command is `mcp-console worker`.
The hidden `serve --worker PATH` option uses `PATH` as one executable name or path, without arguments or shell parsing.

Before spawning the worker, the relay creates one unnamed Unix-domain stream socket pair and places the worker endpoint number in its environment:

```yaml
MCP_CONSOLE_SIDEBAND_FD: <worker reads and writes sideband messages here>
```

The relay clears `FD_CLOEXEC` on the worker endpoint for the spawn, then drops its local copy of that endpoint immediately after spawning.
The worker takes ownership of the inherited file descriptor.

Before executing descendants or evaluated code, a worker must:

1. remove the sideband environment variable;
2. set `FD_CLOEXEC` on the descriptor or otherwise prevent exec descendants from inheriting it; and
3. close the descriptor in fork-only descendants.

A fork child must close its inherited descriptor rather than call `shutdown()`.
Shutdown would affect the socket endpoint shared with the parent process.

Keeping a sideband endpoint open in a descendant can prevent the relay from observing closure and is outside the contract.
Descendants may retain fd 1 or fd 2; their bytes remain part of the worker generation's captured standard streams.

## Transport

### Sideband framing

The sideband is one full-duplex ordered byte connection with separate logical reader and writer halves in each process:

```text
relay writer  ──>  worker reader
relay reader  <──  worker writer
```

Reads and writes proceed independently and remain blocking during normal operation.

Each frame is one UTF-8 JSON object followed by line feed (`\n`).
A sender flushes every frame.
JSON escaping represents embedded newlines and other control characters; frames themselves never interleave.

The current implementation has no general sideband frame-size limit.
A sender must nevertheless produce one complete frame.
Closure after partial JSON is a transport failure.

Every message uses `kind` as its variant tag.
Unknown message kinds, unknown fields, invalid UTF-8, malformed JSON, and fields of the wrong type are protocol violations.
Nested request and manifest objects also reject unknown fields.

`shutdown`, `ready`, `input_received`, `input_cancelled`, `python_prepared`, and `completed` are payload-free: their frames contain exactly `kind` and reject every additional field.

Each sideband direction preserves frame order.
There is no ordering guarantee between the two directions.
The relay continuously reads worker frames after readiness, including while the worker is idle and after an operation result.
There is no result acknowledgment that pauses the relay.

### Standard input

Worker fd 0 is one unbuffered byte stream for the generation.
The server UTF-8 encodes each accepted `stdin` string, and the relay appends those bytes without inspection, line buffering, framing, echo, or an added newline.
Empty input queues no bytes.
Payload end is not EOF, and there is no sideband input frame.
The current transport imposes no stdin queue-size limit.

For a call containing both nonempty stdin and a cell, the relay queues the stdin bytes before it queues the `evaluate` frame.
This is a transport ordering rule, not an acknowledgment that the runtime consumed those bytes.
An already outstanding read may consume them before the cell begins.
Line-oriented reads usually require an explicit newline.

Fd 0 remains open across calls and evaluations.
Closing it retires the worker generation and discards unread input.

### Standard output and standard error

Worker fd 1 and fd 2 are independent raw byte streams.
The relay reads and forwards arbitrary bytes without line buffering.
The readable UTF-8 versus base64 representation used on the outer JSONL stream belongs to [`RELAY_PROTOCOL.md`](RELAY_PROTOCOL.md), not to the worker sideband.

Each standard stream preserves its own byte order.
The worker sideband, stdout, and stderr are independent transports, so there is no chronological cross-source ordering guarantee.
In particular, raw output written before a `completed` or preparation-result frame may be observed after that frame.

## Message schemas

### Server to worker

| Frame                                                             | Required meaning                                                                          |
| ----------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `{"kind":"evaluate","language":"r","source":"..."}`               | Evaluate one complete source string. `language` is `r`, `python`, or `sql`.               |
| `{"kind":"prepare_r","library":"..."}`                            | Apply this resolved R library to the live R search path.                                  |
| `{"kind":"r_resolved","library":"..."}`                           | Return the provisional library selected for the current `resolve_r` request.              |
| `{"kind":"r_resolution_failed","failure":"host","message":"..."}` | Fail the current `resolve_r` request. `failure` is `host`, `interrupted`, or `operation`. |
| `{"kind":"prepare_python","packages":["py-yaml12"]}`              | Add these package requirements through the live managed-Python preparation operation.     |
| `{"kind":"python_resolved","python":"..."}`                       | Return the interpreter path selected for the current `resolve_python` request.            |
| `{"kind":"python_resolution_failed","message":"..."}`             | Return an ordinary failure for the current `resolve_python` request.                      |
| `{"kind":"python_version_resolved","version":"3.12.11"}`          | Return the version selected for the current `resolve_python_version` request.             |
| `{"kind":"python_version_resolution_failed","message":"..."}`     | Return an ordinary failure for the current version request.                               |
| `{"kind":"shutdown"}`                                             | Exit without a sideband reply.                                                            |

### Worker to server

| Frame                                                                                                                                          | Required meaning                                                               |
| ---------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `{"kind":"ready"}`                                                                                                                             | Startup is complete.                                                           |
| `{"kind":"console_output","data":"..."}`                                                                                                       | Publish ordinary console text.                                                 |
| `{"kind":"console_diagnostic","data":"..."}`                                                                                                   | Publish diagnostic console text.                                               |
| `{"kind":"image","data":"...","mime_type":"image/png"}`                                                                                        | Publish one base64-encoded image.                                              |
| `{"kind":"input_requested","prompt":"..."}`                                                                                                    | Report that a managed console read is about to wait for input.                 |
| `{"kind":"input_received"}`                                                                                                                    | Report that the outstanding managed read succeeded.                            |
| `{"kind":"input_cancelled"}`                                                                                                                   | Report that interruption cancelled the outstanding managed read.               |
| `{"kind":"r_prepared","library":"..."}`                                                                                                        | Complete `prepare_r` successfully with the applied library path.               |
| `{"kind":"r_preparation_failed","message":"..."}`                                                                                              | Complete `prepare_r` with an ordinary live-update failure.                     |
| `{"kind":"resolve_r","packages":["cli","glue"]}`                                                                                               | Request host resolution of these plain R package names.                        |
| `{"kind":"r_activated","library":"..."}`                                                                                                       | Report that the worker accepted this resolved R library.                       |
| `{"kind":"r_activation_failed","library":"...","message":"..."}`                                                                               | Report that the worker could not apply this resolved R library.                |
| `{"kind":"resolve_python","request":{"requirements":{"packages":["numpy","pandas"]},"retained_requirements":{"packages":["numpy","pandas"]}}}` | Request host resolution of one managed-Python environment.                     |
| `{"kind":"resolve_python_version","request":{"constraints":[]}}`                                                                               | Request host Python-version selection.                                         |
| `{"kind":"python_activated","requirements":{"packages":["numpy","pandas"]}}`                                                                   | Report that the worker accepted this complete logical managed-Python manifest. |
| `{"kind":"python_prepared"}`                                                                                                                   | Complete explicit Python preparation successfully.                             |
| `{"kind":"python_preparation_failed","message":"..."}`                                                                                         | Complete explicit Python preparation with an ordinary failure.                 |
| `{"kind":"completed"}`                                                                                                                         | Complete the current evaluation.                                               |

`console_output` and `console_diagnostic` remain distinct on this boundary.
Their MCP projection is described in [`BUILTIN_RUNTIME.md`](BUILTIN_RUNTIME.md).

`image.data` must be valid base64.
`mime_type` is passed through as the image MIME type.
A worker must send the complete image before the operation result if the image belongs to that operation.

### Python request objects

A Python requirement manifest has this shape:

```json
{
  "packages": ["numpy", "pandas", "py-yaml12"],
  "python_version": [">=3.11"],
  "exclude_newer": "2026-01-01"
}
```

`packages` is required.
`python_version` and `exclude_newer` are optional and are omitted when empty or absent.

`resolve_python.request` contains two required manifests:

- `requirements` is the physical manifest submitted to the host resolver; and
- `retained_requirements` is the logical manifest to retain after activation.

Their `packages` and `exclude_newer` values must match.
Only `python_version` may differ, allowing physical resolution against an exact active Python patch version while preserving a logical constraint.
The server validates both manifests and their requirement syntax before starting a resolver.

An automatic import may also include `import_resolution` with `module` and `distribution` strings.
The module must be a top-level ASCII Python identifier, the distribution must be a different bare package name present in both manifests, and this metadata is valid only during an evaluation.
The server associates valid metadata with the provisional environment and emits a bounded notice only if the matching `python_activated` event commits it for the current generation.

`resolve_python_version.request.constraints` is a required array of version constraints.
A successful version reply creates no environment candidate and requires no `python_activated` receipt.

These messages carry no environment map.
Host configuration and accepted requirement syntax are specified in [`REQUIREMENTS.md`](REQUIREMENTS.md).

## Readiness and operations

### Readiness

The first worker-sideband message must be `ready`.
The server sends no evaluation or preparation before receiving it.
Startup text may use fd 1 or fd 2, but no semantic worker frame may precede `ready`.

`ready` is sent exactly once.
A second `ready` is a protocol violation.

### Evaluation

An evaluation begins with one `evaluate` frame while the worker is idle.
The `source` string is one complete cell; the protocol has no continuation message.

During the evaluation, the worker may send zero or more console, image, input, R-resolver, R-activation, Python-resolver, Python-version-resolver, or Python-activation messages.
An ordinary language result ends with exactly one `completed` frame.
`completed` carries no payload and no Python manifest.

All sideband output belonging to the evaluation must precede `completed`.
Later sideband output is idle activity.
Raw fd-1 or fd-2 bytes remain subject to the independent-source ordering rule above.

Language parse errors, runtime errors, Python exceptions, and SQL backend errors are ordinary console results: a worker reports their text and then `completed` if it remains usable.
The protocol has no structured language-error message.
Infrastructure failure instead closes or invalidates the worker boundary.

An operation result without the matching active operation, or the wrong result for that operation, is a protocol violation.
New code is not admitted until the previous evaluation result has been collected by the server.
Console and image frames may also be emitted while idle; the relay reads them without a poll.
Sideband order is authoritative only for sideband content, not relative to fd 1 or fd 2.

### Managed input

A managed runtime read sends `input_requested` immediately before it waits on fd 0.
Its `prompt` is the exact runtime prompt, including trailing spaces or an empty string.
The frame observes a read; it does not grant permission to send input, and input may already be queued.

After the read succeeds, the worker sends `input_received` before resuming the runtime.
If interruption cancels it, the worker sends `input_cancelled` before unwinding the runtime.

Only one managed input request may be outstanding.
A second request, a terminal input frame without a request, or `completed` before the terminal input frame is a protocol violation.

Managed input may be requested while idle or evaluating.
A request during noninteractive R or Python preparation fails the preparation and the worker.
Code that reads fd 0 directly emits no input frames.

### Live R preparation

`prepare_r` is an idle-only operation.
The worker applies the supplied resolved library, preserving the rest of its supported live state, and replies with exactly one of:

```text
server -> worker  {"kind":"prepare_r","library":"..."}
worker -> server  {"kind":"r_prepared","library":"..."}
# or
worker -> server  {"kind":"r_preparation_failed","message":"..."}
```

For success, `r_prepared.library` must equal the requested normalized library path.
Any different path is a protocol violation.
`r_preparation_failed` is an ordinary operation result; an infrastructure failure instead closes the worker boundary.

### Nested managed-R resolution

Runtime R resolution is distinct from the idle-only `prepare_r` operation.
During an evaluation or idle runtime callback, a worker may send one `resolve_r` request containing plain package names and wait synchronously for `r_resolved` or `r_resolution_failed`.
The built-in worker uses this automatically; a custom worker may opt into the same callback contract.
The server validates the names again and resolves the complete retained R environment outside the worker sandbox.

An `r_resolved` library is provisional.
After the worker adds it to the live R library search path, it sends `r_activated` before continuing the original package load.
The server owns candidate tracking and commits the retained environment only for a matching activation from the current generation.
It discards unactivated or stale candidates on activation failure, operation completion, retirement, or generation replacement.
A package's later load failure does not undo an environment already reported as activated.

If applying the library fails, the worker sends `r_activation_failed` before propagating the R error.
The server discards that candidate and records the current generation's restart-required requirement state; the activation failure is not itself a worker-transport failure.

For an idle callback, the server atomically reserves environment-change ownership from `resolve_r` admission through `r_activated` or `r_activation_failed`.
Explicit environment preparation cannot enter that interval and returns a nonfatal tool error.
If explicit preparation owns the reservation before the idle request arrives, the server replies with an ordinary `host` failure without invoking the resolver.
It queues that reply before the explicit live-preparation command.
Once `prepare_r` or `prepare_python` has begun, a worker-originated runtime R callback is instead an out-of-phase protocol failure.

`r_resolution_failed.failure` classifies the response:

- `host` is an ordinary request-validation, requirement-state, or host-resolver failure;
- `interrupted` means the host resolver was explicitly interrupted; and
- `operation` means lifecycle cancellation or another operation-level failure and ends the affected worker boundary.

Transport, framing, sideband, and unexpected-response failures still fail the worker boundary rather than becoming host failures.

### Live Python preparation

`prepare_python` is an idle-only operation for a server-managed worker.
The worker performs additive preparation and replies with exactly one `python_prepared` or `python_preparation_failed` result.

Preparation may make nested `resolve_python` and `resolve_python_version` requests.
A typical live activation is:

```text
server -> worker  {"kind":"prepare_python","packages":["py-yaml12"]}
worker -> server  {"kind":"resolve_python","request":{...}}
server -> worker  {"kind":"python_resolved","python":"..."}
worker -> server  {"kind":"python_activated","requirements":{...}}
worker -> server  {"kind":"python_prepared"}
```

`python_prepared` is payload-free.
Before Python initialization it may report successful manifest materialization without a live `python_activated` event.
After initialization, any new resolved environment that the worker activates must be reported with `python_activated` before `python_prepared`.

### Nested managed-Python resolution

A server-managed worker may send `resolve_python` or `resolve_python_version` during an evaluation, preparation, or idle runtime callback.
The request may come from an evaluated Python import through the built-in private bridge, a reticulate API, or R package behavior.
It then waits for exactly one matching success or failure reply.

Every successful `python_resolved` reply is provisional.
When the live runtime accepts that environment, the worker sends `python_activated` with the complete normalized logical manifest.
The manifest must match a resolved candidate or the unchanged current managed environment.
Activation is reported before the enclosing operation result.
For automatic import resolution, `python_activated` is sent before the original import resumes.
A later missing-module or language error does not undo that accepted environment.

An explicit pre-initialization preparation may instead materialize the last resolved candidate and finish with `python_prepared` without activation.
Other unmatched candidates are discarded when the enclosing operation ends.

Managed Python is unavailable to custom workers.
A custom worker must not send `python_activated`; doing so is a protocol failure.
It must not depend on host Python resolution, whose requests are rejected for custom workers.

A custom worker may opt into runtime managed-R resolution and the matching `r_activated` and `r_activation_failed` messages.
The server merges each request with the complete retained R environment and the custom worker's fixed R requirements.
It validates package names before invoking `ir`, so an invalid request receives an ordinary `host` rejection.
Activation messages remain subject to the same candidate and generation matching rules as the built-in worker.

### Synchronous resolver waits

There is no resolver request ID because only one nested R, Python-environment, or Python-version request may be outstanding.
While the built-in worker waits for its matching reply, an unrelated `evaluate` command may already be queued on the sideband.
The worker retains that command and processes it only after the nested request finishes.
An idle runtime R callback and explicit environment preparation are admitted atomically, so those two environment transitions do not queue behind each other.
`shutdown` terminates the wait and begins worker exit.
A response for another resolver kind, or any resolver response without its matching outstanding request, is a protocol failure.

## Ordering and valid results

The following table summarizes the required semantic transitions.
Console, image, and permitted nested resolver frames do not by themselves change the current phase.

| Current phase                  | Frame                                                  | Required result                      |
| ------------------------------ | ------------------------------------------------------ | ------------------------------------ |
| starting                       | worker -> server `ready`                               | idle                                 |
| idle                           | server -> worker `evaluate`                            | evaluating                           |
| idle                           | server -> worker `prepare_r`                           | preparing R                          |
| idle                           | server -> worker `prepare_python`                      | preparing Python                     |
| evaluating                     | worker -> server `completed`                           | idle                                 |
| preparing R                    | worker -> server `r_prepared` with the requested path  | idle                                 |
| preparing R                    | worker -> server `r_preparation_failed`                | idle                                 |
| preparing Python               | worker -> server `python_prepared`                     | idle                                 |
| preparing Python               | worker -> server `python_preparation_failed`           | idle                                 |
| idle or evaluating             | worker -> server `input_requested`                     | same phase, input outstanding        |
| input outstanding              | worker -> server `input_received` or `input_cancelled` | prior phase                          |
| idle or evaluating             | worker -> server `resolve_r`                           | same phase, nested R resolution      |
| nested R resolution            | server -> worker `r_resolved` or `r_resolution_failed` | prior phase                          |
| idle, evaluating, or preparing | worker -> server `resolve_python`                      | same phase, nested resolution        |
| idle, evaluating, or preparing | worker -> server `resolve_python_version`              | same phase, nested version selection |
| nested resolution              | server -> worker matching success or failure reply     | prior phase                          |
| any live phase                 | server -> worker `shutdown`                            | terminal                             |

`r_activated` or `r_activation_failed` may occur while idle or evaluating, but only for a matching provisional R library.
`python_activated` may occur while idle, evaluating, or preparing, but only for a matching managed environment.
An input request during preparation is an error, as described above.

The relay has independent producers for sideband, stdout, stderr, and direct-worker lifecycle.
It serializes their outer events without interleaving frames and preserves each producer's order.
That serialized observation order does not reconstruct chronology between the worker's independent transports.

## Shutdown and closure

For intentional restart or server shutdown, the relay concurrently closes worker fd 0 and attempts to send:

```json
{ "kind": "shutdown" }
```

The worker sends no acknowledgment.
It exits and lets process closure close its sideband and standard streams.
The protocol defines no general sideband half-close operation.
The shutdown frame may arrive while the worker is waiting for a nested resolver reply; it terminates that wait rather than acting as a resolver response.

Fd-0 closure and the `shutdown` frame are both generation-retirement signals, not evaluation or stdin-payload delimiters.
A worker must not require both in a particular order.
If it does not exit within the relay's supplied grace period, the relay forcibly terminates it.
After direct-worker exit or force-stop, the relay reaps the direct child and retires its local transports.
The sandbox launcher owns cleanup of remaining descendants, including those retaining worker descriptors or entering another process group or session.
The server requires successful managed launcher exit as the sandbox-lifetime retirement barrier before replacement.
The exact server-relay acceptance and retirement sequence is specified in [`RELAY_PROTOCOL.md`](RELAY_PROTOCOL.md).

During retirement, the relay forwards every complete worker-sideband frame already buffered or immediately readable.
It may abandon an incomplete frame held open by a descendant that retained the endpoint.
It drains fd 1 and fd 2 before reporting their outer stream closures and the direct worker process outcome.

Outside intentional retirement, worker-sideband EOF is a worker failure.
A worker must flush complete frames before exit; closure midway through a frame is a protocol failure.

## Protocol violations and failure handling

The following fail the worker boundary:

- malformed JSON or invalid worker-sideband UTF-8;
- unknown message kinds or fields;
- a payload on a payload-free message;
- an unexpected first message or repeated `ready`;
- a message or operation result in the wrong phase;
- an R success receipt for a different library;
- an invalid image base64 payload;
- inconsistent managed-input request and receipt ordering;
- an out-of-phase managed-R request or an activation receipt that does not match a provisional library;
- an invalid or unmatched managed-Python request or activation;
- worker-sideband I/O failure or unexpected closure; and
- unexpected worker exit, including exit status zero before intentional retirement completes.

The sideband has no general error reply.
The relay stops and reaps a failed worker, drains its raw output, and reports structured failure and process outcome events on the outer protocol.
Public failure and replacement behavior is described in [`BUILTIN_RUNTIME.md`](BUILTIN_RUNTIME.md).

## Custom-worker conformance

A conforming custom worker:

- accepts the inherited full-duplex sideband endpoint and fd-0/1/2 launch contract;
- removes the sideband bootstrap variable and prevents descendants from inheriting the descriptor;
- sends `ready` first and exactly once;
- accepts complete `evaluate` cells for the declared `r`, `python`, and `sql` language values and ends ordinary outcomes with `completed`;
- uses console, image, and managed-input frames with the exact schemas and ordering above;
- treats fd 0 as a generation-long byte stream;
- implements `prepare_r` when using explicit R or DuckDB requirements and returns the requested normalized library path;
- honors a prepared `R_LIBS`, applies its first resolved R library before loading DuckDB, and uses DuckDB's native extension cache;
- may opt into runtime managed-R resolution and activation messages using the exact candidate-confirmation contract above;
- does not use managed-Python activation messages;
- exits without replying to `shutdown`; and
- defines its own behavior for the process `SIGINT` sent by the relay.

The executable fixture under `tests/fixtures/zod` exercises successful evaluation, exact text and image frames, stdin, R preparation, interruption, protocol violations, standard streams, and bounded shutdown.
Its individual commands are fixture behavior, not additions to this protocol.
See [`../tests/boundaries/README.md`](../tests/boundaries/README.md) for the corresponding public process-boundary suites.
