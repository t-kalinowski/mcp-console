# Command-line reference

Run `mcp-console --help` or a subcommand's `--help` for the installed build's complete option list.
The website's [CLI help](cli-help.qmd) shows the printouts checked by the CLI tests.
Console runs commands on the local host; it does not create remote connections.

## Configure a client

```sh
uvx mcp-console configure-client codex claude
mcp-console configure-client codex --scope global
mcp-console configure-client claude --scope project --dry-run
mcp-console configure-client claude --no-auto-load
mcp-console configure-client codex --pin-version v0.0.5
mcp-console configure-client claude --no-uvx
mcp-console configure-client codex --name console-local -- --writable-root /data
```

`configure-client` registers a stdio MCP server named `console` that launches `uvx mcp-console serve`.
Pass one or more client names; all options apply to every selected client, and repeated client names are configured once.
Use separate invocations for different settings per client.
`--name NAME` changes the server name; arguments after `--` are appended after `serve`.
Install [uv](https://docs.astral.sh/uv/getting-started/installation/) so `uvx` is on the client's `PATH`.
The client itself does not need to be installed when writing its configuration.

The default uvx command has no version suffix.
`--pin-version VERSION` appends the supplied suffix after `@`, for example `mcp-console@v0.0.5` or `mcp-console@latest`.
Use uvx's exact-version syntax or `latest`; version ranges are not supported by this suffix.
An unqualified uvx command may reuse an installed or cached version; `latest` requests the latest available release at launch.

`--no-uvx` configures an absolute path to the running executable instead.
Invoke the desired installation to select it, for example `/opt/mcp-console/bin/mcp-console configure-client claude --no-uvx`.
When the executable is inside a disposable cache, the command first runs `uv tool install mcp-console@VERSION`, using the version running setup, and configures the absolute launcher path reported by `uv tool dir --bin`.
This includes temporary `uvx --no-cache` runs and custom uv cache locations.
The persistent installation requires `uv` during setup; subsequent client launches use the saved executable directly.
The command prints the installation command and executable path before installing.
It leaves existing-tool conflicts to uv and does not force replacement of executable files.
`--no-uvx` conflicts with `--pin-version`; select the desired version before configuring a direct executable.

The default `--scope auto` uses the nearest ancestor with a `.git` directory or worktree marker file.
Outside Git, it selects global scope.
`--scope project` uses that Git root, or the current directory outside Git.
`--scope global` selects global scope regardless of the current directory.

| Client      | Project configuration | Global configuration                                        |
| ----------- | --------------------- | ----------------------------------------------------------- |
| Codex       | `.codex/config.toml`  | `$CODEX_HOME/config.toml`, default `~/.codex/config.toml`   |
| Claude Code | `.mcp.json`           | `$CLAUDE_CONFIG_DIR/.claude.json`, default `~/.claude.json` |

The command prints the selected scope, file path, and fields it sets.
Use `--dry-run` to preview those settings and any persistent installation without writing files or installing, or to apply them manually.
Existing settings outside the printed fields are preserved, including other servers, environment variables, and client preferences.
TOML comments are preserved; JSON is formatted on write.
Malformed configuration and an existing remote server with the selected name cause an error without overwriting the file.
Every selected client's configuration is parsed before installation or writes begin.
Each file is replaced atomically, preserving symlinks and existing permissions; a later write failure can leave earlier files changed, and their completed actions remain in stdout.
Configuration edits assume no concurrent client or setup writer.

By default, configuration also requests that Console tools be visible without a tool search:

- Codex: `enabled = true` and `omit_tools_from = ["deferred"]` in `[mcp_servers.console]`.
- Claude Code: [`alwaysLoad: true`](https://code.claude.com/docs/en/mcp#exempt-a-server-from-deferral) in `mcpServers.console`.

`--no-auto-load` writes `omit_tools_from = []` for Codex or `alwaysLoad: false` for Claude Code, leaving tool discovery to the client.
These settings affect only Console.
Client startup limits still apply; a cold `uvx` launch can delay tool availability.
Codex's Strict Code Mode Only controls tool exposure independently and overrides the visibility setting.
Use a current client version that supports the respective setting.

Restart the client after configuration.
Codex requires project trust before loading project configuration, and Claude Code may ask you to approve a project server.
Configuration leaves these client approvals and tool permissions unchanged.
Legacy Claude Code `.config.json` layouts are not supported by this command.

## MCP server

```sh
mcp-console serve
mcp-console serve --writable-root .
mcp-console serve --no-project-config
```

`serve` speaks MCP over stdin and stdout.
It waits for a client; it is not an interactive terminal.
Keep stdout available for protocol traffic.

Sandboxing is enabled by default.
`--writable-root PATH` adds a writable path for this launch and may be repeated.
More specific permission rules still apply.
It does not create missing directories.

```sh
mcp-console serve --no-sandbox
```

`--no-sandbox` runs evaluated code and dependency preparation with host permissions and removes native descendant-cleanup guarantees.
It also disables [managed temporary storage](SANDBOX_CONFIGURATION.md#profiles-and-temporary-storage).
It conflicts with writable-root options and explicit worker or resolver sandbox settings.
Host cache mode is used; `cache: console` is rejected.

## Standalone sandbox

```sh
mcp-console sandbox -- Rscript analysis.R
mcp-console sandbox --writable-root . -- python script.py
mcp-console sandbox -c sandbox.network=enabled -- python download.py
```

Arguments after `--` belong to the target command.
Stdin also belongs to the target.
This command applies the sandbox and workload environment; it does not provide Console's persistent session, automatic package preparation, plot capture, or recordings.

The default permits host reads and private temporary writes, not workspace writes.
Network access and cleanup limits are described in [Sandbox and trust](SANDBOX.md).

## Configuration options

These options work before or after `serve` and ordinary `sandbox`:

| Option                               | Configuration sources                                           |
| ------------------------------------ | --------------------------------------------------------------- |
| No selection flags                   | Global file, launch-directory project file, then CLI overrides. |
| `--no-project-config`                | Global file and CLI overrides.                                  |
| `--no-global-config`                 | Project file and CLI overrides.                                 |
| `--no-config`                        | CLI overrides only.                                             |
| `--config-file PATH`                 | One explicit YAML or JSON file, then CLI overrides.             |
| `-c KEY=VALUE`, `--config KEY=VALUE` | Apply one dotted assignment; repeat in the desired order.       |

`--config-file` may occur once and cannot be combined with discovery exclusions.
A missing explicit file is an error.
`-c` accepts an assignment, not a file path or a complete root document.

```sh
mcp-console serve --config-file ./session.yaml
mcp-console serve -c 'languages=[python,sql]' -c 'python=.venv'
mcp-console -c 'environment={OMP_NUM_THREADS: "4"}' serve
```

Examples use POSIX shell quoting.
In PowerShell, quote the complete assignment when it contains spaces or structured values.
[Configuration](CONFIGURATION.md) defines paths, merging, and trust.

## Inspect configuration

```sh
mcp-console config resolve > session.yaml
mcp-console config resolve --full --no-config --format json
mcp-console serve --config-file session.yaml
```

`config resolve` defaults to `--prune`, printing only settings that differ from the binary's built-in defaults.
`--full` includes all configuration defaults, generated tool prose, and every parameter setting.
The two flags are mutually exclusive.
Neither mode starts runtimes or prepares packages.
Configuration selection flags and `-c` work before `config` or after `resolve`.
YAML is the default output; `--format json` selects JSON.
For host execution, use `--no-sandbox` during both materialization and launch.
See [inspect and edit resolved configuration](CONFIGURATION.md#inspect-and-edit-resolved-configuration) for supported edits and runtime-dependent defaults.

## Windows sandbox requirements

Native Windows sandboxed launches require MXC, including complete native policies.
Unsupported hosts or policies fail before the workload executes.
MXC needs no provisioning command; existing legacy installations need the [one-time cleanup](WINDOWS.md#legacy-installation-cleanup).
See [Windows](WINDOWS.md#native-sandbox).

## Complete native policy

`mcp-console sandbox --config-env NAME -- COMMAND...` reads a complete native JSON policy from the named launch environment variable.
It bypasses automatic configuration and Console's generated policy defaults; it does not merge with `-c` or writable-root options.

This is an advanced integration interface.
Use the pinned schema and examples linked from [Explicit complete policy](SANDBOX_CONFIGURATION.md#explicit-complete-policy), rather than adapting a `config.yaml` document into it.

## Developer-only commands

`worker`, `worker-relay`, and `resolve`, along with `serve --worker`, `serve --relay`, and private handoff options, are not normal user workflows.
Their contracts live in the [architecture](ARCHITECTURE.md) and protocol guides.
Do not launch them to bypass the public configuration or sandbox boundary.
