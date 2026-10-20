# Packages and environments

In the [first session](getting-started.qmd), your agent could use Python libraries without you creating an environment or installing each package.
By default, Console prepares environments and package libraries for the agent separately from those you use for other work.

For Python, Console prepares its own environment and downloads a suitable interpreter when needed.
For R, it uses your installed R with separate package libraries, without changing the library paths of your other R sessions.
It also prepares DuckDB through its R or Python package to provide the default SQL connection.

Console keeps prepared packages, environments, and extensions in [its own caches](RESOLVER.md#cache-locations) for reuse across sessions.
Starting another session can reuse those installations without resuming the previous session's live objects.
No configuration file is required.

## Add packages as needed {#add-packages-during-an-analysis}

As an analysis develops, the agent may need another library.
For example, you could ask it to use seaborn to redraw the dice simulation's plot.
The agent can request compatible packages without restarting the session or losing existing objects.

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

Adding a package can preserve the work in memory, but switching Python versions or changing package versions already in use can require a restart.
The [Requirements API](REQUIREMENTS_API.md) describes those boundaries and the exact request fields.

## Packages available at startup {#defaults-and-startup-configuration}

Console prepares a starter set of packages, including NumPy, pandas, and Matplotlib for Python and tidyverse packages for R.
Preparation makes them available; the agent still imports or loads the packages it uses.
If an optional starter package cannot be prepared, Console reports the failure and continues with the other defaults.

You can [declare packages at startup](RUNTIME_CONFIGURATION.md#startup-package-declarations) when a project needs a particular set.
Packages you explicitly declare are required, so a failure to prepare one prevents Console from proceeding with an incomplete environment.
The reference lists the [default packages and extensions](RUNTIME_CONFIGURATION.md#default-startup-packages) and explains how to replace them.

## Choose when packages may be prepared {#resolution-policy-admission}

The defaults let the agent add packages as it works.
For a session with a prepared set of dependencies, you can instead keep the startup declaration fixed.
Or you can let missing R loads and Python imports trigger preparation automatically.
Each language can have its own policy.

[Resolution policies](RUNTIME_CONFIGURATION.md#dependency-resolution-policies) documents `fixed`, `explicit`, and `implicit`, with configuration examples.
These policies control Console's package preparation; they do not grant network access or restrict arbitrary installation commands in submitted code.

## Use an existing Python environment

By default, Console prepares and manages its own Python environment.
You can instead use an environment you manage yourself, such as a project's virtual environment or a separate Python installation:

```yaml
python: .venv
```

Console uses the selected environment as supplied, without resolving or installing Python packages.
Install the packages you want your agent to use in that environment, such as pandas for data frames, Matplotlib for plotting, and DuckDB for the default SQL connection.
The session's filesystem and network permissions still apply.
[Interpreters and startup](RUNTIME_CONFIGURATION.md#python-environment-selection) covers compatible installations and environment selection.

## Package sources and permissions {#host-resolution-and-trust}

Console resolves and prepares packages in a separate native sandbox by default.
The resolver sandbox limits writes to dependency caches and temporary directories and restricts network access to allowed package and tooling hosts.
These permissions are separate from those of the agent's code.

Default package sources include PyPI for Python and CRAN through Posit Package Manager for R.
DuckDB extensions come from its core extension repository.

You can use other sources, such as a private Posit Package Manager instance or a company PyPI mirror, through the package managers' native settings.
See [Configure package sources](RESOLVER.md#configuration) for repository settings and download permissions.

Installing a package does not give the agent's code those download or cache permissions.

## Recordings {#saving-an-r-script}

Package requirements are included in the session's [recordings](RECORDING.md), alongside code, output, and plots.

<span id="inspecting-and-replacing-requirements"></span> <span id="accepted-requirement-input"></span> <span id="requirements-for-a-cell"></span> <span id="automatic-r-package-resolution"></span> <span id="automatic-python-import-resolution"></span> <span id="live-preparation"></span> <span id="live-r-preparation"></span> <span id="live-python-preparation"></span> <span id="duckdb-extension-preparation"></span> <span id="restarting-with-requirements"></span>

<span id="custom-workers"></span>

Continue with [Sandbox and trust](SANDBOX.md) to understand what the session and package preparation can access.
