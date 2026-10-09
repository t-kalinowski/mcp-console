# MCP Console

A persistent R, Python, and SQL workspace for agents.
One MCP tool, `send`, runs code and returns text and plots.
Objects, imports, models, and database state remain available between calls.

Python and SQL work without R.
When both runtimes are available, reticulate connects them: Python can read `r.name`, R can read `py$name`, and R-backed SQL can query R data frames.

**Development preview:** interfaces may change.
macOS and Linux are supported; Windows x64 support is [experimental](docs/WINDOWS.md).

## Quickstart

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then install Console from source:

```sh
uv tool install git+https://github.com/t-kalinowski/mcp-console
```

Source installation requires Git, Rust, and platform build tools.
See [Get started](docs/getting-started.qmd) for prerequisites, optional R installation, and client setup.

Configure your MCP client to launch `mcp-console serve` over stdio.
Run the client and Console on the same host, with the client's shell and filesystem tools using the same workspace.

Ask your agent:

> Use Console to generate a small dataset, fit a model, and plot the result.
> Keep the data and model for follow-up.

## Limits and trust boundaries

Code has shell-class capability.
The default sandbox permits host-file reads, restricts direct networking, and permits regular-file writes only in private temporary storage.
**Readable secrets are not protected.** Project configuration is trusted input and may widen permissions.

Dependency preparation has its own permissions.
Recordings contain code, input, and output without redaction, and have no automatic cleanup.
Read [Sandbox and trust](docs/SANDBOX.md) before changing permissions and [Recordings](docs/RECORDING.md) before sharing session files.

There is one session and one active cell per connection.
Errors do not roll back earlier effects.
Restart discards live objects but retains accepted requirements and recordings.

## Documentation

[Get started](docs/getting-started.qmd) · [User guides and reference](docs/README.md) · [Python clients](docs/PYTHON.md) · [R and ellmer](r/README.md) · [Development](docs/DEVELOPMENT.md)

MCP Console grew out of [mcp-repl](https://github.com/posit-dev/mcp-repl).
Licensed under the [MIT license](LICENSE).
