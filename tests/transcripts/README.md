# Transcript tests

A transcript suite is a non-private Python file in this directory.
Each `test_` function in a suite is a transcript case.
The function receives the built binary path and returns a list of mappings.
The runner records each mapping as one document in the matching YAML 1.2 stream under `golden/SUITE/CASE.yaml`.
Generated streams begin with `# fmt: skip file` so repository-wide formatting preserves the serializer's exact output.
Server cases record JSON-RPC requests and responses.
The `help` suite records command lines and stdout in one stream with color disabled.
It adds the exit code for failures and stderr when nonempty.

Run commands from the repository root:

```bash
scripts/test
scripts/test server
scripts/test server::initializes_lists_tools_and_calls_console
scripts/test help
scripts/test --list
scripts/test --update server::initializes_lists_tools_and_calls_console
```

With no selectors, `scripts/test` runs every suite and case.
A suite selector runs every case in that file; a `SUITE::CASE` selector runs one named function.
Use `--update` only to accept an intentional transcript change.

Server cases create an `McpClient`, perform the session, and return `client.finish()`.
Other cases may invoke the binary directly and return their transcript documents.

Each suite is also directly runnable:

```bash
./tests/transcripts/server.py
```

Suite files use an `uv run --script` shebang.
Their `__main__` blocks delegate to `scripts/test`, so direct runs build the binary and run every case in that suite.
