# Proposed custom-resolver contract

**Status: proposed, unapproved, and unimplemented.** This sketch considers an optional custom materializer for local-host sessions.
Tomasz must approve the product and security choices before a separately bounded implementation is considered.
It is deferred investigation, not a release prerequisite.
It does not select configuration syntax, an executable interface, or a plugin system.

## Existing foundation

Console already [validates dependency declarations](../docs/REQUIREMENTS.md#accepted-requirement-input) and admits changes under captured per-runtime acquisition policies.
Installed packages remain usable when Console acquisition is disabled or unavailable.
Acquisition policy, preparation support, and [execution permissions](../docs/RESOLVER.md) are separate constraints.
The current resolver configuration has no custom executable setting.

[Runtime selections](../src/local_runtime.rs) are retained across worker generations; explicit R installation and Python executable/environment identities are inspected.
The selection decisions are immutable within their captured scope, not the bytes at their paths.
Managed environment changes still require the existing candidate/acceptance flow; live Python candidates must match the running interpreter and loaded distributions.

The [preparation owner](../src/resolver/preparation.rs) owns discovery, materialization, inspection, and operation cleanup.
The [server, worker, relay, and native runner](../docs/ARCHITECTURE.md#ownership) retain declaration, activation, transport, and retirement ownership.
`Mode::Custom` in preparation concerns custom workers; it is not a custom-resolver API.
These owners and their confirmed-retirement barriers would remain in place.
Console-owned Docker, SBX, and SSH targets are outside this proposal.

## Proposed preparation boundary

Console would validate and admit the complete logical declaration before invoking a custom resolver.
The resolver would receive that declaration separately from physical preparation constraints: available inspected runtime identities, captured managed-selection constraints, startup/restart/live intent, and authorized environment and output locations.
Live interpreter constraints may narrow physical resolution without rewriting the retained logical declaration.
Existing declaration syntax and captured acquisition policies would remain authoritative.

Accepting a command string would confer no preparation capabilities or additional permissions.
Console would check required preparation support against operator-approved capabilities; the executable's self-report would not grant authority.
Unsupported required work would fail before materialization, without silently switching resolver or runtime.
The resolver could neither unlock a policy nor relax captured worker/resolver permissions, change their environment capture, override an explicit runtime selection, or select a different live interpreter.
Explicit runtime inspection would retain its existing worker-permission boundary; materialization would retain resolver permissions.

## Proposed session preparation responsibilities

Runtime presence, approved resolver support, and policy permission would each be necessary for the requested preparation.

| Session     | Preparation responsibilities when admitted                                                                                                                                                                                                                                                                          |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R-only      | Prepare declared R packages and required infrastructure for the inspected R installation, returning a candidate managed library. Prepare requested DuckDB extensions for the selected managed R provider when applicable.                                                                                           |
| Python-only | Prepare a managed Python environment satisfying the declaration and inspected interpreter constraints. Prepare requested DuckDB extensions for its usable managed Python provider when applicable. An existing Python environment retains preinstalled packages/extensions and bypasses managed Python acquisition. |
| Mixed       | Support each admitted R/Python portion and the selected managed SQL provider's extension preparation. Preserve separate runtime acceptance points and the current SQL connection; preparing both portions does not authorize switching interpreters or SQL providers.                                               |

An R runtime can remain usable through installed libraries without Console preparation.
An existing Python environment would remain externally owned, not become a managed installation target because a resolver supports Python.
Console would distinguish owned temporary outputs, managed environments/caches, and externally owned installations in each request.
Results could reference external installations only for an explicitly approved use; they could not authorize modifying them.
SQL provider selection follows actual runtime capability, independently of public language visibility.
A user-selected database connection would confer no acquisition authority.

## Proposed results and receipts

A materialized result would identify its operation and candidate, prepared locations, and runtime identity/compatibility information for Console inspection.
Console would reject malformed, mismatched, duplicate, unsolicited, or stale results; output paths alone would not authorize activation.
Resolver exit status or a claimed successful inspection would not constitute declaration acceptance or cleanup evidence.

The existing acceptance paths would remain:

- Before interpreter initialization, Console can accept a fully prepared and inspected selection without a live activation receipt.
  Restart prepares and accepts the complete candidate before retiring the old worker; preparation failure preserves the old worker and declaration.
  Failure after retirement cannot restore live state.
- For live R/Python changes, the worker validates and activates the provisional runtime candidate under the existing [activation contract](../docs/WORKER_PROTOCOL.md#live-python-preparation).
  Only the matching acceptance receipt for the current operation, candidate, and generation can commit the retained declaration; neither resolver nor relay decides that commit.
- For live DuckDB extension-only changes, the [server](../src/worker_client/environment/preparation.rs) commits the retained extension declaration after successful host preparation and a current-generation check.
  No worker activation or acceptance receipt is required.
- R and Python have separate acceptance points.
  Accepted activation survives a later import, package load, cell, or other preparation failure.
  Unaccepted candidates are discarded when their operation ends or generation retires; mixed preparation is not an atomic rollback of accepted runtime effects.

Each operation would report a terminal success or error separately from control acknowledgment and cleanup confirmation.
Ordinary preparation errors would remain distinct from interruption, cancellation, and transport/cleanup failures that end the owning boundary.
Control acknowledgment would not prove completion; cancellation would settle through the existing owner and event-driven waits.
A late control acknowledgment would not replace an independent terminal error.
Cancelling an MCP call's wait would not itself cancel shared startup.

The preparation owner would publish a candidate only after operation process/I/O cleanup is confirmed.
Connection retirement would remain a separate receipt, obtained after owned process and I/O settlement; unconfirmed cleanup blocks subsequent preparation or replacement.
Native descendant evidence must come from the existing runner/Job owner, not a resolver self-report, direct-child exit, or pipe EOF.
The [supported-host lifetime limits](../docs/SANDBOX.md#supported-hosts-and-lifetime-limits) still apply, including `--no-sandbox` limits.
Cleanup would retire owned resources; it would not roll back downloads, shared cache writes, build/site hooks, or already accepted mixed-session effects.

## Trust and ownership

The operator would own the decision to trust the executable, wrappers, configuration, package sources/build hooks, and prepared runtime code.
Console would capture the selected executable/configuration and permitted environment/output scope for the connection, while retaining ownership of admission and acceptance.
The resolver would execute within that captured scope, not supply policy changes through its outputs.
Inspection checks identity and compatibility, not package-code safety or complete protection against concurrent replacement.

The current [trust boundary](../docs/RESOLVER.md#trust-boundary) remains: worker-writable tools, configuration, installations, or caches can influence later preparation.
Captured paths do not freeze contents; private cache placement neither validates code nor protects secrets readable by preparation.
Sandboxed preparation on macOS/Linux uses captured resolver permissions; Windows and `--no-sandbox` use host permissions.
A Windows cleanup Job is not a sandbox.

## Decisions requiring Tomasz's approval

- How an operator selects and trusts the executable/configuration and explicitly approves its preparation capabilities, independently of resolver self-report.
- Which managed output forms and references to externally owned environments a first implementation supports, and whether it supports live activation or only startup/restart.
- Whether worker-writable resolver/configuration/artifact inputs are rejected or documented as an unsupported isolation setup; this sketch makes no new enforcement promise.
- Which hosts a first implementation supports and how operators accept host-permission preparation where native enforcement is unavailable or disabled.

Exact configuration/wire design, third-party compatibility commitments, and implementation scope remain unselected.
Any later Console-owned private wire would evolve in lockstep [without an independent version](../docs/WORKER_PROTOCOL.md); this does not establish a stable extension API for independently supplied executables.
Approval of this proposal would not by itself authorize implementation or change the current sandbox contract.
