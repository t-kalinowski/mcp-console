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
A case may return `TranscriptWithCompanion` to place a separate YAML stream at `golden/BOUNDARY/SUITE/CASE.NAME.yaml`.
The runner compares YAML 1.2 values, so equivalent scalar spellings and layouts are accepted.
Server cases record each JSON-RPC client message and any matching response as one YAML document.
They omit the invariant `jsonrpc: "2.0"` field and show a request-response `id` once at the document root when present.
The client validates the omitted JSON-RPC version and response ID before recording each exchange.
Tool calls show the tool name and arguments directly, so a `tools/call` request for `send` is recorded as `send: ARGUMENTS`.
The response's `result` or `error`, when present, appears directly at the document root after the request.
The initialization, initialized notification, and tool-list exchange appear in full only in `client_server/server::initializes_and_lists_tools`.
When selected, that case runs to completion before the remaining transcript cases start.
Before compacting another transcript, the runner verifies that its prefix equals every document in the full snapshot.
An identical prefix becomes a bare `!same-as PATH` document; a different prefix remains in full.
`PATH` identifies the snapshot used for comparison, and the tag does not load the file.
When accepting a handshake change, update the full snapshot before the abbreviated transcripts.
The `cli/help` suite records command lines and stdout in one stream with color disabled.
It adds the exit code for failures and stderr when nonempty.
The `relay_worker/protocol` suite drives the public MCP server through a transparent worker proxy.
The proxy starts the built-in worker inside the server's sandbox, forwards sideband messages and standard streams, and writes parsed events to its private temporary directory for the test to read before shutdown.
The restart case keeps the old generation's capture descriptor open across sandbox cleanup and records the sideband shutdown frame, worker-stdin EOF, and worker-sideband EOF.
The crash-recovery case does the same across an unexpected worker exit and records the observed worker-sideband EOF before the replacement starts.
The suite asserts the public `send` result and records relay-to-worker and worker-to-relay frames under `relay` and `worker` direction labels in approximate order.
Pending standard-output and standard-error chunks are grouped into one event without defining their relative order.
The `client_server/r`, `client_server/python`, and `client_server/sql` suites exercise the built-in worker through the public `send` tool.
The Zod materialization case verifies that initialization and unknown tool calls create no run, while a first `send` or `session` call does.
The recording-failure cases verify that recording disables itself with one standard-error diagnostic while console calls and images continue normally.
The Zod recording case projects `events.jsonl` into a readable YAML sequence in `records_tool_calls_and_images.events.yaml`, followed by the produced session root and file list.

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
A `BOUNDARY/SUITE` selector runs every case in that file; a `BOUNDARY/SUITE::CASE` selector runs one named function.
Use `--update` only to accept an intentional transcript change.
A suite may set `PLATFORMS = {"darwin"}` to restrict execution and snapshot updates to those `sys.platform` values.
Restricted cases remain visible under `scripts/test --list` and are skipped on other platforms.
A suite may set `REQUIRED_COMMANDS = {"ir"}` to skip when a required executable is not on `PATH`.

Server cases create an `McpClient`, perform the session, and return `client._finish()`.
Other cases may invoke the binary directly and return their transcript entries.

Each suite is also directly runnable:

```bash
./tests/transcripts/client_server/server.py
```

Suite files use an `uv run --script` shebang.
Their `__main__` blocks delegate to `scripts/test`, so direct runs build the binary and run every case in that suite.
