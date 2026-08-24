# Requirements and environments

**Status:** Implemented current behavior

This document describes how MCP Console prepares and retains R packages, Python packages, and DuckDB extensions.
The first sections are operational: they explain requirements declared for a `send` cell and what `session(action = "prepare")` and `session(action = "restart")` do.
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
Repeating an accepted requirement is idempotent, and a restart reuses everything retained so far, including R packages and Python distributions resolved automatically during earlier cells.
The current API has no operation to remove a requirement, replace a manifest, select a named environment, or persist the retained configuration across server processes.

The built-in server prepares these defaults before accepting MCP input:

| Environment | Defaults |
| --- | --- |
| R | `tidyverse`, `github::rstudio/reticulate`, `DBI`, `duckdb`, `arrow`, and `nanoarrow` |
| Python | NumPy and pandas when Python is server-managed |
| DuckDB | ICU and JSON extensions |

MCP Console applies no deadline to these startup preflights, which run before the MCP transport starts.
`session(action = "interrupt")` and closing MCP input therefore cannot cancel them; if a resolver does not finish, the server does not begin accepting MCP requests.
Host resolution for changed requirements started by `send`, explicit `prepare`, or `restart` also has no deadline.
The call remains pending until the resolver exits; while MCP input is open, `interrupt` sends `SIGINT` to the active resolver, and closing MCP input cancels it during server shutdown.

Packages supplied by these environments are available but are not attached or imported automatically.
The default DuckDB extensions are installed in DuckDB's native cache but are loaded only when DuckDB needs them inside the sandbox.

A custom worker skips all three default preflights.
Its more limited requirements contract is described under [Custom workers](#custom-workers).

## Requirements for a cell

Use the optional `requirements` field on a code-bearing `send` when a cell needs an exact requirement or a package or extension prepared before evaluation:

```json
{
  "python": "import requests\nprint(requests.__version__)",
  "requirements": {
    "python": ["requests[socks]>=2,<3"]
  }
}
```

The field has the same R, Python, and DuckDB arrays as `session` requirements and requires at least one entry across them.
It may accompany any cell language because the built-in languages share one worker environment: an R requirement can accompany a Python cell, for example.
It is not accepted on an empty poll or a follow-up stdin call.

Requirements are preconditions of the cell.
The server validates the complete declaration, prepares all changed candidates, and reserves the cell as one operation.
It does not dispatch the cell unless that work succeeds, and no other send, preparation, restart commit, or environment change can interleave between successful preparation and evaluation reservation.
Successful additions are retained for later cells and restarts, but the response is the normal cell response and contains no `[prepared]` marker.
An exact repeat performs no resolver or worker preparation.

The behavior depends on worker state:

- Before the worker starts, the server resolves all changed candidates, commits the complete retained environment only after they all succeed, starts the worker, and evaluates the cell.
- With an idle running worker, the server applies the live R, Python, and DuckDB behavior described below, preserving supported live state, then immediately launches the cell in that worker generation.
- With an eligible stopped worker, the server resolves and retains additions without live preparation, starts the normal replacement, and evaluates the cell there.
- After a recoverable live R failure has made further environment changes require restart, new additions fail with `requirements require session restart; cell was not run`.
  The live worker is not destroyed automatically, so its state can be saved before an explicit restart.

A call may also include `stdin`.
Preparation happens first; after the cell is dispatched, the existing stdin-before-evaluate command ordering applies.
`timeout_ms` begins applying only after dispatch, so preparation can make the complete call take longer than the selected evaluation wait timeout.
`session(action = "interrupt")` still targets an active host resolver, and explicit restart or closing MCP input retains its existing resolver-cancellation behavior.

The built-in worker can resolve missing plain R package names and managed Python imports while a cell runs, as described below.
Neither R nor Python source is scanned in advance.
SQL and `install.packages()` do not trigger automatic package resolution, and MCP Console does not replay a cell after a missing-package error.
Prepared packages and extensions still must be attached, imported, or loaded by code when their runtime requires it.

## Automatic R package resolution

The built-in R worker resolves a missing plain package name when evaluated code reaches `library()`, `require()`, `requireNamespace()`, `loadNamespace()`, `::`, or `:::`.
It installs thin wrappers around `base::library` and `base::loadNamespace`, preserves the original base functions, and delegates to them after making the package available.
Both wrappers are needed because `library()` calls `find.package()` and can report a missing package before it reaches `loadNamespace()`.
`require()` delegates to `library()`, `requireNamespace()` delegates to `loadNamespace()`, and the namespace operators use `loadNamespace()` when needed.

The wrappers do not replace `find.package()` or `install.packages()`.
They skip resolution when the package is already attached, loaded, or findable, and preserve base behavior for `library()` help and listing calls.
An explicit non-NULL `lib.loc` and a partial namespace load also bypass automatic resolution because adding a managed library would not satisfy those requests.

Worker-originated requests accept at most 64 plain package names.
Each name must start with an ASCII letter, end with an ASCII letter or digit, and contain only ASCII letters, digits, and dots.
The server validates these names again before invoking IR, so paths, URLs, source prefixes, version selectors, whitespace, and arbitrary IR references from evaluated code are rejected.
Use explicit `requirements.r` for an IR reference such as `github::owner/repository` or for staging packages before evaluation.

For a changed request, the server merges the names with the complete retained R requirement set and resolves that complete set through the existing host-side IR resolver.
It also prepares the complete retained DuckDB extension set for the candidate library.
The candidate remains uncommitted while the worker normalizes it and applies it through the same `.libPaths()` transition used by explicit live R preparation.
Only after the worker reports `RActivated` does the server match the exact candidate, retain it, and add it to the DuckDB R-library history.
The original base operation then continues, so `library()` or `require()` attaches the package and `::` or `:::` loads only its namespace as usual.
Successful automatic resolution emits no preparation marker.

An idle automatic request owns environment changes until the worker reports activation or failure.
Explicit preparation that arrives before that report fails without resolving a second candidate or stopping the worker.
If explicit preparation reserved the transition first, an otherwise idle automatic request receives an ordinary unavailable response instead.
A cell without changed requirements can still queue while an idle automatic request is pending; the worker processes it after the synchronous callback finishes.

This transition does not restart the worker.
Its R process, PID, globals, loaded namespaces, Python state, DuckDB catalog, and stdin state remain in place.
The server commits after `.libPaths()` accepts the candidate but before the original package operation resumes, so the environment remains retained if a later `.onLoad`, namespace operation, or expression in the cell fails.

The worker does not inspect R source before evaluation.
Each missing package is resolved only when execution reaches one of these operations, so unreachable or quoted code does not invoke IR.
Several new package loads in one cell can therefore cause several incremental IR calls in execution order.

An automatic request is part of the active R evaluation.
If `timeout_ms` expires, `send` can return `[running; poll with an empty send]` while its resolver continues; the resolver is not cancelled by that wait timeout.
`session(action = "interrupt")` targets the active resolver, while an unchanged restart or shutdown cancels it.
A restart that also adds requirements serializes behind the active environment resolution before it prepares those additions and replaces the worker.
An interrupted or lifecycle-cancelled request is reported to its operation, and a candidate from a replaced generation cannot commit into its replacement.

If IR cannot resolve a package requested at runtime, `library()` and namespace loads surface their normal R errors, while `require()` may return `FALSE` according to `logical.return`; the worker remains available.
If applying the candidate library fails, the worker reports `RActivationFailed`, the server discards the candidate, and further requirement changes in that generation require restart; the worker remains available so its in-memory state can be saved.
Transport, sideband, protocol, and bridge-infrastructure failures retain the existing worker-failure behavior.

## Automatic Python import resolution

The built-in server-managed Python environment resolves a missing import when Python's ordinary import machinery cannot find it.
The private runtime appends a last-chance finder to `sys.meta_path`, after the existing built-in, frozen, path, and other finders.
Already-installed, local, standard-library, and already-loaded modules therefore resolve without a host request.
Availability queries such as `importlib.util.find_spec()` report the current environment without adding a requirement.
If a missing optional import is reached while the default NumPy or pandas package is initializing, the finder leaves it to ordinary Python behavior instead of starting host resolution.
Importing either available default therefore does not add optional modules encountered during its initialization to the retained manifest.
A later direct import can resolve such a dependency after initialization finishes; an explicit `requirements.python` entry can prepare it earlier.

The finder infers one PyPI distribution from the top-level import name.
A curated table covers established differences such as `yaml` to `pyyaml`, `PIL` to `pillow`, and `sklearn` to `scikit-learn`.
Otherwise a conservative ASCII identifier maps to the same bare distribution name.
The inferred name is validated through the same named-registry requirement path used for explicit managed-Python requests before uv starts.

Automatic inference does not produce versions, extras, markers, paths, URLs, direct references, or other requirement syntax.
It also declines a same-name fallback for broad shared namespaces, a missing submodule whose top-level package is already present, and a standard-library module unavailable in the selected Python build.
These cases report an actionable import error instead of installing an ambiguous or misleading distribution.
A direct missing-submodule import retains its ordinary `ModuleNotFoundError`; for the exact submodule lookup performed by `from package import missing`, MCP Console uses `ImportError` so CPython does not suppress the guidance.
Both forms report the full missing-submodule name.

When inference succeeds, the Python finder calls a private R closure supplied by the reticulate bridge.
That closure snapshots reticulate's current requirement state, adds the inferred distribution through `reticulate::py_require(..., action = "add")`, and materializes the complete manifest through the existing managed-Python callback.
The server returns a provisional environment in a `PythonResolved` reply to the existing `ResolvePython` request.
After reticulate activates a compatible environment, the worker reports `PythonActivated` with the complete normalized logical manifest.
The worker emits that report before the original Python import resumes.
The server matches and commits the candidate when it processes the report; sideband order places it before any later evaluation outcome.

The finder then invalidates Python's import caches and retries the current meta-path finders for the requested module.
If the module is present, the original import continues in place.
The cell is not replayed, and successful resolution emits no preparation marker.
When the import and inferred distribution names differ, the server emits a bounded notice such as `[resolved PyPI distribution 'py-yaml12' for Python import 'yaml12']` after it commits the matching activation.
Same-name inference and explicit preparation emit no resolution notice.
The worker process, Python interpreter, Python objects, R globals, DuckDB catalog, stdin state, and PID remain in place.

A successfully activated environment remains committed if the inferred distribution does not provide the requested module or if later code in the cell fails.
A later cell and a replacement after restart reuse it.
An ordinary failure before activation restores the previous reticulate manifest, discards the provisional candidate, and leaves the worker usable.
Resolver diagnostics name the import and inferred distribution and show the `requirements.python` recovery shape.

Resolution occurs only when execution reaches a missing import.
Unreachable branches and uncalled functions do not invoke uv, and several new imports in one cell resolve incrementally in execution order.
An automatic request belongs to the active Python evaluation, so `timeout_ms` can return `[running; poll with an empty send]` while its resolver and cell continue.
An empty `send` polls that evaluation, and `session(action = "interrupt")` targets its active host resolver.
Restart, shutdown, and generation checks cancel or discard unactivated candidates from an old worker; an earlier `PythonActivated` commit remains retained.

The finder prevents a second automatic resolution while its R callback is active.
A recursive missing import follows ordinary import failure rather than starting another resolver.
The callback is also limited to the main worker process and the Python thread that configured the runtime.
A missing import reached from a fork child or another Python thread reports that the distribution must be prepared before that child or thread starts and does not call R, reticulate, the sideband, or uv.

A nonempty user-selected `RETICULATE_PYTHON` disables this path as well as explicit `requirements.python` additions.
The finder still reports a specific missing-import diagnostic, directing the user to install the distribution into that selected environment or restart MCP Console with managed Python enabled.

Use explicit `requirements.python` when the correct distribution differs from the inferred name, a version, extra, or marker is needed, the namespace is ambiguous, an error asks for an exact requirement, or the package should be prepared before the cell starts.
Explicit requirements make a distribution available but do not import it.

## Staging requirements ahead of time

Use `session` with `action = "prepare"` to add exact requirements without replacing the worker:

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

After a worker starts, requests that add to the retained environment are available only while it is idle:

- R additions can update a worker that implements live R preparation.
- Compatible Python additions can update an idle server-managed built-in worker.
- DuckDB extensions are installed on the host without replacing the worker.

Live R and Python preparation is noninteractive.
Use `send` to satisfy and collect any managed input requested by an idle R callback before preparing R or Python requirements.
If such a request is outstanding or arises during live preparation, the preparation fails and the server stops the worker, losing its in-memory state.

A request with new additions during an active cell is rejected.
If a request with new additions overlaps worker startup, it returns `[requirements not prepared: worker is starting]` without resolving or retaining the additions.
If the worker is stopped, new additions return `[restart required]`; use restart to prepare them and start a replacement.

An explicit restart can cancel an in-flight live R or Python preparation.
The `prepare` call then fails with `R preparation cancelled by restart` or `Python preparation cancelled by restart`, and the restart continues.
The cancelled call does not retain pending candidates; a Python environment already committed by an earlier step remains retained.

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

Explicit Python preparation and automatic imports use the same reticulate additive requirement helper.
The helper snapshots the current manifest before calling `reticulate::py_require(..., action = "add")`.
Before Python initializes, the worker materializes the complete manifest.
After initialization, reticulate checks that the candidate uses the live `libpython` and activates a compatible environment without replacing the interpreter or its objects.

The server retains a Python environment when the worker reports that reticulate accepted its complete normalized manifest.
A runtime import reports that activation before the original import continues.
A successful activation commits independently of later steps in the same mixed request.
If Python succeeds and a following live R update fails, the Python addition remains retained and is available after restart.
The same rule retains an automatically inferred distribution when the requested module or later cell code still fails.

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

Explicit R requirements are IR package references.
MCP Console owns only the framing checks: each request may contain at most 64 nonempty strings, and a string may not contain NUL, carriage return, or newline.
IR owns the accepted package reference syntax and dependency resolution.

Automatic runtime R requests use the narrower plain-name syntax described under [Automatic R package resolution](#automatic-r-package-resolution).
That validator is separate from explicit `requirements.r`, so restricting runtime discovery does not remove supported remote IR references from explicit preparation or restart.

The built-in server uses `$R_HOME/bin/Rscript` when `R_HOME` is set.
Otherwise it runs `R RHOME` using `R` from `PATH` and uses the reported home's `bin/Rscript`.
It passes that exact `Rscript` to IR and uses it for DuckDB resolution.
When Python is server-managed, the same `Rscript` also runs managed-Python resolution.
The server prepends the resolved managed library to inherited `R_LIBS`, preserving its nonempty path entries after the managed library.

The server requires `ir` 0.4.0 or later on `PATH`.
It passes each requirement as a separate `ir run --with` argument; requirement text is never inserted into R source.
Every invocation sets `IR_NO_LOCAL_SOURCES=1`, so IR rejects direct or transitive installation from the local filesystem.
Remote package installation and build code still run with server permissions.

### Python

Explicit managed-Python additions accept named PEP 508 registry requirements.
Package extras, version specifiers, and environment markers are supported, for example:

```text
requests[socks]>=2,<3; python_version >= '3.10'
```

Paths, `file:` URLs, editable requirements, direct references such as `name @ URL`, and local archives or projects are rejected before a resolver starts.
A request may contain at most 64 entries.

Automatic imports use the narrower input described under [Automatic Python import resolution](#automatic-python-import-resolution): one inferred bare distribution name.
They cannot add a version, extra, or marker.
The same registry-only validator checks that name before host resolution.

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
The server skips its managed-Python preflight and rejects Python additions from `send`, `prepare`, `restart`, automatic imports, or other worker-originated managed-resolution requests.
R requirements and DuckDB extensions remain available.
The selected interpreter must still satisfy the [built-in runtime](BUILTIN_RUNTIME.md) requirement for Python 3.10 or later and must initialize under the worker's offline policy.
Imports already available in the selected environment work normally.
A missing import explains that automatic resolution and `requirements.python` are disabled and directs the user to install the distribution into that environment or restart MCP Console with managed Python enabled.

This selection is independent of custom-worker policy.
A custom worker always rejects managed Python requirements, regardless of `RETICULATE_PYTHON`.

## Custom workers

Custom workers start without the built-in R library, managed Python environment, or default DuckDB extensions.
They can use explicit R requirements and DuckDB extensions, and may opt into the worker-protocol runtime R resolution callbacks.
Managed Python requirements remain unavailable for send, prepare, and restart.

Every custom-worker R candidate, whether explicitly or at runtime, includes DBI, DuckDB, and jsonlite so the host can prepare later DuckDB extensions with the same library.
The server supplies the retained library through `R_LIBS` at worker launch.
A running custom worker must implement live R preparation for explicit additions.
If it opts into runtime resolution, it must confirm or reject each provisional library as specified by the [worker protocol](WORKER_PROTOCOL.md).

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

- Explicit R requirements and validated automatic package names become individual process arguments to IR, which receives a constant R program.
- Python manifests, including bare distributions inferred from imports, and version constraints are JSON data on resolver standard input.
- DuckDB extension names are validated JSON data and are not submitted SQL.

Evaluated R code can trigger managed R resolution through the built-in `library()` and `loadNamespace()` bridge.
Only validated plain names cross that worker-to-server boundary, and every automatic IR invocation still sets `IR_NO_LOCAL_SOURCES=1`.
A plain name restricts resolver syntax, but the selected package's installation or build code still runs with server permissions; use only packages you trust.
Evaluated Python imports and reticulate APIs can trigger managed Python resolution, but the same named-registry and version-constraint validation applies before a host resolver starts.
Host resolution and managed-environment startup may run accepted distributions' installation, build, or initialization code with server permissions; use only packages you trust.

### Server-owned uv configuration

When the server starts, it captures inherited `UV_*` variables except `UV_OFFLINE`.
Before each managed-Python resolver starts, it removes the current `UV_*` environment, restores that startup snapshot, and removes `UV_OFFLINE`.
Changes made later by evaluated R or Python code therefore cannot configure host resolution.

The built-in worker has the opposite network policy: it forces `UV_OFFLINE=1` before user code runs inside the network-denied sandbox.

## Failure atomicity and cache effects

Before worker startup and during restart, the server commits the retained R, Python, and DuckDB candidates only after all requested resolution succeeds.
A failure does not change the retained manifest or replace the current worker.
The same pre-start transaction is used for requirements declared by a cell, and any failure prevents that cell from being dispatched.

That transaction covers server-owned state, not external resolver caches.
IR, uv, reticulate, and DuckDB may download, build, or install files before a later step fails.
For example, an earlier extension in a failed multi-extension request can remain in DuckDB's native cache without entering the retained extension set.
A future request may reuse such cache entries.

Live preparation has worker-confirmed commit boundaries.
A Python activation is retained as soon as the worker reports it.
It is not rolled back if an automatic import still cannot find its module, later Python code fails, or a later R step in the same request fails.
An automatic Python candidate that fails before activation is discarded and the earlier reticulate manifest is restored.
An automatic R candidate is retained only after the worker reports that `.libPaths()` accepted its exact library, and it is not rolled back if later namespace loading or cell code fails.
Explicit R and DuckDB changes commit only after their complete live operation succeeds.
For a `send` with explicit requirements, any preparation failure returns through that send and prevents its cell from running, while retaining or discarding candidates according to these existing live-preparation boundaries.
The earlier sections describe how ordinary Python failure, recoverable R failure, and infrastructure failure affect the current worker.
