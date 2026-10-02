# Windows local execution

Native Windows x64 support is experimental and limited to local `mcp-console serve --no-sandbox` with R and Python.
R and Python are peer runtimes: each initializes lazily on first use, in either order, and either can run without the other installed.
Persistent state, interactive input, plots, cooperative interruption, restart, and session recording are supported.
Reticulate provides interoperability when both runtimes and the bridge are available; ordinary Python execution does not initialize R or require reticulate.

Managed R and Python dependencies use the shared hidden `resolve` subcommand, with `ir` and `uv` materializing the environments on the host.
SQL remains deferred; Windows defaults do not prepare DuckDB extensions.
The native sandbox companion, standalone `sandbox` command, and Windows SSH/Docker/Docker Sandbox controllers are also unsupported.
The release workflow does not publish Windows wheels; Windows source checkouts can build and install a local wheel.

## Build and run

Install Rust's MSVC toolchain, Visual Studio's C++ build tools and Windows SDK, Python 3.11 or newer, and uv.
For R execution, install current x64 R and set `R_HOME` to its installation directory (or place R on `PATH`).
R is not required to build or run Python-only sessions.
Windows builds skip sandbox-companion staging and require a checkout without staged Unix companion files in `wheel-data/data/libexec` or `wheel-data/data/share`.

From PowerShell in the repository root:

```powershell
$env:R_HOME = 'C:/Program Files/R/R-4.6.1'
$env:RETICULATE_PYTHON = 'C:/path/to/python.exe'
uv tool install --reinstall .
mcp-console serve --no-sandbox
```

Replace the paths with the installed runtime locations; omit `R_HOME` for a machine without R.
Select an existing Python with the project `python` setting or `-c 'python="C:/path/to/python.exe"'`, then `RETICULATE_PYTHON`.
Use the actual interpreter or virtualenv executable, not a Windows App Execution Alias.
Explicit Python selections use preinstalled packages; `matplotlib` is needed for Python plots.
With no explicit Python selection, `uv` prepares a managed environment; Python-only sessions require `uv` on `PATH`.
R preparation prefers `ir` (at least 0.4.0) on `PATH`, then `uv tool run --from r-lib-ir ir`, then reticulate's uv bootstrap when available.
Without an R resolver bootstrap, R uses preinstalled packages, with an available PATH Python as a fallback.
Use `requirements` to stage packages, add them to a compatible live environment, or replace the declaration with a restart; reached missing imports and R package loads can request dependencies automatically.
See [requirements](REQUIREMENTS.md) for selection, activation, and the host trust boundary.
Python selection is inspected at startup, while interpreter initialization remains lazy.

The server waits for an MCP client on standard input; it does not open an interactive terminal.
Configure clients with command `mcp-console` and arguments `["serve", "--no-sandbox"]`.
There is no automatic fallback from sandboxed execution.
Keep embedded R sources checked out with LF line endings as specified by `.gitattributes`.
R startup selects the first nonempty `R_USER` or `HOME`, then the Windows user profile directory.
Recording follows the shared [recording directory discovery](RECORDING.md), including `MCP_CONSOLE_HOME` and an existing project `.agents/console` directory.

## Lifecycle and platform differences

The Windows relay uses private named pipes with overlapped I/O, inherited events for interrupts and managed stdin wakeups, and process handles for exit observation.
The public MCP messages, server-relay JSONL, and worker sideband message shapes remain shared across platforms.
R and Python interrupts are cooperative and preserve state when handled; a native call that does not check for interruption may require `restart`.
The worker updates interpreter pending state and invokes the C runtime's current SIGINT handler without requiring a console window.
Its idle command wait also wakes for interrupts and consumes them before dispatching a following cell, including an interrupt and cell supplied in the same `send`.
The Windows worker does not service R's background event loop while waiting between cells; idle callbacks such as `later` are unsupported.

Resolvers and Python inspection enter kill-on-close Jobs while suspended, before executing code.
Cancellation and resolver interruption terminate the Job, and results are accepted only after the Job has no active processes.
These Jobs own trusted host preparation, not evaluated user code, and are not sandboxes.
The server invokes `mcp-console resolve` over cancellable pipes; a lost or unconfirmed cleanup receipt blocks replacement.
R resolver scripts remain compile-time embedded and are passed through temporary files because Windows Rscript does not preserve multiline `-e` arguments.
Unsandboxed evaluated code runs with the user's permissions.
Normal retirement reaps the direct worker but does not promise cleanup of its descendants, matching the existing direct-execution boundary.
An inherited output writer cannot keep the server waiting indefinitely after its owned process exits.
Blocked synchronous relay output may leave an I/O thread until relay process exit, after the worker has been retired.

Windows MCP input has one reader and a bounded 128 KiB queue shared between startup and the running transport.
Startup EOF cancels active Python inspection once the reader observes it; queue backpressure can delay EOF observation until input is consumed.
After startup finishes, EOF is reported only after the queued MCP input is consumed, preserving final request responses.
Windows uses a UTF-8 executable manifest, UTF-16 Python configuration, and native executable suffixes.
The built-in worker uses the C runtime's inherited stdin descriptor because R subprocess helpers can clear the Windows standard-handle table.

## Validation

Native acceptance covers Python-first and R-first startup, each runtime without the other, a Unicode virtualenv path, both bridge directions, input, active and idle interrupts including Python sleep and same-call following cells, plots, recording, restart, Python inspection cleanup/cancellation, and packaging serialization.
`tests/windows.py` includes `tests/windows_relay.py`, which checks relay framing, fatal-error ordering, stdin failures, and final sideband delivery.
It also includes `tests/windows_resolver.py`, covering the resolver protocol, ir/uv arguments, real environment materialization, failures, interrupts, and descendant retirement.
Run native commands exclusively in a checkout; the Unix checkout workflow and transcript/sandbox suites are not Windows validation targets.
Windows source and wheel packaging serialize through a blocking native lock in `.dev-workflow/checkout.lock`.

```powershell
cargo fmt --all --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test --all-targets --all-features
python scripts/validate_runtime_sources.py
python tests/architecture.py
cargo build
python tests/windows.py -v
uv build --wheel --out-dir target/windows-wheels
```

The acceptance interpreter needs `packaging` and `matplotlib`; R needs `reticulate` and `jsonlite`.
Resolver acceptance also needs uv and package repository access; use `ir` 0.4.0 or later if it is on PATH.
For installed-wheel acceptance, set `MCP_CONSOLE_TEST_BINARY` to the installed `mcp-console.exe` and run the same tests.
Tests use `rustc` to build small process fixtures.
R source validation additionally needs `Rscript` on `PATH` and `LC_ALL=C`.

Windows error 4551 during process creation indicates a host application-control block.
Local executables and downloaded interpreter DLLs must be permitted by the host policy; this is separate from Console sandbox support.
