# `mcp-console`

# 🚧 UNDER CONSTRUCTION 🚧

**This project is not ready for use.**

`mcp-console` is a ground-up rewrite of [`mcp-repl`](https://github.com/posit-dev/mcp-repl).
It applies the lessons learned from `mcp-repl` to a substantially different product---different enough that a new name makes sense.

MCP Console is being built as a persistent, sandboxed R, Python, and DuckDB SQL console for MCP agents.
It gives an MCP client one live computational workspace instead of a sequence of disposable shell commands.
An agent can submit complete R, Python, or SQL cells, keep state across calls, answer interactive prompts, inspect partial output, and switch languages as a task evolves.

The built-in worker embeds R.
Python runs through reticulate, and SQL runs through a persistent DuckDB connection.
R and Python can access one another's globals through reticulate, while DuckDB can query data frames in the R workspace directly.
R plots made with the default device and open Matplotlib figures are returned as images, SQL results are returned as bounded previews, and long-running work can be polled or interrupted.

## What it is useful for

MCP Console is intended for iterative computational work: inspecting and transforming data, fitting models, running simulations, making plots, debugging code, and checking exact results.

An agent can load data once, build useful objects, inspect an intermediate result in another language, and continue without reconstructing its environment or passing every intermediate value through files and model context.

## Working with the console

The MCP interface has two tools:

- `send` runs one complete R, Python, or SQL cell, can prepare additive requirements needed by that cell, supplies interactive input, or collects pending output.
- `session` stages R or Python packages and DuckDB extensions ahead of time, interrupts work, or restarts the runtime.

Calls to `send` are sequential.
R and Python global state and the in-memory DuckDB catalog remain available until the worker is restarted, replaced after failure, or the server exits.
Prepared requirements remain available across worker restarts, but in-memory language, database, debugger, and unread-input state does not.
Requirements declared on `send` are prepared before its cell runs and remain available to later cells.
Preparation makes packages and extensions available; it does not import, attach, or load them.

## Example workflow

An agent investigating `measurements.csv` could first prepare Matplotlib through `session`:

```json
{
  "action": "prepare",
  "requirements": {
    "python": ["matplotlib"]
  }
}
```

Each remaining block is one complete cell submitted through `send`.
The agent could load the data and fit a model in R:

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

The data, model, Python imports, and DuckDB catalog remain available for later calls until the runtime is restarted or replaced.

## Current status

The repository contains a working Rust MCP server, sandboxed worker relay, built-in mixed-language worker, host dependency resolvers, session recording, and public process-boundary transcript tests.
The registered MCP surface is intentionally limited to `send` and `session`.

The core console currently runs only on macOS.
Linux and Windows support is not implemented, the package is not published, and this repository does not yet document an installation route.
For now, the repository is mainly useful to readers following the design and implementation.

The server already records a JSONL journal of tool calls and results together with image artifacts.
The generated Quarto transcript and human-facing tools for following and inspecting an agent's work remain future design.

Other current limitations include:

- there is one implicit session and no named-session management;
- cells run sequentially, and concurrent `send` calls are unsupported; and
- restart and worker replacement discard R, Python, SQL, debugger, and unread-input state.

## Security boundary

Submitted R, Python, and SQL have shell-class capability inside the worker sandbox.
The worker can read host files, but direct network access and regular-file writes outside its private temporary directory are denied.
This is a process boundary, not a safe evaluator for untrusted code with access to sensitive readable files.

R and Python package installation and DuckDB extension installation run outside the worker sandbox with server permissions.
Those operations may access the network and execute installation or build code, so only trusted requirements should be supplied.
See [Requirements and environments](docs/REQUIREMENTS.md) for the accepted inputs and trust model.

Session records contain submitted source, standard input, declared requirements, results, and images without redaction.
See [Implemented architecture](docs/ARCHITECTURE.md) for recording and process placement.

## Development

The implemented commands are:

```text
mcp-console serve
mcp-console sandbox -- COMMAND [ARG]...
mcp-console --help
mcp-console help [COMMAND]
mcp-console --version
```

`mcp-console serve` communicates with its MCP client over standard input and output.
The standalone `sandbox` command is available for development, but it supervises only its direct child.
Use the MCP server for the supported worker-generation lifecycle.

Run development commands from the repository root:

```text
scripts/format
scripts/check
scripts/test [BOUNDARY/SUITE[::CASE]]
scripts/test --list
scripts/test --update BOUNDARY/SUITE[::CASE]
```

`scripts/format` attempts each installed formatter and leaves failures visible while continuing with the remaining formatters.
`scripts/check` validates extracted runtime sources, checks Rust formatting and Clippy, runs Rust tests, and runs the complete transcript suite.

## Documentation

The [documentation index](docs/README.md) maps current documents by audience.

- [Implemented architecture](docs/ARCHITECTURE.md) explains current process boundaries, ownership, lifecycle, recording, and artifacts.
- [Built-in runtime](docs/BUILTIN_RUNTIME.md) describes user-visible R, Python, SQL, input, output, and graphics behavior.
- [Requirements and environments](docs/REQUIREMENTS.md) describes dependency preparation and its trust boundary.
- [Worker protocol](docs/WORKER_PROTOCOL.md) and [relay protocol](docs/RELAY_PROTOCOL.md) define the exact transport contracts.
  [Registered tool descriptions](docs/TOOL_DESCRIPTIONS.md) is a human-readable mirror of the current agent-facing wording.
- [Transcript test guide](tests/transcripts/README.md) explains selectors, normalization, and golden updates.

The [project vision](design-sketches/VISION.md) and other documents under [`design-sketches/`](design-sketches/README.md) describe intended or exploratory future design, not the implemented system.
When current prose and implementation disagree, source and public acceptance tests are authoritative.

## License

MCP Console is licensed under the [MIT license](LICENSE).
