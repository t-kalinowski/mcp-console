# Boundary tests

Tests capture behavior at process interfaces.
Use the outermost boundary that can observe a regression; private-boundary tests should cover their seam rather than repeat public result text.

| Boundary        | Contract                                                            |
| --------------- | ------------------------------------------------------------------- |
| `client_server` | MCP negotiation, tool schema, results, errors, and delivery.        |
| `server_relay`  | Private JSONL framing, ordering, and generation ownership.          |
| `relay_worker`  | Worker sideband, standard streams, EOF, crash, and shutdown.        |
| `cli`           | Arguments, exit status, signals, terminals, and sandbox guarantees. |

Security and liveness cases may need causal or process assertions in addition to a snapshot.
Do not assert incidental internal sequencing.
Keep a combined case when the interaction itself is a plausible failure mode.

## Running cases

```sh
scripts/test --full --list
scripts/test --locate client_server/server/test_tools
scripts/test client_server/server/test_tools::initializes_and_lists_tools
scripts/test --update client_server/server/test_tools::initializes_and_lists_tools
```

A selector names `BOUNDARY/SUITE` or `BOUNDARY/SUITE::CASE`.
Default runs use the explicit smoke profile; `--full` includes every capability-applicable case.
Selectors retain their scope under either profile.
See the [validation ladder](../../docs/DEVELOPMENT.md#validation-ladder) for the full workflow, installed-binary override, and build ownership.

Cases run in separate processes.
`--jobs N` controls concurrency; `--timeout
SECONDS` changes the default 600-second case deadline, including snapshot work.
On cancellation or timeout, the supervisor requests cleanup and allows 15 seconds before forcibly killing the case.
Fixtures must retire their own subprocesses; killing a case cannot guarantee descendant cleanup.
Runner progress and rerun messages are UI output, not captured protocol records.

## Requirements and execution modes

Declare capabilities beside cases with `@requires(...)` from `support.requirements`; platform detection belongs in test support.
Use explicit execution fixtures:

```python
@executions(DIRECT, SANDBOXED)
def test_persistent_state(binary: Path, execution: Execution) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(r="answer <- 42")
        client.send(r="answer")
        return client.finish()
```

`DIRECT.serve()` supplies `--no-sandbox`; `SANDBOXED.serve()` uses the default sandbox.
There is no runner `--execution` flag.
Keep sandbox-only arguments in sandbox fixtures and policy contracts in the owning boundary's `sandbox/` directory.
Ordinary runtime cases stay with their subject even when sandboxed.

Available modes run sequentially within one case deadline and compare against one snapshot.
During updates, the first mode writes and later modes must match, not overwrite differences.
Unavailable modes report a skip, not validation.
Test-host requirements such as Linux process-observation facilities do not imply the same runtime requirements.

Each case uses a temporary workspace and private `MCP_CONSOLE_HOME`, preserving `HOME` and the caller's R/Python/uv/provider environment.
Home-discovery cases supply their environment explicitly with `use_home_configuration=True`; remove inherited `MCP_CONSOLE_HOME` when testing the default home location.
Remove fixture-owned directories only after processes exit.

## Snapshots

A `test_` function returns a `Transcript`.
Its YAML 1.2 snapshot lives at `tests/snapshots/BOUNDARY/SUITE/CASE.yaml`.
`TranscriptWithCompanions` adds named siblings: YAML companions compare as values; Markdown and Quarto compare as exact UTF-8 text.
Suite paths with an underscore-prefixed component are not discovered.

Never edit snapshots by hand.
Regenerate intentional changes with `scripts/test --update SELECTOR`, review them, and rerun without `--update`.
Only a full unscoped run audits orphan snapshots; a successful full update can remove them.
Focused updates preserve unselected snapshots, and skipped cases retain their snapshots even during full updates.

Preserve complete errors, tracebacks, output, and meaningful ordering.
Normalize only incidental values such as temporary paths.
Use mappings for data, but retain serialized JSON when quoting or escaping is the contract; code and literal output remain strings.
The case chooses the representation, not the serializer.

Narrow exceptions require stronger evidence, not weaker assertions:

- Native-runtime fidelity cases may compare complete output and conditions with a live reference, then record the verified comparison.
  Remove only explicitly irrelevant frontend differences, such as Rscript's `Execution halted` footer.
- Synthetic stress output may use `support.evidence.compact_text()` after full assertions.
  Its literal text and repeat/count entries are lossless; retain diagnostics, boundaries, omissions, paths, images, and final states.

`transcript_normalization` is harness metadata, never a wire field.
MCP request IDs and the invariant JSON-RPC version are abbreviated only after validation.

### Canonical handshake

`client_server/server/test_tools::initializes_and_lists_tools` owns full handshake snapshots and their configured/direct/bare/runtime variants.
Update it before other affected cases.
The runner compares the complete exchange before replacing an exact match with `!same-as`; the tag records that comparison and does not load a file.
Different or incomplete handshakes remain in full.
The canonical case's mode-specific companions are the exception to shared-mode snapshots.

## Fixtures and providers

Use `McpClient` as a context manager, then `initialize_and_list_tools()`, `send()`, and `finish()`.
For overlapping calls, use `start_send()` and `receive()` / `receive_many()`; responses are matched by request ID.
Use `finish_with_standard_error()` when diagnostics are part of the contract.

Shared capability, execution, client, snapshot, checkpoint, and process helpers live in `tests/support/`.
Each boundary's `_harness.py` owns its concrete launch and capture mechanics.
Keep large fixture programs in searchable files under `tests/fixtures/`; see [authoring](AUTHORING.md) for examples and causal gates.

Real provider coverage requires explicit fixtures:

| Provider       | Fixture selection                                                                                                                    |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| Docker         | `MCP_CONSOLE_TEST_DOCKER_IMAGE` or `MCP_CONSOLE_TEST_DOCKER_PYTHON_IMAGE`; see [Docker](../../docs/DOCKER.md).                       |
| Docker Sandbox | `MCP_CONSOLE_TEST_SBX_TEMPLATE` or `MCP_CONSOLE_TEST_SBX_PYTHON_TEMPLATE`; see [SBX](../../docs/DOCKER_SANDBOX.md).                  |
| Localhost SSH  | Private `sshd`, temporary pinned keys, and the shared SSH capability.                                                                |
| External SSH   | `MCP_CONSOLE_TEST_SSH_HOST`; an empty value disables the optional probe. See `support/ssh_external.py` and [SSH](../../docs/SSH.md). |

Unavailable services skip their cases; connected setup failures fail.
Fake provider peers establish orchestration contracts, not real container/VM cleanup.
SBX fixtures serialize real VMs and must not change global provider policy.

For namespace PIDs, use `support.processes.host_process_id` before host observation or signaling.
**Never send a namespace process-group ID of 1 to host group signaling: `kill(-1, ...)` is a broadcast.** Fixture cleanup must identify only its own resources.
