# Boundary tests

A boundary suite is a Python file under one of four directories whose relative path has no component beginning with `_`:

- `client_server` records the public MCP JSON-RPC boundary.
- `server_relay` records the private JSONL boundary between the server and relay.
- `relay_worker` records the worker sideband and standard-stream boundary owned by the relay.
- `cli` records direct command-line invocations.

## Contract ownership

Give each behavior one primary owner and test it at the outermost boundary that can observe the regression:

| Contract                                                                         | Primary owner                                          |
| -------------------------------------------------------------------------------- | ------------------------------------------------------ |
| MCP negotiation, schemas, descriptions, result text, errors, and output ordering | `client_server` snapshots                              |
| CLI syntax, exit status, terminal behavior, and signal behavior                  | `cli` snapshots                                        |
| Sandbox security and process-lifetime guarantees                                 | `cli`, with only necessary MCP integration smoke tests |
| Server-to-relay frame shape, correlation, ordering, and generation ownership     | `server_relay` snapshots                               |
| Relay-to-worker stream routing, framing, EOF, crash, and shutdown behavior       | `relay_worker` snapshots                               |
| Pure parsing or validation policy that cannot usefully be reached externally     | Small table-driven unit tests                          |

Classify a proposed case as a public snapshot, architecture-boundary test, security-or-liveness test, or incidental-implementation test.
Private-boundary tests cover only the architectural seam they observe and do not repeat public result language.
Security and liveness cases may add causal or process assertions for facts a snapshot cannot represent.
Do not test exact internal sequencing unless it is itself an observable contract.

Sandbox contracts live in a `sandbox/` directory within their owning boundary.
Linux namespace and subreaper contracts live in `cli/sandbox/test_linux`; Seatbelt and kqueue fixtures require `MACOS_SANDBOX`.
Fixture process IDs must be resolved through `support.processes.host_process_id` before host observation or signaling when they originate inside a PID namespace.
In particular, namespace process-group ID 1 must never reach host group signaling: Linux interprets `kill(-1, ...)` as a broadcast.
Ordinary runtime, protocol, and lifecycle cases stay with those subjects, including cases that also run sandboxed.
Direct-launch host access and recovery live under `client_server/lifecycle`; plot-session isolation lives under `client_server/r`.

The direct CLI sandbox cases own setup cancellation, large-frame startup, original-stdin identity and closure, argument and standard-stream fidelity, job control, signal and exit status, security policy, and manager-owned retirement.
The public MCP sandbox cases cover sandbox-dependent runtime workflows, startup failure and gating, worker replacement, supervisor loss, restart, and shutdown.
The lifecycle suites own the inherited-descriptor launch matrix in direct and sandboxed modes.
The relay wrapper workflow verifies MCP restart and shutdown when the relay is below the sandbox root and a worker descendant retains its streams.
The direct relay CLI case compares the complete protocol through ordinary direct launch and the public sandbox command, without requiring the relay to be a process-group leader.

Map each non-generic sandbox allowance to the real workflow that requires it and the test that owns that workflow:

| Sandbox allowance           | Motivating workflow                               | Owning test                                                                                |
| --------------------------- | ------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `hw.logicalcpu`             | `parallel::detectCores()`                         | `client_server/r/test_runtime::detects_cpu_cores`                                          |
| `kern.sysv.semmns`          | joblib `loky`                                     | `client_server/python/test_processes::runs_joblib_process_backend`                         |
| POSIX semaphores            | Python spawn multiprocessing                      | `client_server/python/test_processes::runs_spawn_process_after_live_resolution`            |
| PTYs and `kern.boottime`    | `processx`                                        | `cli/sandbox/test_execution::allows_processx_pty_processes` and MCP process-lifetime cases |
| Quarto device/sysctl access | Render generated `ir` document inside the sandbox | `client_server/sandbox/test_quarto::renders_generated_document`                            |
| `__KMP_REGISTERED_LIB_*`    | PyTorch/libomp                                    | Supplied by the pinned native base; no local extension                                     |
| uv platform services        | Offline wheel installation in private storage     | `cli/sandbox/test_uv::installs_a_local_wheel_into_private_storage`                         |

The [policy audit](../../docs/SANDBOX_SUPERVISION.md#policy-extensions-and-compatibility) distinguishes redundant base-policy rules from local exceptions whose current necessity or precise caller is unconfirmed.
Runner protocol parsing belongs to the extraction's executable tests; `tests/sandbox_installation.py` covers the installed caller boundary, one-shot resource closure, and startup without setup EOF.

`cli/sandbox/test_pytorch::matches_unsandboxed_autograd` runs one CPU autograd script outside and inside the default sandbox with the same freshly resolved PyTorch environment.
It compares the loss, full gradient, and thread count against the live unsandboxed run; the snapshot records that comparison without dependency warnings or fixed numerical values.
This is an intentional exception to exact-output snapshots: warnings and other non-result output may change across releases, while nonzero exits and numerical differences still fail with captured stdout and stderr.
Portable Matplotlib image and cache-activation cases live under `client_server/python`; `client_server/sandbox/test_matplotlib` owns host-file write denials and macOS system-font discovery.
The Ragnar SQL workflows remain under `client_server/sql`; `client_server/sandbox/test_ragnar` preserves workspace-write denial followed by successful creation in the worker directory.
The native base policy's `__KMP_REGISTERED_LIB_*` registration allowance remains an unverified compatibility exception.
This comparison does not establish a need for that permission.

When reviewing deletion candidates, separate tests may replace a combined test only when the interaction between those behaviors is not itself a plausible failure mode.

Each `test_` function in a suite is a transcript case.
The runner passes the built binary path to each case, followed by the execution fixture when the case declares execution modes.
Each case returns a `Transcript`: an ordered list of transcript entries.
The runner serializes each entry as one document in the matching YAML 1.2 stream under `tests/snapshots/BOUNDARY/SUITE/CASE.yaml`.
The snapshot hierarchy exactly parallels the suite hierarchy under `tests/boundaries/`.
A case may return `TranscriptWithCompanions` to place named sibling files beside that stream.
YAML companions use names such as `CASE.events.yaml` and are compared as YAML 1.2 values, so equivalent scalar spellings and layouts are accepted.
Markdown and Quarto companions use `CASE.md` and `CASE.qmd` and are compared as exact UTF-8 text.
Server cases record each JSON-RPC client message and any matching response as one YAML document.
They omit the invariant `jsonrpc: "2.0"` field and request-response IDs from the rendered snapshot.
The client still requires every issued request ID to be unique and validates the response ID before recording each exchange.
Tool calls show the tool name and arguments directly, so a `tools/call` request for `send` is recorded as `send: ARGUMENTS`.
The response's `result` or `error`, when present, appears directly at the document root after the request.
Some cases add `transcript_normalization` after the response.
This is structured harness metadata, never a field or text observed at the captured boundary.
Its `target` identifies the normalized value, and its remaining fields describe information omitted or replaced in the snapshot.
The initialization, initialized notification, and tool-list exchange have full references in `client_server/server/test_tools::initializes_and_lists_tools`.
Its primary snapshot records the sandboxed handshake; its `.direct.yaml` companion records the direct handshake.
The `.bare.yaml` and `.bare.direct.yaml` companions preserve the corresponding interfaces when resolver commands are unavailable.
When selected, this reference case runs before the other cases, including during updates.
At each position in a transcript, the runner compares the complete exchange against the appropriate reference before abbreviating it.
This includes multiple client sessions in one case.
A differing or incomplete handshake stays in full.
For a case with declared execution modes, an exact match becomes `!same-as MCP initialization for this execution mode`, with a `bare` prefix for that reference family.
Portable behavior shares one snapshot while each mode verifies its own complete handshake.
Only the canonical reference case's YAML companions define additional handshake variants; an unmatched exchange stays in full.
For other cases, each matching exchange becomes `!same-as PATH`, naming the reference it actually matched; mixed sessions retain their separate references.
The tag documents a verified comparison and does not load the file.
When accepting a handshake change, update the reference case before the abbreviated transcripts.
The `cli/interface/test_help` suite records command lines and stdout in one stream with color disabled.
It adds the exit code for failures and stderr when nonempty.
The `server_relay` suites launch a deterministic scripted relay through an internal development seam.
The execution fixture launches it directly or through the sandbox; it communicates only through the same fd 0/1/2 boundary as the production relay.
Each fixture generation owns its capture directory, independently of sandbox directory layout or process-group ownership.
In sandboxed mode, the native executable owns the process group and the relay is its child.
The suite records complete parsed JSONL frames under `server` and `relay` direction labels.
The truncated-frame case instead records the exact incomplete bytes as base64 under `relay_raw`.
Its snapshots show flat commands and semantic events, operation results without acknowledgments, readable UTF-8 raw chunks and base64 byte fallbacks, interrupt results, structured worker outcomes, and complete stream drainage.
The cross-source case records serialized observation order without claiming chronology between the worker sideband, stdout, and stderr transports.
Server-side response-cut, pending-output-budget, and truncation cases assert the public MCP result while their wire snapshots verify that no cut, budget, or acknowledgment field enters the relay protocol.
The fixture uses explicit filesystem and FIFO release gates so completion, cancellation, retirement, and failure captures do not depend on sleeps, and tests keep capture descriptors open across generation cleanup when necessary.
The `relay_worker` suites drive the public MCP server through a transparent worker proxy.
The proxy starts the built-in worker in the selected execution mode, forwards sideband messages and standard streams, and writes parsed events to its own capture directory for the test to read before shutdown.
The restart case keeps the old generation's capture descriptor open across generation cleanup and records the sideband shutdown frame, worker-stdin EOF, and worker-sideband EOF.
The crash-recovery case does the same across an unexpected worker exit and records the observed worker-sideband EOF before the replacement starts.
The suite asserts the public `send` result and records relay-to-worker and worker-to-relay frames under `relay` and `worker` direction labels in approximate order.
Pending standard-output and standard-error chunks are grouped into one event without defining their relative order.
The `client_server/r`, `client_server/python`, and `client_server/sql` directories exercise the built-in worker through the public `send` tool.
The Zod materialization case verifies that initialization and unknown tool calls create no run, while a first `send` call does.
The authoritative recording-failure cases verify that recording disables itself with one standard-error diagnostic while console calls and images continue normally.
Projection failures disable both derived documents while JSONL events and artifacts continue.
The Zod recording case projects `events.jsonl` and the literal generated `transcript.md` and `transcript.qmd` into `records_tool_calls_and_images.events.yaml`, followed by the produced session root and file list.
The live-recording case uses causal fixture gates to verify that each Markdown snapshot retains the prior bytes as an exact prefix while calls complete, artifacts arrive, and later polls collect them; the server regenerates the Quarto document for source-bearing calls and leaves it unchanged for results, artifacts, and polls.
The Markdown suite's real mixed-language recording case snapshots the public stdio transcript and literal generated documents as sibling `.yaml`, `.md`, and `.qmd` files.
It exercises the built-in R, Python, and SQL runtimes in one session and verifies that the recorded R image artifact is byte-identical to a reference plot.
The suite also verifies both documents with Yamark, and the optional Quarto suite executes generated R and Python cells through `ir` inside the standalone sandbox when `ir` and `quarto` are installed.

## Test support map

Shared helpers under `tests/support/` are grouped by responsibility:

- `requirements.py` centralizes capability availability and skip reasons; `execution.py` defines explicit direct and sandboxed launch fixtures.
- `client.py` owns the public stdio MCP client.
- `cases.py` runs individual cases and their snapshot checks with deadlines and captures their diagnostic output.
- `snapshots.py` formats and compares primary and companion snapshots.
- `normalization.py` contains source-text and diagnostic normalization.
- `checkpoints.py`, `capture.py`, and `processes.py` contain reusable synchronization, stream-reading, and cleanup mechanics.
- `macos.py` contains shared Darwin process inspection and native fixture compilation.
- `assertions.py` contains transcript result assertions and public-output collection.
- `r.py` and `resolvers.py` contain runtime-specific fixture setup.
- `records.py` defines transcript record types, and `suites.py` supports direct suite execution.

Each boundary keeps its concrete launch and capture mechanics in its local `_harness.py`.
Scenarios and their assertions remain in the `test_*.py` suite files.
Large fixture programs live in searchable files under `tests/fixtures/native/`, `tests/fixtures/server_relay/`, and `tests/fixtures/relay_worker/`.

Run commands from the repository root:

```bash
scripts/test
scripts/test client_server/server/test_tools
scripts/test client_server/server/test_tools::initializes_and_lists_tools
scripts/test --list
scripts/test --locate client_server/server/test_tools
scripts/test --locate client_server/server/test_tools::initializes_and_lists_tools
scripts/test --jobs 1 client_server/python/test_runtime
scripts/test --timeout 1800 client_server/requirements/test_r
scripts/test --update client_server/server/test_tools::initializes_and_lists_tools
```

With no selectors, `scripts/test` runs every suite and case in separate processes, with at least two concurrent cases and otherwise one per available CPU by default.
Pass `--jobs N` to set the maximum concurrency or `--jobs 1` to run serially.
Each case has a 600-second deadline that starts when its supervisor launches.
The deadline includes snapshot formatting, comparison, and updates, which run in the supervised case process so the coordinator can keep handling signals and sibling failures.
Use `--timeout SECONDS` to allow longer runs, such as slow resolver workflows.
On timeout, the runner names the case and requests cleanup from its supervisor process.
The supervisor sends the case `SIGINT`, allowing 15 seconds for `finally` blocks and fixture cleanup before forcibly killing that process by PID.
Fixtures remain responsible for their subprocesses; forcibly killing a case cannot guarantee that all its descendants have exited.
After a failure, Ctrl-C, SIGTERM, or SIGHUP, the runner cancels queued cases and gives running cases two seconds to finish before requesting the same bounded cleanup.
Cases observed to exit with `SIGINT` after cleanup was requested are labelled `cancelled`; their captured output is still printed, including errors interrupted during cleanup.
An independent `SIGINT` racing that request can receive the same label: the exit status does not identify which signal caused it.
This affects reporting during an already unsuccessful run; deadlines and other unsuccessful exits remain failures.
Each supervisor watches an ownership pipe, so loss of the runner also requests cleanup, including when the runner is killed with SIGKILL.
The case interpreter has no monitoring thread: fixtures can use `fork` and `preexec_fn`, and forced cleanup still works if native code holds the case's GIL.
Normal runs emit one flushed `.` for every passing case and end the progress line with a newline.
A case that runs for one minute is named with its current status.
The runner reports it again at two-minute elapsed intervals through ten minutes, then once every five minutes, and names it when it finishes.
On failure, the runner prints the fully qualified selector before the error or diff.
Snapshot updates retain their named `updated ...` and `removed ...` records instead of dots.
This output belongs only to the test-runner user interface; it is not captured transcript data or part of the MCP or relay protocol.
A `BOUNDARY/SUITE` selector runs every case in that file; a `BOUNDARY/SUITE::CASE` selector runs one named function.
`--locate SELECTOR` does not run cases.
It prints every matching case, its source file and definition line, and its mechanically derived primary snapshot path.
Collection fails before listing, locating, or running cases when a snapshot has no matching suite and case.
Companion snapshots remain owned by the case-name prefix.
Use `--update` only to accept an intentional transcript change.
A full `scripts/test --update` also removes snapshots for deleted suites and cases, as well as obsolete companion snapshots for cases that ran; selected updates leave other snapshots alone.
Skipped cases retain all their primary and companion snapshots during full updates, even when another case in the same suite runs.

## Requirements and execution modes

Cases run by default.
Declare only the capabilities a case needs, beside its definition, with `@requires(...)` from `support.requirements`.
For example, `@requires(SANDBOX)` identifies a sandbox contract, `@requires(PROCESS_EVENTS)` identifies a test using shared process observation, and `@requires(command("quarto"))` identifies an optional executable.
All platform availability decisions belong in test support.
`WORKER`, `PROCESS_EVENTS`, and `NATIVE_FIXTURES` support macOS and Linux.
Linux process-observation fixtures require procfs, inotify, and pidfds (kernel 5.3 or later); this is a test-host requirement, not a worker runtime requirement.
Linux descriptor compatibility cases use seccomp to reproduce unavailable `close_range` and CLOEXEC-flag support.
R null-fault recovery uses mutually exclusive diagnostic cases: ARM macOS reports `SEGV_ACCERR`, while Linux and Intel macOS report `SEGV_MAPERR`.
Both cases use the same crash and recovery sequence and retain the full R diagnostic, with only libc's null-pointer formatting normalized.
Sandbox contracts remain gated by `SANDBOX`.
Native checkpoint requirements describe the fixture facility, not ownership of the tested runtime contract.
Do not mark a portable case as sandbox-only because its fixture previously launched sandboxed.

Use the shared execution fixture explicitly:

```python
@executions(DIRECT, SANDBOXED)
def test_persistent_state(binary: Path, execution: Execution) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(r="answer <- 42")
        client.send(r="answer")
        return client.finish()
```

`DIRECT.serve(...)` supplies `--no-sandbox`; `SANDBOXED.serve(...)` selects the default sandbox.
`execution.command(binary, ...)` makes the same choice for a CLI command.
`McpClient` executes the arguments it receives and does not choose a mode based on the OS.
Fixed-mode contracts use the same fixtures explicitly and declare their requirements beside the case.
Core detection and multiprocessing run in both modes, retaining explicit coverage of their motivating sandbox allowances without duplicate cases.
The deterministic Zod worker and wire proxy execute the runner's Python through `MCP_CONSOLE_TEST_PYTHON`, so each interpreter is the relay's direct child.

The runner checks each mode's requirements separately, then runs available modes sequentially within the case's existing deadline.
All modes compare against one primary snapshot and the same companions.
During an update, the first available mode writes the snapshot and subsequent modes must match it.
This preserves differences as failures instead of letting the last mode overwrite the result.
The canonical initialization case is the exception: each available mode updates its own full and bare-runtime references (`.direct.yaml` and `.bare.direct.yaml` for direct mode).
Full updates retain initialization references for unavailable modes.
Multi-session transcripts reuse the same mode-aware handshake compaction.
All cases remain discoverable with `--list` and `--locate`; execution and updates report the selector, mode when applicable, and each missing capability's reason.
An explicitly selected unavailable case is reported as skipped.

Server cases create an `McpClient`, call `initialize_and_list_tools()`, perform their `send()` interactions, and return `client.finish()`.
Use `with McpClient(...) as client:` so an assertion also closes the input and reaps the server.
Response reads have a 600-second ceiling, shortened to leave 14 seconds before the runner's case deadline for cleanup and diagnostics.
This uses the remaining case time even for requests made later in a case.
Client shutdown allows 11 seconds for server retirement and reserves two more seconds for a server-only kill and reap, within the supervisor's 15-second cleanup window.
Constructor arguments `response_timeout` and `shutdown_timeout` can override these waits; under the runner, the case deadline and 11-second shutdown cap still apply.
Deadline errors include the server's stderr tail.
To make interleavings explicit, `start_send()` returns a pending transcript entry; `receive(entry)` fills in its response, and `receive_many(entries)` matches responses by request ID regardless of arrival order.
Protocol cases can use `request()`, `start_request()`, `notify()`, and `send_message()` directly.
Use `finish_with_standard_error()` when the case needs to assert server diagnostics alongside its transcript.
Other cases may invoke the binary directly and return their transcript entries.

Each suite is also directly runnable:

```bash
./tests/boundaries/client_server/server/test_tools.py
```

Suite files use an `uv run --script` shebang.
Their `__main__` blocks delegate to `scripts/test`, so direct runs build the binary and run every case in that suite.

Run the runner and MCP client regressions with `uv run --script tests/transcript_runner.py` and `uv run --script tests/mcp_client.py`, or together through `scripts/check-core`.
These scripts prepare their Python dependencies before tests begin, then launch fixture runners with the same interpreter so package resolution does not consume test deadlines.
