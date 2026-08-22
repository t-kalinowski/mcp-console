# Requirements and environments

**Status:** Implemented current behavior

This document describes how MCP Console prepares and retains R packages, Python packages, and DuckDB extensions.
The first sections are operational: they explain what `session(action = "prepare")` and `session(action = "restart")` do.
[Host resolution and trust](#host-resolution-and-trust) explains why requirement input is restricted and which work runs with server permissions.

Prepared requirements configure the built-in worker; they do not attach an R package, import a Python package, or load a DuckDB extension.
Runtime use is covered by the [built-in runtime guide](BUILTIN_RUNTIME.md).
Exact live-worker messages and custom-worker receipts belong to the [worker protocol](WORKER_PROTOCOL.md).

## Retained environments

MCP Console retains one environment configuration in server memory:

- a resolved R library and its complete R requirement set;
- a selected Python environment and normalized Python manifest; and
- a set of prepared DuckDB extension names.

The sets are additive.
Repeating an accepted requirement is idempotent, and a restart reuses everything retained so far.
The current API has no operation to remove a requirement, replace a manifest, select a named environment, or persist the retained configuration across server processes.

The built-in server prepares these defaults before accepting MCP input:

| Environment | Defaults |
| --- | --- |
| R | `tidyverse`, `github::rstudio/reticulate`, `DBI`, `duckdb`, `arrow`, and `nanoarrow` |
| Python | NumPy and pandas when Python is server-managed |
| DuckDB | ICU and JSON extensions |

Packages supplied by these environments are available but are not attached or imported automatically.
The default DuckDB extensions are installed in DuckDB's native cache but are loaded only when DuckDB needs them inside the sandbox.

A custom worker skips all three default preflights.
Its more limited requirements contract is described under [Custom workers](#custom-workers).

## Preparing requirements

Use `session` with `action = "prepare"` to add requirements without replacing the worker:

```json
{
  "action": "prepare",
  "requirements": {
    "r": ["data.table"],
    "python": ["polars>=1"],
    "duckdb": ["fts"]
  }
}
```

`prepare` requires at least one entry across the three arrays.
Each array accepts at most 64 entries.
Successful preparation returns `[prepared]`.

Before a worker starts, the server resolves every changed candidate on the host, commits the retained configuration only after the complete request succeeds, and does not start the worker.
Exact repeats return `[prepared]` without resolving them again.

After a worker starts, preparation is available only while it is idle:

- R additions can update a worker that implements live R preparation.
- Compatible Python additions can update an idle server-managed built-in worker.
- DuckDB extensions are installed on the host without replacing the worker.

Preparation during an active cell is rejected.
If preparation overlaps worker startup, it returns `[requirements not prepared: worker is starting]` without resolving or retaining the additions.
If the worker is stopped, new additions return `[restart required]`; use restart to prepare them and start a replacement.

### Live R preparation

The server resolves a new library containing the complete retained R requirement set.
The worker prepends that library to `.libPaths()`, removes the previous managed library entry, and preserves its other library paths and in-memory state.
The server retains the candidate only after the worker confirms the normalized library path.

An ordinary live R preparation failure leaves the worker available for evaluation, because the caller may need to save in-memory state.
Its live library path may have changed before the failure, so the server does not accept more requirement changes in that generation.
The failed call reports that further changes require restart, and later additions return `[restart required]`.
A successful explicit restart clears this state.

An R transport, protocol, or bridge-infrastructure failure is different: the server stops a worker whose state is no longer known to be usable.

### Live Python preparation

Python additions use reticulate's additive requirement model.
Before Python initializes, the worker materializes the complete manifest.
After initialization, reticulate checks that the candidate uses the live `libpython` and activates a compatible environment without replacing the interpreter or its objects.

The server retains a Python environment when the worker reports that reticulate accepted its complete normalized manifest.
A successful activation commits independently of later steps in the same mixed request.
If Python succeeds and a following live R update fails, the Python addition remains retained and is available after restart.

An ordinary Python preparation failure restores the prior reticulate manifest, discards unaccepted candidates, and leaves the worker usable.
By itself, this failure does not make restart mandatory.
A Python transport, protocol, or bridge-infrastructure failure stops the worker instead.

Before Python first initializes, an evaluated `reticulate::py_require()` can change only the live worker's lazy manifest.
That change becomes retained after successful Python initialization reports its activation, or after an explicit preparation materializes it.
Worker loss before either point loses the uncommitted declaration.

### Live DuckDB preparation

DuckDB extension installation occurs entirely on the host.
There is no DuckDB-specific live-worker request or receipt.
The server installs the complete retained extension set for each relevant resolved R library, so the current worker and later generations can use the extension with their DuckDB version.
It then retains the extension names without changing the worker's R, Python, SQL, or catalog state.

Preparation does not load extension code.
A later `LOAD` or DuckDB automatic load occurs inside the worker sandbox.
DuckDB chooses its compiled default extension repository and version-and-platform native cache; MCP Console does not accept a repository, URL, path, or version selector.

When a DuckDB request also needs a new R library, the worker still uses live R preparation for that library.
Its success and failure semantics therefore follow the R rules above.

## Restarting with requirements

`session(action = "restart")` can add requirements while replacing the worker:

```json
{
  "action": "restart",
  "requirements": {
    "r": ["praise"],
    "python": ["py-yaml12"],
    "duckdb": ["spatial"]
  }
}
```

Omit `requirements` to restart with the retained configuration unchanged.
If additions are present, the server merges them into the complete retained sets and resolves every changed candidate before it stops the current worker.
The R library, Python environment, and DuckDB extension set commit together only after all required host resolution succeeds.

A resolution failure leaves the current worker, its in-memory state, and its retained configuration unchanged.
After successful resolution, restart retires the worker and starts a replacement from the new retained environment.
Restart always loses the old worker's R, Python, SQL, debugger, and unread-stdin state.
The [implemented architecture](ARCHITECTURE.md) owns the replacement lifecycle; [Explicit restart](BUILTIN_RUNTIME.md#explicit-restart) describes its user-visible response ownership and notices.

## Accepted requirement input

### R

R requirements are IR package references.
MCP Console owns only the framing checks: each request may contain at most 64 nonempty strings, and a string may not contain NUL, carriage return, or newline.
IR owns the accepted package reference syntax and dependency resolution.

The server requires `ir` 0.4.0 or later on `PATH`.
It passes each requirement as a separate `ir run --with` argument; requirement text is never inserted into R source.
Every invocation sets `IR_NO_LOCAL_SOURCES=1`, so IR rejects direct or transitive installation from the local filesystem.
Remote package installation and build code still run with server permissions.

### Python

Managed Python accepts named PEP 508 registry requirements.
Package extras, version specifiers, and environment markers are supported, for example:

```text
requests[socks]>=2,<3; python_version >= '3.10'
```

Paths, `file:` URLs, editable requirements, direct references such as `name @ URL`, and local archives or projects are rejected before a resolver starts.
A request may contain at most 64 entries.

Reticulate can also request a Python version during managed operation.
The server accepts version numbers and `==`, `!=`, `<`, `<=`, `>`, and `>=` PEP 440 specifiers.
Interpreter names, executable paths, and installation directories are not accepted as version constraints.

### DuckDB

DuckDB requirements are extension names.
A request may contain at most 64 names.
Each name must be at most 64 ASCII characters, start with a lowercase ASCII letter, and otherwise contain only lowercase ASCII letters, digits, and underscores.

The resolver treats a validated name as data and issues DuckDB's own `INSTALL` for a quoted identifier.
Paths, URLs, repository selectors, version expressions, and SQL fragments are not accepted.

## Python environment selection

The built-in server reads inherited `RETICULATE_PYTHON` when it starts:

- unset, empty, or exactly `managed` selects the server-managed environment;
- any other nonempty value selects that existing Python environment.

A user-selected environment is preserved for the worker.
The server skips its managed-Python preflight and rejects Python additions from `prepare`, `restart`, or worker-originated managed-resolution requests.
R requirements and DuckDB extensions remain available.
The selected interpreter must still satisfy the [built-in runtime](BUILTIN_RUNTIME.md) requirement for Python 3.10 or later and must initialize under the worker's offline policy.

This selection is independent of custom-worker policy.
A custom worker always rejects managed Python requirements, regardless of `RETICULATE_PYTHON`.

## Custom workers

Custom workers start without the built-in R library, managed Python environment, or default DuckDB extensions.
They can use explicit R requirements and DuckDB extensions, but managed Python requirements are unavailable for both prepare and restart.

Every explicitly resolved custom-worker R candidate includes DBI, DuckDB, and jsonlite so the host can prepare later DuckDB extensions with the same library.
The server supplies the retained library through `R_LIBS` at worker launch.
A running custom worker must implement live R preparation and confirm the applied library as specified by the [worker protocol](WORKER_PROTOCOL.md).

Prepared extensions remain in DuckDB's native default cache.
A custom worker must use that cache when it loads them.
It must also apply its first managed R library before loading DuckDB; a DuckDB namespace loaded earlier from inherited libraries is outside this contract.

## Host resolution and trust

The worker sandbox denies direct network access and regular writes outside its private temporary directory.
Dependency resolution is a deliberate exception to that boundary: the server launches R, Python, and DuckDB resolvers on the host, outside the sandbox.

Host resolvers may access the network and their normal caches.
R and Python package installation can execute package installation or build code with the server's filesystem and process permissions.
Managed Python environment startup and Matplotlib font-cache warming can also import or execute selected package code.
Use only trusted requirements and trusted resolver configuration.
`IR_NO_LOCAL_SOURCES` and the Python and DuckDB validation rules reduce the accepted input surface; they do not make arbitrary remote packages safe.

Resolver inputs do not contain submitted cells or `send` stdin:

- R requirements are individual process arguments to IR, which receives a constant R program.
- Python manifests and version constraints are JSON data on resolver standard input.
- DuckDB extension names are validated JSON data and are not submitted SQL.

Evaluated code can trigger managed Python resolution through reticulate, but the same named-registry and version-constraint validation applies before a host resolver starts.

### Server-owned uv configuration

When the server starts, it captures inherited `UV_*` variables except `UV_OFFLINE`.
Before each managed-Python resolver starts, it removes the current `UV_*` environment, restores that startup snapshot, and removes `UV_OFFLINE`.
Changes made later by evaluated R or Python code therefore cannot configure host resolution.

The built-in worker has the opposite network policy: it forces `UV_OFFLINE=1` before user code runs inside the network-denied sandbox.

## Failure atomicity and cache effects

Before worker startup and during restart, the server commits the retained R, Python, and DuckDB candidates only after all requested resolution succeeds.
A failure does not change the retained manifest or replace the current worker.

That transaction covers server-owned state, not external resolver caches.
IR, uv, reticulate, and DuckDB may download, build, or install files before a later step fails.
For example, an earlier extension in a failed multi-extension request can remain in DuckDB's native cache without entering the retained extension set.
A future request may reuse such cache entries.

Live preparation has one additional commit boundary: a Python activation is retained as soon as the worker reports it.
It is not rolled back if a later R step in the same request fails.
R and DuckDB changes commit only after their complete live operation succeeds.
The earlier sections describe how ordinary Python failure, recoverable R failure, and infrastructure failure affect the current worker.
