# Requirements and environments

Local [Windows](WINDOWS.md) sessions use the same managed R/Python resolution and hidden `resolve` subcommand as macOS and Linux.
Windows SQL remains unavailable, and Windows defaults do not prepare DuckDB extensions.

The server retains dependency declarations and resolved environments across worker generations.
Preparation makes packages or extensions **available**; it does not attach R packages, import Python modules, or load DuckDB extensions.
[Send ordering](SEND_OPERATIONS.md) defines when preparation, control, input, and code run.
[Host resolution and trust](#host-resolution-and-trust) is essential: installation runs outside the worker sandbox.

## Retained environments

The retained environment combines an R library/declaration, a normalized Python manifest and inspected interpreter identity, and DuckDB extension names.
It lives in server memory, not across server processes.
A plain restart reuses accepted requirements, including successful automatic additions, without resolving again.

| Target/environment                    | Preparation                                                                                                  |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| Local/SSH with R and resolver support | Managed R, Python when not explicitly selected, and R-backed DuckDB extensions.                              |
| Local/SSH without R                   | Managed Python and extensions through execution-host uv, or an explicitly selected non-managed Python.       |
| Bare R-capable runtime                | Preinstalled packages/adapters; inspection only.                                                             |
| Docker/SBX                            | Preinstalled image/template; inspection only. No implicit resolver or installation.                          |
| Custom worker                         | No built-in defaults; explicit R/DuckDB support, with worker receipts for live R changes. No managed Python. |

The default optional declarations are:

| Environment | Defaults                                                                    |
| ----------- | --------------------------------------------------------------------------- |
| R           | `tidyverse`, `reticulate`, `DBI`, `duckdb`, `arrow`, `nanoarrow`, `yyjsonr` |
| Python      | `numpy`, `pandas`; also `duckdb` without R                                  |
| DuckDB      | `icu`, `json`, `sqlite` with R; `sqlite` without R                          |

Mixed-runtime R infrastructure is separate: reticulate, jsonlite, DBI, DuckDB, Arrow/nanoarrow, pillar, tibble, and utf8 support the bridge and SQL.
Clearing optional requirements does not remove that infrastructure, ambient libraries, preinstalled packages, or caches.
Without R, an empty Python declaration omits DuckDB, but a user-selected DB-API connection can still provide SQL.

Default preparation, built-in worker launch, and enabled R/Python initialization run in the background.
Transport readiness connects input and resolver services before startup hooks run.
When SQL is enabled and its optional provider is installed, bootstrap opens the managed DuckDB connection; first-query work remains lazy.
Discovery and first-use preparation are different stages.
See [shared readiness](SEND_OPERATIONS.md#server-readiness), including early requirements and replacement of an unused prewarmed worker.

## Inspecting and replacing requirements

| Action          | Meaning                                                                                       |
| --------------- | --------------------------------------------------------------------------------------------- |
| `get`           | Read the last committed declaration; no resolution, output consumption, or worker launch.     |
| `add` (default) | Accumulate requirements; exact repeats are no-ops.                                            |
| `set`           | Replace the whole declaration; omitted lists/constraints are empty. No defaults are injected. |
| `reset`         | Restore startup defaults, removing explicit and automatic additions from the declaration.     |

```python
send(requirements={"action": "get"})
send(requirements={"python": ["requests[socks]>=2,<3"]})
send(control="restart", requirements={"action": "set", "python": ["requests>=2"]})
send(control="restart", requirements={"action": "set"})  # No optional requirements.
send(control="restart", requirements={"action": "reset"})
```

`get` rejects code, stdin (even empty), control, and payload fields.
`reset` rejects payload fields too.
A bare `{}` is invalid.
Changed `set`/`reset` on a worker that has accepted user execution requires explicit restart; unchanged replacement is a no-op, though an explicitly requested restart still occurs.
The [startup exception](SEND_OPERATIONS.md#server-readiness) applies only to an unused prewarmed candidate.
Interpreter initialization alone does not require explicit restart for the first `set` or `reset`; user code or nonempty stdin ends this exception.
Failed candidate preparation resumes the existing bootstrap; successful replacement retires it before its successor's hooks run.
Removing declarations does not uninstall or prohibit later runtime use, and automatic resolution may acquire packages again.

Inspection returns `requirements`, `prepared`, and `runtime_requirements` in structured content.
It is a declaration, not an installed-package inventory.
While resolution is pending, it shows only the last commit.
Large manifests are complete in structured content even when omitted from the bounded text preview.
Copy and edit the `requirements` object, then add `action: "set"` to round-trip it.

`python_version` is a list of constraints; `exclude_newer` is a publication cutoff string or null.
Add accumulates version constraints and can fill an unset cutoff, but cannot replace an existing cutoff.
Set clears omitted constraints; reset restores startup constraints.
Effective constraint changes on a live worker need restart.
These actions do not rewrite captured resolver configuration.

## Requirements for a cell

Requirements may accompany any supported cell language, or stand alone to stage dependencies.
They are preconditions: failed preparation withholds that cell.
One environment transition covers preparation through admission, so another call cannot change the environment between them.
Standalone preparation rejects nonempty stdin and returns `[prepared]` on success; bundled preparation adds no marker.
See [operations](SEND_OPERATIONS.md#operations) for partial effects after interrupt and for wait timing.

## Accepted requirement input

Add accepts at most 64 entries per language per call.
Set accepts a complete accumulated manifest without that per-list limit.

| Field            | Accepted input                                                                                                                                                  |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `r`              | Nonempty, single-line `ir` references, with no NUL/CR/LF; explicit remote references are allowed, local sources are rejected by `ir`.                           |
| `python`         | Named PEP 508 registry requirements, including extras, versions, and markers. No paths, editable requirements, file URLs, archives, or `name @ URL` references. |
| `duckdb`         | Names up to 64 ASCII characters: lowercase letter first, then lowercase letters, digits, or underscores. No repository/version/URL/SQL selectors.               |
| `python_version` | Version numbers and supported PEP 440 `==`, `!=`, `<`, `<=`, `>`, `>=` constraints, not executable names or paths.                                              |

Automatic R discovery is narrower: plain names beginning with a letter, ending with a letter/digit, and containing only ASCII letters, digits, or dots.
Automatic Python inference supplies one bare distribution name, not versions, extras, or markers.
The server validates worker requests again before resolution.

## Automatic R package resolution

Managed R resolves missing packages reached through `library()`, `require()`, `requireNamespace()`, `loadNamespace()`, `::`, and `:::`.
Source is not scanned; unreachable code does nothing and the cell is never replayed.
Explicit `lib.loc`, partial namespace loading, already available packages, and library help/listing calls retain ordinary behavior.
`install.packages()` is not intercepted.

The adapter preserves base call frames and the original `loadNamespace()` body and retry path.
This matters for native conditions, tracebacks, and packages that inspect base functions.
Failed automatic resolution preserves the original operation's behavior, including `FALSE` from missing-package `require()` / `requireNamespace()`; explicit preparation instead reports resolver diagnostics.

The host resolves the complete candidate and retained extensions.
The worker applies the candidate `.libPaths()` and reports `RActivated`; only a matching current-generation receipt commits it.
The original load then resumes, attaching only when that operation normally attaches.
A later namespace/cell failure does not undo acceptance.
An activation failure leaves recoverable state available but requires restart before further requirement changes.

An idle automatic callback owns the environment transition until its receipt.
Concurrent explicit preparation cannot resolve and commit a stale snapshot; whichever operation already owns the transition takes precedence.

## Automatic Python import resolution

A last-chance finder runs after Python's existing import finders.
Available standard-library, installed, local, and loaded modules resolve normally; availability queries such as `find_spec()` do not install packages.

Inference maps known differences (`yaml` → `pyyaml`, `PIL` → `pillow`, `sklearn` → `scikit-learn`) and otherwise uses a conservative top-level same-name fallback.
It declines ambiguous shared namespaces, missing submodules of an available package, and absent standard-library modules.
Use explicit `requirements.python` for the right distribution, versions/extras/markers, or preparation before use.

Missing optional imports during an installed distribution's eager importlib initialization retain ordinary Python behavior.
Ownership is established from matching installed files in wheel `RECORD`, not package names.
Later library calls and direct imports remain eligible, as do local callbacks and deferred module bodies.
The implementation in `src/python/` owns the precise import-stack and metadata checks; do not replace them with a package-name heuristic.

The finder proposes an additive manifest.
The host resolves and inspects it; the worker activates it and publishes acceptance before retrying the import.
The cell is not replayed.
A differently named mapping gets a bounded notice only after commit.
Even if the distribution lacks the module or later code fails, a successfully accepted environment remains retained.

Resolution is restricted to the configuring worker thread and process, with a reentrancy guard.
Prepare dependencies before starting other threads or fork children; missing imports there cannot invoke the resolver.
Explicit Python, bare, and prepared targets disable managed resolution.
Already available imports still work.

## Live preparation

Changed additions require an idle worker, except automatic resolution within its own evaluation.
A busy or starting worker does not silently queue standalone preparation; stopped workers with new requirements need restart.
Live R/Python preparation is noninteractive: an outstanding or newly raised managed input request during it is a failure that stops the worker.
Finish interactive work before preparing dependencies.

### Live R preparation

The worker replaces its managed library entry, preserving other paths and live objects.
A sandbox's temporary writable library remains first.
The server commits only the confirmed normalized path.
Ordinary activation failure can leave live library state different from retained state: preserve the worker for recovery, but require restart for further changes.
Transport/protocol/infrastructure failure instead stops a worker whose state cannot be trusted.

### Live Python preparation

Console uses the same native requirement owner with or without R; reticulate metadata is an optional compatibility projection.
Before initialization, preparation can materialize a selection without starting either interpreter.
After initialization, a candidate must match the loaded `libpython` identity and running version and must not change or remove a loaded distribution's version.
Add is not a promise that arbitrary upgrades are safe: a newly added package can change unloaded/transitive dependencies.
Use restart for changes needing fresh imports; changing an already declared distribution requires replacement.

Compatible activation preserves the interpreter, objects, catalog, and selected connection.
It replaces environment-owned paths while preserving user paths and updates child-process selection.
Publication and local commit defer interrupts.
A successfully restored interrupted activation can retry; arbitrary site-hook effects cannot be rolled back, so other unsafe activation failures require restart.
Pre-mutation failures leave the accepted environment usable.

A lazy `reticulate::py_require()` declaration is not yet server-retained.
It is retained after successful initialization or explicit materialization.
Successful activation is its own commit boundary: it survives later import/cell failure or a following live R failure in the same request.

### DuckDB extension preparation

The trusted host uses DuckDB's installation API and normal repository/signature checks.
Loading happens later in the worker.
No SQL catalog, user connection, or runtime object is replaced by an extension-only addition.

R-backed preparation covers each retained R library that may have supplied the current DuckDB.
Python-backed preparation uses the accepted or candidate Python's DuckDB and the captured shared cache.
A changed Python candidate prepares the whole retained extension set even when names are unchanged.
Combined Python and extension additions prepare everything before activation.
Missing DuckDB in a replacement manifest makes extension preparation fail; include `duckdb` explicitly.

Removing a declaration does not remove its cache entry.
Prepared Docker/SBX connections can load preinstalled extensions but disable automatic installation.

## Restarting with requirements

Before startup or restart, resolve and inspect all candidates, then commit the complete retained environment before retiring the old worker.
Failure before commit preserves the old worker and declaration; same-call input/code is withheld.
Once retirement begins, later failure cannot undo the commit or restore live state.
Plain restart uses accepted selections without another resolution.

## Python environment selection

An explicit Console `python` path takes precedence over inherited `RETICULATE_PYTHON`.
For the legacy variable, unset/empty/`managed` means managed; other values select an existing environment.
An explicit selection disables managed Python from every request path.
With R, R and its DuckDB preparation can remain available; without R, host extension preparation is disabled too.
See [configuration](CONFIGURATION.md#python-environment-selection).

Without R, local/SSH preparation uses startup PATH uv and ignores `RETICULATE_UV`; there is no PATH-Python fallback.
R-present preparation prefers PATH `ir`, then uv (`uv tool run --from r-lib-ir ir`), with reticulate bootstrap when needed.
Selected broken tools fail rather than silently selecting alternatives.
`ir` must be at least 0.4.0 and receives the exact selected Rscript.
No available R-present bootstrap means a bare runtime; a bootstrap that fails is an error, not bare mode.
On Windows, resolver processes enter an owned Job before executing; completion requires confirmed descendant retirement.
Interrupt terminates the active resolver Job, preserving the previously accepted environment and worker state.

## Custom workers

Custom workers have no built-in defaults and no managed Python.
Explicit R candidates include DBI, DuckDB, and jsonlite infrastructure and are supplied as `R_LIBS`.
Live R additions require the [worker preparation contract](WORKER_PROTOCOL.md); optional runtime R callbacks must confirm or reject every candidate.
Apply the managed library before loading DuckDB and use its normal extension cache.

## Host resolution and trust

Local `mcp-console resolve` and SSH's trusted preparation owner run with execution- host account permissions, **outside the worker sandbox**.
Installation, builds, Python startup hooks, and cache warming can execute package code there.
Use only trusted requirements, resolvers, configuration, and package sources.

R references become separate `ir` arguments with `IR_NO_LOCAL_SOURCES=1`; Python requirements become validated uv arguments, and DuckDB names are data.
Submitted cells and `send` stdin are not resolver programs.
These restrictions reduce input syntax; they do not make remote package code safe.

**Worker-modifiable resolver inputs are an unresolved escape path.** Capturing paths/environment values does not freeze the files they name.
A worker that can replace a selected uv wrapper, or write a wheel directory selected by `UV_FIND_LINKS` / uv configuration, can cause later preparation to execute its code with host permissions.
Console does not yet isolate those inputs or sandbox resolvers.
Native worker enforcement is not protection against this route.

### Host resolver uv configuration

The preparation owner captures startup `UV_*` values except `UV_OFFLINE`, restores that snapshot for later calls, and uses its captured uv selection.
R-present sessions respect `RETICULATE_UV`; the special `managed` value uses reticulate's managed tool/cache.
Environment changes in evaluated cells do not configure later host resolution, though mutable files still can.

Managed environment creation removes `UV_NO_CACHE` because uv would otherwise delete the selected environment on exit.
Worker code starts with `UV_OFFLINE=1`, including under `--no-sandbox`; that variable configures uv, not process-level network enforcement.
Linux package builds may need system development libraries; see [build prerequisites](../RELEASE.md#private-sandbox-executable).

## Failure atomicity and cache effects

Transactions protect accepted server state, not resolver caches or arbitrary installation effects.
Downloads/builds/extension files can remain after failure.
Prestart/restart candidates commit together; live activations have independent receipts and can partially succeed.
Successfully activated Python remains after a later R failure; accepted R remains after namespace failure.
Preparation failure withholds an accompanying cell, but does not undo a preceding interrupt or input enqueue.
Generation checks discard unaccepted old candidates, never commit them for a replacement.

[Recording](RECORDING.md) distinguishes accepted manifests, source declarations, and replacement boundaries in the journal and generated report.
