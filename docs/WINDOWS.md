# Windows local execution

Native Windows x64 support is experimental and currently limited to local `mcp-console serve --no-sandbox`.
The built-in worker supports persistent R, Python through reticulate, DuckDB SQL, managed dependencies, interactive input, plots, interruption, restart, and session recording.
The native sandbox companion, standalone `sandbox` command, and Windows SSH/Docker/Docker Sandbox controllers are not implemented.
The existing release workflow does not publish Windows wheels; Windows source checkouts can build and install a local wheel.

## Build and run

Install Rust's MSVC toolchain, Visual Studio's C++ build tools and Windows SDK, Python 3.11 or newer, uv, and a current x64 R installation.
Set `R_HOME` to the R installation directory or place R on `PATH`.
Rtools matching the installed R version is needed when an R dependency requires compilation from source.
An existing `ir` on `PATH` must be at least 0.4.0; otherwise use uv as described in [requirements](REQUIREMENTS.md).
Windows builds skip sandbox-companion staging and require a checkout without staged Unix companion files in `wheel-data/data/libexec` or `wheel-data/data/share`.

From PowerShell in the repository root:

```powershell
$env:R_HOME = 'C:/Program Files/R/R-4.6.1'
uv tool install --reinstall .
mcp-console serve --no-sandbox
```

Replace the example R version with the installed version.
The server waits for an MCP client on standard input; it does not open an interactive terminal.
Configure clients with command `mcp-console` and arguments `["serve", "--no-sandbox"]`.
There is no automatic fallback from sandboxed execution.
Use `RETICULATE_PYTHON` to select an existing interpreter, or allow managed resolution to choose one.
Keep embedded R sources checked out with LF line endings as specified by `.gitattributes`.

## Lifecycle and platform differences

The Windows relay uses private named pipes with overlapped I/O for its cancellable transports, inherited event handles for interrupts and managed stdin wakeups, and process handles for exit observation.
The public MCP messages, server-relay JSONL, and worker sideband message shapes remain the same.
Built-in R, Python, and DuckDB interrupts are cooperative and preserve state when handled; a native call that does not check for interruption may require `restart`.
The worker delivers interrupts through interpreter pending flags and the C runtime's current SIGINT handler, including DuckDB's active-query handler, without requiring a console window.
The Windows worker does not yet service R's background event loop while waiting between cells; idle callbacks such as `later` are not supported in this first implementation.
Resolver interruption terminates its Job Object instead of sending a Unix signal.
Resolvers enter a Job while suspended, before executing code, and cleanup is confirmed only after the Job has no active processes.
These Jobs own dependency preparation, not evaluated user code.

Unsandboxed evaluated code runs with the user's permissions.
Normal retirement reaps the direct worker, but does not promise cleanup of its descendants; this is the existing direct-execution boundary.
An inherited output writer cannot keep the server waiting indefinitely after its owned process exits.
Blocked synchronous relay output may leave an I/O thread until relay process exit, after the worker has been retired.

Windows MCP input has one reader and a bounded 128 KiB queue shared between startup and the running transport.
Startup EOF cancels an active resolver once the reader observes it; if a client fills that queue before initialization completes, backpressure delays observation until input is consumed.
Windows uses a UTF-8 executable manifest, UTF-16 Python configuration, and native executable suffixes.
The built-in worker uses the C runtime's inherited stdin descriptor because R subprocess helpers can clear the Windows standard-handle table.

## Validation

The initial implementation was exercised on Windows x64 with R 4.6.1, explicit Python 3.14, and a managed Python environment.
`tests/windows.py` covers the public MCP interface, dependency-process cleanup, startup cancellation, and explicit unsandboxed launch.
The full Unix transcript harness and sandbox suite are not Windows validation targets yet.

```powershell
cargo fmt --all --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test --all-targets --all-features
cargo build
python tests/windows.py -v
uv build --wheel --out-dir target/windows-wheels
```

For installed-wheel acceptance, set `MCP_CONSOLE_TEST_BINARY` to the installed `mcp-console.exe` and run the same tests.
The tests use `rustc` to build small resolver fixtures.
R source validation additionally needs `Rscript` on `PATH`.

Windows error 4551 during process creation indicates a host application-control block.
Local executables and downloaded interpreter DLLs must be permitted by the host policy; this is separate from Console sandbox support.
