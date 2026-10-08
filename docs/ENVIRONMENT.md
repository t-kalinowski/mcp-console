# Environment variables

Most installations do not need additional environment settings.
Prefer [configuration](CONFIGURATION.md) for settings that have a public YAML equivalent, such as `python`, `languages`, `r.vanilla`, and sandbox permissions.

This reference covers variables that Console reads, captures, sets, or removes during local startup, dependency preparation, and built-in R/Python/SQL execution.
It distinguishes those rules from settings interpreted by the underlying runtimes and package tools.
Custom workers can have additional variables of their own.
Running an arbitrary command with `mcp-console sandbox` does not apply the built-in interpreter initialization described below.

## Where to set variables

The **server environment** is the environment of the process that launches `mcp-console serve`: for example, a shell or the MCP client's server configuration.
Server-side settings must be present there before Console starts.
Setting `MCP_CONSOLE_HOME` inside an R or Python cell, or under `environment:` in YAML, does not relocate the running server.

The **worker environment** starts with inherited variables, followed by the shared `environment` mapping.
The **resolver environment**, used for dependency discovery and preparation, additionally applies `resolver.environment`:

```yaml
environment:
  PYTHONPATH: /work/shared/python
  MPLBACKEND: agg
resolver:
  environment:
    UV_HTTP_TIMEOUT: "120"
```

`inherit_environment: false` removes inherited variables from the workload environment.
`resolver.inherit_environment` defaults to the shared setting but can be set independently.
Explicit mappings still apply.
These controls do not clear the trusted supervisor's own environment.

Console then applies its runtime selection, cache, temporary-directory, proxy, and process-transport adjustments.
Those adjustments take precedence over conflicting YAML or inherited values.
The tables below identify the affected names.
R startup files and user code can subsequently change the worker's environment; that does not reconfigure the separate resolver.

Configuration, runtime selection, and resolver settings are retained across worker restarts.
Start a new server connection to pick up changes to the launching environment or YAML.
A worker restart does rerun normal R startup, including changes to the startup files themselves.

All YAML environment values must be strings.
Quote numbers and booleans.
An empty string is a value, not an instruction to unset a variable; `null` is not accepted.
Empty and absent values are not interchangeable, as noted below.

Shared environment values, including credentials, also reach dependency preparation.
Use `resolver.environment` for values needed only by preparation, and avoid printing an entire environment when troubleshooting.

## Server paths and executable discovery

| Variable            | Effect and default                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `MCP_CONSOLE_HOME`  | Absolute path to Console's home directory. Defaults to `$HOME/.agents/console`. It relocates the home fallback `config.yaml` and home session storage, not the operating-system home, package caches, or project-local storage. Empty and relative values are errors when this directory is needed; a literal `~` is not expanded. Set this in the server's launch environment.                                                                                                          |
| `HOME`              | Supplies the fallback Console home directory and expands a leading `~` in the public `python` setting. These uses require an absolute path. Missing or empty `HOME` means no home configuration fallback; it also prevents `~` expansion. The resolver and runtimes use their own effective `HOME` for caches and native startup. A YAML override changes those child uses, not server-side configuration discovery.                                                                     |
| `PATH`              | Used to find R and resolver executables such as `ir` and `uv`. It also affects subprocess discovery. Built-in Python initialization prepends the selected interpreter's executable directory; live managed-environment activation updates that directory. A broken selected executable is an error, not necessarily a reason to continue searching. On Windows, Console's R lookup considers `R.exe`, `R.bat`, and `R.cmd`.                                                              |
| `R_HOME`            | Selects an R installation directory, not an R executable. Otherwise Console looks for R on `PATH`. Presence of `R_HOME`, even if empty or invalid, counts as an R selection and can cause startup failure rather than fallback to Python-only operation. Discovery uses the resolver environment; the resulting R installation is carried into the worker.                                                                                                                               |
| `RETICULATE_PYTHON` | Launch-time Python selection, also recognized without R. A nonempty value other than the literal `managed` selects an existing interpreter and disables Console-managed Python package preparation for that selection. Unset, empty, and `managed` do not count as an explicit interpreter selection. The public `python:` setting takes precedence. Console retains the selection across worker restarts; workload `environment` or `resolver.environment` overrides do not replace it. |
| `RETICULATE_UV`     | uv selection for R/reticulate-backed preparation. An explicit executable takes precedence over `uv` on `PATH` in that Python-resolver path; `managed` delegates acquisition to the reticulate bootstrap when available. An invalid explicit selection can fail rather than fall back. **R-free preparation ignores this variable and uses `uv` on `PATH`.** It is not a universal override for every resolver subprocess.                                                                |
| `SystemRoot`        | Windows only. Used to locate `System32/cmd.exe` for sandbox preflight. A missing value fails that preflight. This is a supervisor setting, not a workload-only override.                                                                                                                                                                                                                                                                                                                 |

Home configuration is currently a fallback, not an additional merged layer: an existing launch-directory `.agents/console/config.yaml` takes its place.
`MCP_CONSOLE_HOME` does not change that rule.
On Windows, Console's home configuration/storage lookup uses `HOME` or `MCP_CONSOLE_HOME`; it does not substitute `USERPROFILE`.
Cache selection has different Windows fallbacks, below.

For example, in a POSIX shell:

```sh
MCP_CONSOLE_HOME=/home/alice/console \
RETICULATE_PYTHON=/home/alice/project/.venv/bin/python \
mcp-console serve
```

Use `python:` in YAML instead of `RETICULATE_PYTHON` when the selection belongs in Console configuration.
Do not use `UV_PYTHON`, `PYTHONHOME`, or an activated shell environment as substitutes for that explicit selection.

## Python and reticulate

These rules apply to built-in Python initialization.
Reticulate-specific attachment behavior applies only when R and reticulate are involved.

| Variable                                | Effect                                                                                                                                                                                                                                                                                                                                     |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `PYTHONPATH`                            | Additional Python import paths. Console preserves it unless `RETICULATE_PYTHONPATH` is present. This selects import paths, not an interpreter.                                                                                                                                                                                             |
| `RETICULATE_PYTHONPATH`                 | Replaces `PYTHONPATH` before native Python initialization, **including when set to an empty string**. This behavior also applies in Python-only sessions. Unset it to retain an existing `PYTHONPATH`.                                                                                                                                     |
| `PYTHONHOME`                            | Removed before Console initializes the selected native Python. Console uses the inspected interpreter's embedding configuration instead.                                                                                                                                                                                                   |
| `PYTHONPLATLIBDIR`                      | Also removed before native Python initialization, to avoid conflicting with the selected interpreter's library layout.                                                                                                                                                                                                                     |
| `VIRTUAL_ENV`                           | Removed from managed uv version-discovery and environment-resolution subprocesses. In the worker, set to the selected virtual environment's prefix, or removed when the selected Python is not a virtual environment. Activation of a shell environment is therefore not a reliable Console Python selector.                               |
| `VIRTUAL_ENV_PROMPT`                    | Preserved as part of the rollback snapshot during live Python environment activation. It is not an interpreter-selection setting; an environment's activation hook may update it.                                                                                                                                                          |
| `RETICULATE_CHECK_REQUIRED_PACKAGES`    | Controls reticulate's required-package check when attaching to a virtual environment. Defaults to enabled. Case-insensitive `true`, `1`, and `yes` enable it; other values, including an explicitly empty string, disable it. This controls the compatibility check, not Console's requirements manifest or automatic package preparation. |
| `RETICULATE_ENABLE_PYTHON_FINALIZER`    | Opts into reticulate's Python finalizer at R process exit. Defaults to disabled. Case-insensitive `true`, `1`, and `yes` enable it; other values do not.                                                                                                                                                                                   |
| `RETICULATE_REMAP_OUTPUT_STREAMS`       | Set to `0` by Console when installing the R/Python integration. Console supplies its own stream integration.                                                                                                                                                                                                                               |
| `R_SESSION_INITIALIZED`                 | Set by the R/Python integration to a reticulate session marker containing the process ID. This is interoperability state, not a user setting.                                                                                                                                                                                              |
| `PYTHONUSERBASE`, `PYTHONPYCACHEPREFIX` | User-site and bytecode-cache locations. Console cache mode redirects them as described below, with an exception for explicitly selected Python's user base.                                                                                                                                                                                |

Changing `RETICULATE_PYTHON` after Python has initialized does not switch the live interpreter.
R-side selection checks can instead report that a restart is required.
For a different launch selection, change the server configuration or launching environment and start a new connection.

When an R-side startup path delegates discovery to reticulate, additional reticulate hints can matter: `RETICULATE_PYTHON_ENV`, `RETICULATE_PYTHON_FALLBACK`, `RETICULATE_USE_MANAGED_VENV`, and `WORKON_HOME`, among others.
They are not independent selectors in Console's Python-only path, and they do not override an already running interpreter.
See reticulate's [order of discovery](https://rstudio.github.io/reticulate/articles/versions.html#order-of-discovery) for the installed reticulate version's rules.

## Python dependency preparation: `UV_*`

Console captures the effective environment variables whose names start with `UV_` for managed Python resolution.
This is an open-ended family, not a fixed allowlist.
Other than the exceptions below, their meaning and validation belong to the installed uv version and the uv subcommand being run.

| Variable or family                                                                             | Console-specific behavior                                                                                                                                                                                                                                                                                                                                                     |
| ---------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `UV_OFFLINE`                                                                                   | **Removed from managed uv preparation commands.** Built-in workers separately set it to `1`, including with `--no-sandbox`. Consequently, setting it in the launching environment does not make Console's dependency resolver offline, and seeing it inside a cell does not describe resolver permissions. Use the supported resolver network policy to restrict preparation. |
| `UV_NO_CACHE`                                                                                  | Removed from the `uv tool run --isolated` command that creates Console's retained Python environment, because uv deletes a no-cache environment when the command exits. Still forwarded to `uv python list` for version discovery. It does not disable caching for retained environment preparation.                                                                          |
| `UV_PYTHON_PREFERENCE`                                                                         | Set to `only-managed` for version discovery and environment preparation. Ambient preferences cannot enable system interpreter candidates.                                                                                                                                                                                                                                     |
| `UV_MANAGED_PYTHON`                                                                            | Removed from managed preparation commands; Console supplies its own `only-managed` preference.                                                                                                                                                                                                                                                                                |
| `UV_NO_MANAGED_PYTHON`                                                                         | Also removed, so it cannot substitute a system interpreter for managed CPython.                                                                                                                                                                                                                                                                                               |
| `UV_PYTHON`                                                                                    | Removed from managed preparation commands. Use `python:` to select an existing interpreter, or `requirements.python_version` to constrain managed version selection.                                                                                                                                                                                                          |
| `UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR`, `UV_PYTHON_BIN_DIR`, `UV_TOOL_DIR`, `UV_TOOL_BIN_DIR` | Cache/install locations are replaced in Console cache mode. In host cache mode they participate in native tool selection and default resolver write grants. A reticulate-managed uv bootstrap can also supply its own cache/install locations.                                                                                                                                |
| Other `UV_*` values                                                                            | Forwarded from the captured resolver environment, subject to the selected uv command and Console's explicit command-line arguments. Examples include index URLs, index credentials, TLS options, timeouts, download mirrors, build controls, and configuration-file selection.                                                                                                |

The [uv environment reference](https://docs.astral.sh/uv/reference/environment/) describes the remaining names.
In particular, package-index and certificate settings are uv settings, not permission grants.
Changing a package mirror may also require changing `resolver.sandbox.network` to permit the mirror and its artifact hosts.
See [resolver configuration](RESOLVER.md#configuration).

Console discovers and materializes only uv-managed CPython, running environments through `uv tool run --isolated --python-preference only-managed` with an explicit interpreter.
If no supported managed version is available, preparation fails rather than selecting a system interpreter.
Use `python:` for an existing interpreter with preinstalled packages.
Forwarding a variable does not imply that a uv project setting will select Console's environment.

## Cache and data locations

Sandboxed local sessions default to `cache: console`; `--no-sandbox` defaults to `cache: host`.
Combining `cache: console` with `--no-sandbox` is rejected.
Windows applies cache redirection, but dependency preparation still runs with host permissions.

### Selecting the Console cache root

Root selection uses the **resolver's effective environment before cache redirection**, not the worker's eventual temporary cache values.

| Variable         | Role                                                                                                                                                                                                                               |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `XDG_CACHE_HOME` | An absolute value selects `<XDG_CACHE_HOME>/mcp-console/dependencies` on every platform, without requiring `HOME`. Relative values are not used for this root selection.                                                           |
| `LOCALAPPDATA`   | Windows fallback when there is no absolute `XDG_CACHE_HOME`: `<LOCALAPPDATA>/mcp-console/cache/dependencies`, provided `LOCALAPPDATA` is absolute.                                                                                 |
| `HOME`           | Otherwise supplies the home fallback: `Library/Caches/mcp-console/dependencies` on macOS, `.cache/mcp-console/dependencies` on Linux, or `AppData/Local/mcp-console/cache/dependencies` on Windows. Must be absolute for this use. |
| `USERPROFILE`    | Windows fallback for an unavailable absolute `HOME` in cache and DuckDB path selection. It does not replace `HOME` for Console's home configuration discovery.                                                                     |

If the required root cannot be determined, Console reports an error.
`MCP_CONSOLE_HOME` does not relocate these caches.

### Variables redirected by `cache: console`

Let **`D`** be the selected `…/mcp-console/…/dependencies` directory.
Console supplies the following values to both preparation and workers, overriding inherited values and conflicting shared/resolver YAML mappings.

| Variable                                 | Value under Console cache mode                                                                                  |
| ---------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `XDG_CACHE_HOME`                         | `D`                                                                                                             |
| `XDG_DATA_HOME`                          | `D/data`                                                                                                        |
| `UV_CACHE_DIR`                           | `D/uv/cache`                                                                                                    |
| `UV_PYTHON_INSTALL_DIR`                  | `D/uv/python`                                                                                                   |
| `UV_PYTHON_BIN_DIR`                      | `D/uv/bin`                                                                                                      |
| `UV_TOOL_DIR`                            | `D/uv/tools`                                                                                                    |
| `UV_TOOL_BIN_DIR`                        | `D/uv/bin`                                                                                                      |
| `IR_CACHE_DIR`                           | `D/ir`                                                                                                          |
| `IR_LIBRARY_ROOT`                        | `D/ir/libraries`                                                                                                |
| `R_USER_CACHE_DIR`                       | `D`                                                                                                             |
| `R_USER_DATA_DIR`                        | `D/data`                                                                                                        |
| `RENV_PATHS_ROOT`                        | `D/renv`                                                                                                        |
| `RENV_PATHS_CACHE`                       | `D/renv/cache`                                                                                                  |
| `RENV_PATHS_SOURCE`                      | `D/renv/source`                                                                                                 |
| `RENV_PATHS_BINARY`                      | `D/renv/binary`                                                                                                 |
| `PKG_CACHE_DIR`                          | `D/R/pkgcache`                                                                                                  |
| `R_PKG_CACHE_DIR`                        | `D`                                                                                                             |
| `MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY` | `D/duckdb/extensions`                                                                                           |
| `MPLCONFIGDIR`                           | `D/matplotlib`                                                                                                  |
| `PYTHONPYCACHEPREFIX`                    | `D/python/bytecode`                                                                                             |
| `PYTHONUSERBASE`                         | `D/python/user`, **except for explicitly selected Python**, which preserves the inherited/configured user base. |

These are launch/preparation assignments, not a promise that every value will remain unchanged inside a cell.
Built-in workers subsequently redirect `XDG_CACHE_HOME` and `MPLCONFIGDIR` to private temporary directories.
Explicitly selected Python retains its own DuckDB settings and preinstalled extensions rather than Console-managed extension preparation.

### Host caches and write permissions

With `cache: host`, Console does not apply the redirection table.
The resolver's effective cache variables and tool defaults determine locations.
On macOS/Linux, Console inspects those paths when generating the default resolver's writable roots.

The inspected host-cache names are `HOME`, `XDG_CACHE_HOME`, `XDG_DATA_HOME`, `R_USER_CACHE_DIR`, the five uv path variables above, `IR_CACHE_DIR`, `IR_LIBRARY_ROOT`, the four `RENV_PATHS_*` names above, `PKG_CACHE_DIR`, `R_PKG_CACHE_DIR`, `MPLCONFIGDIR`, `PYTHONPYCACHEPREFIX`, `PYTHONUSERBASE`, and `MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY`.

For R-related host-cache grants, the cache base is `R_USER_CACHE_DIR`, then `XDG_CACHE_HOME`, then R's platform fallback.
Relative `XDG_CACHE_HOME` and `XDG_DATA_HOME` values are ignored for uv's default grants.
Default host-cache grants require an absolute resolver `HOME`.
See [host cache selection](RESOLVER.md#host-cache-selection) for the complete path formulas.

Console does not inspect uv configuration files to infer additional writable paths.
With host caches, expose a custom path through its environment variable or grant the path explicitly.
An explicit resolver filesystem mapping replaces the default entries; it is not automatically augmented with cache write permissions.

For example:

```yaml
cache: host
resolver:
  environment:
    UV_CACHE_DIR: /home/alice/package-caches/uv
```

### DuckDB extension directory

`MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY` is a user-configurable cache path despite its `MCP_CONSOLE_` prefix.
In host cache mode, set it in the shared or resolver environment before startup.
A nonempty relative value is resolved against the launch directory.
Without it, Console uses `.duckdb/extensions` under the resolver's effective absolute `HOME`, with the Windows `USERPROFILE` fallback.

The selected path is retained for managed extension installation and managed R/Python SQL connections.
Changing it inside a cell does not reconfigure preparation.
Console cache mode replaces it with `D/duckdb/extensions`.
Selecting a path does not itself grant access when an explicit filesystem policy is in use.

## R startup and libraries

Normal R startup is enabled by default (`r.vanilla: false`) and runs inside the worker's sandbox.
R, rather than Console, interprets the standard startup-file variables.

| Variable                                    | Effect                                                                                                                                                                                                                                                                              |
| ------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `R_ENVIRON`                                 | Selects R's site environment file; otherwise R uses its normal site-file location.                                                                                                                                                                                                  |
| `R_ENVIRON_USER`                            | Selects R's user environment file; otherwise R searches for `.Renviron` using its normal current-directory/home rules.                                                                                                                                                              |
| `R_PROFILE`                                 | Selects the site R profile.                                                                                                                                                                                                                                                         |
| `R_PROFILE_USER`                            | Selects the user R profile; otherwise R searches for `.Rprofile` using its normal current-directory/home rules.                                                                                                                                                                     |
| `R_LIBS`                                    | Additional R library paths. Managed R preparation prepends its resolved library to the captured value and supplies the resulting value to the worker. The active `.libPaths()` can subsequently include a worker-temporary library and changes made by native startup or user code. |
| `R_LIBS_USER`, `R_LIBS_SITE`                | Standard R library-path settings, interpreted by R rather than a separate Console parser. Their effect depends on R startup and the installed R version.                                                                                                                            |
| `R_DEFAULT_PACKAGES`                        | Standard R setting for default startup package attachment.                                                                                                                                                                                                                          |
| `R_USER`                                    | On Windows, the embedded R user directory is selected from nonempty `R_USER`, then nonempty `HOME`, then the operating-system home-directory fallback.                                                                                                                              |
| `R_SHARE_DIR`, `R_INCLUDE_DIR`, `R_DOC_DIR` | Console sets these from the selected R installation, along with `R_HOME`. Inherited values for another installation are not retained as overrides.                                                                                                                                  |

`r.vanilla: true` suppresses normal startup files and workspace restoration for workers.
It is not a general clearing of inherited environment variables.
Consult [R's startup documentation](https://stat.ethz.ch/R-manual/R-devel/library/base/html/Startup.html) for native precedence and file semantics.

Discovery and dependency preparation are different: Console uses profile-free R invocations and suppresses `R_ENVIRON`, `R_ENVIRON_USER`, `R_PROFILE`, and `R_PROFILE_USER` for the IR launcher by setting them to `/dev/null` on Unix or `NUL` on Windows.
A mirror, credential, or other setting supplied only by the worker's `.Rprofile` or `.Renviron` therefore does not configure dependency preparation.
Put resolver settings in the launch/shared/resolver environment instead.

Console also supplies these values during managed R preparation:

| Variable                 | Assigned value and purpose                                                                                                           |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| `IR_NO_LOCAL_SOURCES`    | `1`; prevents the IR preparation path from selecting local sources.                                                                  |
| `PKG_SUBPROCESS_TIMEOUT` | `60000` milliseconds; timeout setting supplied to package-preparation subprocess machinery. This is not a timeout for Console cells. |

## Temporary storage, plotting, and native loading

| Variable                              | Built-in worker or launcher behavior                                                                                                                                                                                                                                                                                                                                       |
| ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `TMPDIR`                              | Set to private worker temporary storage, including for built-in workers with `--no-sandbox`. The worker requires it. Console's managed DuckDB connections place spill files and stored secrets below it. The host's temporary-directory settings can still affect allocation of host-side temporary files.                                                                 |
| `TEMP`, `TMP`                         | Also set to private temporary storage by the Windows sandbox lifecycle. Do not assume this Windows sandbox assignment also occurs for a no-sandbox worker.                                                                                                                                                                                                                 |
| `COLUMNS`                             | Set to `200` during built-in worker setup, including with `--no-sandbox`. An inherited value is not retained as the initial worker value.                                                                                                                                                                                                                                  |
| `MPLBACKEND`                          | Defaults to `agg` only when absent. A supplied value, including an empty string, is not replaced by this defaulting step.                                                                                                                                                                                                                                                  |
| `MPLCONFIGDIR`                        | Used as an input to Matplotlib configuration/cache selection, then replaced in the built-in worker with `<TMPDIR>/matplotlib`. Console cache mode has already applied its persistent cache assignment before this worker-private step.                                                                                                                                     |
| `MATPLOTLIBRC`                        | Before worker-private redirection, Console looks for a configuration file at this value, or a `matplotlibrc` inside the named directory. Otherwise it checks the effective Matplotlib configuration directory. A found file is canonicalized and retained in `MATPLOTLIBRC`. Use this variable to select a configuration file independently of redirected cache locations. |
| `XDG_CONFIG_HOME`                     | On Linux, participates in the Matplotlib configuration-directory fallback when no nonempty `MPLCONFIGDIR` is present: `<XDG_CONFIG_HOME>/matplotlib`, otherwise `$HOME/.config/matplotlib`. It does not select Console's `config.yaml`.                                                                                                                                    |
| `XDG_CACHE_HOME`                      | In addition to persistent cache selection, participates in Linux Matplotlib cache lookup before the worker changes it to `<TMPDIR>/cache`. The value observed inside a cell is therefore not the resolver's persistent-cache root.                                                                                                                                         |
| `LD_LIBRARY_PATH`                     | On Linux, Console ensures the selected R installation's `lib` directory is available before loading R, re-executing the worker if needed. Native Python setup also prepends the selected Python prefix's `lib` directory and, when different, the base prefix's `lib` directory. Other existing entries are retained.                                                      |
| `LD_PRELOAD`, `DYLD_INSERT_LIBRARIES` | Removed from the environment used to execute the native sandbox runner. This is launcher handling, not a promise that arbitrary no-sandbox processes or explicitly configured descendants ignore these variables.                                                                                                                                                          |

Before redirecting worker Matplotlib storage, Console can reuse prepared `fontlist-v*.json` cache files.
On Linux, fallback host font-cache lookup uses `XDG_CACHE_HOME` or `$HOME/.cache`; on other platforms the worker fallback uses `$HOME/.matplotlib`.
This does not make the persistent cache writable by the worker.
See [resolver caches](RESOLVER.md#cache-locations).

The worker-private plotting and XDG cache adjustments still occur with `cache: host` and `--no-sandbox`; those options do not disable built-in runtime initialization.

## Networking and other delegated variables

| Variable or family                                                                      | Handling                                                                                                                                                                                                                                                                                                                             |
| --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `HTTP_PROXY`                                                                            | Console's R and Python DuckDB extension-installation programs explicitly read this when `CODEX_NETWORK_PROXY_ACTIVE=1`, then supply it to DuckDB's `http_proxy` setting. This special handling is for extension preparation, not a blanket setting on every user connection.                                                         |
| `CODEX_NETWORK_PROXY_ACTIVE`                                                            | Native-runner marker. Exactly `1` enables the DuckDB preparation behavior above. It is not a user permission switch; setting it without the corresponding proxy environment can break preparation.                                                                                                                                   |
| `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY` and tool-supported lowercase forms | Also interpreted by networking tools and the native runner. Managed proxy configuration can replace the workload's proxy environment. Configure networking through `sandbox.network` or `resolver.sandbox.network`, including explicit upstream-proxy policy, rather than treating inherited proxy variables as sandbox permissions. |
| Certificate, authentication, and package-tool variables                                 | Can reach the effective worker/resolver environment and be interpreted by the relevant dependency. For example, uv documents `SSL_CERT_FILE`, `SSL_CERT_DIR`, and its `UV_*` authentication settings. Console does not define a common parser or guarantee identical support across tools.                                           |

On macOS/Linux, resolver preparation normally uses a managed download proxy with its own permissions.
Windows preparation uses host permissions and does not support that managed resolver proxy.
Worker and resolver network permissions are separate; `UV_OFFLINE=1` in the worker is not evidence that preparation has no network access.

Other native variables can affect execution without appearing in Console's own parser.
For example, CPython processes settings such as `PYTHONHASHSEED`, `PYTHONWARNINGS`, `PYTHONDONTWRITEBYTECODE`, and `PYTHONNOUSERSITE`; R and native libraries have further locale, memory, and package-specific settings.
Consult the [Python environment reference](https://docs.python.org/3/using/cmdline.html#environment-variables) and the relevant library documentation.
Console embeds Python rather than running its ordinary command-line REPL, so command-line-interactive features should not be assumed to apply unchanged; use Console's `startup` setting for explicit session startup code.

This reference does not attempt to enumerate every variable read by every package that user code might import, or by every compiler, installer, or native library those packages invoke.

## Internal variables: leave unset

The following names are implementation details, not supported user configuration.
Console normally supplies, replaces, or consumes them.
An inherited value is not a supported way to enable a capability or change permissions.

| Variable                                                                | Purpose                                                                                                                                                                                                                                                                                                                                                                                                            |
| ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `MCP_CONSOLE_LANGUAGES`                                                 | Internal language/evaluation filter. When present, accepts a comma-separated subset of the exact names `r`, `python`, and `sql`, with no spaces. Unset means all three; empty, incorrectly cased, or malformed values fail parsing. Prefer public `languages:` configuration. That setting controls the public tool schema; this variable also participates in internal bootstrap. Neither is a security boundary. |
| `MCP_CONSOLE_SANDBOX`                                                   | Set to `1` by the sandbox launcher. Runtime integrations inspect it to enable sandbox-specific behavior, including a temporary R package library and Python process-group handling. Changing the marker does not change operating-system permissions.                                                                                                                                                              |
| `MCP_CONSOLE_SANDBOX_SETTINGS`                                          | Serialized Console sandbox settings passed between supervisor and launcher. A value is consumed only through an explicitly selected settings transport, not as an ambient substitute for YAML.                                                                                                                                                                                                                     |
| `MCP_CONSOLE_SANDBOX_CONFIG`                                            | Generated native-runner configuration. Ordinary Console launch replaces an ambient value; it does not use that value to select policy.                                                                                                                                                                                                                                                                             |
| `MCP_CONSOLE_STARTUP_FILE`                                              | Path to a private file containing captured startup configuration. The worker consumes the variable and removes the file before interpreter initialization. Do not set it to a user file.                                                                                                                                                                                                                           |
| `MCP_CONSOLE_LOCAL_RUNTIME`                                             | Serialized, retained R/Python runtime selection. It is not a public configuration format.                                                                                                                                                                                                                                                                                                                          |
| `MCP_CONSOLE_MANAGED_PYTHON`                                            | Serialized managed Python requirements used for worker/reticulate integration. Use the `requirements` tool argument instead.                                                                                                                                                                                                                                                                                       |
| `MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION`                            | Runtime marker for whether the session supports managed environment resolution. Console assigns `1` or `0` at launch. It is not a supported user enable/disable switch.                                                                                                                                                                                                                                            |
| `MCP_CONSOLE_MATPLOTLIB_CACHE`                                          | Prepared font-cache source passed to eligible workers. Normal launch removes an ambient value and supplies the captured source when appropriate. Use public cache configuration rather than setting this marker.                                                                                                                                                                                                   |
| `MCP_CONSOLE_SIDEBAND_READ_FD`, `MCP_CONSOLE_SIDEBAND_WRITE_FD`         | Unix worker-protocol file descriptors. Adopted and removed from the environment before user code runs.                                                                                                                                                                                                                                                                                                             |
| `MCP_CONSOLE_SIDEBAND_READ_HANDLE`, `MCP_CONSOLE_SIDEBAND_WRITE_HANDLE` | Windows worker-protocol handles, with corresponding adoption and environment cleanup.                                                                                                                                                                                                                                                                                                                              |
| `MCP_CONSOLE_INTERRUPT_HANDLE`, `MCP_CONSOLE_INPUT_READY_HANDLE`        | Windows event handles for interruption and managed stdin readiness.                                                                                                                                                                                                                                                                                                                                                |

For custom workers, the transport contract is documented in [Worker protocol](WORKER_PROTOCOL.md).
Low-level `sandbox --config-env NAME` and internal `--settings-env NAME` explicitly select an environment variable containing their respective JSON payloads; the selected name is arbitrary.
Merely defining a similarly named variable does not select that interface.

### Build-time variables are separate

`MCP_CONSOLE_SANDBOX_SOURCE` selects a clean, pinned companion checkout during source installation/staging.
It does not replace the sandbox executable of an already built Console.
Staging also uses its own `XDG_CACHE_HOME`/`HOME` cache convention and toolchain controls; see [release and installation](../RELEASE.md) and [development](DEVELOPMENT.md).

Cargo-supplied values such as `CARGO_CFG_WINDOWS`, `CARGO_CFG_UNIX`, `CARGO_CFG_TARGET_OS`, `CARGO_MANIFEST_DIR`, `OUT_DIR`, `TARGET`, and package metadata are build inputs, not runtime configuration.
Test-harness variables, including `MCP_CONSOLE_TEST_*`, are outside this user reference.

<details>
<summary>Implementation references</summary>

This page was checked against `t-kalinowski/mcp-console` commit `5bb059caaab0b21fe69e771135413620d1234d2d` on October 8, 2026. The linked source files below describe the Console-specific rules; upstream runtime documentation describes delegated behavior.

| Area | Sources |
| --- | --- |
| Configuration and home paths | [`console_paths.rs`](../src/console_paths.rs), [`settings.rs`](../src/settings.rs), [`worker_client/configuration.rs`](../src/worker_client/configuration.rs) |
| Captured launch environment | [`worker_client/process.rs`](../src/worker_client/process.rs), [`resolver/preparation/host.rs`](../src/resolver/preparation/host.rs), [`local_runtime.rs`](../src/local_runtime.rs) |
| Cache locations and grants | [`resolver/cache.rs`](../src/resolver/cache.rs) |
| Python resolver environment | [`resolver/python_configuration.rs`](../src/resolver/python_configuration.rs), [`resolver/managed_python.rs`](../src/resolver/managed_python.rs) |
| R preparation | [`resolver/managed_r.rs`](../src/resolver/managed_r.rs), [`resolver/environment.rs`](../src/resolver/environment.rs) |
| Native Python initialization and activation | [`python/startup.rs`](../src/python/startup.rs), [`python/environment.py`](../src/python/environment.py) |
| Reticulate compatibility | [`python/initialize.R`](../src/python/initialize.R), [`python/reticulate.rs`](../src/python/reticulate.rs), [`python/bridge.R`](../src/python/bridge.R) |
| Worker storage, plots, R loading | [`python.rs`](../src/python.rs), [`worker/coordinator.rs`](../src/worker/coordinator.rs), [`worker/bootstrap.rs`](../src/worker/bootstrap.rs), [`worker/embedded_r/windows.rs`](../src/worker/embedded_r/windows.rs), [`r_environment/bridge.R`](../src/r_environment/bridge.R) |
| DuckDB | [`sql/bridge.R`](../src/sql/bridge.R), [`sql/dbapi.py`](../src/sql/dbapi.py), resolver [`duckdb_extensions.R`](../src/resolver/programs/duckdb_extensions.R) and [`duckdb_extensions.py`](../src/resolver/programs/duckdb_extensions.py) |
| Sandbox and internal transport | [`sandbox.rs`](../src/sandbox.rs), [`sandbox/runner.rs`](../src/sandbox/runner.rs), [`settings/startup.rs`](../src/settings/startup.rs), [`cell.rs`](../src/cell.rs), [worker protocol](WORKER_PROTOCOL.md) |
| Build-only inputs | [`build.rs`](../build.rs), [release documentation](../RELEASE.md) |

</details>
