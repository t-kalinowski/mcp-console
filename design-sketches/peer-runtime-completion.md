# Peer-runtime completion after shared Python bootstrap

This is a follow-up proposal, not implemented behavior.
The current bootstrap and ownership model is described in [ARCHITECTURE.md](../docs/ARCHITECTURE.md#worker).

The common bootstrap accepts a complete inspected Python identity with or without R.
The launch selection can retain both runtimes.
Reticulate no longer owns generic PythonHome, virtualenv activation, PATH, PYTHONPATH, or executable rewriting during initialization.
It still supplies R-specific selection hints, declaration compatibility, conversion metadata, module hooks, and attachment.

## Remaining managed-control boundary

`src/python/native.rs` retains the R-free accepted manifest and selected configuration.
`src/python/requirements.rs` and `requirements/r.rs` retain R declarations, their active-binding representation, history, attributes, and encodings.
`worker_client/environment/preparation.rs` still chooses distinct live-preparation entry points according to R availability.
Those paths already share the host resolver, inspected candidate transport, CPython activation, generation checks, and server acceptance, but do not yet share one worker declaration owner.

Unify these before making Python start without R in an R-capable session:

1. Give `requirements.rs` the single operative declaration and activation state for all workers.
   Move the accepted normalized projection and selected identity out of `native.rs`.
   Keep R protection and representation metadata in `requirements/r.rs`; do not replace its attribute-preserving values with normalized JSON.
2. Route explicit preparation and reached imports through that owner, independently of bridge attachment.
   Keep reticulate argument validation, warnings, version constraints, history, and active bindings as adapters.
   Replace `runtime_python.rs`'s R-availability branch with operation-specific validation, preserving the distinction between a declaration and an accepted environment.
3. Convert the inspected candidate into reticulate's compatibility configuration without a second generic candidate discovery.
   Preserve conversion metadata and exact library compatibility checks.
   The current `bridge.R::candidate_config` and `requirements/r/activation.rs::Adapter::activate` are the remaining boundary.
4. Remove the R closure from ordinary automatic-import resolution once its condition conversion and mutation receipts are represented by the common owner.
   Keep any R-facing wrapper solely for R conditions and interrupts.

`bridge.R::install_python_hooks` also still projects R's startup display width into NumPy and pandas and registers reticulate's Matplotlib load hook.
Move the generic module-load actions into the shared Python runtime before removing this attachment prerequisite; retain R option conversion as an adapter input.
Extend the shared exercise with array/data-frame display and first-import plotting assertions, including user overrides that must not be reset by attachment.

Acceptance in `test_peer_runtime.py`: run one managed exercise with and without R covering idle additions, reached imports, unchanged Python objects and library identity, pre-activation errors, errors after mutation, successful activation followed by failed import, interruption, restart, and crash replacement.
Run the existing representation, transition, automatic-import lifecycle, and SSH ownership suites unchanged.
Assert accepted launch identity after both live activation and preparation while Python is uninitialized.
Resolution, activation, and acceptance must remain separate commits.

## Late R initialization

Moving `initialize_r()` into the first R cell is not sufficient.
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

## Execution-thread constraints

R APIs and protected R objects are bound to the initializing thread.
CPython calls need the appropriate thread state and GIL, while saved initial thread-state restoration and interpreter lifetime remain Console-owned.
Signals and environment variables are process-wide, and synchronous cross-language callbacks can re-enter both runtimes.

The inspected reticulate implementation (`src/python.cpp`, `src/event_loop.cpp`, `R/thread.R`, and `inst/python/rpytools/thread.py`) schedules R callbacks from Python background services onto the main thread and waits for their results.
Positron's use of those services does not establish that two independent blocking Console evaluators can be assigned threads unchanged.
A future split needs a reentrant callback protocol, admission and cancellation rules, GIL-release boundaries, and shutdown ordering that cannot deadlock while one runtime waits on the other.
No scheduling abstraction or extra runtime thread is introduced here.
