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

Each `test_` function in a suite is a transcript case.
The runner passes the built binary path to each case.
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
The initialization, initialized notification, and tool-list exchange appear in full only in `client_server/server/test_tools::initializes_and_lists_tools`.
When selected, that case runs to completion before the remaining transcript cases start.
Before compacting another transcript, the runner verifies that its prefix equals every document in the full snapshot.
An identical prefix becomes a bare `!same-as PATH` document; a different prefix remains in full.
`PATH` identifies the snapshot used for comparison, and the tag does not load the file.
When accepting a handshake change, update the full snapshot before the abbreviated transcripts.
The `cli/interface/test_help` suite records command lines and stdout in one stream with color disabled.
It adds the exit code for failures and stderr when nonempty.
The `server_relay` suites launch a deterministic scripted relay through an internal development seam.
That relay is the server's direct sandbox child and process-group leader, and it communicates only through the same fd 0/1/2 boundary as the production relay.
The suite records complete parsed JSONL frames under `server` and `relay` direction labels.
The truncated-frame case instead records the exact incomplete bytes as base64 under `relay_raw`.
Its snapshots show flat commands and semantic events, operation results without acknowledgments, readable UTF-8 raw chunks and base64 byte fallbacks, interrupt results, structured worker outcomes, and complete stream drainage.
The cross-source case records serialized observation order without claiming chronology between the worker sideband, stdout, and stderr transports.
Server-side response-cut, pending-output-budget, and truncation cases assert the public MCP result while their wire snapshots verify that no cut, budget, or acknowledgment field enters the relay protocol.
The fixture uses explicit filesystem and FIFO release gates so completion, cancellation, retirement, and failure captures do not depend on sleeps, and tests keep capture descriptors open across sandbox cleanup when necessary.
The `relay_worker` suites drive the public MCP server through a transparent worker proxy.
The proxy starts the built-in worker inside the server's sandbox, forwards sideband messages and standard streams, and writes parsed events to its private temporary directory for the test to read before shutdown.
The restart case keeps the old generation's capture descriptor open across sandbox cleanup and records the sideband shutdown frame, worker-stdin EOF, and worker-sideband EOF.
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
The suite also verifies both documents with Yamark, and the optional Quarto suite executes generated R and Python cells through `ir` when `ir` and `quarto` are installed.

## Test support map

Shared helpers under `tests/support/` are grouped by responsibility:

- `client.py` owns the public stdio MCP client.
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
scripts/test --update client_server/server/test_tools::initializes_and_lists_tools
```

With no selectors, `scripts/test` runs every suite and case in parallel, using at least two worker processes and otherwise one per available CPU by default.
Pass `--jobs N` to set the maximum concurrency or `--jobs 1` to run serially.
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
A suite may set `PLATFORMS = {"darwin"}` to restrict execution and snapshot updates to those `sys.platform` values.
Restricted cases remain visible under `scripts/test --list` and are skipped on other platforms.
A suite may set `REQUIRED_COMMANDS = {"ir"}` to skip when a required executable is not on `PATH`.

Server cases create an `McpClient`, perform their `send` interactions, and return `client._finish()`.
Other cases may invoke the binary directly and return their transcript entries.

Each suite is also directly runnable:

```bash
./tests/boundaries/client_server/server/test_tools.py
```

Suite files use an `uv run --script` shebang.
Their `__main__` blocks delegate to `scripts/test`, so direct runs build the binary and run every case in that suite.
