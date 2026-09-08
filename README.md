# `mcp-console`

# 🚧 UNDER CONSTRUCTION 🚧

**This project is not ready for use.**

`mcp-console` is a ground-up rewrite of [`mcp-repl`](https://github.com/posit-dev/mcp-repl).
It applies the lessons learned from `mcp-repl` to a substantially different product---different enough that a new name makes sense.

MCP Console is being built as a persistent, sandboxed R, Python, and DuckDB SQL console for MCP agents.
It gives an MCP client one live computational workspace instead of a sequence of disposable shell commands.
An agent can submit complete R, Python, or SQL cells, keep state across calls, answer interactive prompts, inspect partial output, and switch languages as a task evolves.

The built-in worker embeds R.
Python runs through reticulate, and SQL uses a persistent DuckDB connection by default while allowing R code to select another DBI connection or Python code to select a DB-API connection.
R and Python can access one another's globals through reticulate, while the managed DuckDB backend can query data frames in the R workspace directly.
R plots made with the default device and open Matplotlib figures are returned as images, SQL results are returned as bounded previews, and long-running work can be polled or interrupted.

## Install

MCP Console is currently distributed as native wheels for Apple Silicon and Intel macOS.
Linux and Windows are not supported yet.

A working R installation is required.
Set `R_HOME` or make `R` discoverable on `PATH`.
Dynamic environment resolution normally starts from either `ir` 0.4.0 or later or `uv` on `PATH`.
The first managed server start may download and install the default R and Python requirements.
If no resolver bootstrap is available, the server starts a bare runtime using installed packages.
See [Requirements and environments](https://github.com/t-kalinowski/mcp-console/blob/main/docs/REQUIREMENTS.md) for bootstrap options, managed defaults, and bare runtime behavior.

Run the published command without installing it persistently:

```sh
uv tool run mcp-console --help
uv tool run mcp-console serve
```

Or install it as a persistent `uv` tool:

```sh
uv tool install mcp-console

mcp-console --help
mcp-console serve
```

`mcp-console serve` communicates with its MCP client over standard input and output.
It waits for MCP protocol input rather than presenting an interactive terminal prompt.

## Working with the console

The MCP interface exposes one tool: `send`.
It runs one complete R, Python, or SQL cell, supplies interactive input, prepares additive requirements, applies an optional interrupt or restart, or collects pending output.

Code-bearing calls to `send` are sequential.
The current interface has one implicit session, with no named-session management.
The [send operation reference](https://github.com/t-kalinowski/mcp-console/blob/main/docs/SEND_OPERATIONS.md) defines validation, preparation, control, input, and timeout ordering.
The [built-in runtime guide](https://github.com/t-kalinowski/mcp-console/blob/main/docs/BUILTIN_RUNTIME.md) covers language state, output, graphics, and interoperability.

## Example workflow

An agent investigating `measurements.csv` could load the data and fit a model in one R cell submitted through `send`:

```r
measurements <- readr::read_csv(
  "measurements.csv",
  show_col_types = FALSE
)

fit <- lm(response ~ temperature + group, data = measurements)
measurements$.residual <- residuals(fit)
```

It could then query the live R data frame with DuckDB SQL:

```sql
SELECT
  "group",
  count(*) AS n,
  avg(abs(".residual")) AS mean_abs_residual
FROM measurements
GROUP BY "group"
ORDER BY mean_abs_residual DESC
```

And inspect or plot the same data from Python:

```python
frame = r.measurements

import matplotlib.pyplot as plt

plt.scatter(frame["temperature"], frame[".residual"])
plt.axhline(0)
```

The data, model, Python imports, and DuckDB catalog remain available for later calls until the worker is restarted, replaced, or the server exits.

The server records tool calls and results in a JSONL journal with image artifacts and a Markdown transcript.
It also produces a Quarto source projection, `transcript.qmd`.
Rejected or failed submissions may appear in that file, and rendering it does not reconstruct session control.
See [Recording and artifacts](https://github.com/t-kalinowski/mcp-console/blob/main/docs/ARCHITECTURE.md#recording-and-image-artifacts) for the file formats and rendering behavior.

## Security boundary

By default, submitted R, Python, and SQL have shell-class capability inside the worker sandbox.
The worker can read host files, but direct network access and regular-file writes outside its private temporary directory are denied.
This is a process boundary, not a safe evaluator for untrusted code with access to sensitive readable files.

`mcp-console serve --no-sandbox` launches the relay directly with host permissions.
The worker inherits the host temporary-directory environment, and no sandbox manager tracks or cleans up descendants.
The relay still shuts down and reaps its direct worker normally.

The server installs automatically inferred or explicitly declared R and Python packages and DuckDB extensions outside the worker sandbox with server permissions.
Those operations may access the network and execute installation or build code, so only trusted requirements should be supplied.
See [Requirements and environments](https://github.com/t-kalinowski/mcp-console/blob/main/docs/REQUIREMENTS.md) for the accepted inputs and trust model.

Session records contain submitted source, standard input, requirements, results, and artifacts without redaction.
Rendering the Quarto source projection executes submitted code outside the worker sandbox with the permissions of the `ir` and Quarto processes.
Render only code you trust.

## Development

Install the current checkout on macOS with `cargo install --path .` or `uv tool install --reinstall .`.
Source builds require Python 3, Git, and rustup in addition to the Rust compiler and native build tools.
The first build fetches and compiles the pinned sandbox runner automatically, using a dedicated checkout under Cargo's target directory.
It installs the pinned Rust toolchain if needed and uses the usual Cargo dependency cache.
It does not use another working checkout.

Run development commands from the repository root:

```text
scripts/format
scripts/check
scripts/test [BOUNDARY/SUITE[::CASE]]
scripts/test --list
scripts/test --update BOUNDARY/SUITE[::CASE]
```

The installed executable embeds the private runner and its license notices; [RELEASE.md](RELEASE.md) describes the build and runtime caches.
See [AGENTS.md](https://github.com/t-kalinowski/mcp-console/blob/main/AGENTS.md) for development rules and the repository map, and the [boundary test guide](https://github.com/t-kalinowski/mcp-console/blob/main/tests/boundaries/README.md) for test selection and snapshot updates.
The standalone `mcp-console sandbox -- COMMAND [ARG]...` command is also available for development; [macOS sandbox supervision](https://github.com/t-kalinowski/mcp-console/blob/main/docs/SANDBOX_SUPERVISION.md) defines its lifecycle, terminal behavior, and limitations.

## Documentation

The [documentation index](https://github.com/t-kalinowski/mcp-console/blob/main/docs/README.md) maps current documents by audience.

- [Implemented architecture](https://github.com/t-kalinowski/mcp-console/blob/main/docs/ARCHITECTURE.md) explains current process boundaries, ownership, lifecycle, recording, and artifacts.
- [macOS sandbox supervision](https://github.com/t-kalinowski/mcp-console/blob/main/docs/SANDBOX_SUPERVISION.md) explains standalone terminal and signal ownership, manager-owned observed-descendant retirement, fallback cleanup, and remaining tracking limitations.
- [Built-in runtime](https://github.com/t-kalinowski/mcp-console/blob/main/docs/BUILTIN_RUNTIME.md) describes user-visible R, Python, SQL, input, output, and graphics behavior.
- [Send operations](https://github.com/t-kalinowski/mcp-console/blob/main/docs/SEND_OPERATIONS.md) defines validation and execution order for each `send` combination.
- [Requirements and environments](https://github.com/t-kalinowski/mcp-console/blob/main/docs/REQUIREMENTS.md) describes dependency preparation and its trust boundary.
- [Worker protocol](https://github.com/t-kalinowski/mcp-console/blob/main/docs/WORKER_PROTOCOL.md) and [relay protocol](https://github.com/t-kalinowski/mcp-console/blob/main/docs/RELAY_PROTOCOL.md) define the exact transport contracts.
  [Tool description guidance](https://github.com/t-kalinowski/mcp-console/blob/main/docs/TOOL_DESCRIPTIONS.md) covers editorial rules; the [canonical handshake snapshot](https://github.com/t-kalinowski/mcp-console/blob/main/tests/snapshots/client_server/server/test_tools/initializes_and_lists_tools.yaml) records the registered wording.
- [Boundary test guide](https://github.com/t-kalinowski/mcp-console/blob/main/tests/boundaries/README.md) explains selectors, normalization, and snapshot updates.
- The [release guide](https://github.com/t-kalinowski/mcp-console/blob/main/RELEASE.md) describes PyPI setup, publication, verification, and recovery.

The [project vision](https://github.com/t-kalinowski/mcp-console/blob/main/design-sketches/VISION.md) and other documents under [`design-sketches/`](https://github.com/t-kalinowski/mcp-console/blob/main/design-sketches/README.md) describe intended or exploratory future design, not the implemented system.
When current prose and implementation disagree, source and public acceptance tests are authoritative.

## License

MCP Console is licensed under the [MIT license](https://github.com/t-kalinowski/mcp-console/blob/main/LICENSE).
