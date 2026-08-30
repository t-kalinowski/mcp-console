# Transcript tests

A transcript suite is a non-private Python file under one of four boundary directories:

- `client_server` records the public MCP JSON-RPC boundary.
- `server_relay` records the private JSONL boundary between the server and relay.
- `relay_worker` records the worker sideband and standard-stream boundary owned by the relay.
- `cli` records direct command-line invocations.

Each `test_` function in a suite is a transcript case.
The runner passes the built binary path to each case.
Each case returns a `Transcript`: an ordered list of transcript entries.
The runner serializes each entry as one document in the matching YAML 1.2 stream under `golden/BOUNDARY/SUITE/CASE.yaml`.
A case may return `TranscriptWithCompanions` to place named sibling files beside that stream.
YAML companions use names such as `CASE.events.yaml` and are compared as YAML 1.2 values, so equivalent scalar spellings and layouts are accepted.
Markdown and Quarto companions use `CASE.md` and `CASE.qmd` and are compared as exact UTF-8 text.
Server cases record each JSON-RPC client message and any matching response as one YAML document.
They omit the invariant `jsonrpc: "2.0"` field and request-response IDs from the rendered golden.
The client still requires every issued request ID to be unique and validates the response ID before recording each exchange.
Tool calls show the tool name and arguments directly, so a `tools/call` request for `send` is recorded as `send: ARGUMENTS`.
The response's `result` or `error`, when present, appears directly at the document root after the request.
Some cases add `transcript_normalization` after the response.
This is structured harness metadata, never a field or text observed at the captured boundary.
Its `target` identifies the normalized value, and its remaining fields describe information omitted or replaced in the golden.
The initialization, initialized notification, and tool-list exchange appear in full only in `client_server/server::initializes_and_lists_tools`.
When selected, that case runs to completion before the remaining transcript cases start.
Before compacting another transcript, the runner verifies that its prefix equals every document in the full snapshot.
An identical prefix becomes a bare `!same-as PATH` document; a different prefix remains in full.
`PATH` identifies the snapshot used for comparison, and the tag does not load the file.
When accepting a handshake change, update the full snapshot before the abbreviated transcripts.
The `cli/help` suite records command lines and stdout in one stream with color disabled.
It adds the exit code for failures and stderr when nonempty.
The `server_relay/protocol` suite launches a deterministic scripted relay through an internal development seam.
That relay is the server's direct sandbox child and process-group leader, and it communicates only through the same fd 0/1/2 boundary as the production relay.
The suite records complete parsed JSONL frames under `server` and `relay` direction labels.
The truncated-frame case instead records the exact incomplete bytes as base64 under `relay_raw`.
Its snapshots show flat commands and semantic events, operation results without acknowledgments, readable UTF-8 raw chunks and base64 byte fallbacks, interrupt results, structured worker outcomes, and complete stream drainage.
The cross-source case records serialized observation order without claiming chronology between the worker sideband, stdout, and stderr transports.
Server-side response-cut, pending-output-budget, and truncation cases assert the public MCP result while their wire snapshots verify that no cut, budget, or acknowledgment field enters the relay protocol.
The fixture uses explicit filesystem and FIFO release gates so completion, cancellation, retirement, and failure captures do not depend on sleeps, and tests keep capture descriptors open across sandbox cleanup when necessary.
The `relay_worker/protocol` suite drives the public MCP server through a transparent worker proxy.
The proxy starts the built-in worker inside the server's sandbox, forwards sideband messages and standard streams, and writes parsed events to its private temporary directory for the test to read before shutdown.
The restart case keeps the old generation's capture descriptor open across sandbox cleanup and records the sideband shutdown frame, worker-stdin EOF, and worker-sideband EOF.
The crash-recovery case does the same across an unexpected worker exit and records the observed worker-sideband EOF before the replacement starts.
The suite asserts the public `send` result and records relay-to-worker and worker-to-relay frames under `relay` and `worker` direction labels in approximate order.
Pending standard-output and standard-error chunks are grouped into one event without defining their relative order.
The `client_server/r`, `client_server/python`, and `client_server/sql` suites exercise the built-in worker through the public `send` tool.
The Zod materialization case verifies that initialization and unknown tool calls create no run, while a first `send` call does.
The authoritative recording-failure cases verify that recording disables itself with one standard-error diagnostic while console calls and images continue normally.
Projection failures disable both derived documents while JSONL events and artifacts continue.
The Zod recording case projects `events.jsonl` and the literal generated `transcript.md` and `transcript.qmd` into `records_tool_calls_and_images.events.yaml`, followed by the produced session root and file list.
The live-recording case uses causal fixture gates to verify that each Markdown snapshot retains the prior bytes as an exact prefix while calls complete, artifacts arrive, and later polls collect them; the server regenerates the Quarto document for source-bearing calls and leaves it unchanged for results, artifacts, and polls.
The Markdown suite's real mixed-language recording case snapshots the public stdio transcript and literal generated documents as sibling `.yaml`, `.md`, and `.qmd` goldens.
It exercises the built-in R, Python, and SQL runtimes in one session and verifies that the recorded R image artifact is byte-identical to a reference plot.
The suite also verifies both documents with Yamark, and the optional Quarto suite executes generated R and Python cells through `ir` when `ir` and `quarto` are installed.

Run commands from the repository root:

```bash
scripts/test
scripts/test client_server/server
scripts/test client_server/server::initializes_and_lists_tools
scripts/test cli/help
scripts/test --list
scripts/test --jobs 1 client_server/python
scripts/test --update client_server/server::initializes_and_lists_tools
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
Use `--update` only to accept an intentional transcript change.
A full `scripts/test --update` also removes goldens for deleted suites and cases, as well as obsolete companion goldens for cases that ran; selected updates leave other goldens alone.
A suite may set `PLATFORMS = {"darwin"}` to restrict execution and snapshot updates to those `sys.platform` values.
Restricted cases remain visible under `scripts/test --list` and are skipped on other platforms.
A suite may set `CASE_PLATFORMS = {"case_name": {"darwin"}}` to apply the same restriction to individual cases without hiding them from discovery or allowing another platform's full update to remove their snapshots.
A suite may set `REQUIRED_COMMANDS = {"ir"}` to skip when a required executable is not on `PATH`.

Server cases create an `McpClient`, perform their `send` interactions, and return `client._finish()`.
Other cases may invoke the binary directly and return their transcript entries.

Each suite is also directly runnable:

```bash
./tests/transcripts/client_server/server.py
```

Suite files use an `uv run --script` shebang.
Their `__main__` blocks delegate to `scripts/test`, so direct runs build the binary and run every case in that suite.
