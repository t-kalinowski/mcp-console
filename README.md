# MCP Console

A persistent, sandboxed REPL for data analysis in R, Python, and SQL, built for AI agents.

Give your agent a live workbench: load data once, explore questions, refine plots, read help, and debug code with objects and models still in memory.
One MCP tool, `send`, provides interactive input, text output, plots, and execution controls.

Code runs in an [OS-level sandbox](docs/SANDBOX.md) by default, with controls over filesystem writes and direct network access for long-running and unattended work.
Console automatically prepares managed environments and packages; no Console configuration file is required to get started.

Use [R, Python, or SQL](docs/BUILTIN_RUNTIME.md), or combine them in one session.
The built-in runtimes are embedded in a single worker process, with data transfer between languages that does not require intermediate files.
Console works with MCP-compatible clients such as Codex and Claude Code, without tying the workspace to a model provider.

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

Console prepares common data packages and can resolve additional dependencies as code needs them.
See [Requirements and environments](docs/REQUIREMENTS.md) for details.

Ask your agent:

> Use Console to generate a small dataset, fit a model, and plot the result.
> Keep the data and model for follow-up.

## Where Console fits

- **Unattended agents:** carry long-running analysis through to results within configured sandbox permissions.
- **Model evaluations:** give agents a persistent data-analysis environment for realistic evaluation tasks.
- **Conversational analysis:** let the agent work in its own session while you focus on questions and results, rather than a shared code editor.
- **Large-data investigations:** load a dataset once and keep intermediate results as new questions arise.
- **Debugging and package development:** pause execution, inspect live objects, and evaluate code where a failure occurs.

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
