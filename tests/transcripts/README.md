# Transcript tests

Each Python file under `cases/` defines one imperative external server scenario.
The runner records every JSON-RPC input and output as one document in the matching YAML 1.2 stream under `golden/`.

Run commands from the repository root:

```bash
scripts/test
scripts/test server
scripts/test server another-case
scripts/test --list
scripts/test --update server
```

With no case names, `scripts/test` runs every case.
Case names are exact filename stems.
Use `--update` only to accept an intentional transcript change.

Each case defines `run(client)`.
Use the shared client methods for initialization and tool calls so the case contains only the behavior under test.
By default a case runs `mcp-console`; define `server_argument_sets` when the same transcript must also hold for another implemented server entry point.
