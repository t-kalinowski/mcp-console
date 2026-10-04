# MCP Console

An interactive, persistent computational workspace for agents.
One MCP tool, `send`, runs R, Python, and SQL, returns text and plots, and supports interactive input, dependency preparation, interruption, and restart.
Data, models, imports, and database state survive between calls.

With both runtimes available, Python reads R objects through `r.name`, R reads Python objects through `py$name`, and SQL can query live R data frames.
Python and SQL also work without R.
Responses keep large outputs out of the model context while retaining logs and artifacts on disk.

## Status

**Development preview:** interfaces may change.
macOS and Linux are supported.
Windows x64 has experimental [local R and Python support with a native sandbox](docs/WINDOWS.md).
Local and [SSH](docs/SSH.md) sessions can prepare dependencies.
[Docker](docs/DOCKER.md) images and [Docker Sandbox](docs/DOCKER_SANDBOX.md) templates use preinstalled runtimes and packages.

## Quickstart

Install [uv](https://docs.astral.sh/uv/getting-started/installation/).
To use R, install it with [rig](https://github.com/r-lib/rig#id-installation) and `rig add release`.

Source installation also requires Git, [rustup](https://rustup.rs/) with Rust 1.95 or later, and platform build tools.
On macOS, run `xcode-select --install`.
On Ubuntu:

```sh
sudo apt-get update
sudo apt-get install -y build-essential git pkg-config libcap-dev libcurl4-openssl-dev binutils
```

Install Console and its private sandbox runner:

```sh
uv tool install git+https://github.com/t-kalinowski/mcp-console
```

Configure your MCP client to launch `uvx mcp-console serve` over stdio.
For [Codex](https://developers.openai.com/codex/mcp):

```sh
codex mcp add console -- uvx mcp-console serve
codex
```

For [Claude Code](https://code.claude.com/docs/en/mcp):

```sh
claude mcp add --transport stdio console -- uvx mcp-console serve
claude
```

Installation builds the pinned sandbox runner with its own toolchain.
Initial runtime preparation can download interpreters, packages, and extensions.
See [build prerequisites](RELEASE.md#private-sandbox-executable) and [dependency selection](docs/REQUIREMENTS.md).

Without R, local and SSH sessions use `uv` to select Python.
To use an existing project environment instead, put `python: .venv/bin/python` in `.agents/console/config.yaml`; that environment's packages must already be installed.
See [configuration](docs/CONFIGURATION.md).

## Try an analysis

Check that your client exposes `send` (`/mcp` in Codex), then ask:

> Use MCP Console to explore the Palmer Penguins data from the R package `palmerpenguins`.
> Fit a logistic regression in R to predict penguin sex from body measurements, summarize the data by species with SQL, and plot the model's predictions with Python and Matplotlib.
> Keep the data and model for follow-up.

Then ask:

> Where does the model make the most mistakes?
> Show me a plot and the path to the recorded session transcript.

For a Python-only session, ask Console to generate and analyze a small simulated dataset with NumPy and pandas.
See the [runtime guide](docs/BUILTIN_RUNTIME.md) for language bridges, SQL connections, input, and plots.

## Recordings and reports

Sessions produce a readable `transcript.md`, a source-only `transcript.qmd`, raw cell logs, and image artifacts.
Records go under `.agents/console/sessions/` when `.agents/console` already exists in the launch directory, otherwise under `~/.agents/console/sessions/`.
[`MCP_CONSOLE_HOME`](docs/CONFIGURATION.md) relocates that fallback directory.

The Quarto document is a starting point for a report, not a replay or checkpoint.
Review a copy before rendering: it can contain failed or rejected submissions, omits interactive input, and executes outside the worker sandbox.
See [recording and rendering](docs/RECORDING.md).

## Limits and trust boundaries

Evaluated code has shell-class capability.
The default local native sandbox allows host-file reads, restricts direct networking, and denies regular-file writes outside private temporary storage.
**It does not protect readable secrets.** Trusted [policy configuration](docs/SANDBOX_CONFIGURATION.md) can change these defaults.
There is no automatic unsandboxed fallback; constrained Linux hosts may lack the required [capabilities](docs/LINUX_COMPATIBILITY.md).

Dependency preparation uses a separate [resolver sandbox](docs/RESOLVER.md), with isolated caches and a managed package-source proxy.
Its broker handles policy and data; package installation, builds, imports, and inspection run inside that sandbox.
Explicit `--no-sandbox` preparation uses host permissions and ordinary host caches.
See the [dependency trust boundary](docs/REQUIREMENTS.md#host-resolution-and-trust).

There is one implicit session and cells run sequentially.
Cells are not transactional; an error can leave earlier changes in place.
Restart discards live state across languages, but retains accepted requirements and recordings.
Each response contains at most 8 KiB of text, with separate image limits; raw text retention is capped at 1 GiB per cell.
Reading omitted output requires filesystem access to the controller's recording directory.

Recordings include source, stdin, requirements, and output **without redaction**.
There is no aggregate retention quota or automatic cleanup.

## More

[Python clients](docs/PYTHON.md) · [ellmer integration](r/README.md) · [Documentation](docs/README.md) · [Contributing](AGENTS.md)

MCP Console grew out of [`mcp-repl`](https://github.com/posit-dev/mcp-repl).
Licensed under the [MIT license](LICENSE).
