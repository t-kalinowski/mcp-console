# Requirements and environments

**Status:** Implemented current behavior

This document describes how MCP Console prepares and retains R packages, Python packages, and DuckDB extensions.
The [`send` operation-order reference](SEND_OPERATIONS.md) owns validation timing, control and stdin ordering, failure effects, and wait timeouts.
This guide describes preparation before a cell, standalone preparation, and requirements included in restart.
[Host resolution and trust](#host-resolution-and-trust) explains why requirement input is restricted and which work runs with server permissions.

Local and SSH [Python sessions without R](BUILTIN_RUNTIME.md#python-sessions-without-r) support explicit preparation when Console manages the environment through uv on the execution host.
The default uses uv from `PATH`; setting `python` in the Console config selects a non-managed environment without invoking uv.
The [sans-R runtime contract](BUILTIN_RUNTIME.md#python-sessions-without-r) uses the same trusted host resolver as mixed-language sessions.
There is no automatic PATH-Python fallback.
`requirements.python` and `requirements.duckdb` prepare additions alone or before a Python or SQL cell.
An unused warm worker can be replaced to satisfy the initial declaration.
After startup, an idle worker accepts `action: "add"` for new Python distributions and DuckDB extensions in the same request; already retained declarations are no-ops.
After stateful work is admitted, changes to a declared distribution or constraint and changed `set` or `reset` declarations need `control: "restart"`.
Unchanged declarations are a no-op.
The existing prestart/restart transaction resolves the complete Python candidate and inspects its executable before preparing all retained extensions with that candidate's DuckDB.
The execution-host preparation owner owns cancellation, output, and child cleanup for the Python-backed extension operation.
Before startup and during restart, resolution, inspection, or extension preparation failure preserves the current worker and environment; successful commit updates the retained manifest, executable, and launch configuration together.
Live extension installation uses the accepted managed Python environment and captured cache directory without resolving or inspecting another interpreter.
It commits the complete extension declaration only after host preparation succeeds in the same worker generation.
Live Python preparation resolves against the running executable, inspects the candidate, checks `libpython`, and prepares retained extensions before sending the candidate to the worker.
The worker reports activation before the server commits the candidate manifest and native launch configuration.
Plain restarts and crash replacement reuse that accepted environment without another resolution.
Explicit Python selections do not enable preparation.
Managed sans-R Python also resolves a reached missing import through the existing finder; R requirements remain unavailable.
The default sans-R managed Python declaration includes NumPy, pandas, and DuckDB.
`get` reports the accepted declaration, `reset` restores these startup defaults, and `set` retains exactly the requested declaration, including an empty set.
DuckDB is resolved with the rest of the Python manifest through the hidden host resolve process before the environment is accepted; the worker never installs it through Console preparation.
If it is absent after an explicit replacement, Python and a selected DB-API connection remain usable, while managed SQL reports how to obtain DuckDB.
An extension requirement with no importable DuckDB package in the candidate fails and explains how to include `duckdb` in `requirements.python`.
The managed sans-R declaration includes the SQLite extension, prepared on the execution host during background warmup.
Python preparation uses one control path with or without R; R declaration compatibility is an optional adapter.

Local managed preparation runs through the hidden `mcp-console resolve` command on the host.
Its private JSON exchange carries requirement manifests, resolved environment data, controls, and cleanup receipts.
The server retains the same declarations and commits candidates only after preparation completes.

Prepared requirements configure the built-in worker; they do not attach an R package, import a Python package, or load a DuckDB extension.
Runtime use is covered by the [built-in runtime guide](BUILTIN_RUNTIME.md).
Exact live-worker messages and custom-worker receipts belong to the [worker protocol](WORKER_PROTOCOL.md).

[SSH targets](SSH.md) use the same capability discovery and managed preparation on the execution host.
The controller never discovers local R/Python or executes resolvers for remote sessions.
A separate trusted remote preparation owner captures resolver settings during background discovery without starting a worker.
The same owned warmup then prepares defaults and inspects the selected Python environment, with or without R.
With R present, `requirements`, background default preparation, automatic R/Python requests, and restart preparation use the remote R installation and its resolver settings.
A session with no R uses remote uv for its managed Python and DuckDB defaults and additions; missing uv or failed resolution is reported.
A selected or broken R installation still reports an R error.
An explicit remote Python interpreter disables managed Python additions.
With R present, managed R and its DuckDB connection remain available; without R, the selected environment may supply DuckDB or a custom DB-API connection.
The local server keeps requirement merging, transaction and activation decisions, generation ownership, and recording.
Remote results require confirmed resolver cleanup before commit; uncertain completion blocks further preparation and replacement.

[Docker targets](DOCKER.md) and [Docker Sandbox targets](DOCKER_SANDBOX.md) deliberately use a preinstalled image environment.
Capability probes never select `uv` or `ir`; requirement changes, automatic resolution, and worker preparation callbacks are disabled.
`requirements.action="get"` can inspect the retained declaration.
Reticulate cannot silently create a managed environment.
Rebuild the image and start a new server session to add packages.
For R-free prepared targets, the shared probe selects and inspects preinstalled CPython inside the image/template under workload policy.
It never opens a preparation session, runs uv or ir, installs an interpreter, or opens a SQL catalog.
Requirement mutations are rejected before restart, code dispatch, or input delivery.
Inspection returns the retained empty declaration, not an inventory of image packages.
Worker restart keeps the selected interpreter; changing dependencies requires a rebuilt image/template and a new server session.
Preinstalled DuckDB extensions load from the target cache, while automatic extension installation is disabled for its Console-owned connection.

On Linux, preparing a managed environment can compile R packages, including the resolver's own `pak` tooling.
On Debian and Ubuntu, install `build-essential`, `pkg-config`, and `libcurl4-openssl-dev` for that bootstrap.
Additional R packages can require their own system libraries and development headers.

## Retained environments

MCP Console retains one environment configuration in server memory:

- a resolved R library and its complete R requirement set;
- a selected Python environment and normalized Python manifest; and
- a set of prepared DuckDB extension names.

The sets are additive by default.
Repeating an accepted requirement is idempotent, and a restart reuses everything retained so far, including R packages and Python distributions resolved automatically during earlier cells.
Use `requirements.action` to inspect or replace the declaration.
There are no named environments or persistence across server processes.

The built-in server prepares these defaults during automatic background warmup:

| Environment | Defaults                                                                            |
| ----------- | ----------------------------------------------------------------------------------- |
| R           | `tidyverse`, `reticulate`, `DBI`, `duckdb`, `arrow`, and `nanoarrow`                |
| Python      | NumPy and pandas when Python is server-managed; sans-R sessions also include DuckDB |
| DuckDB      | ICU, JSON, and SQLite extensions with R; SQLite in sans-R managed Python            |

NumPy is a default Python dependency with and without R, with the same deterministic native bootstrap.
An explicitly selected interpreter remains a non-managed environment: its owner installs these packages before starting Console.

R-present managed defaults apply when startup finds a resolver bootstrap from `ir` on `PATH`, `uv` on `PATH`, an explicit `uv` selection, or ambient reticulate.
Server-managed Python additionally needs `uv`; when only `ir` is on `PATH`, the resolved reticulate installation supplies it.
If no R-present resolver bootstrap is available, the built-in server retains no managed environment, rejects requirement mutations, and starts a bare runtime from the packages already available to R, reticulate, and DuckDB.
R, Python, and SQL cells remain available, with ordinary R missing-package errors and explicit unavailable-adapter diagnostics where appropriate.

Discovery and default preparation begin automatically in a session-owned blocking warmup after protocol and recording ownership are installed.
MCP initialization, tools/list, ping, and requirements inspection remain available while execution-host discovery, downloads, target probes, or interpreter hooks are blocked.
Discovery and dependency work have no evaluation timeout; EOF and interrupt use their owned cancellation and confirmed retirement paths.
The first code call joins existing startup and its evaluation wait includes any remaining warmup.
Explicit requirements remain preconditions outside that wait budget.
An unused worker can be discarded for changed initial requirements after a complete candidate resolves; polling and inspection do not claim it.
Once code or nonempty input is admitted, changes follow the ordinary live rules and cannot silently discard in-memory state.
Startup failure is retained until explicit restart, with no automatic background retry loop.

Host resolution for changed requirements submitted through `send` also has no deadline.
The call remains pending until the resolver exits; while MCP input is open, `send(control = "interrupt")` sends `SIGINT` to the active resolver, and closing MCP input cancels it during server shutdown.

Packages supplied by these environments are available but are not attached or imported automatically.
The default DuckDB extensions are installed in DuckDB's native cache but are loaded only when DuckDB needs them inside the worker.

A custom worker skips all three default preparations.
Its more limited requirements contract is described under [Custom workers](#custom-workers).

## Inspecting and replacing requirements

`requirements.action` accepts `get`, `add`, `set`, and `reset`:

| Action          | Meaning                                                                                                                                                                                                                  |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `get`           | Return the normalized, committed declaration without starting a worker, resolving packages, consuming output, or changing evaluation state. Reject code, stdin (including empty stdin), control, and all payload fields. |
| `add` (default) | Accumulate requirements, preserving idempotency and supported live additions.                                                                                                                                            |
| `set`           | Replace the entire declaration. Omitted language lists and Python constraints are cleared. No startup defaults are merged into the replacement.                                                                          |
| `reset`         | Restore the configured startup declaration, discarding explicit and automatically acquired requirements. Reject all package lists and constraint fields, even empty or null ones.                                        |

```python
send(requirements={"action": "get"})
send(requirements={"python": ["requests"]})
send(control="restart", requirements={"action": "set", "python": ["requests>=2"]})
send(control="restart", requirements={"action": "set"})
send(control="restart", requirements={"action": "reset"})
```

`{"action": "set"}` selects no optional requirements.
It is equivalent to empty `r`, `python`, and `duckdb` lists, and differs from `reset`.
A bare `{}` remains invalid.
Ordinary restart preserves the selected declaration, including an empty declaration; subsequent additions do not restore cleared defaults.
Automatic resolution remains enabled and later user code can acquire requirements again.
Replacement does not delete caches or prohibit packages already available through infrastructure, ambient R libraries, or a user-selected interpreter.

Inspection returns `requirements`, `prepared`, and `runtime_requirements` in MCP `structuredContent`.
`requirements` is a declaration, not an installed-package inventory.
It contains the three language lists, `python_version` (a list of supported version constraints), and `exclude_newer` (a publication cutoff string or null).
Copy `requirements`, edit it, add `action: "set"`, and submit it to replace the declaration without losing constraints.
Before discovery finishes, inspection returns `requirements: null`, `prepared: false`, and `status: "discovering"`; failed discovery reports `status: "failed"`.
Missing discovery is not an empty installed environment.
Once the declaration is known, `prepared: false` distinguishes it from a resolved environment.
While evaluation or resolution is busy, inspection returns the last committed snapshot, never an uncommitted candidate.
Small snapshots also appear as JSON text.
Larger snapshots return a short text notice and the complete structured content; the Python and R wrappers return complete JSON text for inspection.
`add` accepts at most 64 entries per language per call.
`set` accepts complete manifests accumulated through repeated additions and has no per-list count limit.

The managed mixed-language worker separately prepares its R infrastructure: `reticulate`, `jsonlite`, `DBI`, `duckdb`, `arrow`, `nanoarrow`, `pillar`, `tibble`, and `utf8`, with their dependencies.
These packages support the implemented Python bridge and SQL adapters.
They remain available when the optional declaration is empty.
NumPy and pandas are optional Python conveniences; an empty managed Python declaration runs standard-library code without either distribution.
In a sans-R session, an empty declaration also omits DuckDB; SQL can still use a user-selected DB-API connection.
R/Python conversions that need NumPy still require it.
Custom workers retain their existing `DBI`, `duckdb`, and `jsonlite` preparation infrastructure.
Preinstalled and unmanaged targets have no Console-managed package infrastructure to report.

A changed `set` or `reset` with a live worker requires `control="restart"`.
A call without it fails before resolution or mutation and leaves no pending replacement.
Without a live worker, the server resolves and retains the candidate without starting a worker unless the call otherwise requests one.
It resolves the complete candidate before committing or retiring the old worker.
Failure or cancellation preserves the retained environment and worker; resolver cache and build effects may remain.
After commit, the existing restart and admission path runs any accompanying cell only in the successfully started replacement.
Failure after retirement cannot restore discarded interpreter state.
An unchanged replacement requires no preparation, but an explicitly requested restart still happens and excludes uncommitted activations from the retired generation.
`set` and `reset` cannot accompany `control="interrupt"`.

`add` also accepts `python_version` and `exclude_newer`: version constraints accumulate, and a supplied cutoff can fill an unset cutoff but cannot replace an existing one.
Changing these constraints with a live worker requires restart.
`set` clears omitted constraints; `reset` restores startup constraints (currently none).
Captured resolver settings such as `UV_*` remain startup configuration and are not rewritten by these session operations.
Sans-R managed sessions support these actions for Python and DuckDB extensions.
Idle additive changes can commit live when they add new Python distributions, DuckDB extensions, or both.
Changing a declared distribution and changed declaration replacements require restart.
User-selected Python, bare runtimes, Docker, and Docker Sandbox support inspection without enabling preparation.

Committed replacements are recorded with their declaration and call ID in the event journal and Markdown transcript.
Quarto records the environment boundaries, including Python constraints, and disables automatic execution after a replacement.
Recreate those environments before enabling replay; one cumulative or final manifest may not satisfy historical cells.
Successful additions update the committed `get` snapshot and appear as ordinary tool calls and results in the event journal and Markdown transcript.
They do not create a Quarto replacement boundary.

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

For the default `add` action, supply R, Python, or DuckDB arrays with at least one entry, or Python constraints.
`set` accepts empty lists and omitted fields.
It may accompany any cell language because the built-in languages share one worker environment: an R requirement can accompany a Python cell, for example.
Without a cell, requirements perform standalone preparation or participate in a restart transaction.
Requirements are cell preconditions, with the control-specific ordering and partial effects described in the [operation table](SEND_OPERATIONS.md#operations).
No other operation can change the environment between successful preparation and admission of the accompanying cell.

Without inline control, the preparation behavior depends on worker state:

- Before the worker starts, the server resolves all changed candidates, commits the complete retained environment only after they all succeed, starts the worker, and evaluates the cell.
- With an idle running worker, the server applies the supported live R, Python, or DuckDB behavior described below, then immediately launches the cell in that worker generation.
- With an eligible stopped worker, the server resolves and retains additions without live preparation, starts the normal replacement, and evaluates the cell there.
- A stopped sans-R worker still needs an explicit restart for changed requirements.
- After a recoverable live R failure has made further environment changes require restart, new additions fail with `requirements require session restart; cell was not run`.
  The live worker is not destroyed automatically, so its state can be saved before an explicit restart.

The built-in worker can resolve missing plain R package names and managed Python imports while a cell runs, as described below.
Neither R nor Python source is scanned in advance.
SQL and `install.packages()` do not trigger automatic package resolution, and MCP Console does not replay a cell after a missing-package error.
Prepared packages and extensions still must be attached, imported, or loaded by code when their runtime requires it.

## Automatic R package resolution

The built-in R worker resolves a missing plain package name when evaluated code reaches `library()`, `require()`, `requireNamespace()`, `loadNamespace()`, `::`, or `:::`.
It prepares missing `library()` packages before running R's original body in the same call frame, preserving the caller's expression in errors.
This preparation precedes namespace loading because `library()` can report a missing package in its `find.package()` check.
For `base::loadNamespace`, it runs R's original formals and body in a private lexical environment that prepares the package at the existing retryable missing-package path.
If preparation succeeds, namespace loading continues; otherwise R's original `withRestarts()` body signals the original condition with its native call chain.
Preserving the original body keeps packages that inspect `loadNamespace()` compatible.
`require()` delegates to `library()`, `requireNamespace()` delegates to `loadNamespace()`, and the namespace operators use `loadNamespace()` when needed.

These adapters do not replace `find.package()` or `install.packages()`.
They skip resolution when the package is already attached, loaded, or findable, and preserve base behavior for `library()` help and listing calls.
An explicit non-NULL `lib.loc` and a partial namespace load also bypass automatic resolution because adding a managed library would not satisfy those requests.

Worker-originated requests accept at most 64 plain package names.
Each name must start with an ASCII letter, end with an ASCII letter or digit, and contain only ASCII letters, digits, and dots.
The server validates these names again before invoking `ir`, so paths, URLs, source prefixes, version selectors, whitespace, and arbitrary `ir` references from evaluated code are rejected.
Use explicit `requirements.r` for an `ir` reference such as `github::owner/repository` or for staging packages before evaluation.

For a changed request, the server merges the names with the complete retained R requirement set and resolves that complete set through the existing host-side `ir` resolver.
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
Each missing package is resolved only when execution reaches one of these operations, so unreachable or quoted code does not invoke `ir`.
Several new package loads in one cell can therefore cause several incremental `ir` calls in execution order.

An automatic request is part of the active R evaluation.
If `timeout_ms` expires, `send` can return `[running; poll with an empty send]` while its resolver continues; the resolver is not cancelled by that wait timeout.
Interrupt targets the active resolver, while restart or shutdown cancels it.
A restart that also adds requirements serializes behind the active environment resolution before it prepares those additions and replaces the worker.
An interrupted or lifecycle-cancelled request is reported to its operation, and a candidate from a replaced generation cannot commit into its replacement.

If `ir` cannot resolve a package requested at runtime, `library()` and namespace loads surface their normal R errors, preserving the original condition class, message, call, and package fields.
`require()` and `requireNamespace()` return `FALSE` for missing packages, as in base R.
With its default `quietly = FALSE`, `requireNamespace()` prints a failure diagnostic; `quietly = TRUE` suppresses that output.
The worker remains available.
Explicit `requirements.r` preparation still reports the resolver failure.
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
The inferred name is validated through the same named-registry requirement path used for explicit managed-Python requests before `uv` starts.

Automatic inference does not produce versions, extras, markers, paths, URLs, direct references, or other requirement syntax.
It also declines a same-name fallback for broad shared namespaces, a missing submodule whose top-level package is already present, and a standard-library module unavailable in the selected Python build.
These cases report an actionable import error instead of installing an ambiguous or misleading distribution.
A direct missing-submodule import retains its ordinary `ModuleNotFoundError`; for the exact submodule lookup performed by `from package import missing`, MCP Console uses `ImportError` so CPython does not suppress the guidance.
Both forms report the full missing-submodule name.

When inference succeeds, the finder calls the shared native requirement owner, which forms an additive request from the retained manifest.
Optional R declaration metadata is projected before mutation and committed after successful activation.
The server resolves the complete candidate through the captured host resolver and returns a provisional environment in the existing `PythonResolved` exchange.
The reply carries the inspected candidate configuration, constrained to the running interpreter.
After Console activates a compatible environment, the worker reports `PythonActivated` with the complete normalized logical manifest.
The worker emits that report before the original Python import resumes.
The server matches and commits the candidate when it processes the report; sideband order places it before any later evaluation outcome.

The finder then invalidates Python's import caches and retries the current meta-path finders for the requested module.
If the module is present, the original import continues in place.
The cell is not replayed, and successful resolution emits no preparation marker.
When the import and inferred distribution names differ, the server emits a bounded notice such as `[resolved PyPI distribution 'py-yaml12' for Python import 'yaml12']` after it commits the matching activation.
Same-name inference and explicit preparation emit no resolution notice.
The worker process, Python interpreter, Python objects, DuckDB catalog, stdin state, and PID remain in place; an R-present session also retains its R globals.
New subprocesses use the activated environment and can import its retained packages.

A successfully activated environment remains committed if the inferred distribution does not provide the requested module or if later code in the cell fails.
A later cell and a replacement after restart reuse it.
An ordinary failure before activation discards the provisional candidate and leaves the accepted environment usable; the R adapter restores its previous reticulate manifest.
Resolver diagnostics name the import and inferred distribution and show the `requirements.python` recovery shape.

Resolution occurs only when execution reaches a missing import.
Unreachable branches and uncalled functions do not invoke `uv`, and several new imports in one cell resolve incrementally in execution order.
An automatic request belongs to the active Python evaluation, so `timeout_ms` can return `[running; poll with an empty send]` while its resolver and cell continue.
An empty `send` polls that evaluation, and interrupt targets its active host resolver.
Restart, shutdown, and generation checks cancel or discard unactivated candidates from an old worker; an earlier `PythonActivated` commit remains retained.

The finder prevents a second automatic resolution while its callback is active.
A recursive missing import follows ordinary import failure rather than starting another resolver.
The callback is also limited to the main worker process and the Python thread that configured the runtime.
A missing import reached from a fork child or another Python thread reports that the distribution must be prepared before that child or thread starts and does not call the resolver sideband or `uv`.

A user-selected Python environment disables this path and explicit `requirements.python` additions.
The finder still reports a specific missing-import diagnostic, directing the user to install the distribution into that selected environment or restart MCP Console with managed Python enabled.

Use explicit `requirements.python` when the correct distribution differs from the inferred name, a version, extra, or marker is needed, the namespace is ambiguous, an error asks for an exact requirement, or the package should be prepared before the cell starts.
Explicit requirements make a distribution available but do not import it.

## Staging requirements ahead of time

Use a requirements-only `send` to add exact requirements without replacing the worker:

```json
{
  "requirements": {
    "r": ["data.table"],
    "python": ["polars>=1"],
    "duckdb": ["fts"]
  }
}
```

Each array accepts at most 64 entries, and the request needs at least one entry overall.
The [standalone preparation row](SEND_OPERATIONS.md#operations) specifies its result, input restrictions, and timeout behavior.

Before a worker starts, the server resolves every changed candidate on the host, commits the retained configuration only after the complete request succeeds, and does not start the worker.
Exact repeats return `[prepared]` without resolving them again.

After a worker starts, changed additions to the retained environment are available only while it is idle:

- R additions can update a worker that implements live R preparation.
- Compatible Python additions can update an idle server-managed built-in worker.
- DuckDB extensions are installed on the execution host without replacing the worker, including in a managed sans-R session.

Live R and Python preparation is noninteractive.
Use `send` to satisfy and collect any managed input requested by an idle R callback before preparing R or Python requirements.
If such a request is outstanding or arises during live preparation, the preparation fails and the server stops the worker, losing its in-memory state.

A request with new additions during an active cell is rejected.
An unclaimed worker is replaceable for initial requirements, including during startup.
Once startup input or code has claimed it, new live preparation waits for an idle initialized operation slot or returns `[requirements not prepared: worker is starting]` without changing the declaration.
If the worker is stopped, new additions return `[restart required]`; use restart to prepare them and start a replacement.

An explicit restart can cancel an in-flight live R or Python preparation.
The preparation call then fails with `R preparation cancelled by restart` or `Python preparation cancelled by restart`, and the restart continues.
The cancelled call does not retain pending candidates; a Python environment already committed by an earlier step remains retained.

### Live R preparation

The server resolves a new library containing the complete retained R requirement set.
The worker places that library first among managed `.libPaths()` entries, removes the previous managed library entry, and preserves its other library paths and in-memory state.
In a sandboxed built-in R session, its writable temporary library remains ahead of the managed library.
The server retains the candidate only after the worker confirms the normalized library path.

An ordinary live R preparation failure leaves the worker available for evaluation, because the caller may need to save in-memory state.
Its live library path may have changed before the failure, so the server does not accept more requirement changes in that generation.
The failed call reports that further changes require restart, and later additions return `[restart required]`.
A successful explicit restart clears this state.

An R transport, protocol, or bridge-infrastructure failure is different: the server stops a worker whose state is no longer known to be usable.

### Live Python preparation

The shared requirement owner retains live interpreter identity separately from provisional declarations and resolved candidates.
Reached imports use its native resolver callback with or without R; they no longer call `reticulate::py_require()`.
The server validates each inferred addition against the accepted declaration and resolves and inspects its candidate on the execution host.
The optional R adapter prepares the declaration's representation and history before interpreter mutation and commits them afterwards.

Idle tool preparation uses the same worker operation with or without R.
It resolves the complete declaration through the common owner and projects detached R metadata when that adapter exists.
Normal idle preparation reaches an initialized interpreter.
Supported startup declarations may materialize a candidate during the initialization phase before CPython loads.
Once Python is live, resolution pins the accepted executable and activation preserves its objects.
R declarations preserve argument conversion, package warnings, history, live version and package checks, and R conditions.
Their candidate library and prefixes come from the host-inspected identity.
Execution-host inspection captures conversion metadata; worker attachment never runs another Python metadata subprocess.

The shared activation operation compares the candidate and retained interpreter's `libpython` strings exactly before conversion metadata or interpreter mutation.
For a compatible candidate, it runs the environment's activation script and updates executable, multiprocessing, and child-process setup without replacing the interpreter or its objects.
Compatibility and other failures before mutation leave the accepted declaration and usable worker intact.
An activation exception or other unsafe failure marks the generation restart-required; this also applies to failures surfaced through an R condition.
The R adapter preserves the original Python exception and traceback through reticulate's condition boundary.
Only successful activation and process setup can publish the complete manifest to the server.
For an R declaration, the adapter records pending activation, then reticulate accepts the configuration and writes the binding that publishes `PythonActivated`.

The server retains a Python environment and its host-inspected launch identity when the worker publishes its complete normalized manifest.
The R adapter publishes through the existing active binding after committing its presentation metadata.
A successful startup declaration retains both through initialization publication.
Failed or discarded candidates change neither retained value.
A runtime import reports that activation before the original import continues.
A successful activation commits independently of later steps in the same mixed request.
If Python succeeds and a following live R update fails, the Python addition remains retained and is available after restart.
The same rule retains an automatically inferred distribution when the requested module or later cell code still fails.

In a managed sans-R session, an idle `action="add"` request can add new named Python distributions and DuckDB extensions before a Python or SQL cell or as a standalone request.
An exact retained requirement is a no-op.
A different requirement for an already-declared distribution needs `control="restart"` and `action="set"`; the distribution name comes from the same PEP 508 parser used for request validation.
This add-only rule does not promise that arbitrary package upgrades can be switched in a running interpreter.
It compares requirement declarations, not installed versions: resolving a new distribution can select different versions of existing or transitive dependencies.
Live preparation neither locks those versions nor unloads already-imported modules.
Use explicit restart for upgrades or dependency changes that need fresh imports.
The host resolves the complete candidate through the hidden resolver using the running environment's executable, then inspects the candidate and compares its `libpython` with the worker's active configuration.
The host prepares the complete retained DuckDB extension set, including additions from the same request, against the candidate before activation.
No candidate declaration appears in `action="get"` while this work is pending.
The worker requests and receives the approved candidate through the shared resolver exchange, then uses the common activation operation without initializing R or installing packages inside the worker.
For an automatic import, the importing cell stays suspended while the server prepares that candidate through the same host path; the worker activates it and retries the import through the existing finder.
Its `PythonActivated` report commits the manifest, native launch configuration, and any new extension declaration together before a same-call cell begins.
Python objects, the managed DuckDB catalog, and the selected SQL connection remain in the worker; a later cell error does not discard an accepted activation.
Validation, resolution, inspection, and compatibility failures leave the current worker and accepted environment intact.
An activation exception retains its Python traceback and withholds same-call code and input; further requirement changes require restart because activation-script side effects cannot be rolled back.
The worker is not restarted automatically.

An ordinary Python preparation failure restores the prior reticulate manifest, discards unaccepted candidates, and leaves the worker usable.
By itself, this failure does not make restart mandatory.
A Python transport, protocol, or bridge-infrastructure failure stops the worker instead.

Startup hooks may declare requirements before Python loads through the native owner.
Their declaration becomes retained after successful initialization publishes its activation.
Ordinary R cells see already initialized Python and follow its live add-only rules.
Use launch configuration or requirements with explicit restart for interpreter changes.

### DuckDB extension preparation

DuckDB extension installation occurs entirely on the trusted host resolver.
It uses DuckDB's default repository and signature checks, then leaves `LOAD` to the worker.
The server accepts validated names, not repository, URL, path, or version selectors.

In managed sans-R sessions, `requirements.duckdb` is available before first worker startup, during explicit restart, and as an idle live addition, alone or with a Python or SQL cell.
The resolver runs the accepted or candidate environment's Python in isolated mode and imports its DuckDB package; `PYTHONPATH` and workspace modules do not redirect this helper.
It calls DuckDB's extension installation API with names as data, without R or a separate DuckDB executable.
On a Python environment change, the server inspects the candidate first and prepares the complete retained extension set against its DuckDB version, even if the extension names did not change.
For an idle live addition, it uses the accepted managed Python environment without resolving packages, reinspecting the interpreter, or requesting worker activation.
With an absolute `HOME` at server startup, the managed worker reads the same `HOME/.duckdb/extensions` version-and-platform cache captured for the resolver.
Managed sans-R startup requires an absolute `HOME` for preparing the default SQLite extension.
Its spill and stored-secret paths remain in private worker storage, which retirement removes without deleting shared extensions.
Preparation never loads extensions, executes submitted code, opens a worker database, or changes a selected DB-API connection.
The worker's interpreter, Python objects, managed DuckDB catalog, and selected SQL connection survive a successful live addition.
Changed live `set` and `reset` declarations, interpreter constraints, `exclude_newer`, and changes to a declared Python distribution require explicit restart; an effective no-op remains a no-op even when those fields are present.
Combined Python and extension additions prepare all retained extensions against the Python candidate before activation.
Host installation failure or cancellation leaves the worker and committed requirements unchanged, and same-call code and input do not reach the worker.
The shared extension cache may retain downloads made before failure.
While preparation is pending, `requirements.action="get"` reports the committed declaration.
Removing a declaration does not uninstall the cache entry or prohibit a later `LOAD`.

In R-present sessions, there is no DuckDB-specific live-worker request or receipt.
The server installs the complete retained extension set for each relevant resolved R library, so the current worker and later generations can use the extension with their DuckDB version.
It then retains the extension names without changing the worker's R, Python, SQL, or catalog state.

Preparation does not load extension code.
A later `LOAD` or DuckDB automatic load occurs inside the worker, which is sandboxed by default.
With `serve --no-sandbox`, extension code uses the selected host account's or container's filesystem, process, and network access.
DuckDB chooses its compiled default extension repository and version-and-platform native cache.

When a DuckDB request also needs a new R library, the worker still uses live R preparation for that library.
Its success and failure semantics therefore follow the R rules above.

## Restarting with requirements

`send(control = "restart")` can add requirements while replacing the worker, with or without a cell:

```json
{
  "control": "restart",
  "requirements": {
    "r": ["praise"],
    "python": ["py-yaml12"],
    "duckdb": ["spatial"]
  }
}
```

A restart can continue with a cell in the same call:

```json
{
  "control": "restart",
  "requirements": {
    "python": ["polars>=1"]
  },
  "python": "import polars as pl\npl.__version__"
}
```

Omit `requirements` to restart with the retained configuration unchanged.
The [restart row and preparation notes](SEND_OPERATIONS.md#operations) describe resolution, commit, replacement, and what can remain after failure.
The [implemented architecture](ARCHITECTURE.md) owns the replacement lifecycle; [Explicit restart](BUILTIN_RUNTIME.md#explicit-restart) describes its user-visible response ownership and notices.

## Accepted requirement input

### R

Explicit R requirements are `ir` package references.
MCP Console owns only the framing checks: each request may contain at most 64 nonempty strings, and a string may not contain NUL, carriage return, or newline.
`ir` owns the accepted package reference syntax and dependency resolution.

Automatic runtime R requests use the narrower plain-name syntax described under [Automatic R package resolution](#automatic-r-package-resolution).
That validator is separate from explicit `requirements.r`, so restricting runtime discovery does not remove supported remote `ir` references from explicit preparation or restart.

The built-in server uses `$R_HOME/bin/Rscript` when `R_HOME` is set.
Otherwise it runs `R RHOME` using `R` from `PATH` and uses the reported home's `bin/Rscript`.
It passes that exact `Rscript` to `ir` and uses it for DuckDB resolution.
When Python is server-managed, the host resolver's selected `uv` executable creates and updates the environment directly.
Python version inventory and selection run directly through the same `uv` executable.
For local host resolution, Python preparation does not inspect or validate a managed R library and does not invoke `R`, `Rscript`, or `ir`.
When direct `uv` is available, the server prepares the Python candidate before the R library candidate; it still commits the complete prestart environment only after all candidates succeed.
R-present SSH preparation continues to supply its selected managed R library through `R_LIBS`; the sans-R path does not invoke R, Rscript, or ir.
The R resolver prepends the resolved managed library to inherited `R_LIBS`, preserving its nonempty path entries after the managed library.

The server prefers `ir` from `PATH`.
If `ir` is absent and `uv` is present, it runs `uv tool run --from r-lib-ir ir`.
It does not fall back when a selected PATH entry fails to start, resolve, or report a supported version.
The selected `ir` must be version 0.4.0 or later.
The server passes each requirement as a separate `ir run --with` argument; requirement text is never inserted into R source.
Every invocation sets `IR_NO_LOCAL_SOURCES=1`, so `ir` rejects direct or transitive installation from the local filesystem.
The `uv` path may download `r-lib-ir`; remote package installation and build code still run with server permissions.

When `ir` is the only command on `PATH` and managed Python is selected, the server first resolves the default R library, then runs a fixed `reticulate:::uv_binary()` program through that library's exact `Rscript`.
The resulting `uv` executable becomes the session's managed-Python resolver.
When neither command is on `PATH`, the server checks the selected ambient R installation for this capability during background discovery and invokes it during owned preparation.
A usable ambient reticulate can bootstrap `uv`, which then supplies `ir`.
If reticulate or that capability is absent, the server enters bare mode.
An installed reticulate namespace that fails to load is a startup error.
A selected bootstrap that later fails reports an operation error; it does not change the advertised requirements capability to bare mode.

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

The R-backed resolver issues DuckDB's own `INSTALL` for a quoted identifier.
The sans-R resolver passes each name to DuckDB's Python installation API as data on the execution host.
Paths, URLs, repository selectors, version expressions, and SQL fragments are not accepted.

## Python environment selection

The local built-in server uses the top-level `python` setting when present; otherwise it reads inherited `RETICULATE_PYTHON` when it starts:

- unset, empty, or exactly `managed` selects the server-managed environment;
- any other nonempty value selects that existing Python environment.

A user-selected environment is preserved for the worker.
The server skips managed-Python preparation and rejects Python additions from every `send` call shape, automatic imports, or other worker-originated managed-resolution requests.
With R present, R requirements and DuckDB extensions remain available.
Without R, host extension preparation is unavailable for a user-selected Python environment; its preinstalled extensions and custom DB-API connections remain usable.
The selected interpreter must still satisfy the [built-in runtime](BUILTIN_RUNTIME.md) requirement for Python 3.10 or later and must initialize under the worker's offline policy.
Imports already available in the selected environment work normally.
A missing import explains that automatic resolution and `requirements.python` are disabled and directs the user to install the distribution into that environment or restart MCP Console with managed Python enabled.

This selection is independent of custom-worker policy.
A custom worker always rejects managed Python requirements, regardless of `RETICULATE_PYTHON`.

## Bare runtime

Bare mode is selected only when no resolver bootstrap is available from `ir` on `PATH`, `uv` on `PATH`, an explicit `uv` selection, or ambient reticulate.
The server skips default R, Python, and DuckDB preparation.
The stable initial `send` schema retains configured language fields and conditional requirements prose.
After discovery, inspection reports the bare declaration and requirement changes are rejected.
Automatic R wrappers and the Python import resolver callback are disabled.
Installed packages and ambient language adapters continue to work.
Missing R packages keep their ordinary `library()` behavior, while missing Python imports explain that dynamic resolution is unavailable.
Install `ir` or `uv` and restart MCP Console to enable dynamic resolution.

## Custom workers

Custom workers start without the built-in R library, managed Python environment, or default DuckDB extensions.
They can use explicit R requirements and DuckDB extensions, and may opt into the worker-protocol runtime R resolution callbacks.
Managed Python requirements remain unavailable for standalone preparation, cell preconditions, and restart.

Every custom-worker R candidate, whether explicitly or at runtime, includes DBI, DuckDB, and jsonlite so the host can prepare later DuckDB extensions with the same library.
The server supplies the retained library through `R_LIBS` at worker launch.
A running custom worker must implement live R preparation for explicit additions.
If it opts into runtime resolution, it must confirm or reject each provisional library as specified by the [worker protocol](WORKER_PROTOCOL.md).

Prepared extensions remain in DuckDB's native default cache.
A custom worker must use that cache when it loads them.
It must also apply its first managed R library before loading DuckDB; a DuckDB namespace loaded earlier from inherited libraries is outside this contract.

## Host resolution and trust

`mcp-console resolve` is the intended trust boundary for dependency preparation.
Its callers treat it as a trusted command for explicit requirements and automatic imports, accepting structured results only after confirmed subprocess cleanup.
SSH's private `ssh-prepare` command uses the same preparation implementation on the execution host.
Responsibility for making resolution safe belongs to this boundary; callers retain ownership of declarations, admission, generations, and commits.

For local execution, the resolver permissions and startup environment described below belong to the `resolve` subprocess, which runs with the server account's permissions and inherited startup environment.
For [SSH execution](SSH.md#trusted-preparation), they belong to the trusted preparation owner on the execution host.

On macOS and Linux, the default worker sandbox denies direct network access and regular writes outside its private temporary directory and any [explicit writable roots](SANDBOX_CONFIGURATION.md#additional-writable-paths).
Dependency resolution is a deliberate exception to that boundary: the local `resolve` subprocess launches R, Python, and DuckDB resolvers on the host, outside the sandbox.
With `serve --no-sandbox`, the worker and loaded package or extension code use the selected host account's or container's filesystem, process, and network access.
The requirement validation and trusted-resolver rules apply in both modes.

Host resolvers may access the network and their normal caches.
R and Python package installation can execute package installation or build code with the server's filesystem and process permissions.
Managed Python environment startup and Matplotlib font-cache warming can also import or execute selected package code.
Use only trusted requirements and trusted resolver configuration.
`IR_NO_LOCAL_SOURCES` and the Python and DuckDB validation rules reduce the accepted input surface; they do not make arbitrary remote packages safe.

That trust is currently an assumption: the resolver does not yet enforce a secure boundary against malicious package code or worker-modifiable resolver inputs.
Console does not check whether the worker can modify its executable, configuration files, caches, Python installations, or configured local package sources.
Capturing environment values and executable paths does not freeze the files they name.
For example, if the startup `PATH` selects a uv wrapper in a writable workspace, a client can use a Python cell to replace that wrapper, then request a new named package with `control: "restart"`.
The resolver invokes the retained path outside the worker sandbox, so the replacement runs with full host permissions before the old worker is retired.
A writable wheel directory selected by startup `UV_FIND_LINKS` or uv configuration provides another route: a client-created wheel can supply startup code executed during preparation.
These crafted paths can execute client-controlled code outside the worker sandbox even though the submitted requirement contains no path or URL.
This implementation does not close those paths.
Follow-up work will enforce the boundary within the preparation command, including running resolvers in a sandbox.
SSH sans-R support does not add that enforcement or change the caller's trust assumption.

Resolver inputs do not contain submitted cells or `send` stdin:

- Explicit R requirements and validated automatic package names become individual process arguments to `ir`, which receives a constant R program.
- Validated Python requirements, including bare distributions inferred from imports, become separate `uv tool run --with` arguments; the selected version and optional exclusion date use `--python` and `--exclude-newer`, and resolver standard input is closed.
- The resolved interpreter path is returned through a resolver-created output file, not standard output or evaluated R source.
- Validated Python version constraints remain in server memory and are sent to the resolver command; Rust filters the JSON inventory returned by direct `uv python list`, whose standard input is closed.
- DuckDB extension names are validated JSON data and are not submitted SQL.

Evaluated R code can trigger managed R resolution through the built-in `library()` and `loadNamespace()` bridge.
Only validated plain names cross that worker-to-server boundary, and every automatic `ir` invocation still sets `IR_NO_LOCAL_SOURCES=1`.
A plain name restricts resolver syntax, but the selected package's installation or build code still runs with server permissions; use only packages you trust.
Evaluated Python imports and reticulate APIs can trigger managed Python resolution, but the same named-registry and version-constraint validation applies before a host resolver starts.
Host resolution and managed-environment startup may run accepted distributions' installation, build, or initialization code with server permissions; use only packages you trust.

### Host resolver `uv` configuration

Sans-R sessions select uv from the startup `PATH` and ignore `RETICULATE_UV`.
In R-present sessions, an explicit `RETICULATE_UV` startup value is retained.
Otherwise the host resolver selects `uv` from `PATH`, from the managed R library's reticulate installation, or from ambient reticulate.
An invalid explicit executable fails when invoked; the server does not replace it with a `PATH` executable.
Direct Python version inventory and managed-environment creation receive that stable selection.
When `RETICULATE_UV=managed`, the host resolver obtains reticulate's managed executable and uses that same executable, cache directory, and Python installation directory for direct version inventory.
The reticulate bootstrap invocation receives `RETICULATE_UV=managed`, so reticulate validates or installs its managed `uv` rather than recursively selecting an absent `PATH` command.
When the server starts, the host resolver command captures inherited `UV_*` variables except `UV_OFFLINE`.
Before each managed-Python or bootstrap resolver starts, it removes the current `UV_*` environment, restores that startup snapshot, and removes `UV_OFFLINE`.
Changes made later by evaluated R or Python code therefore cannot configure host resolution.

Direct version discovery uses `uv`'s managed and system inventories under the server's startup `PATH`.
It does not add reticulate's separately registered virtualenv directories to `PATH`, so a system interpreter must be discoverable by `uv` there.
Enabled `UV_MANAGED_PYTHON` and `UV_NO_MANAGED_PYTHON` settings are normalized to their equivalent `UV_PYTHON_PREFERENCE` values before direct calls, and recognized disabled aliases are removed.
This avoids conflicting command-line and environment selectors while preserving the requested source policy.
Conflicting or invalid source settings remain unchanged so `uv` reports them normally.
Managed-environment creation passes each validated requirement as its own argument and removes its resolver-created interpreter-path output file after the resolver call.
Sans-R Python resolution uses the captured execution-host `uv` configuration without a managed R library.
R-present SSH preparation retains its existing `R_LIBS` behavior.
It removes `UV_NO_CACHE` after restoring the trusted startup snapshot because `uv tool run` deletes a no-cache tool environment when that command exits; Python version inventory and the other resolver calls retain the setting.
Ordinary Matplotlib cache-warm failures remain best effort, but an interrupt during cache warming fails the preparation before its candidate environment can be committed.

The built-in worker forces `UV_OFFLINE=1` before user code runs, including with `serve --no-sandbox`.
This setting configures `uv`; only the default sandbox supplies a process-level network restriction.

## Failure atomicity and cache effects

Before worker startup and during restart, the server commits the retained R, Python, and DuckDB candidates only after all requested resolution succeeds.
A failure does not change the retained manifest or replace the current worker.
The same pre-start transaction is used for requirements declared by a cell, and any failure prevents that cell from being dispatched.
For inline restart, the same failure also prevents stdin enqueue and worker replacement.

That transaction covers server-owned state, not external resolver caches.
`ir`, `uv`, reticulate, and DuckDB may download, build, or install files before a later step fails.
For example, an earlier extension in a failed multi-extension request can remain in DuckDB's native cache without entering the retained extension set.
A future request may reuse such cache entries.

Live preparation has worker-confirmed commit boundaries.
A Python activation is retained as soon as the worker reports it.
It is not rolled back if an automatic import still cannot find its module, later Python code fails, or a later R step in the same request fails.
An automatic Python candidate that fails before activation is discarded and the earlier reticulate manifest is restored.
An automatic R candidate is retained only after the worker reports that `.libPaths()` accepted its exact library, and it is not rolled back if later namespace loading or cell code fails.
Explicit R and DuckDB changes commit only after their complete live operation succeeds.
For a `send` with explicit requirements, any preparation failure returns through that send and prevents its cell from running, while retaining or discarding candidates according to these existing live-preparation boundaries.
After inline interrupt, this preparation boundary does not roll back the already acknowledged signal or same-call stdin enqueue.
The earlier sections describe how ordinary Python failure, recoverable R failure, and infrastructure failure affect the current worker.
