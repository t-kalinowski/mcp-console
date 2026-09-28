# Peer-runtime completion after shared Python bootstrap

This is a follow-up proposal, not implemented behavior.
The current bootstrap and ownership model is described in [ARCHITECTURE.md](../docs/ARCHITECTURE.md#worker).

The common bootstrap accepts a complete inspected Python identity with or without R.
The launch selection can retain both runtimes.
Reticulate no longer owns generic PythonHome, virtualenv activation, PATH, PYTHONPATH, or executable rewriting during initialization.
It still supplies R-specific selection hints, declaration compatibility, conversion metadata and attachment.

## Remaining managed-control boundary

The shared owner in `src/python/requirements.rs` now retains worker live identity and its normalized declaration, resolved candidates, and provisional R declaration values.
The separate `native.rs` state and reticulate resolved-selection slot are removed.
Reached imports use one native callback in both compositions.
The R compatibility adapter projects metadata before activation and commits after success; declarations retain their byte/NA/attribute-preserving representation.
Host-inspected identity supplies activation paths and library compatibility; exact-executable conversion metadata replaces generic `candidate_config()` discovery.
NumPy, pandas, and Matplotlib setup now runs once in the common Python runtime and preserves user overrides.
Unsafe R declaration activation failure now marks the generation restart-required, matching automatic-import activation.

This is an implemented increment, not completed peer-runtime composition.
The remaining control edits precede late R:

1. Replace the `python_only()` branch in `worker_client/environment/preparation.rs` and `python::Runtime::prepare()`'s required reticulate adapter with one ordinary tool-preparation operation.
   The current `Requirements::prepare()` still calls `reticulate::py_require()` in R-present workers; native tool preparation uses a host-supplied candidate.
   Project a detached R declaration when an adapter exists, resolve through the common owner, then activate only when Python is initialized.
   Preserve the lazy `PythonPrepared` commit and its inspected launch identity when Python is still uninitialized.
   Do not publish `PythonActivated` for mere materialization.
2. Keep R argument validation, warnings, live add-only restrictions, exact conditions, and history in the compatibility adapter.
   Keep tool set/reset semantics on the server's replacement boundary.
   Remove the remaining parallel preparation orchestration after migration; do not retain a dispatcher over both implementations.
3. Replace the remaining R-availability checks with idle-addition, prestart, reached-import, R-declaration, and replacement checks.
   Reached imports already use the same declaration validation with and without R.
   Preserve candidate resolution, interpreter mutation, and generation-checked acceptance as separate boundaries.
4. Make `python::Runtime::evaluate()` enter ordinary shared bootstrap without first requiring `reticulate::Adapter::ensure_initialized()`.
   Decide when R hints genuinely require R initialization, document precedence against an explicit or host-resolved selection, and attach reticulate only for integration.
   Keep external startup adoption and independent setup/attachment retry states.

The peer-runtime suite shares bootstrap/replacement, module defaults, and automatic resolver failure/cancellation/unsafe-activation exercises with and without R.
The transition suite reuses the host-inspected rejection fixture in R-present sessions.
The callback suite causally observes idle `later` output before retrieving it with Python or SQL, without another R cell.
Existing lifecycle suites retain generation rejection, failed-import publication, restart/crash, and R metadata coverage.
The two import reentry/finder fixtures now intercept ordinary Python activation (`runpy.run_path`), because imports no longer call the public R declaration function.
The lifecycle interruption fixture now blocks a public NumPy hook during common setup, then verifies retry preserves the exact Python object.
Common module defaults execute after private evaluator installation, through the existing setup-exception boundary; R does not suspend interrupts around those Python calls.
Managed import callback installation also belongs to that completion boundary, so interruption leaves setup retryable.
The startup-adoption case now verifies reached-import resolution and retained publication for an interpreter initialized before Console's R hooks.
An external startup package can choose its original environment again on restart, before Console applies retained declarations.
Those declarations remain accepted, but adoption does not replay activation into the running interpreter; reconciling already-declared missing packages with that external selection remains part of the unfinished control boundary.

Missing acceptance for the next control increment: one shared idle-tool exercise including lazy materialization, provisional R declarations, resolver interruption, unsafe activation, combined Python/R/DuckDB changes, and stale-generation publication.
Keep successful import followed by failed cell, pre-mutation rejection, and R condition/representation checks separate from infrastructure failure.
Run SSH execution-host ownership and prepared-target suites after the control migration.

## Late R initialization

Moving `initialize_r()` into the first R cell is not sufficient.
Demand must also include Python-side R access and an R-owned SQL provider.
Once R is initialized, the coordinator must keep pumping its scheduled callbacks while idle even if no R cell is ever submitted again.
Four current dependencies would break an already-running Python interpreter:

- `worker/coordinator.rs::reexec_with_r_library_path` re-executes the worker on Linux.
  Move this loader preparation to the pre-interpreter launch phase using the captured R home.
  It may still happen before the first Python cell, but it must never run after CPython has started.
  A later re-exec is not a supported recovery path.
- `worker/interrupt.rs::STATE` installs either native or R callbacks once.
  `embedded_r.rs::initialize_r_repl` currently installs the R-owned pending/suspended state.
  Replace that exclusive ownership with a process-lifetime interrupt owner and an explicit R attachment transition that preserves a queued interrupt and reinstalls Python services after R changes handlers.
  Do not clear pending delivery merely to complete attachment.
- `embedded_r.rs::initialize_r` returns R's session temporary directory, which currently anchors mixed-runtime Python caches.
  Choose process-lifetime private storage before either interpreter starts.
  Late R must use its own valid session directory without relocating existing Python caches or letting R cleanup remove live Python storage.
- `python::Runtime::initialize` and `sql::Bridge::initialize` construct R bridges eagerly.
  Build them on actual R attachment.
  R capability in the tool schema must remain distinct from initialized R state.
  Reticulate discovery callbacks and R-side selection hints require R; the common declaration owner must decide when compatibility selection requires R before Python can start.

R's `setup_Rmainloop()` initializes integration; it does not own Console's subsequent command loop.
Late R can remain on the existing coordinator thread.
It must preserve Python's saved thread state, reinstall native stream/input/interrupt services where R or reticulate changes them, and attach conversion to the captured interpreter without rediscovery.
Callback setup must not hold Rust library-state locks across R or Python execution.

Acceptance: an actual initialization probe must show R absent while Python creates an object, connection, input state, and plot configuration.
After the first R operation, all Python identities and selected DB-API connections must survive.
Test the reverse order, synchronous R-to-Python-to-R callbacks, failed/retried attachment, queued and nested interrupts, input cancellation, plots, and shutdown.
Linux must assert stable PID and Python object identity across late R initialization.
Keep direct and native-sandbox executions, plus SSH and prepared-target coverage.

SQL retains its current providers.
Default provider selection must use runtime capabilities; attachment must not discard a selected DBI/DB-API connection or eagerly initialize SQL.
SQL-only startup and separate runtime threads are outside this work.

Prepared discovery in `target_launch/runtime.rs` still requires Python even when R is present.
Permit genuine optional-Python absence while preserving failures for invalid explicit selections; probe only inside the execution target, keep prepared targets non-managed, and extend both Docker and SBX capability tests.
This gap is unchanged by the shared-import increment.
Real SBX acceptance needs an initialized provider policy and a prepared digest-qualified template.

## Execution-thread constraints

R APIs and protected R objects are bound to the initializing thread.
CPython calls need the appropriate thread state and GIL, while saved initial thread-state restoration and interpreter lifetime remain Console-owned.
Signals and environment variables are process-wide, and synchronous cross-language callbacks can re-enter both runtimes.

The inspected reticulate implementation (`src/python.cpp`, `src/event_loop.cpp`, `R/thread.R`, and `inst/python/rpytools/thread.py`) schedules R callbacks from Python background services onto the main thread and waits for their results.
Positron's use of those services does not establish that two independent blocking Console evaluators can be assigned threads unchanged.
A future split needs a reentrant callback protocol, admission and cancellation rules, GIL-release boundaries, and shutdown ordering that cannot deadlock while one runtime waits on the other.
No scheduling abstraction or extra runtime thread is introduced here.
