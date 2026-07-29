# Transcript tests

A transcript suite is a non-private Python file in this directory.
Each `test_` function in a suite is a transcript case.
The function receives the built binary path, runs one imperative server session, and returns its transcript.
The runner records each JSON-RPC request and response pair as one document in the matching YAML 1.2 stream under `golden/SUITE/CASE.yaml`.
Notifications are input-only documents.

Run commands from the repository root:

```bash
scripts/test
scripts/test server
scripts/test server::initializes_lists_tools_and_calls_console
scripts/test --list
scripts/test --update server::initializes_lists_tools_and_calls_console
```

With no selectors, `scripts/test` runs every suite and case.
A suite selector runs every case in that file; a `SUITE::CASE` selector runs one named function.
Use `--update` only to accept an intentional transcript change.

Each case creates an `McpClient`, performs the session, and returns `client.finish()`.
The binary arguments are explicit in the `McpClient` constructor.

Each suite is also directly runnable:

```bash
./tests/transcripts/server.py
```

Suite files use an `uv run --script` shebang.
Their `__main__` blocks delegate to `scripts/test`, so direct runs build the binary and run every case in that suite.
