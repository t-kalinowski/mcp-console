# MCP Tool Description Guidance

The [canonical handshake snapshot](../tests/snapshots/client_server/server/test_tools/initializes_and_lists_tools.yaml) records the registered tools, schemas, and descriptions returned by `tools/list`.
The registered strings and Rust doc comments in [`src/server.rs`](../src/server.rs), with placement and enforcement prose in [`src/server/execution.rs`](../src/server/execution.rs), define that prose.
The security paragraph reflects the effective target and selected provider.
Review changes in the snapshot and regenerate it intentionally using the [boundary test guide](../tests/boundaries/README.md).
Ordinary tests check the committed expectation; they do not regenerate it.

Tool descriptions occupy recurring agent context.
Keep them concise and action-oriented, and include facts that affect whether or how an agent calls the tool or interprets its result.

- Use the tool-level description for scope, language selection, persistence, sequential evaluation, polling, interoperability, and the security boundary.
- Give concrete language-selection criteria before execution and polling instructions: DuckDB SQL for structured-file and database inspection, filtering, joins, aggregation, and nested JSON extraction; R for vectorized data and string operations, statistics, and plots; Python when its libraries or format-specific parsing simplify the task.
  Mention direct CSV, Parquet, JSON, and JSONL access, built-in JSON support, read-only SQLite attachment, and bounded SQL previews that abbreviate long text cells.
  Describe SQLite as a default only when it is in the startup declaration: managed built-in sessions include it, while custom workers require explicit preparation.
  Show extension preparation calls only when preparation is available; other sessions require a preinstalled SQLite extension.
  Keep selection guidance consistent with the available languages; encourage switching languages while reusing persistent state only when multiple languages are enabled.
- Put field-specific rules on their properties: accepted inputs, preparation, stdin and control ordering, timeout behavior, result display, and plotting.
  Avoid repeating those rules in the tool-level description.
- Preserve warnings about state changes that survive errors and controls that discard state.
  Include exact bridge names and familiar interfaces such as DuckDB, DBI, and dplyr when they tell the agent how to complete a workflow.

Leave tutorials and analysis-specific examples to the runtime guides.
Omit implementation details that do not change agent behavior, such as interpreter backends, worker IPC, the internal journal, and exact output limits.

For native enforcement, the `send` description reflects the captured native built-in and explicit filesystem/network selection.
For `":workspace"`, describe fixed workspace writes, private temporary storage, and readable metadata paths protected from writes by default; state that explicit native rules can change those defaults.
Do not describe `.agents/console` as an unconditional write denial or imply that a `read` entry only denies writes.

For Docker Sandbox compute enforcement, describe the owned microVM, explicit shared paths, and externally managed Docker policy and host integrations.
Do not label it unsandboxed host execution or imply native policy equivalence, protected metadata within writable shares, or a frozen inherited policy.
Describe controller recording paths separately from VM files; shared paths can expose controller records to the worker.

For prepared Docker and SBX targets, derive available languages from target runtime discovery.
Sans-R descriptions expose Python and enabled SQL, omit R, and describe the Console-owned SQL catalog without calling the environment managed.
Dependencies and extensions must come from the captured image/template; missing imports do not install packages.
`requirements.action="get"` reports the retained declaration, not an inventory of preinstalled distributions.
Describe rebuilding the image/template and starting a new server session separately from a plain worker restart that retains the interpreter and resets state.
