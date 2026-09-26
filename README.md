# MCP Console

MCP Console is an interactive, persistent computational workspace for agents.
One MCP tool, `send`, provides R, Python, and SQL cells, plotting and image output, interactive input, dependency preparation, polling, interruption, and restart.
Data, models, imports, and database state survive between calls, so an analysis can move between languages without starting over.

R, Python, and DuckDB are embedded in a single worker process.
Python can access R variables through `r.name`, R can access Python objects through `py$name`, and SQL can query live R data frames.
Data passes through in-process object bridges and conversions; NumPy can view R numeric arrays directly in memory.
An agent can choose the language and libraries that fit each step without managing data transfers between separate runtime sessions.

Model-visible text is bounded to 8 KiB per response, with separate image limits.
The server keeps recordings, plot artifacts, and raw cell output outside the model context; raw text retention is capped at 1 GiB per cell.
A separate runtime process executes cells, with native sandboxing enabled by default and explicit ownership of startup, interruption, and cleanup.

## Status

This is a **development preview** with changing interfaces.
MCP Console supports macOS and Linux; Windows is unsupported.
See the [runtime limitations](docs/BUILTIN_RUNTIME.md#current-limitations) and [sandbox lifetime limits](docs/SANDBOX.md#supported-hosts-and-lifetime-limits).

Local sessions can run Python without R.
When R is absent, Console uses `uv` to resolve its default Python environment, or uses `python3` then `python` from `PATH` when `uv` is absent.
These sessions support Python execution, input, plots, interrupts, restart, and recording; requirement changes, automatic package installation, and SQL are unavailable.
With R installed, the worker retains mixed R/Python execution through reticulate and a persistent DuckDB connection for SQL.
See [Python sessions without R](docs/BUILTIN_RUNTIME.md#python-sessions-without-r) for selection and package limitations.

## Quickstart

Use an MCP client of your choice, such as [Codex](https://developers.openai.com/codex/mcp), [Claude Code](https://code.claude.com/docs/en/mcp), or [OpenCode](https://opencode.ai/docs/mcp-servers/).

Use [uv](https://docs.astral.sh/uv/getting-started/installation/) for installation and the default local Python environment.
If you need R, install [rig](https://github.com/r-lib/rig#id-installation), then run `rig add release`.

Installing the current source also needs Git, [rustup](https://rustup.rs/) with Rust 1.95 or later, and your platform's build tools.
On macOS, install the Xcode Command Line Tools with `xcode-select --install`.
On Ubuntu, install the build dependencies with:

```sh
sudo apt-get update
sudo apt-get install -y build-essential git pkg-config libcap-dev libcurl4-openssl-dev binutils
```

Install the current checkout, including its private sandbox runner:

```sh
git clone --depth 1 https://github.com/t-kalinowski/mcp-console.git
cd mcp-console
uv tool install --python 3.12 --reinstall .
```

Configure your client to launch `uvx mcp-console serve` as a stdio server.
For example, with Codex:

```sh
codex mcp add console -- uvx mcp-console serve
codex
```

Or with Claude Code:

```sh
claude mcp add --transport stdio console -- uvx mcp-console serve
claude
```

uv supplies Python 3.12, the first installation builds the pinned runner with its own Rust toolchain, and the first analysis prepares R and Python packages and DuckDB extensions.
These steps can download interpreters, packages, and build dependencies and take several minutes.
See [source installation](RELEASE.md#private-sandbox-executable) and [managed dependencies](docs/REQUIREMENTS.md#retained-environments) for details.

## Try an analysis

Check that your client exposes the console's `send` tool (`/mcp` in Codex), then ask:

> Use MCP Console to tell me something interesting about the Palmer Penguins dataset.
> Load the data from the R package `palmerpenguins`, letting the console prepare any missing packages.
> Fit a small logistic regression in R to predict penguin sex from body measurements.
> Use SQL to summarize the live data by species, then use Python and Matplotlib to plot the data and the model's predictions.
> Explain what you found, keeping the data and model in the console for follow-up questions.

Follow up in the same conversation:

> Using the model and data already in the console, where does the model make the most mistakes?
> Show me a plot and the path to the recorded console session transcript.

Records and plot artifacts are written under `.agents/console/sessions/<run-id>/` in the server's working directory.
Your client uses its configured model; the exact calls and responses can vary.

## Reproducible reports

Each session produces a `transcript.md` with recorded calls and results, and a `transcript.qmd` containing the code as a Quarto document.
Console automatically keeps the QMD front matter up to date with declared R and Python dependencies, including packages resolved dynamically during the session.
Rendering with `ir` prepares those dependencies and reruns the code in a fresh R session, capturing new results and plots in HTML or another Quarto output format.
This gives you a starting point for a reproducible report: copy the document to refine the analysis and add narrative.
See the [recording and rendering guide](docs/ARCHITECTURE.md#recording-cell-output-and-image-artifacts) for commands and setup for SQL or remote sessions.

## Architecture

The [process diagram and ownership guide](docs/ARCHITECTURE.md#process-layout) explain the boundaries:

- The **server** owns the session, operation admission, retained requirements, bounded responses, host dependency preparation, and recordings.
- The **relay** transports worker events, delivers signals, and shuts down and reaps its direct worker.
- The **runtime worker** owns live R, Python, and SQL state and evaluates one cell at a time.
- The **private sandbox runner** owns native enforcement, private temporary storage, and descendant supervision within its [documented limits](docs/SANDBOX.md#supported-hosts-and-lifetime-limits).

Restart discards in-memory language and database state while retaining selected requirements in the server.
Use `send(requirements={"action": "get"})` to inspect them.
Add is the default; `set` replaces the complete declaration, including with no optional packages, and `reset` restores startup defaults.
Changed replacements of a live worker require `control="restart"`; see [requirements management](docs/REQUIREMENTS.md#inspecting-and-replacing-requirements).
Recordings remain files; they are not session checkpoints.
The architecture separates host setup and recording from evaluated code, while the shared worker enables interoperation and means a restart affects all three languages.

[SSH](docs/SSH.md) selects remote transport and execution while retaining local recordings.
[Docker](docs/DOCKER.md) adds an owned container; [Docker Sandbox/SBX](docs/DOCKER_SANDBOX.md) uses provider-managed microVM enforcement instead of the native runner.
These targets have distinct prerequisites, policies, and cleanup contracts.

## Limits and trust boundaries

Submitted code has shell-class capability.
The default local native sandbox permits host-file reads, restricts direct networking, and denies regular-file writes outside private temporary storage.
It does not protect sensitive files that the worker can read.
Trusted [project configuration](docs/SANDBOX_CONFIGURATION.md#project-configuration) can change this policy; there is no automatic unsandboxed fallback.
Linux requires mounted `/proc` and permission for the native sandbox's namespace and policy operations; see [host requirements](docs/LINUX_COMPATIBILITY.md).
Restricted containers or host security policy may prevent startup.

Dependency preparation runs **outside the worker sandbox** and may execute trusted installation, build, or initialization code with host permissions.
Use only trusted requirements and resolver configuration.
See the [dependency trust boundary](docs/REQUIREMENTS.md#host-resolution-and-trust).

There is one implicit session and cells run sequentially.
Restart, worker replacement, and server exit discard live state.
Recordings contain source, stdin, requirements, outputs, and artifacts without redaction, and have no aggregate retention quota or automatic cleanup.
Retrieving omitted output requires filesystem access to the server's recording directory.

The generated Quarto document can include failed or rejected submissions.
Review a copy before rendering; it executes code outside the worker sandbox.

## Further reading

- [Documentation index](docs/README.md), [runtime behavior](docs/BUILTIN_RUNTIME.md), and [`send` operation order](docs/SEND_OPERATIONS.md).
- [Python clients and integrations](docs/PYTHON.md) and the [ellmer R package](r/README.md).
- [Configuration](docs/CONFIGURATION.md), [native sandbox policy](docs/SANDBOX_CONFIGURATION.md), and [dependency preparation](docs/REQUIREMENTS.md).
- [Development rules and source map](AGENTS.md), [testing and snapshot updates](tests/boundaries/README.md), and [build, packaging, and release procedures](RELEASE.md).

MCP Console grew out of [`mcp-repl`](https://github.com/posit-dev/mcp-repl).
Documents under [`design-sketches/`](design-sketches/README.md) describe exploratory future work, not current functionality.
Licensed under the [MIT license](LICENSE).
