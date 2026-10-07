# MCP tool descriptions

Tool descriptions recur in agent context.
Include information that changes tool choice, call construction, or result interpretation; keep architectural explanations in the guides.

## Source and stability

[`src/server/presentation.rs`](../src/server/presentation.rs) composes named [prose sections](../src/server/presentation/sections.rs); [`src/server/arguments.rs`](../src/server/arguments.rs) defines the input schema.
The [canonical handshake snapshot](../tests/snapshots/client_server/server/test_tools/initializes_and_lists_tools.yaml) records it.
Regenerate that snapshot deliberately through the [boundary tests](../tests/boundaries/README.md), never by editing expected output.
Writable-root companions capture the extra grant; affected fixtures use separate direct/sandbox snapshots because their advertised policies differ.

Descriptions depend on captured configuration, not completed runtime discovery.
The presentation profile selects sections from configured languages, built-in or custom worker selection, and host preparation.
Platform conditionals select Windows language and temporary-directory guidance without matching or removing sentences.
For the same configuration, they stay stable as background startup finishes or fails.
The captured `languages` list selects direct code fields, field guidance, and language-sharing sections together.
Omission retains the legacy `MCP_CONSOLE_LANGUAGES` filter; neither discovery nor interpreter initialization changes the advertised schema.
Hidden source keys, including null values, are rejected before same-call effects.
Requirements remain visible for hidden SQL providers, and execution still checks actual runtime availability.
For an explicit `languages` configuration, tool and exposed-field descriptions mention only visible languages, including requirements, control, and timeout guidance.
Tool-level SQL guidance describes file queries directly; runtime diagnostics identify any missing dependencies and preparation needed.
Requirement keys remain available even when their language is hidden; their descriptions then explain host-provider preparation without advertising a direct code field.
Visibility selects direct code fields and applicable examples, not SQL ownership: managed DuckDB still uses R when available and Python otherwise.
Show bridge examples only when both source languages are visible.
When Python and SQL are visible but R is hidden, describe user-owned DB-API selection and qualify managed `sql_connection()` and frame registration: those helpers require Python-owned DuckDB.

Advertising an unavailable language lets an agent identify the missing prerequisite and ask for installation authorization, at the cost of a rejected call before discovery is known.
It neither proves availability nor authorizes installation.
Installing a runtime requires a new server session; worker restart retains captured selection.

## Editorial rules

Start with a complete cell, automatic display, inspecting its output, and reusing persistent objects.
Ordinary use does not require declaration inspection, preparation, language switching, or restart.
Put concise language-selection guidance, sequential execution, polling, and the security boundary at tool level.
Lead the arguments with `r`, `python`, `sql`, `timeout_ms`, `control`, `stdin`, and `requirements`, omitting hidden fields.
Give each other fact one primary home: display, plots, bridges, and connection helpers on language fields; wait timing on timeout; queued input on stdin; lifecycle effects on control.
Keep exact bridge/helper names when they enable a workflow.
Preserve warnings about effects surviving errors and restart discarding state.

Keep general preparation and its ordering/failure rules on requirements, action semantics on action, and accepted syntax on package fields.
Inspection reads the retained declaration, not an installed-package inventory.
Preserve omitted-field semantics, payload restrictions, hidden-provider preparation, and restrictions for custom workers and selected environments.
Keep interrupt's partial effects explicit: supported interrupt-plus-cell preparation follows the signal and queued input, which are not rolled back on failure.

Response notices own current state, omitted-output locations, and missing-provider preparation instructions.
Keep the empty-send polling rule in the tool description; do not repeat the notices' detailed instructions or advertise a help API.
Do not assume a client has this repository or remove a capability's only usable explanation in favor of an external guide.

Prefer concrete choices: SQL for structured-file/database inspection and aggregation, R for vectorized/statistical work, Python when its libraries fit the task.
Make cross-language guidance conditional on configured languages.
Do not turn the description into a tutorial, package inventory, backend explanation, or transcript-format specification.
Long recipes and implementation details belong in the existing guides: subprocess choices and plot behavior in [runtime behavior](BUILTIN_RUNTIME.md), resolver details in [requirements](REQUIREMENTS.md), deadlines and interrupt grace in [send ordering](SEND_OPERATIONS.md), and recording paths in [recordings](RECORDING.md).

## Comparing the advertised definition

Capture the canonical handshake and its companions before editing, then regenerate through `scripts/test --update client_server/server/test_tools::initializes_and_lists_tools`.
Compare the top-level description, the sum of every schema description (including nested requirements), and the complete tool object from `result.tools[0]`.
Object-key order is presentation; the argument order may change while schema values remain identical.
Use the same compact JSON serialization and tokenizer on both versions; report UTF-8 bytes as a reproducible count alongside tokens.
Remove every `description` key recursively, including inside arrays, and require identical remaining tool objects for each profile.
This comparison covers names, schema structure, constraints, defaults, enums, and other metadata without imposing a prose-size quota.

## Capability and security claims

Describe configured capabilities through concrete operations, without a general availability disclaimer in the tool description.
Keep consequential package and connection restrictions beside the fields that need them.
Distinguish supported capabilities from installed dependencies.
An advertised SQL field does not prove that DuckDB, SQLite extensions, or an R/Python bridge is available.
Custom workers do not inherit the built-in catalog or package defaults.
Host preparation examples require resolver support; bare runtimes and explicitly selected Python require preinstalled dependencies.
Requirement inspection is a declaration, not an installed-package inventory.

Native prose must reflect selected filesystem and network policy.
Name private `TMPDIR` storage (`TEMP` and `TMP` too on Windows) and concrete writable paths from the normalized policy.
Do not expand paths or probe filesystem permissions.
Describe write grants as subject to more specific read/deny rules, rather than promising writes throughout each tree.
Workspace metadata is protected by default, not by an unchangeable denial ceiling; explicit native rules can alter those defaults.
A `read` entry grants reads as well as narrowing writes.

Review the resulting handshake as an agent would: can it choose and call the tool without reading implementation details, and are its safety claims true for the configured session?
