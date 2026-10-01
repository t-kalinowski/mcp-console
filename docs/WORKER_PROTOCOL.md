# Worker protocol

This private interface connects one relay to one worker generation, including custom workers selected by `serve --worker PATH`.
[Architecture](ARCHITECTURE.md) explains ownership; [relay protocol](RELAY_PROTOCOL.md) defines the outer transport.
The schemas in [`src/worker_protocol.rs`](../src/worker_protocol.rs), framing in [`src/sideband.rs`](../src/sideband.rs), and executable boundary tests are authoritative.
There is no sideband version negotiation; incompatible changes must also update target-envelope compatibility.

## Launch and transport

The relay starts one executable with piped fd 0, 1, and 2 and two anonymous sideband pipes:

```text
MCP_CONSOLE_SIDEBAND_READ_FD   worker reads relay messages
MCP_CONSOLE_SIDEBAND_WRITE_FD  worker writes semantic events
```

The worker owns those endpoints.
Before user code or descendants run, remove both environment variables, set close-on-exec on both descriptors, and close them in fork-only children without disturbing the parent's endpoints.
A descendant retaining sideband endpoints violates the closure contract.
Descendants may retain stdout/stderr, subject to bounded retirement drainage.

Each sideband direction is ordered UTF-8 JSONL: one JSON object followed by `\n`, flushed after every frame.
Frames must not interleave.
There is no general frame-size limit.
Partial-frame closure, malformed JSON, invalid UTF-8, unknown kinds or fields, and wrong field types fail the boundary.
Nested objects also reject unknown fields; payload-free messages contain only `kind`.

Fd 0 is one generation-long byte stream, not records.
Accepted strings are UTF-8 encoded and appended without newline, echo, or line buffering; empty input adds nothing.
Bundled stdin precedes `evaluate` on the relay command stream, but an existing read can consume it before that cell starts.
Payload end is not EOF; fd-0 closure retires the generation and discards unread input.
There is no general stdin queue-size limit.

Fd 1 and fd 2 are independent raw byte streams.
Each source preserves its own order, but no chronological order exists across sideband, stdout, and stderr.
Raw output written before an operation result can be observed afterward.
Outer base64 encoding, backpressure, and drainage belong to the [relay](RELAY_PROTOCOL.md).

## Message schemas

Every frame has a string `kind` plus exactly the fields listed below.
`string[]` means an array of strings; an em dash means no payload.

### Server to worker

| Kind                                                           | Fields                                                              |
| -------------------------------------------------------------- | ------------------------------------------------------------------- |
| `evaluate`                                                     | `language`: `r`, `python`, or `sql`; `source`: string               |
| `prepare_r`, `r_resolved`                                      | `library`: string                                                   |
| `r_resolution_failed`                                          | `failure`: `host`, `interrupted`, or `operation`; `message`: string |
| `prepare_python`                                               | `packages`: string[]                                                |
| `python_resolved`                                              | `python`: string; optional `native`: inspected activation candidate |
| `python_resolution_failed`, `python_version_resolution_failed` | `message`: string                                                   |
| `python_version_resolved`                                      | `version`: string                                                   |
| `shutdown`                                                     | —                                                                   |

### Worker to server

| Kind                                                       | Fields                                                   |
| ---------------------------------------------------------- | -------------------------------------------------------- |
| `ready`, `completed`, `python_prepared`                    | —                                                        |
| `runtime_initialized`                                      | `complete`: boolean; built-in interpreter bootstrap only |
| `console_output`, `console_diagnostic`                     | `data`: string                                           |
| `image`                                                    | `data`: valid base64 string; `mime_type`: string         |
| `input_requested`                                          | `prompt`: string                                         |
| `input_received`, `input_cancelled`                        | —                                                        |
| `r_prepared`, `r_activated`                                | `library`: string                                        |
| `r_preparation_failed`                                     | `message`: string                                        |
| `resolve_r`                                                | `packages`: string[]                                     |
| `r_activation_failed`                                      | `library`: string; `message`: string                     |
| `resolve_python`                                           | `request`: Python resolution request                     |
| `resolve_python_version`                                   | `request`: object with required `constraints`: string[]  |
| `python_activated`, `python_activation_failed`             | `requirements`: complete Python manifest                 |
| `python_preparation_failed`, `python_preparation_rejected` | `message`: string                                        |

For example:

```json
{ "kind": "evaluate", "language": "python", "source": "2 + 2" }
```

The worker reports ordinary language output and then `{"kind":"completed"}`.
The sideband has no structured language-error result, poll, interrupt, response cut, output acknowledgment, session name, or general request ID.
Interrupt is a relay-owned process signal; response assembly is server-owned.

### Python request objects

A manifest requires `packages`; `python_version` defaults to `[]` and `exclude_newer` to null:

```json
{
  "packages": ["requests>=2"],
  "python_version": [">=3.11"],
  "exclude_newer": "2026-01-01"
}
```

Empty version lists and absent cutoffs are omitted when serialized.
`resolve_python.request` requires two manifests: physical `requirements` for resolution and logical `retained_requirements` for commit.
Their packages and cutoff must match; version constraints may differ to pin physical resolution to the active interpreter while retaining a broader declaration.
Optional `initialized` defaults to false and identifies a live interpreter requiring the accepted executable.

Optional `import_resolution` contains `module` and `distribution` strings.
It is valid during evaluation or built-in interpreter bootstrap: the module is a top-level ASCII identifier and the distribution a bare name present in both manifests.
The server validates it against the proposed addition and emits any differently-named resolution notice only after matching activation.

`python_resolved.native` contains `selected` and `requirements`.
`selected.embedding` requires `python`, `libpython`, and `python_home`; `selected` also requires `prefix`, `exec_prefix`, `base_prefix`, and `base_exec_prefix`.
These are execution-host-inspected strings, not rediscovery hints.
The built-in worker requires this candidate for managed replies, including R-side declarations.
No request carries an arbitrary resolver environment map.

## Readiness and operations

`ready` must be the first semantic frame and occur exactly once.
Startup diagnostics may use raw stdout/stderr before it.
For the built-in worker, readiness means command admission is available, not that either interpreter has initialized.
Enabled R and Python then initialize on the existing serialized worker thread; hooks may emit output, images, input, resolver, and activation messages before evaluation.
Bootstrap ends with `{"kind":"runtime_initialized","complete":true}`; interrupted retryable setup reports `complete:false`.
It sends no `completed` frame and consumes no Python user-cell filename ID.
The server withholds an accepted cell's `evaluate` frame until bootstrap finishes, while delivering its stdin normally.
An interrupted bootstrap withholds any cell admitted before its incomplete receipt, including a cell whose evaluator has not begun waiting; a later cell can retry incomplete setup in the same interpreter.
Fatal startup failure follows ordinary generation failure and replacement handling.
Custom workers retain their existing readiness and evaluation contract and do not send this event.
Default local and target launchers opt into interpreter bootstrap with the private `worker --bootstrap-runtimes` argument.
An internal worker invoked through a custom launcher retains on-demand initialization unless that launcher explicitly selects the bootstrap protocol.

The server admits one evaluation or explicit preparation at a time.
Each ordinary operation has exactly one matching terminal result:

| Command          | Successful result                               | Ordinary failure result                                                     |
| ---------------- | ----------------------------------------------- | --------------------------------------------------------------------------- |
| `evaluate`       | `completed`                                     | Language error text followed by `completed`, when the worker remains usable |
| `prepare_r`      | `r_prepared` with the requested normalized path | `r_preparation_failed`                                                      |
| `prepare_python` | `python_prepared`                               | `python_preparation_rejected` or `python_preparation_failed`                |
| `shutdown`       | Process exit                                    | No sideband reply                                                           |

A result without its matching active operation, a wrong result kind, or a different R library receipt is a protocol violation.
All semantic output and images belonging to an operation must precede its result; later frames are idle activity.
The relay reads idle frames continuously without waiting for a client poll or result acknowledgment.
New code is admitted only after the server collects the previous evaluation result.

### Managed input

A managed read sends `input_requested` with the exact prompt immediately before waiting on fd 0.
It then sends `input_received` before resuming, or `input_cancelled` before interruption unwinds the runtime.
Only one request may be outstanding, including while idle.
A duplicate request, unmatched terminal input event, or completion before input termination fails the boundary.
Preparation is noninteractive: an input request during R or Python preparation fails both preparation and the worker.
Direct fd-0 readers emit no input events.

### Nested managed-R resolution

During evaluation, built-in interpreter bootstrap, or an idle callback, `resolve_r` requests host resolution of validated plain package names.
The server resolves the complete retained environment outside the worker sandbox.
`r_resolved` is provisional: after applying the library, the worker sends matching `r_activated` before continuing the package load.
Only that current-generation receipt commits the candidate.
A later package-load or cell error does not undo activation.

If application fails, send `r_activation_failed` before propagating the R error.
The candidate is discarded and further requirement changes need restart; this receipt alone does not stop the worker.
`r_resolution_failed.failure` distinguishes ordinary host/validation failure (`host`), explicit resolver interruption (`interrupted`), and lifecycle/operation failure ending the boundary (`operation`).
Transport errors must not be disguised as host failures.

An idle callback reserves environment-change ownership until activation or failure.
Explicit preparation cannot enter that interval.
If explicit preparation reserved first, the server replies to the callback with ordinary host failure before its preparation command.
A runtime R callback after explicit preparation begins is out of phase.

### Live Python preparation

Idle `prepare_python` uses the same worker-owned control path with or without R:

```text
prepare_python -> resolve_python -> python_resolved
               -> python_activated -> python_prepared
```

The server resolves and inspects the complete candidate against the accepted executable, including applicable DuckDB extensions.
The worker validates and activates that candidate; the server commits its manifest and launch identity only on the matching receipt.
Before interpreter initialization, `python_prepared` can instead commit the last materialized candidate without `python_activated`.

Pre-mutation compatibility rejection returns `python_preparation_rejected` and leaves the accepted environment usable.
Unsafe activation failure reports diagnostics before `python_preparation_failed`, withholds same-call input/code, and requires restart before more changes.
Activation is not a rollback mechanism for arbitrary site-hook effects.
See [live environment behavior](REQUIREMENTS.md#live-python-preparation) for compatibility and interruption semantics.

### Nested managed-Python resolution

`resolve_python` and `resolve_python_version` are allowed during evaluation, built-in interpreter bootstrap, Python preparation, or idle runtime callbacks.
Environment replies are provisional; version replies create no environment candidate.
The worker reports a complete normalized logical manifest in `python_activated` before the enclosing result or resumed import.
It must match a provisional candidate or the unchanged managed environment.
A matching `python_activation_failed` reports unsafe post-mutation failure during evaluation, preparation, or idle activity and marks changes restart-required without itself stopping the worker.

An accepted activation survives later import, cell, or subsequent preparation failure.
Unaccepted candidates are discarded when the operation ends or the generation retires.
The relay neither tracks candidates nor decides commits.

### Synchronous resolver waits

Only one nested R, Python-environment, or Python-version request may be outstanding, so no resolver request ID is needed.
The worker waits for exactly the matching reply.
It may retain an already-queued `evaluate` for after the callback; `shutdown` terminates the wait.
Wrong-kind, duplicate, or unsolicited resolver replies are protocol failures.
Generation checks prevent an old receipt from committing into a replacement.

## Shutdown and closure

The relay concurrently closes fd 0 and attempts `shutdown`; the worker must not require both signals in a particular order.
It exits without acknowledgment, or the relay forcibly terminates and reaps the direct child after the supplied grace.
Remaining descendants and private storage belong to the selected runner or compute provider, not this sideband.

Outside intentional retirement, unexpected sideband EOF or worker exit, including status zero, fails the generation.
The relay drains within its bounded allowances and reports closure and process outcome through the outer protocol.
Those events do not prove sandbox, remote-host, container, or VM retirement.
See [relay retirement](RELAY_PROTOCOL.md#retirement-and-failure).

## Custom-worker conformance

A custom worker implements the descriptor, framing, readiness, input, operation-result, shutdown, and signal contracts above.
`--worker PATH` selects one executable without arguments or shell parsing; its SIGINT behavior is worker-defined.

Custom workers can use explicit R/DuckDB preparation and optionally nested managed-R callbacks.
They must honor prepared `R_LIBS`, apply the first managed R library before loading DuckDB, and use the native extension cache.
Managed Python resolution and activation are unavailable to custom workers.

[`tests/fixtures/zod`](../tests/fixtures/zod) exercises the custom-worker contract.
Fixture commands are test behavior, not protocol extensions.
Use the [boundary tests](../tests/boundaries/README.md) for conformance and failure coverage.
