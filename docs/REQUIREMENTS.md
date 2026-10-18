# Packages and environments

By default, Console prepares environments and package libraries for your agent separately from those you use for other work.
No configuration file is required.

- **Python:** Console prepares its own Python environment, downloading a suitable interpreter when needed.
  Packages stay separate from your system Python and existing virtual environments.
- **R:** Console uses your installed R with separate package libraries.
  These are added to the Console session without changing the library paths of your other R sessions.
- **DuckDB:** Console prepares the SQL engine through its R or Python package, along with the configured extensions.

Prepared environments, packages, and extensions are kept in [Console's own caches](RESOLVER.md#cache-locations) for reuse across sessions.

The agent's code runs in an [OS-level sandbox](SANDBOX.md) by default, with restricted filesystem writes and direct network access.
Package preparation has [separate permissions](#host-resolution-and-trust), including its own sandbox on macOS and Linux.

## Add packages as needed {#add-packages-during-an-analysis}

The agent can add compatible packages as an analysis develops without restarting the session or losing existing objects.
For Python, Console prepares and activates an updated virtual environment in the running interpreter.
For R, it updates the session's library paths.
Adding DuckDB extensions preserves the SQL connection and tables.

With explicit preparation, the agent declares the R packages, Python packages, or DuckDB extensions it needs in a tool call's `requirements` argument.
For example, the agent can request `seaborn` alongside the code that imports it:

```javascript
console.send({
  requirements: { python: ["seaborn"] },
  python: "import seaborn as sns",
});
```

Console prepares the requested packages or extensions before running the accompanying code.
The agent can also make a request on its own.

With implicit preparation, a call such as `library(palmerpenguins)` in R or `import seaborn` in Python can trigger preparation when a package is missing.
Console resolves the package and continues the load or import without rerunning the cell.

R defaults to `implicit`, which also allows explicit package requests.
Managed Python defaults to `explicit`; you can [choose `implicit`](#resolution-policy-admission) to enable automatic preparation on import.

Switching Python versions or changing package versions already in use can require a restart, which clears live objects.
See [Requirements API](REQUIREMENTS_API.md) for request details.

## Packages available at startup {#defaults-and-startup-configuration}

Console prepares these packages and extensions at startup:

| Runtime | Packages or extensions                                                                                    |
| ------- | --------------------------------------------------------------------------------------------------------- |
| R       | `tidyverse`, `dplyr`, `dbplyr`, `reticulate`, `DBI`, `duckdb`, `arrow`, `nanoarrow`, `yyjsonr`, `ggplot2` |
| Python  | `numpy`, `pandas`, `matplotlib`, `plotnine`; also `duckdb` without R                                      |
| DuckDB  | `icu`, `json`, `sqlite`, when the managed provider is available                                           |

Preparation makes packages available; the agent still uses ordinary imports, R package-loading calls, or database extension-loading commands in its code.
Extensions already built into the selected DuckDB do not need downloading.

## Choose startup packages

You can replace the default startup package lists in `config.yaml`:

```yaml
r:
  packages: [dplyr, ggplot2]
python:
  managed:
    packages: [numpy, pandas]
```

An omitted list keeps the defaults.
An empty list (`[]`) removes the additional startup requests; it does not remove Console's required runtime packages, uninstall packages, or delete cached installations.

[Configuration](CONFIGURATION.md#startup-package-declarations) covers interpreter versions and startup settings.
[Package preparation and caches](RESOLVER.md) covers download permissions and storage locations.

## Choose when packages may be prepared {#resolution-policy-admission}

R and managed Python each have a resolution policy.
The three policies allow progressively more package preparation: `fixed` → `explicit` → `implicit`.

### `fixed`: prepare packages at startup

Console prepares the startup packages and keeps that package declaration fixed for the connection.
The agent can use available packages, but cannot ask Console to add packages or change their versions during the session.

### `explicit`: also accept package requests

In addition to preparing startup packages, Console accepts explicit package requests from the agent during the session.
For example, the agent can request `seaborn` before running code that imports it.
The request can accompany the code that uses the package, so a separate call is not required.

Ordinary R package-loading calls and Python imports use available packages; they do not trigger preparation on their own.

### `implicit`: also prepare packages on load or import

This adds automatic preparation to everything `explicit` allows.
When the agent's code tries to load a missing package, that operation can trigger Console to resolve and prepare it:

- In R, an ordinary call such as `library(palmerpenguins)` or `loadNamespace("palmerpenguins")` can prepare the missing package and continue loading it.
- In Python, an ordinary import such as `import seaborn` can prepare the missing package and continue importing it.

Preparation happens when execution reaches the load or import.
The agent does not need to make a separate package request, and Console does not rerun the whole cell.
Explicit package requests remain available when the agent needs to specify a version or other requirements.

### Set a policy

R defaults to `implicit`; managed Python defaults to `explicit`.
You can choose a policy for each in `config.yaml`:

```yaml
r:
  packages: [dplyr]
  resolution: explicit
python:
  managed:
    packages: [numpy, pandas]
    resolution: fixed
```

R's `disabled` policy turns off all managed R package preparation, including Console's R infrastructure.
For preinstalled-only Python, select an [existing environment](#use-an-existing-python-environment) rather than a managed one.

Changing a policy requires a new server connection.
These policies control Console's package preparation, not arbitrary installation commands in code or network permissions.
DuckDB extension preparation follows the policy of its managed provider.

## Use an existing Python environment

To use a project's environment:

```yaml
python: .venv
```

Console uses the packages already installed there and does not manage Python package installation for that selection.
Standard-library Python code needs no additional packages.
Install third-party packages for the analysis you plan to run: for example, pandas for pandas operations, Matplotlib for pyplot plots, or DuckDB for the default Python-owned SQL connection.

A user-selected SQLite connection can provide SQL using Python's standard library.
See [database connections](BUILTIN_RUNTIME.md#select-a-native-connection).

The interpreter must still meet Console's [existing-environment requirements](CONFIGURATION.md#python-environment-selection), including the CPython version and shared embedding library.
Selecting an existing environment changes package management; it does not grant extra filesystem or network permissions.

## Package sources and permissions {#host-resolution-and-trust}

On macOS and Linux, Console resolves and prepares packages in a separate sandbox by default.
It can read files, write to dependency caches and temporary directories, and download from allowed package and tooling hosts.
These permissions are separate from those of the agent's code.

Default package sources include PyPI for Python and CRAN through Posit Package Manager for R.
DuckDB extensions come from its core extension repository.

You can use other sources, such as a private Posit Package Manager instance or a company PyPI mirror, through the package managers' native settings.
See [Configure package sources](RESOLVER.md#configuration) for repository settings and download permissions.

Windows preparation and `--no-sandbox` use host permissions.
[Sandbox and trust](SANDBOX.md) describes the platform-specific limits.

## Save an analysis {#saving-an-r-script}

Accepted package requests persist across a session restart, not across new server connections.
They are not a lockfile or an inventory of every installed package.

A saved script must declare its own packages and inputs; it does not inherit live objects or the session's package declaration.
An R script can use [ir frontmatter](https://r-lib.github.io/ir/run.html) for a separate `ir run script.R` invocation.

A recorded QMD is a starting point for a report, not automatic reproducible replay.
See [Render a reviewed copy](RECORDING.md#render-a-reviewed-copy).

<span id="inspecting-and-replacing-requirements"></span> <span id="accepted-requirement-input"></span> <span id="requirements-for-a-cell"></span> <span id="automatic-r-package-resolution"></span> <span id="automatic-python-import-resolution"></span> <span id="live-preparation"></span> <span id="live-r-preparation"></span> <span id="live-python-preparation"></span> <span id="duckdb-extension-preparation"></span> <span id="restarting-with-requirements"></span>

For requirement actions, accepted input, and live-preparation semantics, see [Requirements API](REQUIREMENTS_API.md).

<span id="custom-workers"></span>

Custom workers follow the [worker conformance contract](WORKER_PROTOCOL.md#custom-worker-conformance).
