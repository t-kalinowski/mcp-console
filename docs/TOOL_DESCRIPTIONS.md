# MCP Tool Description Guidance

The [canonical handshake snapshot](../tests/snapshots/client_server/server/test_tools/initializes_and_lists_tools.yaml) records the registered tools, schemas, and descriptions returned by `tools/list`.
The registered strings and Rust doc comments in [`src/server.rs`](../src/server.rs) define that prose.
Review changes in the snapshot and regenerate it intentionally using the [boundary test guide](../tests/boundaries/README.md).
Ordinary tests check the committed expectation; they do not regenerate it.

Tool descriptions occupy recurring agent context.
Keep them concise and action-oriented, and include facts that affect whether or how an agent calls the tool or interprets its result.

- Use the tool-level description for scope, language selection, persistence, sequential evaluation, polling, interoperability, and the security boundary.
- Put field-specific rules on their properties: accepted inputs, preparation, stdin and control ordering, timeout behavior, result display, and plotting.
  Avoid repeating those rules in the tool-level description.
- Preserve warnings about state changes that survive errors and controls that discard state.
  Include exact bridge names and familiar interfaces such as DuckDB, DBI, and dplyr when they tell the agent how to complete a workflow.

Leave tutorials and analysis-specific examples to the runtime guides.
Omit implementation details that do not change agent behavior, such as interpreter backends, worker IPC, the internal journal, and exact output limits.
