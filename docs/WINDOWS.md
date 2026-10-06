# Windows local execution

Native Windows x64 support is experimental and supports local sandboxed or `--no-sandbox` sessions with R and Python.
R and Python are peer runtimes: enabled interpreters initialize in the background before the first cell, and either can run without the other installed.
Persistent state, interactive input, plots, cooperative interruption, restart, and session recording are supported.
Reticulate provides interoperability when both runtimes and the bridge are available; Python-only sessions do not require R or reticulate.

Managed R and Python dependencies use the shared hidden `resolve` subcommand, with `ir` and `uv` materializing the environments on the host.
SQL remains deferred; Windows defaults do not prepare DuckDB extensions.
The release workflow does not publish Windows wheels; Windows source checkouts can build and install a local wheel.

## Build and run

Install Rust's MSVC toolchain, Visual Studio's C++ build tools and Windows SDK, Python 3.11 or newer, and uv.
For R execution, install current x64 R and set `R_HOME` to its installation directory (or place R on `PATH`).
PATH discovery supports `R.exe`, `R.bat`, and `R.cmd`, including rig's batch launcher.
It searches directories in PATH order, preferring `.exe`, then `.bat`, then `.cmd` within each directory.
An explicit `R_HOME` takes precedence; a broken selected installation or launcher reports an error.
R is not required to build or run Python-only sessions.
Windows builds stage the pinned native sandbox executable and both Windows helpers.
Installed wheels expose an environment-bound Python launcher so `uv tool install` can copy the command onto PATH while the native executable and verified helpers remain together under the tool environment's `libexec` directory.
Install CMake for the companion build.
Cargo-only builds first need `python scripts/stage-sandbox-runner`.

From PowerShell in the repository root:

```powershell
$env:R_HOME = 'C:/Program Files/R/R-4.6.1'
$env:RETICULATE_PYTHON = 'C:/path/to/python.exe'
uv tool install --reinstall .
mcp-console sandbox-setup
mcp-console serve
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
Python selection is inspected at startup, and enabled interpreters initialize in the background before the first cell.

The server waits for an MCP client on standard input; it does not open an interactive terminal.
Configure clients with command `mcp-console` and arguments `["serve"]`; use `["serve", "--no-sandbox"]` to explicitly run with host permissions.
Local sandboxed sessions redirect resolver and worker caches to `%LOCALAPPDATA%/mcp-console/cache/dependencies`, or `<XDG_CACHE_HOME>/mcp-console/dependencies` when selected.
`cache: host` retains host caches, as does `--no-sandbox` by default.
Cache redirection does not sandbox Windows dependency preparation; it retains host permissions and its owned Job lifecycle.
There is no automatic fallback from sandboxed execution.
Keep embedded R sources checked out with LF line endings as specified by `.gitattributes`.
R startup selects the first nonempty `R_USER` or `HOME`, then the Windows user profile directory.
Recording follows the shared [recording directory discovery](RECORDING.md), including `MCP_CONSOLE_HOME` and an existing project `.agents/console` directory.

## Native sandbox

`mcp-console sandbox-setup` explicitly provisions the Console sandbox accounts and network rules through Windows UAC.
Run it interactively, then use `mcp-console sandbox-setup --status` to check readiness.
Ordinary sandbox launches fail with setup guidance if provisioning is missing; they do not silently retry unsandboxed or choose a weaker backend.
Setup uses Console accounts, separate from Codex accounts.
Persistent state defaults to `%LOCALAPPDATA%\mcp-console`.

The default elevated backend enforces restricted networking and filesystem writes.
`:workspace`, `:read-only`, and `--writable-root` use native policy composition.
Private storage is exported through `TMPDIR`, `TEMP`, and `TMP`.
Standalone execution uses the same bundle: `mcp-console sandbox -- python script.py`.

For explicitly network-enabled workloads, the restricted-token backend avoids account provisioning:

```yaml
sandbox:
  windows_sandbox_level: restricted-token
  network: enabled
```

It restricts writes but requires host reads; read-deny policies are rejected.
`windows_state_dir` optionally selects an absolute persistent state directory; use the matching `sandbox-setup --state-dir PATH` when provisioning.
Use one stable directory per Windows user: accounts and firewall policy are machine resources, and capability ACL entries persist on filesystem objects.
Managed proxy configuration, custom cleanup timeouts, and Unix-only backend options are unsupported and fail before target launch.

The native runner owns a non-breakaway Job for each workload.
It terminates remaining descendants and confirms zero active processes before reporting exit.
Only that receipt permits a replacement generation and private-storage removal.
Cleanup failure retains storage and blocks replacement.
The Windows frontend waits for the runner; forced frontend termination does not confirm cleanup.
Process handles monitor both the frontend and session owner.
Runner/helper death can terminate Jobs, but does not guarantee storage deletion.

## Lifecycle and platform differences

The Windows relay uses private named pipes with overlapped I/O, inherited events for interrupts and managed stdin wakeups, and process handles for exit observation.
Pipe permissions use the current logon SID so restricted tokens can open both endpoints; remote clients are rejected and both endpoints are connected before the worker starts.
The public MCP messages, server-relay JSONL, and worker sideband message shapes remain shared across platforms.
R and Python interrupts are cooperative and preserve state when handled; a native call that does not check for interruption may require `restart`.
The worker updates interpreter pending state and invokes the C runtime's current SIGINT handler without requiring a console window.
Its idle command wait also wakes for interrupts and consumes them before dispatching a following cell, including an interrupt and cell supplied in the same `send`.
After R initializes, the idle wait also wakes for Windows messages and services R's background event loop, including `later` timers, on the interpreter thread.
The wait retires pending pipe I/O and releases the command-reader lock before dispatching callbacks, so callback input and resolver requests can use the same sideband.
Incomplete command frames survive message and interrupt wakeups.

Resolvers and Python inspection enter kill-on-close Jobs while suspended, before executing code.
Cancellation and resolver interruption terminate the Job, and results are accepted only after the Job has no active processes.
These Jobs own trusted host preparation, not evaluated user code, and are not sandboxes.
The server invokes `mcp-console resolve` over cancellable pipes; a lost or unconfirmed cleanup receipt blocks replacement.
After spawning a resolver, the parent releases the command builder's child-side pipe handles so a rejected startup can confirm shutdown and EOF without a spurious retirement timeout.
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
After R compatibility calls, Console restores that table from the live CRT descriptors so Python subprocesses can inherit stdio.
Deferred R startup synchronizes the Win32 and CRT environment views before restoring default packages.
Python bootstrap uses native separators when R supplies a forward-slash executable path, so CPython finds the selected virtualenv's `pyvenv.cfg`.
R's `commandArgs()` retains the selected launcher and the full interactive startup arguments, matching the Unix worker's interpreter identity.
Live Python activation also updates an already imported Joblib process backend's interpreter selection; virtualenv launchers must not retain handles intended for its task processes.

## Validation

Native sandbox acceptance covers policy enforcement, stdio/exit propagation, private storage, and descendant retirement before restart.
Native runtime acceptance covers Python-first and R-first startup, each runtime without the other, a Unicode virtualenv path, both bridge directions, input, active and idle interrupts including Python sleep and same-call following cells, plots, recording, restart, Python inspection cleanup/cancellation, and packaging serialization.
`tests/windows.py` includes `tests/windows_relay.py`, which checks relay framing, fatal-error ordering, stdin failures, and final sideband delivery.
It also includes `tests/windows_resolver.py`, covering the resolver protocol, ir/uv arguments, real environment materialization, failures, interrupts, and descendant retirement.
Run native commands exclusively in a checkout.
The default workflow runs native Windows acceptance.
The full workflow also discovers and runs the shared boundary cases whose declared capabilities are available.
Installation checks additionally exercise the copied public command from an isolated `uv tool install`, including sandbox setup inspection, worker state/restart, stdio, and full-width exit status.
The `.cmd` launchers work in PowerShell and Command Prompt; `python scripts/COMMAND` is an equivalent entry point using an explicitly selected Python.
Python 3.11 or newer is required; CI uses Python 3.13.

Configure `R_HOME` before running the complete suite, and use the C locale to match CI.
An explicit `R_HOME` selects the installation for runtime and resolver tests and enables R sandbox acceptance.
Replace the example path below with the installed R directory.

```powershell
$env:R_HOME = 'C:/Program Files/R/R-4.6.1'
$env:LC_ALL = 'C'
scripts/preflight.cmd
scripts/stage-sandbox-runner.cmd
scripts/test.cmd --list
scripts/test.cmd --locate WindowsConsole.test_python_without_r
scripts/test.cmd WindowsConsole.test_python_without_r
scripts/test.cmd client_server/python/test_runtime
scripts/test.cmd --full --list
scripts/format.cmd
scripts/check.cmd
scripts/check.cmd --full
scripts/with-checkout.cmd cargo build
scripts/review-diff.cmd origin/main
```

`preflight` is read-only; its Windows inventory currently skips companion inspection.
Stage the Windows companion with `scripts/stage-sandbox-runner.cmd` before native build, test, or check commands; packaging stages it automatically.
R is optional in its inventory so Python-only setups can be inspected; the complete acceptance suite needs both runtimes.
Failed optional R probes remain visible in the inventory without failing preflight; required tool and probe failures still fail it.
`test` builds `target/debug/mcp-console.exe` unless `MCP_CONSOLE_TEST_BINARY` selects an installed executable; no selectors runs all native cases.
Boundary selectors use the shared `BOUNDARY/SUITE::CASE` syntax and support `--update`, `--jobs`, and `--timeout`.
Unscoped `test --full` adds all applicable shared cases.
Shared concurrency defaults to at most six cases on Windows.
`check` validates embedded sources, architecture, Rust formatting, Clippy, Rust tests, and native acceptance.
`--full` adds shared boundary cases, portable transcript-runner/MCP-client/release tooling regressions, wheel acceptance, and source-install acceptance in a temporary virtualenv.
`format` runs ruff, yamark, rustfmt, and air, reports every failure, and only returns failure with `--strict`.
Install those formatters separately; missing tools and host policy blocks are reported rather than silently ignored.

### Testing parity and remaining gaps

Native input/interrupt acceptance uses the public `waiting for stdin` receipt before providing input, then observes loop output before interrupting.
It requires no networking or host-to-sandbox fixture access.
The same scenario runs directly and with an explicitly network-enabled restricted token in `WindowsSandbox.test_restricted_token_input_and_interrupt`.
To run it against an already-provisioned elevated, network-restricted backend, set `MCP_CONSOLE_TEST_WINDOWS_STATE_DIR` to its absolute state directory and run `WindowsSandbox.test_provisioned_network_restricted_input_and_interrupt`.
The test never provisions accounts or chooses another backend.
Without that opt-in directory, it reports unavailable coverage; a skip does not establish network-restricted compatibility.

Inspection, resolver, raw relay, and inherited-writer fixture checkpoints use unique local named pipes with ordinary host ACLs and non-inheritable handles.
Their readiness and release operations have bounded deadlines; cancellation joins pending native I/O before releasing its storage.
The host pins process identities before release or cancellation.
These are host-plumbing tests, not sandbox-enforcement evidence.
`WindowsSandbox.test_network_enabled_allows_loopback_exchange` explicitly tests positive loopback communication with networking enabled.
The provisioned backend's loopback restriction still needs separate acceptance against its established policy contract; restricted-token tests do not imply loopback denial.

Shared discovery reports unavailable capabilities per case and execution mode; a skip is not validation.
Windows full checks exercise portable R/Python execution, startup, bridge attachment, input, plots, managed activation, process creation, recording, CLI configuration, and protocol behavior in addition to native acceptance.
The shared Windows pipe reader uses blocking native reads with socket notifications.
Each shared case runs in a kill-on-close Job; cancellation gives the case 15 seconds to run cleanup, then requires confirmed descendant retirement before deleting its workspace.
Owner-loss cleanup remains independent of the case interpreter, including native calls holding Python's GIL.
Portable idle R callback cases declare `R_EVENT_LOOP` and run on Windows; Unix FIFO input-handler fixtures retain `POSIX`.
Windows `later` uses window messages and timers rather than Unix polled-event hooks.
Native acceptance also exercises callback input, plots, interruption, and shared R/Python state directly and with a restricted token, and preserves incomplete UTF-8 commands across timer dispatch.
Shared interpreter-identity cases launch native `R.exe` and `Rscript.exe` and compare resource paths by filesystem identity, since the stock Windows launcher may shorten `R_HOME` to an 8.3 path.
The native child-launch fixture uses ASCII arguments because the stock `R.exe` delegates through an ANSI command line; shared cell tests separately exercise Unicode R input.

| Exclusion                                                                                                                  | Assessment and Windows coverage                                                                                                                                                                                                                                                     |
| -------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| SQL                                                                                                                        | Runtime features are deferred; cases declare those capabilities rather than failing while launching an unavailable runtime.                                                                                                                                                         |
| Seatbelt/bubblewrap policy, ELF interposition, procfs, Unix signals, PTYs, descriptor inheritance, and non-UTF-8 filenames | OS-specific contracts and fixtures remain on their owning platforms. Native Windows policy, relay, input, cancellation, and Job retirement have separate acceptance cases. The shared `sandbox` capability refers to Unix fixtures, not absence of a Windows sandbox.               |
| Shell/shebang fake workers and resolvers, FIFO checkpoints, Unix virtualenv or R-library layouts                           | Remaining fixture debt for otherwise supported behavior. These cases declare `POSIX`; Windows native tests cover some corresponding contracts, but do not replace every skipped admission, SDK, resolver, and lifecycle scenario. Port the fixture before removing its requirement. |
| Unix staging/release executable fixtures and Rust Unix descriptor fixtures                                                 | Keep the native ABI/build requirements. Windows checkout ownership, packaging, and installation are exercised through the native workflow; portable release-manifest, client, and runner checks run on both platforms.                                                              |

Handshakes, CLI usage, and early explicit-selection failures retain Windows snapshots where public behavior differs.
Generic snapshots remain the Unix references; platform updates preserve both and remove only obsolete companions owned by the updated case.
Use `test --full --list` and capability skip diagnostics to audit current coverage instead of treating native acceptance alone as parity.

Build, test, and packaging entry points share `.dev-workflow/checkout.lock`, outside `target`.
Use `with-checkout` for direct build commands.
Independent workflows fail when busy; source/wheel packaging waits, and nested packaging reuses the workflow's owner.
Windows workflow phases use Jobs to retire descendants on completion or cancellation before releasing the lock.
These are development-command ownership guarantees; they do not add sandboxing to evaluated user code.

The acceptance interpreter needs `packaging` and `matplotlib`; R needs `reticulate` and `jsonlite`.
Resolver acceptance also needs uv and package repository access.
Full checks include shared plotting and R resolver cases that require `ir` 0.4.0 or later on PATH; install it with `uv tool install r-lib-ir`, as CI does.
For installed-wheel acceptance, set `MCP_CONSOLE_TEST_BINARY` to the installed `mcp-console.exe` and run the same tests.
When selecting a copied tool command outside its environment, also set `MCP_CONSOLE_TEST_NATIVE_BINARY` to that environment's `libexec/mcp-console.exe` for fixtures that require the native PID or relocate its bundle.
Tests use `rustc` to build small process fixtures.
R source validation finds `Rscript.exe` under `R_HOME` (including `bin/x64`) or on `PATH`, and uses `LC_ALL=C` for the syntax checker.

Windows error 4551 during process creation indicates a host application-control block.
Local executables and downloaded interpreter DLLs must be permitted by the host policy; this is separate from Console sandbox support.
