# Configuration

For workspace writes and one permitted API host:

```yaml
sandbox:
  filesystem:
    read_write: [.]
  network:
    proxy:
      domains:
        allow: [api.example.com]
```

All configuration fields use snake_case; environment-variable names keep their actual spelling.
Domains and paths are list values, never mapping keys.
Worker and resolver permissions are independent.
See [sandbox configuration](SANDBOX_CONFIGURATION.md) for defaults and native limitations.
The same short example is available as [config.yaml](../examples/config.yaml).

`serve` and ordinary `sandbox` launches load configuration in this order:

1. Global `config.yaml` in `$MCP_CONSOLE_HOME`, when set, otherwise `~/.agents/console/config.yaml`.
2. Project `.agents/console/config.yaml` in the captured launch directory.
3. Each `-c KEY=VALUE` / `--config KEY=VALUE`, in command-line order.

No ancestor directories are searched.
Missing files are normal; with neither file, Console uses its built-in defaults.
Each selected file must be a supported YAML mapping.
Unreadable or malformed files fail launch, even if a later override could replace their contents.
Symlinks to regular files are allowed; dangling links and non-regular targets are errors.
If both locations resolve to the same file, Console reads it once, after excluding disabled sources.
Application settings are validated and defaults supplied only after all layers and overrides have been merged.
Overrides may appear before or after the subcommand and do not edit files.

Use discovery flags before or after `serve` or ordinary `sandbox`:

| Invocation            | Configuration inputs                |
| --------------------- | ----------------------------------- |
| Normal launch         | Global, project, then CLI overrides |
| `--no-project-config` | Global, then CLI overrides          |
| `--no-global-config`  | Project, then CLI overrides         |
| `--no-config`         | CLI overrides only                  |

Combining `--no-global-config` and `--no-project-config` behaves like `--no-config`.
Excluded files are not inspected or read, including invalid or unreadable files.
`--no-global-config` and `--no-config` do not resolve the global Console directory for configuration discovery; recording and other storage operations may still require it.
These flags do not change recording-location selection.

Use `--config-file PATH` to select one YAML or JSON mapping as the sole file source.
It skips both automatic locations, including global path resolution, and applies any `-c` overrides afterward.
The selected file must exist and be readable; a missing file is an error.
An empty mapping (`{}`) uses built-in defaults.
The option works before or after `serve` or ordinary `sandbox`, may be supplied only once, and cannot be combined with discovery exclusions.
Relative paths are resolved from the launch directory; `~` and environment-variable references are not expanded.

Automatic project loading treats project configuration as trusted launcher input.
It can affect executable selection, child environments, and requested sandbox permissions.
Trusting code to run inside a sandbox is not equivalent to trusting it to define the sandbox.
Global configuration supplies defaults, not a mandatory security ceiling: project settings may override global settings under the ordinary merge rules.
Integrations opening unfamiliar projects should use `--no-project-config` until they authorize project configuration.

Paths keep their existing launch-relative meaning, including paths supplied by global configuration.
They are not rebased onto the configuration file's directory.
Console captures effective settings once before runtime preparation and reuses them across worker restarts and resolver operations.
Restart Console after editing `config.yaml`; restarting a worker does not reload it.

```sh
mcp-console serve -c 'sandbox.filesystem.read_write=[.]'
mcp-console serve --no-project-config -c 'sandbox.filesystem.read_write=[.]'
mcp-console serve --no-global-config
mcp-console serve --no-config -c 'sandbox.filesystem.read_write=[.]'
mcp-console serve --config-file ./session.yaml
mcp-console -c 'sandbox.filesystem.read_write=[.]' serve -c sandbox.network=enabled
mcp-console sandbox -c 'environment={LABEL: analysis}' -- Rscript analysis.R
```

`-c/--config` accepts dotted `KEY=VALUE` assignments, including structured inline values; it does not accept a file path or a complete root mapping.
Console has no application-config blob or config-file environment variable.
`MCP_CONSOLE_HOME` selects the global directory, and ordinary `sandbox --config-env NAME` accepts the separate native runner's JSON policy rather than application configuration.
Application discovery flags, `--config-file`, and `-c` overrides cannot be combined with `--config-env` or internal `--settings-env`.

See [resolver settings](RESOLVER.md) and [sandbox settings](SANDBOX_CONFIGURATION.md) for available keys.

Console supports local-host execution only.
Run the MCP client and Console together on the intended host; the client or deployment tooling owns remote connections, containers, and VMs.

Local sandboxed sessions use Console-specific caches by default.
The top-level `cache: host` setting or `-c cache=host` opts into host installations and cache locations.
`--no-sandbox` defaults to host caches.
See [cache locations](RESOLVER.md#cache-locations) for platform paths.

## Console home

The home Console directory is `~/.agents/console`.
Set `MCP_CONSOLE_HOME` to an absolute directory to relocate global `config.yaml` and `sessions/` without changing `HOME` or R, Python, or uv storage.
It selects the global location rather than adding a layer.
Empty or relative values are errors when that directory is needed; `~` is not expanded.
Without `MCP_CONSOLE_HOME`, Console uses an absolute, nonempty `HOME`.
On Windows, an absent or empty `HOME` uses native user-home discovery (`USERPROFILE`, then the Windows user-profile API).
A supplied relative home path remains an error.

Configuration layering and recording-location selection are independent; project recordings require only an existing project `.agents/console` directory.
See [recording](RECORDING.md).

## Model-visible languages

Expose SQL alone, or SQL and Python, with a top-level list:

```yaml
languages: [sql]
```

```sh
mcp-console serve -c 'languages=[sql,python]'
```

The list accepts a nonempty subset of `r`, `python`, and `sql` on every supported host.
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

Select an existing interpreter or standard virtual environment with:

```yaml
python: .venv
```

This overrides inherited `RETICULATE_PYTHON`, is retained across restarts, and is unavailable with custom workers.
Paths, including bare filenames, are relative to the launch directory.
The equivalent mapping is `python: {existing: .venv}`.
Directories require `pyvenv.cfg` and `bin/python` on Unix or `Scripts/python.exe` on Windows.
Executable paths retain their spelling and symlinks so a selected venv keeps its package environment.
Recognized Conda installations are unsupported; unrelated Conda environment variables do not exclude ordinary venvs.
Interpreter symlink targets are checked for Conda installations without changing the selected executable path.

A leading `~` expands using the server's absolute `HOME`, including in a quoted override such as `-c 'python=~/.venv/bin/python'`.
Missing, empty, or relative `HOME` is an error when expansion is requested; `~user` and environment-variable references are not expanded.

Explicit selection uses preinstalled Python packages and bypasses managed Python preparation.
Without R or an explicit selection, Console uses uv on the local host.
A broken selected interpreter is an error, not a reason to fall back.
Existing interpreter inspection, including any environment startup hooks, uses the worker's captured permissions.
See [runtime selection](BUILTIN_RUNTIME.md).

Choose the first available Python candidate with:

```yaml
python:
  first_available:
    - existing: .venv
    - active_venv
    - managed: {}
```

Only an absent existing path advances the list.
A present broken environment, dangling interpreter symlink, or invalid activation path fails selection.
`active_venv` reads the incoming `VIRTUAL_ENV`; unset or empty means absent.
Launchers must preserve this variable, or select an explicit path.
The selected interpreter is captured once, including across worker restarts; later candidates are never inspected or prepared.

The list is flat and nonempty, with at most one `active_venv` and one optional managed candidate last.
Exhausting the list fails startup.
`managed: {}` uses the existing managed Python defaults and also works at the top level to override an inherited `RETICULATE_PYTHON`.
Managed selection currently accepts an empty options mapping.
An explicit managed choice requires available managed preparation; it does not select a PATH interpreter when preparation is unavailable.
When `python` is omitted, Console retains the launch-time `RETICULATE_PYTHON` compatibility behavior.

## Native R startup

The built-in R interpreter uses R's normal startup by default:

```yaml
r:
  vanilla: false
```

Omitting `r` or `r.vanilla` has the same effect.
R reads environment files and site/user profiles, restores `.RData`, runs `.First()`, and attaches default packages with its native discovery and ordering.
Console passes `--quiet`, `--interactive`, and `--no-save`; ordinary shutdown does not save a workspace or prompt to save one.
Profile settings such as `options(width = ...)` are preserved.
Console still installs its transport, interrupt, graphics, and runtime integration.
Its managed `device` option captures cell plots; presentation preferences such as width remain under R's control.
Startup hooks run before Console's runtime bridges and managed plot device attach.
Console-managed dependency resolution becomes available after those bridges attach.
Startup plots use R's native device and its ordinary filesystem permissions.
R/Python attachment must retain the running Python interpreter; a conflicting selection requires explicit restart.

Use R's native `--vanilla` behavior as an explicit escape hatch:

```sh
mcp-console serve -c r.vanilla=true
```

`r.vanilla` accepts a boolean and controls only the built-in R interpreter.
Explicit `r` settings are rejected with custom workers or relays.
It does not require R in a Python-only session or change discovery and dependency preparation, which remain profile-free.

Native startup runs inside each worker after sandbox entry, with that worker's ordinary permissions.
Startup files receive no additional grants.
With `--no-sandbox`, native startup runs without worker sandbox enforcement.
Console does not discover, capture, source, or replay the files itself.

Startup output and diagnostics use the ordinary MCP output path.
A profile error that R survives leaves R usable; Console does not interpret diagnostic text as a fatal error.
If R exits, fails initialization, or is interrupted during initialization, fix the startup file externally and send `control: "restart"` in the same MCP connection to authorize one new attempt after retirement.
Calls, polls, and dependency preparation do not automatically retry that startup.
MCP initialization, discovery, and controls remain available while R starts or after the worker fails.

The boolean setting is captured at server launch and retained across worker restarts.
Changing `config.yaml` requires a new server connection.
Editing `.Rprofile` takes effect on the next worker restart because R reads it again in the new worker.
Native R startup precedes the separately configured Console startup source below; the two retain distinct roles.

## Session startup source

The built-in worker can run one captured R or Python program after its interpreter and session helpers are ready, before the first SQL cell:

```yaml
startup:
  language: python
  code: |
    import sqlite3
    connection = sqlite3.connect("analysis.sqlite")
    _console.sql_connection(connection)
```

For R, set `language: r` and construct a normal DBI connection:

```yaml
startup:
  language: r
  code: |
    connection <- DBI::dbConnect(duckdb::duckdb(), dbdir = ":memory:")
    .console$sql_connection(connection)
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

The program must finish by leaving a usable connection selected through `.console$sql_connection(connection)` in R or `_console.sql_connection(connection)` in Python.
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

Startup requires the built-in worker and relay and is supported on macOS, Linux, and Windows.
It does not grant access to databases or other host paths, or add network permissions; persistent writable databases still need an existing sandbox write grant.

## Keys and values

Dotted assignment keys address nested mappings, not list indexes.
For a literal dotted key, supply its containing object:

```sh
mcp-console serve -c 'environment={"APP.VERSION": "v1", COUNT: "42"}'
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
An empty project file mapping (`{}`) preserves all global settings; an empty list replaces an existing list with an empty list.
For example, global `environment: {KEEP: global, CHANGE: global}` and project `environment: {CHANGE: project}` produce `environment: {KEEP: global, CHANGE: project}`.
To clear and rebuild one, assign `null`, then the new mapping in a later override.
Only the final result is validated.

These rules are schema-independent: changing `sandbox.network.proxy.mode` does not remove an explicitly configured `socks5` sibling.
Selecting `limited` with that sibling still present is an error.
The final document is validated before selecting variants, supplying defaults, and compiling native policy; `--writable-root` adds grants after layering.

Settings are captured once and reused across worker generations.
Worker and resolver launches consume that captured input without rediscovering YAML.
Explicit native `--config-env` and internal `--settings-env` inputs are already complete and reject `-c` overrides, `--no-project-config`, and `--no-config`.

The layering code is in [`src/config.rs`](../src/config.rs) and `src/config/`; application decoding belongs to [`src/settings.rs`](../src/settings.rs).

## Shared environment

```yaml
environment:
  OMP_NUM_THREADS: "4"
resolver:
  environment:
    OMP_NUM_THREADS: "1"
```

The worker receives inherited launch variables, then top-level `environment` values.
Preparation receives the same values, then individual `resolver.environment` overrides.
Console-owned runtime, cache, private-storage, managed-proxy, and transport adjustments take precedence afterward.
Shared values, including credentials, are also available to dependency preparation.
Values must be strings; quote numbers and booleans.

`inherit_environment` defaults to true; `resolver.inherit_environment` defaults to the top-level setting.
False excludes inherited launch variables and retains explicit configuration values.
An isolated environment must explicitly supply any workload variables it needs, including paths used for cache selection.
These controls apply to workloads, including `serve --no-sandbox`, without changing the supervisor's own loader/helper environment.
Settings are captured once; worker environment mutations and restarts do not reconfigure preparation.
See [environment variables](ENVIRONMENT.md) for launch settings, runtime selection, cache locations, and Console-owned assignments.

## Expanded example

Most settings below can be omitted.
Worker and resolver permissions are independent:

```yaml
cache: console
inherit_environment: true
environment:
  OMP_NUM_THREADS: "4"
sandbox:
  filesystem:
    read_only: [./data]
    read_write: [.]
    deny: [./secrets]
  network:
    proxy:
      mode: full
      domains:
        allow: [api.example.com, "*.example.org"]
        deny: [blocked.example.org]
      socks5: tcp
      allow_upstream_proxy: false
    sockets:
      unix_sockets: []
    allow_local_binding: false
resolver:
  environment:
    OMP_NUM_THREADS: "1"
    UV_INDEX_URL: https://packages.example.org/simple
  sandbox:
    network:
      proxy:
        domains:
          allow: [packages.example.org, artifacts.example.org]
```

The generated [configuration transcripts](../tests/snapshots/cli/test_public_configuration/) pair user YAML with the native runner policy in separate documents.
CLI layering examples include a middle document showing the overrides.
Host paths and process IDs are normalized; other policy values are retained.
They record normalization and native launches on the exercised platform; capability limits remain described in [sandbox configuration](SANDBOX_CONFIGURATION.md#native-capabilities).

## Migration

Global configuration previously served only as a fallback when the project file was absent.
It now supplies the first layer even when a project file exists, so unrelated global settings survive sparse project configuration.
Use `--no-config` with CLI overrides when neither automatic file should contribute settings.

The public format deliberately replaces the previous native-shaped YAML.
Unknown fields, old shapes, camelCase aliases, null permission selectors, and incompatible explicit settings fail with a configuration path.
There are no profiles, `extends`, `add`, or raw-options fields.

| Previous input                                        | Public replacement                                            |
| ----------------------------------------------------- | ------------------------------------------------------------- |
| `extends: :workspace`                                 | `sandbox.filesystem: {read_write: [.], read_only: [.claude]}` |
| `extends: :read-only`                                 | Omit `sandbox` to keep the worker baseline                    |
| `sandbox.environment` / `sandbox.inherit_environment` | Top-level `environment` / `inherit_environment`               |
| `filesystem.entries`                                  | `filesystem.read_only`, `read_write`, and `deny` lists        |
| Sibling `proxy` and `network: restricted`             | `network: {proxy: {...}}`                                     |
| `proxy: null`                                         | Explicit `network: restricted` or `network: enabled`          |
| `enableSocks5` / `enableSocks5Udp`                    | Full-mode `socks5: disabled`, `tcp`, or `tcp_udp`             |
| Domain permission mapping                             | `domains: {allow: [...], deny: [...]}`                        |
| `unixSockets` / `dangerouslyAllowAllUnixSockets`      | `sockets.unix_sockets: [...]` or `dangerously_allow_all`      |
| Native fields directly under `resolver`               | The same public schema under `resolver.sandbox`               |

The native runner protects writes to `.git`, `.agents`, `.codex`, and `.aws` beneath writable roots, subject to its permission rules.
The former workspace profile also protected `.claude`; include `read_only: [.claude]` to retain that explicit restriction.
OS-specific runner controls are unavailable in this public format.
The separate, explicit complete-native-policy CLI transport remains unchanged; it is not a field in `config.yaml`.
Explicit `sandbox` or `resolver.sandbox` settings are rejected with `serve --no-sandbox`, rather than silently discarded.
