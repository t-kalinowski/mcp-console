# MCP tool descriptions

Tool descriptions recur in agent context.
Include information that changes tool choice, call construction, or result interpretation; keep architectural explanations in the guides.

## Source and stability

[`src/server/presentation.rs`](../src/server/presentation.rs) composes named [prose sections](../src/server/presentation/sections.rs); [`src/server/arguments.rs`](../src/server/arguments.rs) defines the input schema.
The [canonical handshake snapshot](../tests/snapshots/client_server/server/test_tools/initializes_and_lists_tools.yaml) records it.
Regenerate that snapshot deliberately through the [boundary tests](../tests/boundaries/README.md), never by editing expected output.
Writable-root companions capture the extra grant; affected fixtures use separate direct/sandbox snapshots because their advertised policies differ.
Direct-handshake snapshots are shared across supported platforms; the discovery summary does not name the operating system.

Descriptions depend on captured configuration, not completed runtime discovery.
The presentation profile selects sections from configured languages, built-in or custom worker selection, and host preparation.
Platform conditionals select temporary-directory guidance without matching or removing sentences.
For the same configuration, they stay stable as background startup finishes or fails.
The captured `languages` list selects direct code fields, field guidance, and language-sharing sections together.
Omission uses the `MCP_CONSOLE_LANGUAGES` filter; neither discovery nor interpreter initialization changes the advertised schema.
Hidden source keys, including null values, are rejected before same-call effects.
Requirements remain visible for hidden SQL providers, and execution still checks actual runtime availability.
For both `languages` configuration and the environment filter, tool and exposed-field descriptions mention only visible languages, including requirements, control, and timeout guidance.
Tool-level SQL guidance describes file queries directly; runtime diagnostics identify any missing dependencies and preparation needed.
Requirement keys remain available even when their language is hidden; their descriptions then explain host-provider preparation without advertising a direct code field.
Visibility selects direct code fields and applicable examples, not SQL ownership: managed DuckDB still uses R when available and Python otherwise.
Show bridge examples only when both source languages are visible.
When Python and SQL are visible but R is hidden, describe user-owned DB-API selection and `_console.sql_connection()` as the active native getter, which errors for an R-owned connection; frame registration requires Python-owned DuckDB.

Advertising an unavailable language lets an agent identify the missing prerequisite and ask for installation authorization, at the cost of a rejected call before discovery is known.
It neither proves availability nor authorizes installation.
Installing a runtime requires a new server session; worker restart retains captured selection.

## Deferred tool discovery

Client behavior checked on 2026-10-09.
These are client presentation limits; recheck the linked sources when changing discovery guidance.

The `send` description returned by `tools/list` and the server `instructions` returned by MCP initialization serve different purposes.
Console publishes the same profile-specific summary in server instructions and at the start of the tool description, as the [canonical handshake](../tests/snapshots/client_server/server/test_tools/initializes_and_lists_tools.yaml) records.
The summary names the configured languages, describes when to choose Console, and explains why retained state helps.
Custom-worker summaries leave runtime capabilities to the worker.
Call construction begins in the next paragraph, so lazy-loading clients can choose the server without first loading the tool definition.

### Codex

For regular MCP servers, Codex uses initialization `instructions` as the namespace description ([source](https://github.com/openai/codex/blob/36ae1561b9324c93d5638b45eb19fe2cc070a581/codex-rs/codex-mcp/src/rmcp_client.rs#L839-L854)).
The optional `deferred_tool_world_state` feature is [disabled by default upstream](https://github.com/openai/codex/blob/36ae1561b9324c93d5638b45eb19fe2cc070a581/codex-rs/features/src/lib.rs#L1551-L1555).
When enabled, its [namespace preview](https://github.com/openai/codex/blob/36ae1561b9324c93d5638b45eb19fe2cc070a581/codex-rs/core/src/context/world_state/tools.rs#L25-L75) takes only the first line, trims surrounding whitespace, and retains at most 250 Unicode characters.
A longer line becomes its first 247 characters followed by `...`; truncation can split a word or sentence.
This is a namespace-preview limit, not a universal limit on tool descriptions.

The rendered namespace block has a shared 4 KiB UTF-8 budget, including tags and formatting.
The [budget allocator](https://github.com/openai/codex/blob/36ae1561b9324c93d5638b45eb19fe2cc070a581/codex-rs/core/src/context/world_state/tools_budget.rs#L46-L115) reserves namespace names first, then shares remaining description space one character per namespace at a time.
Descriptions can therefore be shortened further or omitted; names can also be omitted if the names alone exceed the budget.

### Claude Code

With tool search enabled, Claude Code initially loads tool names and server instructions.
Discovery loads the selected tool definitions, including their descriptions and input schemas; the [documented behavior](https://code.claude.com/docs/en/mcp#scale-with-mcp-tool-search) does not define a separate short preview extracted from each tool description.
Tool search is enabled by default on supported configurations.

Claude Code [truncates each tool description and each server's instructions at 2,048 characters by default](https://code.claude.com/docs/en/mcp#for-mcp-server-authors).
This cap applies to the whole text, with no documented first-line or sentence-count rule.
Users can change it for every MCP server in a session with [`CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH`](https://code.claude.com/docs/en/env-vars#variables), available since v2.1.280; it accepts a positive whole number of characters.

For server discovery instructions, describe the tasks, when to select Console, and its key capabilities first.
Keep the first line within 250 characters for the Codex preview, with any further guidance before Claude Code's default 2,048-character cap.
Keep full call construction and result interpretation guidance in the tool definition.

## Editorial rules

Start with a discovery summary within 250 characters on one line.
Include debugging, simulations, and comparing approaches alongside calculations, data analysis, queries, and plots; Console also supports iterative development work.
Follow it with complete-cell and automatic-display guidance.
Keep ordinary tool descriptions within Claude Code's default 2,048-character cap; configured writable paths can expand the launch-policy prose.
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
State argument defaults in prose, including at tool level, because clients may omit schema defaults and field descriptions from agent context.
When an agent is waiting for completion, recommend a long poll (`timeout_ms=300000`) and explain that it returns early on completion or an input request; discourage repeated short polls such as `timeout_ms=1000`.
Do not assume a client has this repository or remove a capability's only usable explanation in favor of an external guide.

Prefer concrete choices: SQL for structured-file/database inspection and aggregation, R for vectorized/statistical work, Python when its libraries fit the task.
Make cross-language guidance conditional on configured languages.
Built-in R guidance includes a compact bridge to [saved scripts](REQUIREMENTS.md#saving-an-r-script); omit it when R is hidden or the worker is custom.
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
