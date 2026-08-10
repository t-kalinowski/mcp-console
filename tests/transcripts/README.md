# Transcript tests

A transcript suite is a non-private Python file in this directory.
Each `test_` function in a suite is a transcript case.
The runner passes the built binary path to each case.
Each case returns a `Transcript`: an ordered list of transcript entries.
The runner serializes each entry as one document in the matching YAML 1.2 stream under `golden/SUITE/CASE.yaml`.
The runner compares YAML 1.2 values, so equivalent scalar spellings and layouts are accepted.
Server cases record each JSON-RPC client message and any matching response as one YAML document.
They omit the invariant `jsonrpc: "2.0"` field and show a request-response `id` once at the document root when present.
The client validates the omitted JSON-RPC version and response ID before recording each exchange.
Tool calls show the tool name and arguments directly, so a `tools/call` request for `send` is recorded as `send: ARGUMENTS`.
The response's `result` or `error`, when present, appears directly at the document root after the request.
The initialization, initialized notification, and tool-list exchange appear in full only in `server::initializes_and_lists_tools`.
Before compacting another transcript, the runner verifies that its prefix equals every document in the full snapshot.
An identical prefix becomes a bare `!same-as PATH` document; a different prefix remains in full.
`PATH` identifies the snapshot used for comparison, and the tag does not load the file.
When accepting a handshake change, update the full snapshot before the abbreviated transcripts.
The `help` suite records command lines and stdout in one stream with color disabled.
It adds the exit code for failures and stderr when nonempty.
The `worker` suite drives the public MCP server through a transparent worker proxy.
The proxy starts the built-in worker inside the server's sandbox, forwards sideband messages and standard streams, and writes parsed events to its private temporary directory for the test to read before shutdown.
The restart case keeps the old generation's capture descriptor open across sandbox cleanup and records the sideband shutdown frame, worker-stdin EOF, and worker-sideband EOF.
The crash-recovery case does the same across an unexpected worker exit and records the observed worker-sideband EOF before the replacement starts.
The suite asserts the public `send` result and records the wire events as YAML mappings in approximate order.
Pending standard-output and standard-error chunks are grouped into one event without defining their relative order.
The `r`, `python`, and `sql` suites exercise the built-in worker through the public `send` tool.
The Zod recording case includes the normalized literal `events.jsonl` and binary image in its golden so the produced on-disk layout and journal format remain directly reviewable.

Run commands from the repository root:

```bash
scripts/test
scripts/test server
scripts/test server::initializes_and_lists_tools
scripts/test help
scripts/test --list
scripts/test --update server::initializes_and_lists_tools
```

With no selectors, `scripts/test` runs every suite and case.
A suite selector runs every case in that file; a `SUITE::CASE` selector runs one named function.
Use `--update` only to accept an intentional transcript change.
A suite may set `PLATFORMS = {"darwin"}` to restrict execution and snapshot updates to those `sys.platform` values.
Restricted cases remain visible under `scripts/test --list` and are skipped on other platforms.

Server cases create an `McpClient`, perform the session, and return `client.finish()`.
Other cases may invoke the binary directly and return their transcript entries.

Each suite is also directly runnable:

```bash
./tests/transcripts/server.py
```

Suite files use an `uv run --script` shebang.
Their `__main__` blocks delegate to `scripts/test`, so direct runs build the binary and run every case in that suite.
