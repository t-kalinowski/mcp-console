# MCP tool descriptions

Tool descriptions recur in agent context.
Include information that changes tool choice, call construction, or result interpretation; keep architectural explanations in the guides.

## Source and stability

[`src/server/presentation.rs`](../src/server/presentation.rs) composes named [prose sections](../src/server/presentation/sections.rs); [`src/server/arguments.rs`](../src/server/arguments.rs) defines the input schema.
The [canonical handshake snapshot](../tests/snapshots/client_server/server/test_tools/initializes_and_lists_tools.yaml) records it.
Regenerate that snapshot deliberately through the [boundary tests](../tests/boundaries/README.md), never by editing expected output.

Descriptions depend on captured configuration, not completed runtime discovery.
The presentation profile selects sections from configured languages, built-in or custom worker selection, and host preparation.
Small platform conditionals select Windows language and interrupt guidance without matching or removing sentences.
For the same configuration, they stay stable as background startup finishes or fails.
`MCP_CONSOLE_LANGUAGES` filters direct code fields; execution still checks actual runtime availability.

Advertising an unavailable language lets an agent identify the missing prerequisite and ask for installation authorization, at the cost of a rejected call before discovery is known.
It neither proves availability nor authorizes installation.
Installing a runtime requires a new server session; worker restart retains captured selection.

## Editorial rules

Put scope, useful language-selection guidance, persistence, sequential execution, polling, interoperability, and the security boundary at tool level.
Put field-specific input and ordering rules on the fields, without repeating them above.
Keep exact bridge/helper names when they enable a workflow.
Preserve warnings about effects surviving errors and restart discarding state.

Prefer concrete choices: SQL for structured-file/database inspection and aggregation, R for vectorized/statistical work, Python when its libraries fit the task.
Make cross-language guidance conditional on configured languages.
Do not turn the description into a tutorial, package inventory, backend explanation, or transcript-format specification.

## Capability and security claims

Distinguish supported capabilities from installed dependencies.
An advertised SQL field does not prove that DuckDB, SQLite extensions, or an R/Python bridge is available.
Custom workers do not inherit the built-in catalog or package defaults.
Host preparation examples require resolver support; bare runtimes and explicitly selected Python require preinstalled dependencies.
Requirement inspection is a declaration, not an installed-package inventory.

Native prose must reflect selected filesystem and network policy.
Workspace metadata is protected by default, not by an unchangeable denial ceiling; explicit native rules can alter those defaults.
A `read` entry grants reads as well as narrowing writes.

Review the resulting handshake as an agent would: can it choose and call the tool without reading implementation details, and are its safety claims true for the configured session?
