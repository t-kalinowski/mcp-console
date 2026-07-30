# Transcript tests

A transcript suite is a non-private Python file in this directory.
Each `test_` function in a suite is a transcript case.
The runner passes the built binary path to each case.
Each case returns a `Transcript`: an ordered list of transcript entries.
The runner serializes each entry as one document in the matching YAML 1.2 stream under `golden/SUITE/CASE.yaml`.
The runner compares YAML 1.2 values, so equivalent scalar spellings and layouts are accepted.
Server cases record JSON-RPC requests and responses.
They omit the invariant `jsonrpc: "2.0"` field and show a matching request and response `id` once at the document root.
The client validates both fields before recording the compact transcript.
The `help` suite records command lines and stdout in one stream with color disabled.
It adds the exit code for failures and stderr when nonempty.

Run commands from the repository root:

```bash
scripts/test
scripts/test server
scripts/test server::initializes_lists_tools_and_calls_send
scripts/test help
scripts/test --list
scripts/test --update server::initializes_lists_tools_and_calls_send
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
