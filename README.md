# MCP Console

MCP Console is an interactive, persistent computational workspace for agents.
One MCP tool, `send`, provides R, Python, and SQL cells, interactive input, dependency preparation, polling, interruption, and restart.
Data, models, imports, and database state survive between calls, so an analysis can move between languages without starting over.

Model-visible text is bounded to 8 KiB per response, with separate image limits.
The server keeps recordings, plot artifacts, and raw cell output outside the model context; raw text retention is capped at 1 GiB per cell.
A separate runtime process executes cells, with native sandboxing enabled by default and explicit ownership of startup, interruption, and cleanup.

## Status

This is a **development preview** with changing interfaces.
The scripted installation check exercises the current checkout and default sandbox on Apple Silicon macOS.
That check does not establish readiness for arbitrary workloads.
MCP Console supports macOS and Linux; Windows is unsupported.
See the [runtime limitations](docs/BUILTIN_RUNTIME.md#current-limitations) and [sandbox lifetime limits](docs/SANDBOX.md#supported-hosts-and-lifetime-limits).

The built-in worker requires **R even for Python and SQL**.
It embeds R, uses reticulate for Python interoperability, and provides a persistent DuckDB connection for SQL.
Python-only execution is not implemented.

## Quickstart

Connect MCP Console to Codex and try an analysis that keeps its state across R, SQL, and Python.
These commands install **the current source checkout**, including its private sandbox runner.

Prerequisites:

- A working, signed-in Codex CLI for the agent walkthrough.
- R on `PATH` (tested with R 4.6.1).
- `uv`, Git, rustup, and Rust 1.95 or later; the commands select Python 3.12 (`uv` can download it).
- Native build tools: Xcode Command Line Tools on macOS; a C compiler, `pkg-config`, libcap and libcurl development headers, and binutils on Linux.
- On Linux, mounted `/proc` and permission for the native sandbox's namespace and policy operations; see [host requirements](docs/LINUX_COMPATIBILITY.md).
  Restricted containers or host security policy may prevent startup.

Install the checkout and register it with Codex:

```sh
git clone --depth 1 https://github.com/t-kalinowski/mcp-console.git
cd mcp-console
uv tool install --python 3.12 --reinstall .
codex mcp add console -- uvx mcp-console serve
codex
```

The first installation builds the pinned runner with its own Rust toolchain; the first analysis prepares R and Python packages and DuckDB extensions.
These steps can download interpreters, packages, and build dependencies and take several minutes.
See [source installation](RELEASE.md#private-sandbox-executable), [managed dependencies](docs/REQUIREMENTS.md#retained-environments), and [Codex MCP configuration](https://developers.openai.com/codex/mcp) for details.

## Try it with Codex

In Codex, use `/mcp` to check that `console` exposes `send`, then ask:

> Use MCP Console for this analysis.
> Use `timeout_ms=10000` for cells and polls; wait for each cell to finish before continuing.
> In R, create six orders: web revenues 120, 150, 180 with costs 80, 90, 120; store revenues 100, 140, 160 with costs 70, 100, 110.
> Compute each order's profit and the total profit.
> Query the live R data frame from SQL to aggregate profit by channel.
> Then access that data from Python, retain the channel totals, and plot them.

Follow up in the same conversation:

> Using the channel totals already in the console, calculate the profit gap in another Python call.
> Show the plot and the path to the recorded transcript.

The expected total profit is 280: store contributes 120 and web contributes 160, a gap of 40.
No external dataset is needed.
Records and plot artifacts are written under `.agents/console/sessions/<run-id>/` in the directory where you start Codex.
This is a model-driven workflow using your Codex account; the exact calls and responses can vary.

### Scripted installation check

To run the same analysis without a model, quit Codex and run this from the repository root:

```sh
uv tool run --python 3.12 --from ".[client]" python examples/persistent-analysis.py
```

The [complete example](examples/persistent-analysis.py) uses one connection for four cells, polls unfinished work, and closes the session on completion or error.
It prints the results above, `Session closed.`, and paths to the transcript and PNG; the simple Python client represents the image as `[image/png output]`.
See the [scripted check and Python integrations](docs/PYTHON.md#scripted-installation-check) for installation details and other clients.
The published PyPI 0.0.3 wheels predate this Python client; the check uses the current checkout.

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
