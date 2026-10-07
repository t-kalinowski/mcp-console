# Configuration

`serve` and ordinary `sandbox` launches load configuration in this order:

1. `.agents/console/config.yaml` in the launch directory, or the home Console `config.yaml` **only if the project file is absent**.
2. Each `-c KEY=VALUE` / `--config KEY=VALUE`, in command-line order.

No ancestor directories are searched.
An unreadable or invalid project file fails launch rather than falling back.
With neither file, configuration starts empty.
Overrides may appear before or after the subcommand and do not edit files.

```sh
mcp-console serve -c extends=:workspace
mcp-console -c extends=:workspace serve -c sandbox.network=enabled
mcp-console sandbox -c 'sandbox.environment={LABEL: analysis}' -- Rscript analysis.R
```

See [resolver settings](RESOLVER.md) and [sandbox settings](SANDBOX_CONFIGURATION.md) for available keys.

Console supports local-host execution only.
Run the MCP client and Console together on the intended host; the client or deployment tooling owns remote connections, containers, and VMs.

Local sandboxed sessions use Console-specific caches by default.
The top-level `cache: host` setting or `-c cache=host` opts into host installations and cache locations.
`--no-sandbox` defaults to host caches.
See [cache locations](RESOLVER.md#cache-locations) for platform paths.

## Console home

The home Console directory is `~/.agents/console`.
Set `MCP_CONSOLE_HOME` to an absolute directory to relocate fallback `config.yaml` and `sessions/` without changing `HOME` or R, Python, or uv storage.
Empty or relative values are errors when fallback is needed; `~` is not expanded.

Project configuration and project recordings take precedence independently: configuration requires the project file, while recordings require only an existing project `.agents/console` directory.
See [recording](RECORDING.md).

## Model-visible languages

Expose SQL alone, or SQL and Python, with a top-level list:

```yaml
languages: [sql]
```

```sh
mcp-console serve -c 'languages=[sql,python]'
```

The list accepts a nonempty subset of `r`, `python`, and `sql`; SQL is not yet supported on Windows.
An override replaces the entire list.
Omitting `languages` preserves the standard full interface and the internal `MCP_CONSOLE_LANGUAGES` filter.
An explicit list determines the public interface independently of that internal runtime setting.

The selection is captured at server launch.
Tool discovery, descriptions, and source-argument validation use the same selection, including before runtime discovery and after worker restart.
Editing the configuration file requires a new server connection to change the interface.
A hidden source field is rejected even when its value is `null`, before any same-call restart, dependency preparation, or input delivery.

Visibility does not select or disable embedded runtimes or choose a SQL provider.
SQL can use hidden R or Python without a model-facing setup cell.
Runtime availability still determines whether an exposed language can execute.
Polling, stdin, controls, and all supported requirements remain available, including R/Python requirements needed by SQL.
For a missing managed provider, follow the SQL diagnostic's package requirements and restart; selected Python environments require preinstalled packages.
Custom workers retain their own language, SQL, and preparation contracts; Console does not supply their SQL backend.

This is a usability setting.
It does not restrict what SQL can do or change the sandbox and dependency trust boundaries.

## Python environment selection

Select an existing interpreter with:

```yaml
python: .venv/bin/python
```

This overrides inherited `RETICULATE_PYTHON`, is retained across restarts, and is unavailable with custom workers.
Paths, including bare filenames, are relative to the launch directory.

A leading `~` expands using the server's absolute `HOME`, including in a quoted override such as `-c 'python=~/.venv/bin/python'`.
Missing, empty, or relative `HOME` is an error when expansion is requested; `~user` and environment-variable references are not expanded.

Explicit selection uses preinstalled Python packages and bypasses managed Python preparation.
Without R or an explicit selection, Console uses uv on the local host.
A broken selected interpreter is an error, not a reason to fall back.
See [runtime selection](BUILTIN_RUNTIME.md).

## Session startup source

The built-in worker can run one captured R or Python program after its interpreter and session helpers are ready, before the first SQL cell:

```yaml
startup:
  language: python
  code: |
    import sqlite3
    connection = sqlite3.connect("analysis.sqlite")
    console_sql_connection(connection)
```

For R, set `language: r` and construct a normal DBI connection:

```yaml
startup:
  language: r
  code: |
    connection <- DBI::dbConnect(duckdb::duckdb(), dbdir = ":memory:")
    console_sql_connection(connection)
```

Use normal driver arguments for database paths, read-only access, threads, memory, and other engine settings.
The connection remains a native object on its owning interpreter; Console does not proxy it or commit its transactions.
Source uses the existing YAML/CLI layering, for example `-c startup.language=python`.
It is captured once at server launch, including across explicit worker restarts; edits to the file require a new server to be captured.
The encoded startup source is limited to 32 KiB (32,768 bytes), including the JSON language/code fields, UTF-8 source, and JSON escaping.
Oversized effective configuration is rejected before spawning, with its encoded byte count and the limit; newline and other escape-heavy source can reach the limit before its source file does.
Source is not included in the tool schema or automatically echoed as a submitted cell.
Each generation receives source through a private temporary file; process-launch environments carry only its path.
The worker reads and unlinks the file before interpreter setup, including after Linux's R-loader re-exec, and consumes the path from its environment.
The launch owner removes temporary storage if startup fails before consumption.
The transport leaves no source payload in runtime environments, child inheritance, or OS process-environment snapshots.
Sandboxed launch grants access only to this private transport directory, which is removed after worker readiness; it grants no access to other host paths.
Output explicitly emitted by the program and runtime errors remain visible.
Python figures are finalized and published before startup completes, including when the program fails; the first cell response or idle poll can collect them without running a Python cell.

The program must finish by leaving a usable connection selected through the existing `console_sql_connection(connection)` helper.
It must be a user-created native connection; selecting or retrieving Console's managed default does not satisfy startup.
The final selection must remain on the startup interpreter; a reset or provider switch from the other interpreter does not satisfy startup.
It runs once per worker generation, on the serialized interpreter thread with the ordinary resolver, input, output, and interrupt services.
MCP initialization, discovery, and ping remain available while it runs; early cells wait behind startup.
Dependencies should be installed or prepared normally; startup source does not run inside the trusted host resolver.
DuckDB extension preparation still follows the automatic managed provider; a user-selected driver owns its own extension configuration.

Failure, missing runtime, invalid connection, or interruption withholds SQL for that generation.
Console neither selects a managed fallback nor retries partially executed source when runtime setup resumes or the worker crashes.
Explicit restart authorizes one new attempt after confirmed retirement; it does not undo earlier side effects.
Startup code is ordinary user code, not a transaction, and may already have changed files or external systems.
A worker launched with configured startup no longer qualifies for automatic unused-worker replacement on a changed requirements declaration; use explicit restart.

With no startup source, the automatic managed DuckDB default is unchanged.
After successful startup, ordinary connection selection and reset apply: reset restores the automatic managed default, does not rerun source, and leaves user connections and transactions open.
A failed startup receipt cannot be cleared by resetting the connection helper.
See [native connection selection](BUILTIN_RUNTIME.md#sql-and-duckdb).

Startup requires the built-in worker and relay and is currently supported on macOS and Linux.
It does not grant access to databases or other host paths, or add network permissions; persistent writable databases still need an existing sandbox write grant.
SQL remains unsupported on Windows.

## Keys and values

Dotted assignment keys address nested mappings, not list indexes.
For a literal dotted key, supply its containing object:

```sh
mcp-console serve -c 'sandbox.environment={"APP.VERSION": "v1", COUNT: "42"}'
```

Inline values accept YAML strings, booleans, finite numbers, `null`, nested lists, and objects.
Objects accept `:` or `=`, including mixed separators; trailing commas are allowed.
Quotes follow YAML rules.
This is not a TOML parser and performs no variable expansion or file inclusion.
YAML tags are ignored; the underlying values still undergo validation.

Quote strings that resemble numbers or booleans.
An empty assignment is invalid; use `""` for an empty string.
Quote the whole assignment for the shell when it contains spaces or punctuation.

## Merge rules

Mappings merge recursively.
Lists, scalars, and explicit `null` replace the previous value.
A mapping replaces a non-mapping, but an empty mapping does **not** clear an existing mapping.
To clear and rebuild one, assign `null`, then the new mapping in a later override.
Only the final result is validated.

These rules are schema-independent: changing `kind` or `extends` does not remove inherited siblings.
Defaults, profile expansion, application decoding, and native-policy validation happen afterward; `--writable-root` adds grants after layering.

Settings are captured once and reused across worker generations.
Worker and resolver launches consume that captured input without rediscovering YAML.
Explicit native `--config-env` and internal `--settings-env` inputs are already complete and reject `-c` overrides.

The layering code is in [`src/config.rs`](../src/config.rs) and `src/config/`; application decoding belongs to [`src/settings.rs`](../src/settings.rs).
