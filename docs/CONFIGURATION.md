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

See [resolver settings](RESOLVER.md), [sandbox settings](SANDBOX_CONFIGURATION.md), [SSH](SSH.md), [Docker](DOCKER.md), and [Docker Sandbox](DOCKER_SANDBOX.md) for available keys.

Local sandboxed sessions use Console-specific caches by default.
The top-level `cache: host` setting or `-c cache=host` opts into host installations and cache locations.
`--no-sandbox` defaults to host caches.
See [cache locations](RESOLVER.md#cache-locations) for platform paths and execution-target limits.

## Console home

The home Console directory is `~/.agents/console`.
Set `MCP_CONSOLE_HOME` to an absolute directory to relocate fallback `config.yaml` and `sessions/` without changing `HOME` or R, Python, uv, and provider storage.
Empty or relative values are errors when fallback is needed; `~` is not expanded.

Project configuration and project recordings take precedence independently: configuration requires the project file, while recordings require only an existing project `.agents/console` directory.
See [recording](RECORDING.md).

## Python environment selection

Select an existing interpreter with:

```yaml
python: .venv/bin/python
```

This overrides inherited `RETICULATE_PYTHON`, is retained across restarts, and is unavailable with custom workers.
Paths, including bare filenames, are relative to the launch directory locally or `target.workspace` on an execution target.
The controller does not resolve target paths.

For local selection, a leading `~` expands using the controller's absolute `HOME`, including in a quoted override such as `-c 'python=~/.venv/bin/python'`.
Missing, empty, or relative `HOME` is an error when expansion is requested; `~user` and environment-variable references are not expanded.

Explicit selection uses preinstalled Python packages and bypasses managed Python preparation.
Without R or an explicit selection, local/SSH sessions use uv on the execution host.
Prepared Docker/SBX targets always use preinstalled packages: their probe tries the explicit selection, then `RETICULATE_PYTHON`, then `python3` / `python` on the workload PATH.
A broken selected interpreter is an error, not a reason to fall back.
See [runtime selection](BUILTIN_RUNTIME.md).

## Managed SQL connection

The built-in worker captures SQL settings when the server starts:

```yaml
sql:
  provider: python
  database: analysis.duckdb
  read_only: false
  options:
    threads: 2
    memory_limit: 256MB
```

`provider` accepts `auto` (the default), `r`, or `python`.
Automatic selection uses execution-host R availability; explicit Python selection works with R installed, including SQL-only sessions.
A broken explicit provider or connection configuration reports an error without switching providers.
Model-visible languages and interpreter initialization order do not select the managed provider.

`database` defaults to `:memory:`.
File paths resolve on the execution host, relative to the worker workspace; the controller does not inspect or expand them.
Parent directories must already exist.
`read_only` defaults to false and requires a file-backed database.
A persistent catalog survives worker restart; an in-memory catalog belongs to its worker generation.
Restart retains the captured settings, even if the configuration file changes.

`options` supports only `threads` (an integer from 1 through 2147483647) and `memory_limit` (a nonempty DuckDB memory-size string).
DuckDB validates engine values during connection construction.
These settings apply to the first managed connection, including background warmup.
A Python provider needs DuckDB in its selected environment; in a managed session, prepare it through `requirements.python: [duckdb]` before SQL use.
SQL demand does not reinstall packages removed from the session requirements.
Unknown fields and options fail configuration decoding.
YAML and ordered CLI overrides use the ordinary merge rules, for example `-c sql.options.threads=4`.

Engine settings grant no filesystem or network access.
A writable database requires an existing sandbox write grant; read-only workspace access remains read-only.
Console owns spill/secret storage, extension-cache handoff, progress output, and disabled Python replacement scans; SQL options cannot override them.
Prepared targets use preinstalled packages and retain their extension-install policy.
SQL configuration does not change resolver/cache policy or ambient DuckDB configuration.

A runtime-selected DBI or DB-API connection takes precedence until reset; these options apply only to the managed default, not user connections.
See [native connection selection and registration](BUILTIN_RUNTIME.md#sql-and-duckdb).
Nondefault SQL settings require the built-in worker.
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
Execution hosts consume that captured input without rediscovering YAML.
Explicit native `--config-env` and internal `--settings-env` inputs are already complete and reject `-c` overrides.

The layering code is in [`src/config.rs`](../src/config.rs) and `src/config/`; application decoding belongs to [`src/settings.rs`](../src/settings.rs).
