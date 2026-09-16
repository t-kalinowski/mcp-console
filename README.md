# MCP Console

MCP Console is an interactive, persistent computational workspace for agents.
One MCP tool, `send`, provides R, Python, and SQL cells, interactive input, dependency preparation, polling, interruption, and restart.
Data, models, imports, and database state survive between calls, so an analysis can move between languages without starting over.

Model-visible text is bounded to 8 KiB per response, with separate image limits.
The server keeps recordings, plot artifacts, and raw cell output outside the model context; raw text retention is capped at 1 GiB per cell.
A separate runtime process executes cells, with native sandboxing enabled by default and explicit ownership of startup, interruption, and cleanup.

## Status

This is a **development preview** with changing interfaces.
The walkthrough below exercises the current checkout through an installed Python client and the default sandbox; it does not establish readiness for arbitrary workloads.
It has been tested on Apple Silicon macOS.
MCP Console supports macOS and Linux; Windows is unsupported.
See the [runtime limitations](docs/BUILTIN_RUNTIME.md#current-limitations) and [sandbox lifetime limits](docs/SANDBOX.md#supported-hosts-and-lifetime-limits).

The built-in worker requires **R even for Python and SQL**.
It embeds R, uses reticulate for Python interoperability, and provides a persistent DuckDB connection for SQL.
Python-only execution is not implemented.

## Quickstart

This deterministic walkthrough needs no model API key, chat application, or external dataset.
It installs **the current source checkout**, including its private sandbox runner.
The published PyPI 0.0.3 wheels predate the Python client used here; installing `mcp-console[client]` from PyPI is not a substitute.

Prerequisites:

- R on `PATH` or selected by `R_HOME` (tested with R 4.6.1).
- `uv`, Git, rustup, and Rust 1.95 or later; the commands select Python 3.12 (`uv` can download it).
- Native build tools: Xcode Command Line Tools on macOS; a C compiler, `pkg-config`, libcap development files, and binutils on Linux.
- On Linux, mounted `/proc` and permission for the native sandbox's namespace and policy operations; see [host requirements](docs/LINUX_COMPATIBILITY.md).
  Restricted containers or host security policy may prevent startup.

Run these commands in a terminal:

```sh
cd "$(mktemp -d)"
git clone --depth 1 https://github.com/t-kalinowski/mcp-console.git
uv venv --python 3.12
uv pip install "./mcp-console[client]"
mkdir workspace
cd workspace
../.venv/bin/python ../mcp-console/examples/persistent-analysis.py
```

The first installation fetches and compiles the pinned runner with its own Rust toolchain.
The first cell prepares the default R and Python packages and DuckDB extensions; the plot also prepares Matplotlib.
These steps may download interpreters, packages, and build dependencies and can take several minutes.
No dataset is downloaded.
See [source installation](RELEASE.md#private-sandbox-executable) and [managed dependencies](docs/REQUIREMENTS.md#retained-environments) for details.

Success includes `Total profit: 280`, a SQL table with store profit 120 and web profit 160, `Best channel: web ($160 profit)`, and `Profit gap: $40`.
The script then prints `Session closed.` and the paths to the real Markdown transcript and PNG.
The simple Python client prints `[image/png output]`; open the printed PNG path to view the plot.

Records are under `workspace/.agents/console/sessions/<run-id>/`, relative to the temporary directory created above.
Keep that directory if you want to inspect the results later.
The script polls unfinished work and has a ten-minute deadline; its context manager closes the connection on completion or error.
A timeout from an individual `send` limits waiting, not execution.

## One workspace, several languages

Read [the complete example](examples/persistent-analysis.py): it uses one live connection for four cells.

1. R creates six synthetic orders, computes each order's profit, and totals it.
2. SQL groups the live R data frame by sales channel.
3. Python accesses `r.orders`, prints the best channel, and creates a bar plot.
4. A follow-up Python cell compares the previously computed totals without recreating the data.

For a model-driven version, connect an existing [supported Python integration](docs/PYTHON.md#chatlas) and ask:

> Use the synthetic orders in `examples/persistent-analysis.py`.
> Compute profit in R, query the live data by channel with SQL, and plot it with Python.
> Then compare the channels using the retained state and report the transcript and plot paths.

That optional run needs your model provider's credentials and may incur charges.
The scripted walkthrough above makes no model calls.

## Architecture

The [process diagram and ownership guide](docs/ARCHITECTURE.md#process-layout) explain the boundaries:

- The **server** owns the session, operation admission, retained requirements, bounded responses, host dependency preparation, and recordings.
- The **relay** transports worker events, delivers signals, and shuts down and reaps its direct worker.
- The **runtime worker** owns live R, Python, and SQL state and evaluates one cell at a time.
- The **private sandbox runner** owns native enforcement, private temporary storage, and descendant supervision within its [documented limits](docs/SANDBOX.md#supported-hosts-and-lifetime-limits).

Restart discards in-memory language and database state while retaining prepared requirements in the server.
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

Dependency preparation runs **outside the worker sandbox** and may execute trusted installation, build, or initialization code with host permissions.
Use only trusted requirements and resolver configuration.
See the [dependency trust boundary](docs/REQUIREMENTS.md#host-resolution-and-trust).

There is one implicit session and cells run sequentially.
Restart, worker replacement, and server exit discard live state.
Recordings contain source, stdin, requirements, outputs, and artifacts without redaction, and have no aggregate retention quota or automatic cleanup.
Retrieving omitted output requires filesystem access to the server's recording directory.

`transcript.md` shows recorded calls and results.
`transcript.qmd` is a source projection that can include failed or rejected submissions; it is neither an exact replay nor a checkpoint.
Rendering a local projection executes code outside the worker sandbox and does not reconstruct session control, input, or the original artifacts.
See [recording and rendering](docs/ARCHITECTURE.md#recording-cell-output-and-image-artifacts) before rendering it.

## Further reading

- [Documentation index](docs/README.md), [runtime behavior](docs/BUILTIN_RUNTIME.md), and [`send` operation order](docs/SEND_OPERATIONS.md).
- [Python clients and integrations](docs/PYTHON.md) and the [ellmer R package](r/README.md).
- [Configuration](docs/CONFIGURATION.md), [native sandbox policy](docs/SANDBOX_CONFIGURATION.md), and [dependency preparation](docs/REQUIREMENTS.md).
- [Development rules and source map](AGENTS.md), [testing and snapshot updates](tests/boundaries/README.md), and [build, packaging, and release procedures](RELEASE.md).

MCP Console grew out of [`mcp-repl`](https://github.com/posit-dev/mcp-repl).
Documents under [`design-sketches/`](design-sketches/README.md) describe exploratory future work, not current functionality.
Licensed under the [MIT license](LICENSE).
