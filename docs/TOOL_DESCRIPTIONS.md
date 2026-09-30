# MCP Tool Description Guidance

The [canonical handshake snapshot](../tests/snapshots/client_server/server/test_tools/initializes_and_lists_tools.yaml) records the registered tools, schemas, and descriptions returned by `tools/list`.
The registered strings and Rust doc comments in [`src/server.rs`](../src/server.rs), with placement and enforcement prose in [`src/server/execution.rs`](../src/server/execution.rs), define that prose.
The security paragraph reflects captured target configuration and the selected provider.
Tool construction does not inspect interpreters, create a worker client, or read discovery results.
For the same explicit configuration, the schema is stable across managed, bare, R-present, and sans-R environments.
`MCP_CONSOLE_LANGUAGES` filters direct code fields only; execution still checks discovered runtime and preparation capabilities.
MCP discovery does not wait for runtime discovery or dependency preparation; the configured tool interface remains stable when that background work completes.
Review changes in the snapshot and regenerate it intentionally using the [boundary test guide](../tests/boundaries/README.md).
Ordinary tests check the committed expectation; they do not regenerate it.

## Supported capabilities and host availability

The configured interface advertises capabilities Console supports, including languages that may be missing from the execution host.
This is an intentional trade-off: tailoring the schema to installed runtimes gives an agent more precise guidance, but hiding an unavailable language also hides the opportunity to make it available.
For example, an advertised R field lets an agent attempt an R task, report that R is missing, and ask the user to authorize installation on the execution host.
The cost is less environment-specific guidance and a possible rejected call before the agent learns what is available.
Advertising a capability does not authorize installation or promise that the current host can satisfy it.

Explicit configuration still constrains the interface: `MCP_CONSOLE_LANGUAGES` removes disabled code fields, and prepared Docker/SBX targets expose requirement inspection without dependency preparation.
Execution validates discovered runtime availability; an R cell on a sans-R session or a Python cell on an R-only prepared target is rejected before worker startup or replacement.
For SSH, dependencies belong on the remote execution host; prepared targets require an updated image or template.

Runtime selection is captured for each server session.
After installing or configuring a missing runtime, start a new MCP Console server session.
`control="restart"` replaces the worker while retaining the server's runtime selection.

Server startup captures and validates launch configuration, including applicable local native-policy preflight, before serving MCP.
Runtime discovery and applicable initial preparation then run independently of `initialize`, `tools/list`, and `ping`.
A host with no viable runtime can still advertise the configured interface; `send` reports the retained preparation failure.
See [server readiness](SEND_OPERATIONS.md#server-readiness) for waiting, cancellation, and recovery.

## Writing guidance

Tool descriptions occupy recurring agent context.
Keep them concise and action-oriented, and include facts that affect whether or how an agent calls the tool or interprets its result.

- Use the tool-level description for scope, language selection, persistence, sequential evaluation, polling, interoperability, and the security boundary.
- Give concrete language-selection criteria before execution and polling instructions: DuckDB SQL for structured-file and database inspection, filtering, joins, aggregation, and nested JSON extraction; R for vectorized data and string operations, statistics, and plots; Python when its libraries or format-specific parsing simplify the task.
  Mention direct CSV, Parquet, JSON, and JSONL access, built-in JSON support, read-only SQLite attachment, and bounded SQL previews that abbreviate long text cells.
  Describe SQLite as a built-in managed default conditional on DuckDB preparation support; do not infer an installed extension from the schema.
  Do not attribute a built-in catalog or package defaults to custom workers.
  For host targets, make extension preparation examples conditional on resolver support; for prepared targets, omit them and require preinstalled extensions.
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

For prepared Docker and SBX targets, retain configured language fields regardless of discovered runtimes.
Describe runtime availability, cross-language sharing, and R-owned versus Python-owned SQL conditionally.
Do not claim that discovery has completed or include discovered runtime paths or image identities in tool prose.
The configured target alone establishes that dependency preparation is unavailable.
Dependencies and extensions must come from the captured image/template; missing imports do not install packages.
`requirements.action="get"` reports the retained declaration, not an inventory of preinstalled distributions.
Describe rebuilding the image/template and starting a new server session separately from a plain worker restart that retains the interpreter and resets state.
